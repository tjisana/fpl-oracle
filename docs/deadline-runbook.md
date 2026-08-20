# Deadline-morning runbook

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
