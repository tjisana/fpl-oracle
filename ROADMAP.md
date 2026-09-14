# fpl-oracle roadmap

**PLAN.md is the build log — what was done and why.** This file is the short
list of what's NEXT. Keep it skimmable: one line per item, detail lives in the
linked code or in PLAN.md.

**End goal:** a daily-running service that harvests FPL YouTube creators,
extracts their picks/transfers/chip plans, and tells me what to do about MY
squad this week — surfaced in a web app, deployed in the cloud.

---

## NOW — GW4 deadline (Sat 2026-09-12, 12:30 UTC / 08:30 EDT)

GW3 decision (2026-09-04): Thomas → Konsa, Triple Captain on Haaland,
wildcard deferred to GW4 with the solver's squad as the template. Grade it
against GW3 points before trusting the GW4 report.

- [x] **Price snapshot moved to AWS** (2026-09-10): Lambda + S3, 07:00 UTC
      daily, alarm emails on failure. `scripts/sync_snapshots.sh` pulls it
      down. Laptop launchd jobs removed. See `docs/daily-jobs.md`.
- [x] **Gemini made the default transcript source** (2026-09-10) after the
      third IP block in three gameweeks. See the ADOPTED section below.
- [x] **Read the GW4 videos via Gemini** (2026-09-11): 13/14 active
      creators; 3 of 8 Gemini reads came back with timings that passed
      validation (the rest `:untimed`). FPL BlackBox's 87-minute show was
      the miss: the free tier's per-MINUTE input cap is 250k tokens and a
      video costs ~100 tokens/second, so one request cannot cover more
      than ~40 min. FIXED 2026-09-12 by reading long videos in 30-minute
      windows spaced a quota period apart (`chunk_windows`), all-or-
      nothing, provenance `:untimed:chunked`. No paid tier needed.
      (A full warm at 5 videos/creator would be 22 h; don't.)
- [x] **Run extraction, then solve, `--gameweek 4` on both.** Done
      2026-09-12: 14/14 active creators, 94% name match. WILDCARD played
      with the solver's squad, Palmer captain; decision recorded in
      `data/runs/20260912T041124Z-b3fe09d/decision.md`. Grade after GW4.
- [ ] **Reserve fallback** (decided 2026-09-12). Roster now carries a
      `CreatorStatus`: Heisenberg RETIRED (dead channel); FPL Matthew,
      Planet FPL and FPL Family RESERVE — "when there's nothing else".
      Today RESERVE simply means not harvested. The follow-up: when fewer
      than N active creators contribute to a gameweek (N to decide; the
      scorecard says 13/14 is normal), run extraction over the reserves
      too. Planet FPL also needs a per-creator title hint ("The Weekender";
      its 60-min length is now readable via windows); Matthew needs a relaxed
      attribution rule for the FFH channel, which risks crediting strangers.
- [ ] **Timing enrichment** — Gemini transcripts are `:untimed`; add a
      laptop-side pass that aligns a scraped caption track (when one can be
      fetched without a block) onto the Gemini text so deep links come back.

### GW3 deadline (done 2026-09-04) — kept for the record

GW2 result: 114 pts (GW1: 34), OR 8.09M → 2.27M. Transfer made: Wirtz →
Szoboszlai (the report's #2 move). Into GW3 with 1 FT, £0.5m bank, all
chips unused.

- [x] **Cache warmed for GW3** (2026-09-03): 52 transcripts, 0 blocked at
      6s pacing. Price snapshot taken. 14/18 creators have a GW3 video.
- [x] **Daily jobs installed 2026-09-10** (launchd, per `docs/daily-jobs.md`;
      `launchctl list | grep fpl-oracle` shows both). They were NOT installed
      after GW3 despite the earlier tick: snapshots missed 09-04 to 09-10.
      Snapshot is non-negotiable (prices change nightly); warm is free,
      spends no LLM credits, and collects creator posting-schedule data.
- [x] **Wildcard week** — most GW3 titles were wildcard / free-hit drafts.
      Three changes shipped (2026-09-03): free-hit picks excluded from the
      transfer recommender (kept for the £100m squad); a chip-consensus
      section reading the already-extracted `chip_plans`; a wildcard-gap
      line comparing the owned squad with the solver's on the solver's own
      objective.
- [x] **Run extraction, then solve.** Done 2026-09-04; decision recorded in
      `data/runs/20260904T163052Z-917ec6b/decision.md`.
- [ ] **Creator posting schedule** — every cached listing and extraction
      carries a publish timestamp; after a few gameweeks, derive when each
      creator actually posts and gate the warm job on it instead of guessing.

### GW2 deadline (done 2026-08-28) — kept for the record

- [x] Gameweek-agnostic harvest (`--gameweek N`, publish-date gating,
      skip-if-recent).
- [x] Transcript warmer (`ingest/warm_cache.py`).
- [x] My-squad wiring (`fpl/entry.py` → run record, league standings).
- [x] Price snapshot (`data/snapshots/`).
- [x] Transfer recommender (`solver/transfers.py`), wired to the report.
- [x] Blocked-transcript retry, Gemini fallback run by hand for 4 creators.

## ADOPTED 2026-09-10 — Gemini is the default transcript source

**Decision:** `ingest/sourcing.py` routes every transcript fetch through
Gemini by default; the caption scraper is opt-in (`--source captions`) for
the real timestamps it still uniquely provides. Trigger: a third IP block
in three gameweeks (2026-09-10, 6s pacing, residential IP, blocked on the
19th fetch), which falsified "rate matters more than IP class".
Still open from the list below: timestamps (Gemini's are discarded, so
the evidence trail's deep links degrade to untimed for Gemini-read
videos — a laptop-side "timing enrichment" pass from captions is the
planned fix), and the free tier's 8-hours-of-video-per-day cap (paid tier
lifts it; the YouTube-URL feature itself is still no-charge in preview).
The GW4 run doubles as the widened comparison the last bullet asked for.
Found on the first full run (2026-09-11): the free tier's per-minute
input cap (250k tokens) means one request cannot cover more than ~40
minutes of video. Solved 2026-09-12 with windowed reads (30-min offsets,
60 s apart), so BlackBox's 87-minute show and Planet FPL's hour-long one
read on the free tier; the paid tier is no longer a prerequisite for any
creator.

**Status 2026-08-28 (kept for the record): evaluated on real data, not yet
adopted. This is the most consequential open decision in the project.**

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
load unchanged. Gemini was a deliberate FALLBACK until 2026-09-10; it is
now the default (see the heading).

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

## LEARNING TRACK — the owner's, runs alongside everything (added 2026-09-14)

The project is two cohesive halves — the engine (harvest → extract →
consensus → solve → report) and the site — and both are a teaching tool
and a portfolio piece for the owner's job search (TypeScript/React, AWS,
API design, applied AI). Morsel style throughout: one concept, one small
task, check-in; the owner types the code. Order agreed 2026-09-14:

- [ ] **Engine walkthrough** (owed since GW3): the design decisions, one
      topic per session, captured in a doc (precedent:
      `docs/phase1-design-review.md`). Topics: two-variable-set solver
      objective and price anchoring; band-relative consensus and "avoid
      is a score not a veto"; wire-schema vs strict-schema extraction and
      the retry loop; name resolver tiers; mechanical nuance attribution;
      transfer recommender's yardstick and refusal to price hits; free-hit
      guard / chip consensus / wildcard gap; transcript sourcing (scraper
      vs Gemini, the windowed read); the cloud snapshot and why files
      before a database. Recommended to go FIRST.
- [ ] **Front-end track**, in this order, each a morsel session against
      DESIGN.md and the coupon components: (1) restart `npm run dev` in
      `web/` and read DESIGN.md beside the page; (2) read the reference
      tab's files: Box → Countdown → SquadRows → Checklist → MySquad;
      (3) Report tab (render the run's markdown; fixture first);
      (4) Creator ownership grid (table of players × creators, reuses rows
      and tally); (5) Evidence trail (per-player picks, search box, URL
      state so names on other tabs link here); (6) My squad LIVE state,
      after Claude's live module lands.
- [ ] **Learning log**: `docs/learning/` — one short page per concept, in
      the owner's words after each morsel; doubles as interview prep.
- [ ] **Architecture page**: one diagram of the seam — pipeline, storage
      (S3 + DB), API, site, cloud — kept current; the page to open in an
      interview.

Tailwind (v4) is in and does layout and phone/desktop switching via
utility classes; the coupon's look lives in named CSS classes in
`web/src/index.css`; shadcn/ui is installed (decision 8) but the coupon
rebuilds its own controls.

## THEN — web app, release one (target: a working URL by early Oct 2026)

Design is settled in `docs/web-app-design.md` (19 decisions, four tab specs,
north star). This is the ordered work list; tick here as things land.

### Claude: engine, database, API

- [ ] **Database** — SQLite via SQLAlchemy + Alembic (Postgres later is a
      connection string). One table per thing the pipeline already emits:
      runs, creators, players, videos, transcripts, picks + votes, chip
      plans, per-run consensus scores, solver squad, transfer
      recommendation, decisions, daily prices. Carry `quote` and
      `timestamp_s` columns on picks from day one even though extraction
      does not fill them yet. Pipeline writes each run; a one-off backfill
      loads the run folders already under `data/runs/`.
- [ ] **Read-only API** — FastAPI over the DB plus a thin live layer. One
      endpoint per screen element (latest run, run by id, my squad, report
      text, creator ownership table + grid, evidence per player, price
      history, live gameweek feed). Every endpoint named and documented as
      a tool a model could call (north star). OpenAPI → a script that
      regenerates TypeScript types under `web/src/api/`.
- [ ] **Live module in `fpl/`** — `event/{gw}/live/`, `fixtures/`, bonus
      from BPS, predicted autosubs; pure functions over API responses.
- [ ] **Dev loop** — one command runs API + web; Docker Compose with
      Postgres for later; a fixture DB with sample data so the front end
      can be built without running a pipeline.
- [ ] **Quote + timestamp extraction** — verbatim `quote` per pick with a
      mechanical substring check; `timestamp_s` only where the transcript
      has trusted timings (Gemini reads are `:untimed` until timing
      enrichment lands). Was targeted for GW4; slipped. The evidence
      trail ships with paraphrase + video link until this lands.

### Owner: front end (`web/`, learning project, morsel-taught)

- [x] Vite + React + TS scaffold (committed 2026-09-12).
- [x] Tailwind + shadcn/ui, tabs shell — built 2026-09-14 by Impeccable as
      the reference, with the design system: the Pools Coupon world
      (PRODUCT.md, DESIGN.md, `web/src/index.css` tokens, `components/coupon/`).
- [x] My squad tab, PLANNING state — the reference tab, built 2026-09-14
      against a fixture cut from the real GW4 run (`web/src/fixtures/`).
      Owner: the LIVE state, once the live module exists.
- [ ] Report tab (markdown as-is, then sections rebuilt one at a time).
- [ ] Creator ownership grid.
- [ ] Evidence trail (paraphrase + link now; quote + deep link later).

### Deployment (after the tabs work)

- [ ] S3 + CloudFront for `web/`; Lambda or a small container for the
      API; the pipeline's cloud half scheduled; managed Postgres. All in
      the existing CDK app under `infra/` (TypeScript).
- [ ] **Durable storage for the pipeline** is already started: prices live
      in S3. Transcripts and run records must follow before any pipeline
      stage moves to the cloud — an ephemeral cache means re-reading the
      season every run.
- [ ] Explicitly NOT building: a predicted-points table, and any write to
      FPL (decision 19: the site never writes to FPL; hand-off checklist
      instead).

### Later releases (designed for now, not built)

- Controls: buttons that enqueue warm / extract / solve as jobs with
  visible status (job endpoints, never long-running requests).
- Assistant text box: an LLM with tool use over the site's own API.
- Live rank estimate; run-over-run ownership change (needs history —
  the schema keeps per-run votes so it is possible).
- Consensus momentum across days — the payoff of daily harvesting.

## KNOWN ISSUES

- [ ] **Pricing reconciliation degrades between a deadline and that gameweek's
      last fixture.** `my_squad.gameweek` is the last FINISHED gameweek while
      the price rollback keys on bootstrap's `is_current` event, so for ~2 days
      a week they differ and the market-value check cannot roll prices back
      (note says so; affordability reads UNVERIFIED). Fix: fetch the
      in-progress gameweek's picks (public after the deadline) so squad and
      current event agree. Pre-deadline runs, the normal path, are unaffected.
- [ ] **Default data paths are RELATIVE** (`data/...` in `ingest/`, `extract/`,
      `fpl/`, `pipeline`). Any command run from a cwd other than the repo root
      silently builds a parallel `data/` tree there — on 2026-09-03 a dry-run
      from `data/extractions/` created `data/extractions/data/cache/` (201
      files). Anchor defaults on the package/repo root (or an env var) and
      delete the stray tree.
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

- [ ] **Phonetic name mangles still unresolved** — ~71 of 478 picks.
      `'Virgil van Djk'`, `'Dominic Calvalu'`, `'In Burmo'`, `'Victor Yres'`.
      First-name references were fixed (`b12e4a1`); this is the remaining class.
      Re-run `scripts/name_probe.py` after any resolver change — it is the safety net.
      **MAY BE DISSOLVED ENTIRELY** by the Gemini shift above, which reads names off
      on-screen graphics instead of guessing phonetically: 96.1% vs 80.5% resolution
      on real GW2 videos. Settle that question before investing further in the
      resolver — this class of bug may not be worth fixing twice.
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
- [x] ~~4 eligible creators contributed nothing to the GW1 run (FPL Heisenberg,
      FPL Matthew, Planet FPL, Pras).~~ Pras has contributed GW2-GW4. The
      other three never did in four gameweeks: Heisenberg retired, Matthew
      and Planet FPL moved to RESERVE on 2026-09-12 (see NOW). Scorecard
      after GW4: 11 creators 4/4, Pras and Hints 3/4, BlackBox 2/4 (length
      limit), Family 1/4 (now RESERVE). ~72% of roster weight shows up
      reliably; the weights were computed as if all 18 would.
