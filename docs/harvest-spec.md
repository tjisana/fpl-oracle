# Harvest spec — unattended transcript collection (Phase 1)

Status: APPROVED by owner 2026-10-27 (daily limit 18). Open items resolved below.
Routing (CLAUDE.md): `ingest/` is not one of the restricted modules, so this is
implementer (Sonnet) work from this spec, reviewed by a fresh Opus subagent.

## Why

GW8 (2026-10-22/23) showed what breaks an unattended run:
- Gemini free tier: 5 requests/min and 20 requests/day per model, plus 503
  "high demand". All 14 transcripts failed; we switched to captions by hand.
- Ten of twelve final videos were posted on the same day (Thu 22 Oct), so the
  daily cap is hit in the one window that matters.
- A video over 30 minutes costs one request per window (BlackBox: 3). Failed
  attempts probably count against the cap too (unverified), so retries must be
  budgeted, not looped.
- `run_extract` falls back to cached videos only; it never switches the
  transcript source for a failing video by itself.

## Goal

One idempotent command that can run many times a day (by hand now, by a
scheduler later) and, for the target gameweek, leaves a usable transcript in
the cache for as many creators' final videos as possible, spending the scarce
Gemini budget where it adds the most.

    uv run python -m fpl_oracle.ingest.harvest --gameweek 9 [--dry-run]

## Behaviour

1. **Target.** `--gameweek N` (default: the next gameweek from bootstrap).
   Deadline time comes from bootstrap `events`.
2. **Selection.** Reuse the existing per-creator selection
   (`run_extract.pick_gameweek_video` / `videos_for_creator`, Shorts filter,
   shared-channel attribution). Do not reimplement it.
3. **Skip what is done.** A video whose transcript is already cached with an
   accepted source is not touched. Re-running changes nothing.
4. **Gemini request budget, per model.** Persist counters in ONE shared file,
   `data/ingest_state/gemini_usage.json` (model -> quota day -> count, plus
   per-model exhausted-for-day flags), NOT in the per-gameweek file: quotas are
   per project/model/day, so two gameweeks on the same Pacific day share one
   budget. (Gemini calls from `run_extract`/`warm_cache` do not go through
   this counter: known limitation.) Quota day = the calendar day in
   `America/Los_Angeles` (Google: daily limits reset at midnight Pacific;
   limits apply per PROJECT and per MODEL). Config is a mapping
   `model -> daily_limit`, default `{"gemini-3.5-flash": 18}` (the observed
   free-tier cap is 20 requests/day for this model; 2 kept spare). Adding a
   second model with its own limit must be a config change only: when the
   first model's budget is spent or its daily 429 fires, try the next model in
   the configured order before falling back to captions.
   - Count every attempt that reaches the API, including failures (Google
     does not document whether failed requests count; assume they do).
   - Cost of a video = number of 30-minute windows (`ceil(duration/1800)`).
5. **Priority.** Order pending videos by creator weight (`roster.weights`),
   highest first. A video the remaining budget cannot cover uses captions
   immediately, with reason `budget`. (Gemini can see on-screen squads, so a
   later iteration may prefer it for team-reveal videos; out of scope now.)
6. **Failure handling per video.**
   - 503 / timeout: mark `retry_after` (30 min, then 2 h, then 6 h), do NOT
     retry within the same invocation, move on.
   - 429 per-minute: sleep 65 s once, then continue.
   - 429 daily: mark Gemini exhausted for the day; remaining videos go to
     captions straight away.
7. **Deadline backstop.** If a selected video still has no transcript and the
   deadline is within `--backstop-hours` (default 6), use captions regardless
   of budget or retry timers.
8. **Captions.** Pace requests. On the first `IpBlocked` stop all caption
   attempts for this invocation and leave those videos pending (do not loop).
9. **State.** `data/ingest_state/{gameweek}.json`, written atomically. Per
   video: `video_id, creator_id, status (pending|ok|failed), source, attempts,
   gemini_requests_used, transient_failures, last_error, retry_after,
   fell_back_reason`. The daily Gemini counters are NOT in this file (see 4).
   An exclusive `flock` on `data/ingest_state/harvest.lock` is held for the
   whole run; a second run fails fast with a nonzero exit. Non-capacity Gemini
   errors (bad response, 400, missing key) go straight to captions with reason
   `gemini_error` / `gemini_unavailable`, not through the 503 backoff.
10. **Report.** Print a table per creator (video, source, status). List
    creators with no transcript and hours to the deadline. When fewer than
    half the weight has a transcript within 12 h of the deadline, print a
    loud warning (a hook for ntfy later — do not add ntfy now).
11. **Provenance.** The transcript's existing source string (`gemini:<model>`,
    `youtube-captions`) must survive so later steps can compare name-match
    rate by source.

## Known limitations (deferred)

- Unknown video duration costs 1 request; long videos may cost/429 more.
- 429 daily-vs-minute is a substring heuristic; parsing RetryInfo/quotaId later.
- `--dry-run` spends no Gemini request but still calls the YouTube API.
- State writes use mkstemp (0600) and skip a directory fsync; `retry_delay` may
  sleep after the last attempt; `attempts` counts gate-aborted tries.

## Out of scope

AWS/Fargate, the title classifier, waves, ntfy, the creator transfer watcher,
changes to extraction or the solver. `run_extract` is only expected to find a
warm cache; its `--source` flag stays.

## Tests (offline, no network)

Fake Gemini and caption clients; injected clock. Cover: idempotent rerun;
budget accounting including multi-window videos; priority order; 503 sets
`retry_after` and is not retried in-run; per-minute 429 sleeps once; daily
429 switches the rest to captions; deadline backstop; `IpBlocked` stops
caption attempts; state file round-trips and survives a crash mid-write.
Live calls go in `@pytest.mark.network` tests only.

## Acceptance

- `--dry-run` shows what would be fetched, from which source, and the budget
  left, without spending a request.
- A simulated GW8-like day (10 same-day videos, 2 multi-window) ends with every
  video transcribed by Gemini or captions, never exceeding the budget.
- ruff, ty, and the fast pytest suite pass.

## Resolved questions

1. Daily limit 18 — approved by the owner.
2. Quota day rolls over at midnight US Pacific (Google rate-limits page).
3. Report flags every caption fallback with its reason (default; owner did
   not object).

## Still open (owner, not blocking)

- Per-model free-tier limits for other Gemini models are only shown in the
  owner's AI Studio rate-limit page; the spec's per-model config is how any
  extra headroom gets used once known.
