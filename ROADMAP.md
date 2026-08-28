# fpl-oracle roadmap

**PLAN.md is the build log — what was done and why.** This file is the short
list of what's NEXT. Keep it skimmable: one line per item, detail lives in the
linked code or in PLAN.md.

**End goal:** a daily-running service that harvests FPL YouTube creators,
extracts their picks/transfers/chip plans, and tells me what to do about MY
squad this week — surfaced in a web app, deployed in the cloud.

---

## NOW — GW2 deadline (Fri 2026-08-28, 17:30 UTC / 13:30 EDT)

GW1 result: 34 pts vs a 50 average, OR 8,092,210, 8 left on the bench.
Bottom or near-bottom of every private league. 1 free transfer, £0.0 bank.

- [x] **Gameweek-agnostic harvest** — selection was hardcoded to GW1, so the
      system silently expired the moment the season moved on. `run_extract`
      now takes `--gameweek N` (defaults to the FPL API's next event), gates
      generic in-season titles on a publish-date window, and skips-if-recent
      so a retry can't re-bill every creator.
- [x] **Transcript warmer** (`ingest/warm_cache.py`) — decouples the free,
      slow, blockable YouTube stage from the paid extraction stage.
- [x] **My-squad wiring** — `fpl/entry.py` was built last week and connected
      to nothing; it now flows into the run record, with league standings and
      season points parsed onto `EntrySummary`.
- [x] **Price snapshot** — `data/snapshots/2026-08-28.json` (616 players).
      Run this DAILY; purchase price is only reconstructible going forward.
- [ ] **Retry 3 blocked transcripts** before the paid run: fpl-blackbox,
      fpltips, fpl-dylan. We were IP-blocked mid-harvest on 2026-08-27.
- [ ] **Run extraction, then solve.** See `docs/deadline-runbook.md`.
- [ ] **Transfer recommender does not exist yet** — the pipeline still solves
      a fresh 15 from £100m, which is the WRONG question in-season. With 1 FT
      and no bank, do not ship its squad output as-is.

## STRONGLY CONSIDERING — move transcript ingestion to Gemini

**Status 2026-08-28: evaluated on real data, not yet adopted. This is the
most consequential open decision in the project.**

The whole pipeline currently rests on a reverse-engineered endpoint.
There is NO official way to read a third-party YouTube video's captions —
`captions.download` requires OAuth as the video's owner and 403s for
everyone else — so `ingest/transcripts.py` scrapes the internal endpoint
YouTube's player uses. No contract, no quota, no SLA, and it got this
machine IP-blocked for 5+ hours on GW2 deadline eve, costing creators
from the harvest.

Gemini takes a YouTube URL as a first-class input (Google owns both
sides). Measured on real GW2 videos, including the exact ones we were
blocked from scraping:

| | Scraped captions | Gemini |
| --- | --- | --- |
| Picks resolved to real players | 207/257 = **80.5%** | 49/51 = **96.1%** |
| Creators | 10 | 2 |
| Blocked by YouTube | yes | no |

The resolution gap is the headline. It is consistent with the mechanism —
Gemini WATCHES the video and reads names off on-screen team sheets, where
ASR guesses phonetically ('Burmo' for Mbeumo, 'Virgil van Djk',
'Calvat Leuin'). That mangling is our oldest unresolved quality issue
(see KNOWN ISSUES), and this may simply dissolve it. Two creators is
suggestive, not conclusive — widen the sample before committing.

**What must be resolved before adopting:**

- [ ] **Timestamps are unusable.** Gemini's segment timings are invented:
      a 317s video came back spanning 461s, a 1832s video spanning 2638s
      as six segments. `ingest/gemini_transcripts.py` validates against
      the real duration and DISCARDS all timings when they don't fit,
      marking the source `:untimed`. That protects the evidence trail
      from wrong deep links but does not restore it — and timestamped
      creator quotes are the differentiator no other tool has. Options:
      align Gemini text against a scraped caption track when one exists,
      or accept untimed transcripts for creators we cannot scrape.
- [ ] **Preview status.** The YouTube-URL feature is in preview and free
      of charge, with pricing and limits explicitly subject to change.
      Model cost if it leaves preview, and decide the fallback.
- [ ] **Free tier caps at 8 hours of video/day.** The roster is ~18
      creators x ~20 min = ~6 hours. Fits, without much room.
- [ ] **503s under load** — 2 of the first 4 calls. `MAX_ATTEMPTS` is 3;
      confirm that is enough at full roster scale.
- [ ] **Widen the comparison** — run both sources across all creators for
      one gameweek and compare resolution rates properly.

Already built (`ingest/gemini_transcripts.py`, `feat/gemini-transcripts`):
fetch + timing validation + provenance. `Transcript.source` now records
where text came from, defaulting to `youtube-captions` so existing files
load unchanged. Gemini is a deliberate FALLBACK today, not the default.

## NEXT — in-season (the actual product)

The GW1 system answers a cold-start question: "pick 15 from nothing, £100m."
In-season inverts it: "given the squad I own, is any change worth making, and
is it worth -4?" Different objective, much smaller search space.

- [ ] **My-squad awareness** — pull my actual squad via `entry/{id}/event/{gw}/picks/`,
      plus bank, team value, free transfers. Everything below depends on this.
- [ ] **Transfer recommender** — replace the 15-man ILP with a delta optimizer:
      rank candidate transfers by consensus gain vs. the -4 hit cost.
- [ ] **Extraction scope widening** — creators' videos cover transfers in/out,
      chip strategy, and "how I set up for next week". v1 extraction only handled
      squad reveals; `time_horizon` and chip plans are the gaps.
- [ ] **Price-change awareness** — the FPL API now carries `price_change_projections`
      (with `likelihood`, 3-day offsets), `price_change_hourly_rate`,
      `price_change_locked_until`. Dormant until the season starts. This creates a
      DAILY cadence need: acting before a rise/fall is a real decision.
- [ ] **Creator scoreboard** — 19/20 creators have verified FPL entry ids. Track
      their live ranks + whether their picks paid off. Makes the weights honest.

## THEN — deployment + web app

- [ ] **Durable storage FIRST.** `data/` is gitignored and ephemeral on Lambda/Fargate.
      If the transcript cache doesn't survive between runs, every run re-fetches the
      whole season and gets IP-blocked. This is a precondition, not an optimization.
- [ ] **Split the pipeline at the cache boundary** (recommended, £0): fetch transcripts
      on a local machine → sync to S3; run extract → solve → report in the cloud.
      "residential IP, never blocked" was the original reasoning here — TESTED and
      FALSIFIED on 2026-08-27 (this machine, a residential IP, was IP-blocked for
      hours after a burst harvest). A residential IP still lowers block risk vs. a
      shared cloud IP, but "never" was wrong and rate matters more than IP class at
      our volume. See `docs/transcript-blocking.md` for the full incident writeup,
      the proxy-vs-pacing recommendation (don't buy a proxy yet), and how to
      configure `WebshareProxyConfig` support (already wired, off by default) if it
      later becomes warranted. Verify with `scratchpad/yt_block_probe.py` from an
      EC2 box before building.
- [ ] **Web app** (single-user, no auth). Four screens worth building:
      1. Action queue — ranked "do this by then", with TWO clocks (nightly price
         change ~01:30 UTC, and the gameweek deadline).
      2. Evidence trail — click a recommendation, get the creator quotes with
         timestamped YouTube deep links. The differentiator; no other tool has it.
      3. Consensus momentum — who's gaining/losing backing over days. This is the
         entire payoff of daily harvesting; a weekly snapshot destroys it.
      4. My squad — availability, price risk, sentiment per player.
- [ ] Explicitly NOT building: a predicted-points table (every site has one, ours
      would be worse), and auto-apply/autopilot (FPL has no write API — it means
      automating against their session, an account-risk question).

## KNOWN ISSUES

- [ ] **YouTube IP-blocks under burst load** — harvesting ~34 transcripts in
      one go tripped an `IpBlocked` on 2026-08-27. Block was confirmed still
      active 3h30m+ later. `warm_cache` stops the run on the first block
      rather than hammering it, and now paces network fetches (`--delay`,
      default 2.0s ±40% jitter) plus a consecutive-failure circuit breaker.
      `scripts/retry_blocked_transcripts.sh` now backs off exponentially
      (20m/40m/80m/120m-cap) instead of a flat 20-minute probe interval.
      Optional proxy support is wired into `ingest/transcripts.py` (off by
      default). Full writeup, and why we're not buying a proxy yet, in
      `docs/transcript-blocking.md`.
- [ ] **A creator whose only content is titled for the PREVIOUS gameweek is
      dropped.** `is_gameweek_video` rejects an explicit wrong-gameweek number
      outright, which cost us fpl-family for GW2 ("FPL GW1 Review + BIG
      Transfers Incoming"). Deliberate — the alternative is picking up stale
      videos — but revisit if it keeps costing real creators.

- [ ] **Phonetic name mangles still unresolved** — MAY BE DISSOLVED by the
      Gemini shift above: it reads names off on-screen graphics rather than
      guessing phonetically, and scored 96.1% vs 80.5% resolution on real GW2
      videos. Evaluate that before investing further in the resolver.
- [ ] _(original)_ **Phonetic name mangles** — ~71 of 478 picks. `'Virgil van Djk'`,
      `'Dominic Calvalu'`, `'In Burmo'`, `'Victor Yres'`. First-name references were
      fixed (`b12e4a1`); this is the remaining class. Re-run `scripts/name_probe.py`
      after any resolver change — it is the safety net.
- [ ] **Uneven resolution across creators** distorts weighting: FPL Salah loses 41%
      of picks to name resolution, Gianni Buttice 0%.
- [x] ~~**Re-extraction re-pays for creators already done**~~ — FIXED. `run_extract`
      now reuses an existing `data/extractions/{video_id}.json` for the selected
      video and counts the reuse separately; `--force` overrides.
- [x] ~~First-name references resolved to the wrong player~~ — FIXED. `b12e4a1`
      added an explicit first-name path; `1abf491` then fixed a wrong-match
      regression it introduced (accent-stripping made "Jérémy" and "Jeremy"
      different keys, hiding Doku behind an academy player). Live roster now:
      523/537 first-name references correct, **0 wrong** (was 8 correct / 33 wrong).
- [ ] **`selling_price` is not available from public FPL endpoints** — it lives on
      the authenticated `my-team/{id}/` endpoint. `fpl/entry.py` leaves it `None`
      rather than recomputing from `now_cost` (which would misstate budget on every
      transfer decision). PATH FORWARD, no credentials needed: `entry/{id}/transfers/`
      is public and carries the cost each player was bought/sold at, so purchase price
      is derivable for transferred-in players; the initial squad needs a price snapshot
      we can take ourselves once running daily.
- [ ] **Free-transfer derivation is unvalidated** — structurally sound, but no squad
      has transfer history this season yet. Spot-check after a few gameweeks.
- [ ] **Tier-2 phonetic matching is loose** — a first name that sounds like a
      teammate's surname can still slip through the fuzzy tiers.
- [x] ~~**FPL Matthew unattributed**~~ — DECIDED 2026-08-28, deliberately left out.
      His channel-mates name themselves in titles so they attribute cleanly; his
      videos don't, and the shared FFH channel hosts non-registry people, so
      attributing unclaimed videos to him would risk a stranger's picks carrying
      his weight. One lost creator is the cheaper error. See `roster/registry.py`.
- [ ] 4 eligible creators contributed nothing to the GW1 run (FPL Heisenberg,
      FPL Matthew, Planet FPL, Pras). Pras matters most — he's The FPL Wire's
      channel primary, so his absence silently costs Zophar and Lateriser too.
