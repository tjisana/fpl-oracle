# Deadline-morning runbook

## GW4 — Sat 2026-09-12, 12:30 UTC / 08:30 EDT

State on 2026-09-10 (~04:00 UTC): 18 GW4-window transcripts cached from 4
creators (Let's Talk FPL, FPL Harry, FPL Raptor, Pras) before YouTube
blocked the IP; 14 creators unread. Gemini is now the DEFAULT transcript
source, so the block no longer matters. The Gemini free tier caps at 8
hours of video per day; extraction reads ONE video per creator, and the
GW4 selection measured on 2026-09-11 is 14 videos / 5.6 h (4.2 h uncached)
— under the cap, so the paid tier is not needed. Skip the full warm
(5 videos per creator, 22 h) and let extraction fetch exactly what it
selects.

Owner context: GW3 used Triple Captain (Haaland); wildcard planned for
THIS deadline with the solver's £100m squad as the template; 1 FT,
~£0.1m bank. Saturday deadline, so "deadline morning" is Friday evening
plus a Saturday-morning refresh.

### 1. (Optional) warm only the selected videos via Gemini

Not required: step 3 fetches the selected video per creator itself, via
Gemini by default. Only warm if you want the fetches done ahead of the
paid step — and then cap it: `--max-per-creator 1` keeps it near the
selection instead of 22 h of podcasts. Expect `gemini:<model>:untimed`
provenance (timings are discarded — see ROADMAP). If a run stops with the
breaker's "8-hours-per-day cap" message, wait for the daily reset or
enable the paid tier on the key (YouTube input is still no-charge).

### 2. Refresh the listing and read what extraction will select (free)

```
uv run python -m fpl_oracle.extract.run_extract --gameweek 4 --force-refresh --dry-run
```

### 3. Extract, then solve (paid: ~18 Claude calls)

```
uv run python -m fpl_oracle.extract.run_extract --gameweek 4 --force-refresh
uv run python -m fpl_oracle.pipeline --gameweek 4 --force-refresh
```

PASS `--gameweek 4` EXPLICITLY. Before either, pull the cloud price
snapshots: `./scripts/sync_snapshots.sh`.

### 4. Read, in this order

1. Grade the GW3 decision (`data/runs/20260904T163052Z-917ec6b/decision.md`)
   against actual GW3 points.
2. `## Wildcard gap` and `## Chip consensus` — this is the wildcard week.
3. `## This week's transfer` — only if the wildcard is NOT played.
4. `data/extractions/match_quality.md` — resolution per creator; compare
   Gemini-read creators against the 4 scraped ones (the widened
   comparison the roadmap asked for).

---

## GW3 — Fri 2026-09-04, 17:30 UTC / 13:30 EDT

State on the eve (2026-09-03 ~20:30 UTC): transcript cache warm for every
creator with a GW3 video (52 fetched, 0 blocked, 6s pacing held), price
snapshot taken, 14/18 creators resolved to a GW3 video, YouTube block lifted.
Nothing has been extracted yet — extraction is tomorrow's first paid step.

Owner context: 1 free transfer, £0.5m bank, both first-half wildcard and
free hit unused (first-half chips expire after GW19). GW3 is the last
gameweek before the international break; GW4's deadline is Sat 2026-09-12
12:30 UTC, eight days later.

### 1. Refresh the listing and read what extraction will select (free)

```
uv run python -m fpl_oracle.extract.run_extract --gameweek 3 --force-refresh --dry-run
```

`--force-refresh` is REQUIRED: without it the 6h uploads cache can hide a
"final thoughts" video posted this morning. Read the list before spending.

### 2. Extract, then solve

PASS `--gameweek 3` EXPLICITLY on both commands (`is_next` flips to GW4 the
moment the deadline passes).

```
uv run python -m fpl_oracle.extract.run_extract --gameweek 3 --force-refresh
uv run python -m fpl_oracle.pipeline --gameweek 3 --force-refresh
```

Roughly 14 Claude extraction calls; re-running is safe (skip-if-recent).
If a creator's transcript is missing and YouTube is blocking again, the
fallback chain uses cached older videos; Gemini is available by hand via
`ingest/gemini_transcripts.py`.

### 3. Read this, in this order

1. **`## This week's transfer`** — the one-free-transfer answer. Note the
   line naming free-hit drafts kept out of this section (new for GW3).
2. **`## Wildcard gap`** — one number for how far your squad sits from the
   solver's £100m squad, in the same units as the transfer deltas, plus
   how many of your 15 the solver keeps. This is the wildcard-or-not input.
3. **`## Chip consensus`** — how many creators are wildcarding THIS week
   versus later. Stated intentions only.
4. **`data/extractions/match_quality.md`** — resolution per creator, and
   the free-hit pick count per creator.

The wildcard decision itself is yours. The report gives the gap, the
room's timing split, and the transfer alternative; it does not price a chip.

---

## GW2 — Fri 2026-08-28, 17:30 UTC / 13:30 EDT

Do these in order. Steps 1-2 are free; step 3 is the only one that spends.

### 1. Retry the three blocked transcripts (free)

The unthrottled harvest on 2026-08-27 pulled 34 transcripts in 59 seconds
and was IP-blocked. `fpl-blackbox`, `fpltips` and `fpl-dylan` were lost to
it. A poller may already have recovered them overnight — check first:

```
tail -5 /tmp/fpl-retry.log
uv run python -m fpl_oracle.ingest.warm_cache --since 2026-08-22 --dry-run
```

If they are still missing and the block has lifted, warm them slowly:

```
uv run python -m fpl_oracle.ingest.warm_cache --since 2026-08-22 --delay 4 \
  --creator-ids fpl-blackbox fpltips fpl-dylan
```

If still blocked, proceed without them — 10 creators is a legitimate basis.
Do NOT lower `--delay` to go faster; that is what caused the block.

### 2. Check what extraction will select (free, no Claude calls)

```
uv run python -m fpl_oracle.extract.run_extract --gameweek 2 --dry-run
```

13/18 creators resolved when last checked. Read the list before spending.

### 3. Extract, then solve

PASS `--gameweek 2` EXPLICITLY, on both commands. The FPL API's `is_next`
flips to GW3 the moment the 17:30 UTC deadline passes, so a run started
late would silently target GW3 and find nothing.

```
uv run python -m fpl_oracle.extract.run_extract --gameweek 2
uv run python -m fpl_oracle.pipeline --gameweek 2 --force-refresh
```

Extraction is the paid stage: one Claude call per creator, ~13 creators.
Re-running is safe — the skip-if-recent guard reuses extractions already
on disk and only pays for what is missing. Use `--force` only to
deliberately re-extract.

### 4. Read this, in this order

1. **`## This week's transfer`** — the first section of `report.md`, and
   the only part actionable with one free transfer. Everything below it
   describes the solver's £100m squad, which is the wildcard answer.
2. **A `FORCED_MOVE` verdict** means an owned player is unavailable. As of
   2026-08-27, Chiesa is injured (`status='i'`, 0% chance).
3. **The pricing line.** If it says UNVERIFIED, the derived selling prices
   did not reconcile against FPL's own squad value — confirm affordability
   in the FPL app before making the transfer.
4. **`data/extractions/match_quality.md`** — did every creator's picks
   resolve? A creator with 0 matched picks contributed nothing.

### If the pipeline refuses to solve

`REFUSING TO SOLVE: no usable creator picks for GW2` is a correct,
deliberate refusal, not a crash. With zero picks the objective degenerates
to "spend £100m" and returns a confident-looking squad backed by nothing.
It means extraction produced no GW2 data — fix that rather than working
around it.

---

## General notes (any gameweek)

For the owner, at 8am, under time pressure. Command-first.

## 1. Check the plan first — always

```
uv run python -m fpl_oracle.deadline --dry-run
```

Touches nothing (no network, no spend). Prints the three stages, what
each costs, and how many creators/API calls stage 2 will burn. Read it.
If the creator count looks wrong, stop and check `roster/registry.py`
before spending anything.

## 2. Run it for real

```
uv run python -m fpl_oracle.deadline
```

Runs all three stages in order:

| Stage | What | Cost | Rough time |
| --- | --- | --- | --- |
| 1. warm transcript cache | `ingest.run_ingest` — the Phase 1 demo runner: fetches transcripts for up to 3 eligible creators and stops. **Best-effort, never fatal** — a failure (including its routine non-zero exit when it saves fewer than 3) is warned about and stepped over. Stage 2 refreshes video selection for *every* creator itself, bypassing the 6h uploads cache. | free (YouTube quota only) | ~1–2 min |
| 2. re-extract | `extract.run_extract` — finds each eligible creator's GW1 video, one Claude call per creator | **costs money** — this is the expensive stage | ~1–3 min per creator, so several minutes total for ~15–20 creators |
| 3. re-solve | `pipeline.run_pipeline(force_refresh=True)` — force-refetches FPL availability, re-solves, writes the report; nuance pass runs by default | force-refresh + solve is free; the nuance pass **costs money** (1 Claude call) — skip with `--no-nuance` | under a minute (solve) + ~10–30s (nuance) |

Each stage prints a banner and its own elapsed time. At the end you get:

- `run_id`, squad cost, formation, captain/vice, report path
- a one-line **DIFF vs the previous run**: who's in, who's out, whether
  the captain changed — this is the single most useful line on deadline
  morning, read it before you read anything else. It is also the FIRST
  section of `report.md` itself now (`## Squad diff vs previous run`),
  not just terminal output — so it survives a scroll back through
  yesterday's terminal, and a bare `pipeline.py` run (no `deadline.py`
  chain) gets one too.

## 3. Before you ship — eyeball these four things

1. **`data/extractions/match_quality.md`** — did every creator's picks
   resolve? A creator with 0 matched picks contributed nothing this run;
   check whether that's a real absence (no GW1 video posted) or a
   resolver miss worth fixing before the squad is trusted. The report's
   own "Evidence quality / caveats" section names any eligible creator
   who contributed nothing, and a resolved/total pick count per creator,
   so a silent 40%-of-one-creator's-picks loss doesn't hide behind the
   run-wide skip count.
2. **The report's "Availability" section, and the Availability column on
   the squad tables** — a DOUBTFUL player (e.g. `status='d'`, 25% chance
   of playing) is discounted in the objective but is NOT excluded from
   the squad by design, so it can still be sitting in your starting XI.
   The report calls out every non-AVAILABLE squad member by name right
   under the header; if that section says "every squad member is fully
   available," there's nothing to check here. This is exactly the set
   that moves on deadline morning, which is the whole reason
   `--force-refresh` exists — also check the header's "FPL data: fetched
   ..." line to confirm the availability data itself is actually fresh,
   not a stale cache read.
3. **The report's "Vetoed by availability" section** — this table is
   filtered to excluded players creators actually backed (`backers > 0`);
   on a real run that's typically one row out of dozens of structurally
   excluded players, not the full veto list. Read it as "the veto cost
   you someone people wanted," not as a complete injured/suspended
   roster.
4. **The thin-captaincy warning** — if the report flags the captaincy
   election as thin (too few creators named an armband this run), treat
   the suggested captain as a lean, not a settled call, and sanity-check
   it yourself before locking it in.

If all four look fine, ship the squad from the printed report path.

## 4. If a stage fails

Nothing already written to disk is lost or touched — stage 1's
transcripts and stage 2's extractions are already on disk by the time a
later stage runs, and this command never deletes or overwrites anything
under `data/runs/`. The failure message prints the exact resume command;
otherwise use these directly:

```
# full run from the top (stage 1 can't fail the run — it only warns):
uv run python -m fpl_oracle.deadline

# stage 2 (extract) failed — RETRY it, skipping the already-warm cache:
uv run python -m fpl_oracle.deadline --skip-ingest

# stages 1+2 are good, only re-run the solve (e.g. it errored, or you
# just want a fresh availability-forced re-solve without re-paying for
# extraction):
uv run python -m fpl_oracle.deadline --skip-ingest --skip-extract
```

Note the asymmetry on a stage-2 failure: the resume command RETRIES
extraction rather than skipping it. Skipping a failed extract would solve
on the extractions from the LAST run — a stale-picks squad that looks
completely normal. The command that does that is offered explicitly, and
only as a deliberate fallback.

Add `--no-nuance` to any of the above to skip the nuance LLM call (e.g.
you're out of budget, or just iterating on the squad itself).

## Flags, at a glance

| Flag | Effect |
| --- | --- |
| `--dry-run` | print the plan and cost; touch nothing; exit 0 |
| `--skip-ingest` | reuse existing transcripts, skip stage 1 |
| `--skip-extract` | reuse existing extractions, skip stage 2 (the money-saving one when resuming) |
| `--no-nuance` | skip the nuance LLM call in stage 3 |

## If you only need a re-solve, not a full rerun

No new videos, no new picks needed — just want the latest availability
flags applied to the existing extractions:

```
uv run python -m fpl_oracle.pipeline --force-refresh
```

This is `pipeline.py` directly, not `deadline.py` — same force-refresh
behaviour, but skips stages 1–2 entirely and defaults to **no** nuance
pass (opt in with `--nuance` if you want it here too).
