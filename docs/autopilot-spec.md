# Autopilot spec — scheduled harvest in AWS + `harvest status`

Status: DRAFT for owner approval (2026-11-06). Builds on `harvest-spec.md`
(Phase 1, shipped f35d4c2). Routing (CLAUDE.md): `ingest/` Python is
implementer (Sonnet) work from this spec, reviewed by a fresh Opus subagent;
the CDK stack is main-session work with the owner running the AWS steps.

## Goal

The night before a deadline, Claude can run one command and say truthfully:
"these creators' videos are harvested, these are not, this is how fresh each
is", then recommend from whatever exists. Not a guarantee of completeness.

## What the data says (GW1-10, 15 channels, 119 selection videos)

- Selection videos are posted 88% within 72 h of the deadline, 77% within
  48 h, 69% within 36 h, 41% within 24 h, 16% within 12 h (median 29 h
  before). At the ~20 h night-before checkpoint only about half exist.
- ~3 relevant videos per creator per week (~40 per gameweek); ~1.5 Gemini
  requests per video (30-min windows).
- Listing uploads costs 1 YouTube API unit per channel (10,000/day quota):
  scanning is effectively free. The scarce resource is Gemini requests
  (free tier: 18/day used by us, reset 08:00 UTC; failed attempts count).
- YouTube blocks the captions library from AWS IPs (CloudShell test
  2026-11-06: `RequestBlocked`). Gemini reads the video from Google's side
  and is expected to work from AWS (UNTESTED; needs the key in AWS).

## Architecture

```
EventBridge Scheduler (hourly) -> ECS Fargate task (public subnet, public IP,
no NAT) running `python -m fpl_oracle.ingest.autopilot tick`
   pulls state from S3 -> decides if a scan is due -> scan -> classify ->
   spend Gemini budget -> pushes state + transcripts to S3 -> exits
```

- **Why Fargate, not Lambda:** long Gemini reads exceed 15 minutes.
- **Why hourly trigger, task decides:** the cadence is a function of hours to
  the deadline (below); a tick that is not due exits in seconds.
- **State in S3** (the existing FplOracle bucket, new prefixes):
  `ingest_state/` (ledger, per-GW harvest state, `gemini_usage.json`),
  `transcripts/` (transcript cache JSON). The task downloads at start and
  uploads at end. Single flight: an S3 lock object created with a
  conditional write (If-None-Match) and a TTL; a second tick that finds a
  live lock exits.
- **Secrets:** `GEMINI_API_KEY`, `YOUTUBE_API_KEY` in Secrets Manager (or SSM
  SecureString). The OWNER enters them in the console; nothing is passed
  through chat or the repo.
- **No captions in AWS:** the task runs with captions disabled
  (`--no-captions`). A video Gemini cannot cover becomes
  `needs_captions`, not a failure.
- **Laptop fill-in (optional, built after M2):** `harvest --captions-only
  --sync` pulls the ledger, fetches captions for `needs_captions` videos from
  the laptop's IP, pushes the transcripts back.
- **Local fallback:** if Gemini-from-AWS fails the M2 test, run the same
  `tick` from a macOS launchd job; the code is identical.

## Cadence (hours to the target gameweek deadline)

| hours_to | tick acts |
|---|---|
| > 72 | once a day, at 08:10 UTC (just after the Gemini reset) |
| 72 to 6 | every 3 h |
| 6 to 0 | every hour |
| < 0 | switches to the next gameweek (daily cadence) |

The next gameweek is the first event whose deadline is in the future.
Scans also run on the 08:10 UTC tick every day regardless, so a quota reset
is never wasted.

## Ledger (`ingest_state/ledger.json`)

One row per discovered video, keyed by video_id: `creator_id, title,
published_at, duration_s, gameweek, kind, first_seen_at, status
(new|pending|ok|needs_captions|skipped), source, last_error, retry_after`.
Gameweek = the first deadline after `published_at`. Shorts and videos with
`kind` in {review, live, show} are `skipped`. The per-GW harvest state from
Phase 1 stays and is fed from the ledger, so `harvest.py` is reused, not
rewritten.

## Classification (`kind`)

v0 is the regex used in the 2026-11-06 analysis (selection / final / transfers
/ plan / review / other), kept behind a `Classifier` Protocol so the Haiku
classifier (ROADMAP) drops in unchanged. Priority multiplier: selection and
final 1.0, transfers/plan 0.6, other 0.3, review/live/show 0 (skip).

## Budget policy (extends harvest-spec §4-5)

Daily limit stays 18 (config). Per Pacific quota day:

1. Order pending videos by `creator_weight x kind_multiplier`, then newest.
2. **Final-window reserve:** a quota day that overlaps the 36 h before the
   deadline is "protected". On a protected day videos published more than
   36 h before the deadline may use at most `limit - 10` requests; videos
   in the final window (or kind selection/final) may use the whole limit.
3. Outside protected days there is no reserve: unused budget is lost at
   reset, so spend it on anything with a multiplier > 0.
4. A creator's older video is demoted to the lowest priority when a newer
   selection/final video from them is already in the ledger (it still gets
   harvested if budget is left).
5. 503 / 429 handling, backoff and `needs_captions` follow Phase 1, except
   that in AWS mode the fallback is `needs_captions`, never captions.
6. Deadline backstop (Phase 1, 6 h): in AWS mode it only raises a loud
   `LOW COVERAGE` flag (ntfy hook, not built here).

## `harvest status` (readiness report)

`uv run python -m fpl_oracle.ingest.status --gameweek N [--from-s3]`
(default gameweek: the next one). `--from-s3` downloads the ledger first, so
Claude can run it from the laptop at the start of any session.

Per creator (weight order): newest relevant video (title, hours old), and a
freshness state:

- `FRESH`: newest relevant video is transcribed (source shown: gemini or
  captions).
- `STALE`: an older video is transcribed, a newer one exists but is not
  (pending / needs_captions / retry time); age of the transcribed video shown.
- `MISSING`: no relevant video yet for this gameweek.

Footer: hours to deadline, the share of creator weight that is FRESH and the
share FRESH+STALE, Gemini budget left today, a verdict
(`READY` >= 80% of weight FRESH, `PARTIAL` >= 50% FRESH+STALE, else `THIN`),
and the list of missing creators. Also written to
`ingest_state/readiness.json` for later UI/ntfy use. Exit code 0 always
(informational).

## Pipeline follow-up (separate task, after M1)

Consensus uses each creator's latest HARVESTED video with a staleness
haircut (initial flat 0.75, a guess, recorded in `run.json`; calibrate by
comparing early-video picks with the same creator's final picks once
autopilot has weeks of data). The report gets a per-creator freshness table
so stale data is never presented as fresh.

## Milestones

- **M1 (local, no AWS):** ledger, discovery, classifier v0, budget policy,
  cadence function, `status`; all offline tests with fake clocks/clients.
- **M2 (AWS):** container + CDK (Fargate task, scheduler, S3 prefixes,
  secrets, task role), deploy by the owner, then the Gemini-from-AWS test.
  Docker must be available where the image is built (confirm).
- **M3:** laptop `--captions-only --sync`; pipeline freshness/haircut.
- **M4:** Haiku classifier. **M5:** ntfy alert on `LOW COVERAGE`.

## Tests (offline)

Cadence table at the boundaries; gameweek assignment around a deadline;
reserve rule on protected vs unprotected days; priority order and
demotion; lock contention; `needs_captions` in AWS mode; status verdicts for
FRESH/STALE/MISSING and the weight thresholds; ledger round-trip and
atomic write. Live calls only in `@pytest.mark.network` tests.

## Out of scope

Creator-transfer watcher, ntfy, Claude extraction scheduling (stays an
explicit yes, or a later spend-capped option), web UI, residential proxies.
