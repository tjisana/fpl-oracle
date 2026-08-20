"""Deadline-morning rerun: the one command to run on the morning of a GW1
(or any gameweek) deadline, chaining every stage that needs to be fresh.

    uv run python -m fpl_oracle.deadline --dry-run   # ALWAYS run this first
    uv run python -m fpl_oracle.deadline

SQUAD DIFF: computing "what changed vs. the previous run" needs the run
BEFORE this rerun started (captured before anything gets overwritten) and
the squad/captain THIS rerun just solved — both of which `pipeline.
run_pipeline` already has internally, so it computes the diff itself now
(see `pipeline.SquadDiff`) rather than this module computing it
separately. `SquadDiff`/`load_previous_run`/`diff_vs_previous` used to
live here; they moved to `pipeline.py` so `report.gameweek` could read
them too without importing this module (which would run backwards — a
report shouldn't depend on the thing that orchestrates it) or duplicating
the model. This module just reads `result.diff` off the `PipelineResult`
for its own terminal summary.

STAGES, IN ORDER, AND WHAT THEY COST:

1. warm the transcript cache (`ingest.run_ingest`) — FREE. YouTube Data
   API quota only; no LLM call. BEST-EFFORT AND NEVER FATAL: this is the
   Phase 1 demo runner, which stops after `TARGET_TRANSCRIPT_COUNT` (3)
   transcripts and exits non-zero when it saves fewer, so its result says
   nothing about whether the rerun is healthy. Stage 2 is what actually
   refreshes video selection — it does its own `list_recent_videos` +
   `fetch_transcript` for EVERY eligible creator, not just three — so a
   stage-1 failure here is logged and stepped over rather than aborting
   the run. Letting it gate the chain would mean the least important
   stage could cost you a deadline-morning squad.
2. re-extract picks (`extract.run_extract`) — COSTS MONEY. One Claude
   extraction call per eligible creator with a findable GW1 video, and
   the stage that genuinely refreshes video selection for all of them
   (including creators who had no GW1 video at the last run). Run from
   here with `force_refresh=True`, which bypasses the 6-hour YouTube
   uploads-listing cache — without it a run at 08:00 would reuse a 06:00
   listing and could not see a video posted at 07:30, which is exactly
   when team reveals land. This is the expensive stage; skip it with
   `--skip-extract` if you only need to re-solve (e.g. resuming after
   stage 3 failed). A stage where EVERY creator failed is fatal here:
   see the zero-success guard in `main`.
3. re-solve (`pipeline.run_pipeline`, `force_refresh=True`) — the
   force-refresh + solve itself is FREE. Forces a live refetch of FPL
   availability (bypassing the normal 6h cache) so injury/suspension
   flags from the last few hours are seen, then re-solves the squad and
   writes the report. By default this stage ALSO runs the nuance LLM
   pass over the solved squad (COSTS MONEY, one additional Claude call)
   — this is the full "ship it" command, unlike bare `pipeline.main()`
   where nuance is opt-in. Pass `--no-nuance` to skip it and keep this
   stage free too.

`--dry-run` prints the plan and its cost, in plain terms, and exits 0
WITHOUT touching the network or spending anything. Run it first, always —
it is the safe way to sanity-check what a deadline-morning run will do
before it does it.

A failing stage never loses earlier stages' work: ingest and extract
already wrote their output to disk by the time stage 3 runs, and this
module never deletes or overwrites anything under `data/runs/`. On
failure this prints the exact resume command and exits non-zero.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Protocol

from fpl_oracle.extract.run_extract import run_extraction
from fpl_oracle.fpl.players import PlayerDB
from fpl_oracle.ingest.run_ingest import TARGET_TRANSCRIPT_COUNT, eligible_creators
from fpl_oracle.ingest.run_ingest import main as ingest_main
from fpl_oracle.pipeline import DEFAULT_RUNS_DIR, SquadDiff, run_pipeline
from fpl_oracle.roster.registry import REGISTRY

logger = logging.getLogger(__name__)

RESUME_AFTER_INGEST = "uv run python -m fpl_oracle.deadline --skip-ingest"
RESUME_AFTER_EXTRACT = "uv run python -m fpl_oracle.deadline --skip-ingest --skip-extract"


# ---------------------------------------------------------------------------
# The narrow structural shape this module actually reads off a
# `pipeline.PipelineResult`. A `Protocol`, not the concrete class, so
# tests can hand `_print_final_summary` a lightweight duck-typed fake
# instead of constructing a full `RunRecord`/`Squad` — and so this module
# stays decoupled from `PipelineResult`'s full shape, needing only the
# handful of fields it actually prints. `SquadDiff` itself now lives in
# `pipeline.py` (see the module docstring) — imported directly above,
# not duck-typed, since it's a plain data model with no I/O to fake.
# ---------------------------------------------------------------------------


class _SquadPlayerLike(Protocol):
    player_id: int


class _SquadLike(Protocol):
    players: list[_SquadPlayerLike]
    total_cost_m: float


class _SolverOutcomeLike(Protocol):
    captain: int | None
    vice_captain: int | None
    formation: str
    relaxed_captaincy: bool
    captain_options_required: int


class _RunRecordLike(Protocol):
    run_id: str
    solver: _SolverOutcomeLike


class _PipelineResultLike(Protocol):
    squad: _SquadLike
    record: _RunRecordLike
    report_path: Path | None
    diff: SquadDiff


# ---------------------------------------------------------------------------
# Stage runner + banners.
# ---------------------------------------------------------------------------


def _banner(label: str) -> None:
    print(f"\n=== {label} ===")


def _run_stage(label, fn, *args, **kwargs):
    _banner(label)
    started = time.monotonic()
    result = fn(*args, **kwargs)
    elapsed = time.monotonic() - started
    print(f"  done in {elapsed:.1f}s")
    return result


def _dry_run_report(*, skip_ingest: bool, skip_extract: bool, nuance: bool) -> None:
    print("DRY RUN — no network calls, no subprocess calls, nothing spent. Plan only.\n")

    if skip_ingest:
        print("1. warm transcript cache: SKIPPED (--skip-ingest)")
    else:
        print(
            "1. warm transcript cache (ingest.run_ingest) — FREE (YouTube Data API quota "
            f"only), BEST-EFFORT:\n"
            f"   fetches transcripts for up to {TARGET_TRANSCRIPT_COUNT} eligible creators "
            "and stops. A failure here is logged and stepped over, never fatal — stage 2 "
            "refreshes videos for EVERY creator itself."
        )

    candidates = eligible_creators(REGISTRY)
    if skip_extract:
        print("2. re-extract picks: SKIPPED (--skip-extract)")
    else:
        print(
            "2. re-extract picks (extract.run_extract) — COSTS MONEY:\n"
            f"   up to {len(candidates)} creators x 1 Claude extraction call each "
            f"({len(candidates)} eligible creators registered), plus YouTube Data API "
            "quota for the video lookups. This is the expensive stage."
        )

    print(
        "3. re-solve (pipeline.run_pipeline, force_refresh=True) — FREE:\n"
        "   force-refetches FPL availability (bypasses the 6h cache) and re-solves; "
        "writes run.json, squad.json, and report.md."
    )
    if nuance:
        print(
            "   + nuance pass (on by default here): 1 additional Claude call — COSTS MONEY.\n"
            "     pass --no-nuance to skip it."
        )
    else:
        print("   nuance pass: SKIPPED (--no-nuance).")


# ---------------------------------------------------------------------------
# Final summary.
# ---------------------------------------------------------------------------


def _player_name(db: PlayerDB, player_id: int | None) -> str:
    if player_id is None:
        return "none"
    player = db.get(player_id)
    return player.web_name if player is not None else f"id {player_id}"


def _print_final_summary(result: _PipelineResultLike, diff: SquadDiff, db: PlayerDB) -> None:
    record, squad = result.record, result.squad
    _banner("done")
    print(f"run_id: {record.run_id}")
    print(f"squad cost: £{squad.total_cost_m}m")
    print(f"formation: {record.solver.formation}")
    print(
        f"captain: {_player_name(db, record.solver.captain)}  "
        f"vice-captain: {_player_name(db, record.solver.vice_captain)}"
    )
    if record.solver.relaxed_captaincy:
        print(
            f"captaincy constraint RELAXED to {record.solver.captain_options_required} "
            "required option(s) — see run.json"
        )
    if result.report_path is not None:
        print(f"report: {result.report_path}")

    print()
    if diff.previous_run_id is None:
        print("DIFF vs previous run: no previous run found (this is the first recorded run).")
        return
    in_names = ", ".join(_player_name(db, pid) for pid in diff.players_in) or "none"
    out_names = ", ".join(_player_name(db, pid) for pid in diff.players_out) or "none"
    if diff.captain_changed:
        cap_note = (
            f"captain changed: {_player_name(db, diff.previous_captain)} -> "
            f"{_player_name(db, diff.new_captain)}"
        )
    else:
        cap_note = "captain unchanged"
    print(f"DIFF vs {diff.previous_run_id}: in [{in_names}] / out [{out_names}] / {cap_note}")


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deadline-morning rerun: refresh videos, re-extract picks, force-refresh "
            "FPL availability, and re-solve. Run --dry-run first."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and its API cost, then exit; touches nothing",
    )
    parser.add_argument(
        "--skip-ingest", action="store_true", help="skip stage 1 (reuse existing transcripts)"
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="skip stage 2 (reuse existing extractions — resume after a stage-3 failure "
        "without re-paying for extraction)",
    )
    parser.add_argument(
        "--no-nuance", action="store_true", help="skip the nuance LLM pass in stage 3"
    )
    args = parser.parse_args(argv)

    # Without this the root logger sits at WARNING with no handlers, so every
    # logger.info() in the stages below is DROPPED — including run_extract's
    # per-creator "N picks (M matched)" line and its "no GW1 team video found".
    # The owner would watch several silent minutes of paid extraction with no
    # way to tell eighteen successes from zero.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    nuance = not args.no_nuance

    if args.dry_run:
        _dry_run_report(skip_ingest=args.skip_ingest, skip_extract=args.skip_extract, nuance=nuance)
        return 0

    if args.skip_ingest:
        print("1/3 warm transcript cache: SKIPPED (--skip-ingest)")
    else:
        # Best-effort, never fatal — see the module docstring. run_ingest
        # is the Phase 1 demo runner: it stops at 3 transcripts and exits
        # non-zero when it saves fewer, so its exit code is not a health
        # signal for this rerun. Stage 2 refreshes every creator's video
        # itself, so a failure here costs nothing but a warm cache.
        try:
            rc = _run_stage("1/3 warm transcript cache (ingest)", ingest_main)
            if rc != 0:
                print(
                    f"\nWARNING: ingest exited with code {rc} — continuing anyway. "
                    "Stage 2 refreshes videos for every creator regardless; this stage "
                    "only pre-warms the transcript cache.",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001 - best-effort stage, never fatal
            print(
                f"\nWARNING: warm transcript cache failed ({e}) — continuing anyway. "
                "Stage 2 refreshes videos for every creator regardless.",
                file=sys.stderr,
            )

    if args.skip_extract:
        print("2/3 re-extract picks: SKIPPED (--skip-extract)")
    else:
        try:
            summary = _run_stage("2/3 re-extract picks", run_extraction, force_refresh=True)
            print(
                f"   extracted {summary.succeeded}/{summary.attempted} creators "
                f"({summary.failed} produced nothing) — {summary.report_path}"
            )
            # Per-creator failures are non-fatal inside run_extraction, so a run
            # where EVERY creator failed returns as quietly as a perfect one. Left
            # unchecked, stage 3 would then solve whatever is on disk — yesterday's
            # extractions — and produce a completely normal-looking squad built on
            # stale picks. That silent-wrong-answer is the worst outcome available
            # here, so zero successes is fatal.
            if summary.succeeded == 0:
                raise RuntimeError(
                    f"no creator produced an extraction ({summary.failed} failed). "
                    "Continuing would solve on the PREVIOUS run's extractions and ship "
                    "a stale squad that looks entirely normal. "
                    f"See {summary.report_path} for the per-creator reasons."
                )
            if summary.succeeded < summary.attempted / 2:
                print(
                    f"\nWARNING: only {summary.succeeded} of {summary.attempted} creators "
                    "extracted — the squad below rests on far less evidence than usual. "
                    f"Check {summary.report_path} before shipping it.",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001 - report + resume, never lose earlier work
            # RETRY extraction (--skip-ingest), don't skip it. run_extract is
            # already per-creator non-fatal and flushes its report in a
            # `finally`, so reaching here means something broad went wrong,
            # and the extractions on disk are from the LAST run. Skipping
            # the failed stage would quietly ship a squad built on stale
            # picks — offered below as a deliberate fallback, never as the
            # default resume.
            print(f"\nSTAGE FAILED: re-extract picks — {e}", file=sys.stderr)
            print(f"Resume with: {RESUME_AFTER_INGEST}", file=sys.stderr)
            print(
                "Or, to solve on the EXISTING (stale) extractions without re-extracting: "
                f"{RESUME_AFTER_EXTRACT}",
                file=sys.stderr,
            )
            return 1

    nuance_fn = None
    if nuance:
        from fpl_oracle.report.nuance import generate_nuance

        nuance_fn = generate_nuance

    try:
        result = _run_stage(
            "3/3 re-solve (forced availability refresh)",
            run_pipeline,
            runs_dir=DEFAULT_RUNS_DIR,
            force_refresh=True,
            nuance_fn=nuance_fn,
        )
    except Exception as e:  # noqa: BLE001 - report + resume, never lose earlier work
        resume = RESUME_AFTER_EXTRACT + ("" if nuance else " --no-nuance")
        print(f"\nSTAGE FAILED: re-solve — {e}", file=sys.stderr)
        print(f"Resume with: {resume}", file=sys.stderr)
        return 1

    # Everything of value — run.json, squad.json, latest.json, report.md, and
    # any nuance call already paid for — is on disk by this point. The summary
    # is presentation. If it throws (a player lookup, a formatting slip), the
    # owner must still be told WHERE the squad is, not shown a traceback that
    # reads as "the run failed" while a perfectly good squad sits on disk.
    try:
        # `result.diff` was computed inside `run_pipeline` itself (against
        # the run that `data/runs/latest.json` pointed to before stage 3
        # started) — see the module docstring.
        db = PlayerDB.load()  # reads back the cache stage 3 refreshed; no extra network hit
        _print_final_summary(result, result.diff, db)
    except Exception as e:  # noqa: BLE001 - the run SUCCEEDED; only the summary failed
        print(f"\nRun completed, but printing the summary failed: {e}", file=sys.stderr)
        print(f"The squad and report ARE written — run_id {result.record.run_id}", file=sys.stderr)
        print(f"  report: {result.report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
