"""Single pipeline entry point: stored extractions -> solved squad, plus an
immutable run record.

    uv run python -m fpl_oracle.pipeline

Stages: load the FPL player DB -> load every stored `ResolvedExtraction`
from `data/extractions/` -> build per-creator picks + weights from
`roster/` -> apply the availability facts-veto -> weighted consensus
scoring -> captaincy election -> the PuLP squad solve. Every run is
written to `data/runs/{run_id}/` (never overwritten — see `_reserve_run_dir`)
alongside a `RunRecord` that ties the shipped squad back to the exact
input files (path + sha256) and code version (git SHA/branch/dirty) that
produced it, so a deadline-morning re-run is auditable rather than a
silent overwrite.

NETWORK: the only network access here is the existing cached FPL client
(`fpl.players.PlayerDB.load`, 6h-fresh bootstrap-static cache). This
module deliberately does NOT run YouTube ingestion or LLM extraction —
those cost API money and are run separately via `ingest.run_ingest` /
`extract.run_extract`. This module only consumes what they already wrote
to disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from fpl_oracle.consensus.captaincy import CaptaincyElection, elect, required_options
from fpl_oracle.consensus.scoring import score_picks
from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import PickAction
from fpl_oracle.fpl.availability import Availability, filter_squad_candidates
from fpl_oracle.fpl.players import PlayerDB, Position
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY
from fpl_oracle.roster.weights import weight_for
from fpl_oracle.solver.squad import Squad, build_squad

logger = logging.getLogger(__name__)

DEFAULT_EXTRACTIONS_DIR = Path("data/extractions")
DEFAULT_RUNS_DIR = Path("data/runs")


class GitInfo(BaseModel):
    """Code-version provenance. All fields are `None` together when git
    itself is unavailable (no `.git`, no `git` binary) — never a crash."""

    commit_sha: str | None
    short_sha: str | None
    branch: str | None
    dirty: bool | None = Field(description="uncommitted changes present, or None if unknown")


class ExtractionInput(BaseModel):
    """One consumed extraction file — enough to verify, byte-for-byte,
    that a later re-check is looking at the same input."""

    path: str
    video_id: str
    creator_id: str
    sha256: str


class ExcludedPlayer(BaseModel):
    player_id: int
    reason: str


class RunCounts(BaseModel):
    extractions_loaded: int
    picks_used: int
    picks_skipped_unresolved: int
    players_in_pool: int = Field(description="availability-filtered candidate pool the solver saw")


class SolverOutcome(BaseModel):
    total_cost_m: float
    formation: str = Field(description="starting XI as DEF-MID-FWD, e.g. '3-4-3'")
    captain: int | None
    vice_captain: int | None
    captain_options_required: int
    relaxed_captaincy: bool


class RunRecord(BaseModel):
    """The immutable audit record for one pipeline run."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    git: GitInfo
    inputs: list[ExtractionInput]
    creator_weights: dict[str, float] = Field(description="weights actually used this run")
    excluded_players: list[ExcludedPlayer]
    counts: RunCounts
    solver: SolverOutcome


# ---------------------------------------------------------------------------
# Git metadata (read-only shell-outs; never fatal — see GitInfo docstring).
# ---------------------------------------------------------------------------


def _run_git_command(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_git_info() -> GitInfo:
    """Best-effort git provenance. Never raises: a missing `git` binary or
    a non-repo checkout just yields all-`None` fields."""
    commit_sha = _run_git_command(["rev-parse", "HEAD"])
    short_sha = _run_git_command(["rev-parse", "--short", "HEAD"])
    branch = _run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    status = _run_git_command(["status", "--porcelain"])
    dirty = None if status is None else bool(status)
    return GitInfo(commit_sha=commit_sha, short_sha=short_sha, branch=branch, dirty=dirty)


def _base_run_id(started_at: datetime, git: GitInfo) -> str:
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{git.short_sha or 'nogit'}"


def _reserve_run_dir(runs_dir: Path, base_run_id: str) -> tuple[str, Path]:
    """Claim a fresh, never-overwritten run directory. The timestamp+SHA
    id is unique to the second in the ordinary case; the numeric suffix
    is a safety net for two runs landing in the same second (e.g. two
    reruns fired back-to-back with no code change) so a rerun can NEVER
    silently overwrite a previous run's record."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = base_run_id
    suffix = 0
    while (runs_dir / run_id).exists():
        suffix += 1
        run_id = f"{base_run_id}-{suffix}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    return run_id, run_dir


# ---------------------------------------------------------------------------
# Loading stored extractions.
# ---------------------------------------------------------------------------


def _list_extraction_files(directory: Path) -> list[Path]:
    """Every `*.json` extraction in `directory`, sorted for determinism.
    The `*.json` glob already excludes `match_quality.md`; a missing
    directory is not an error — the pipeline just runs on zero picks
    (e.g. before the first extraction run)."""
    if not directory.exists():
        logger.info(
            "pipeline: extraction directory %s not found — running on zero picks", directory
        )
        return []
    return sorted(directory.glob("*.json"))


def _load_extractions(directory: Path) -> list[tuple[Path, bytes, ResolvedExtraction]]:
    """Load + parse every extraction file. A file that fails to parse is
    skipped with a warning rather than aborting the whole run — consistent
    with `run_extract.py`'s own per-item-non-fatal philosophy."""
    loaded: list[tuple[Path, bytes, ResolvedExtraction]] = []
    for path in _list_extraction_files(directory):
        raw = path.read_bytes()
        try:
            resolved = ResolvedExtraction.model_validate_json(raw)
        except ValidationError as e:
            logger.warning("pipeline: skipping unparseable extraction file %s: %s", path, e)
            continue
        loaded.append((path, raw, resolved))
    return loaded


def _extraction_input(path: Path, raw: bytes, resolved: ResolvedExtraction) -> ExtractionInput:
    return ExtractionInput(
        path=str(path),
        video_id=resolved.extraction.video_id,
        creator_id=resolved.extraction.creator_id,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _build_picks(
    loaded: list[tuple[Path, bytes, ResolvedExtraction]],
) -> tuple[dict[str, list[tuple[int, PickAction, int]]], int, int]:
    """Fan every loaded extraction's picks into `picks_by_creator`, using
    only picks that resolved to a real `player_id` (per-pick MATCHED
    stamp from `extract.run_extract.resolve_extraction`). Returns the
    picks map plus (used, skipped-as-unresolved) counts."""
    picks_by_creator: dict[str, list[tuple[int, PickAction, int]]] = defaultdict(list)
    used = 0
    skipped = 0
    for _path, _raw, resolved in loaded:
        creator_id = resolved.extraction.creator_id
        for pick in resolved.extraction.picks:
            if pick.player_id is None:
                skipped += 1
                continue
            picks_by_creator[creator_id].append((pick.player_id, pick.action, pick.conviction))
            used += 1
    return dict(picks_by_creator), used, skipped


def _creator_weights(creators: list[Creator]) -> dict[str, float]:
    """Weight for every creator in the registry, via the `weight_for()`
    dispatcher over each creator's own stored evidence (not the `.weight`
    field cached on `Creator` at registry-definition time)."""
    return {
        c.creator_id: weight_for(
            c.verification,
            past_finishes=c.past_finishes,
            documented_finishes=c.documented_finishes,
            self_claimed_finishes=c.self_claimed_finishes,
        )
        for c in creators
    }


# ---------------------------------------------------------------------------
# Solver-outcome presentation (captain/vice, formation) — reporting-only
# logic; no change to solver/consensus selection rules.
# ---------------------------------------------------------------------------


def _pick_captain_vice(election: CaptaincyElection, squad: Squad) -> tuple[int | None, int | None]:
    """A single captain + vice for the run record. `Squad` only guarantees
    that >= `captain_options_required` of the election's options made the
    squad (`captain_options_included`) — it does not single out one as
    THE captain. This picks the election's best-first candidate ranking,
    filtered down to players that actually made the squad: the top one is
    captain, the next is vice. Purely a reporting decision over already-
    computed rankings; no solver/consensus logic changes."""
    squad_ids = {p.player_id for p in squad.players}
    ranked_in_squad = [c.player_id for c in election.candidates if c.player_id in squad_ids]
    captain = ranked_in_squad[0] if ranked_in_squad else None
    vice = ranked_in_squad[1] if len(ranked_in_squad) > 1 else None
    return captain, vice


def _formation(squad: Squad) -> str:
    starters = Counter(p.position for p in squad.starting_xi)
    return f"{starters[Position.DEF]}-{starters[Position.MID]}-{starters[Position.FWD]}"


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def run_pipeline(
    extractions_dir: Path = DEFAULT_EXTRACTIONS_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    player_db: PlayerDB | None = None,
    registry: list[Creator] | None = None,
) -> tuple[RunRecord, Squad, Path]:
    """Run the full stored-extractions -> solved-squad pipeline and write
    an immutable run record. `player_db`/`registry` are injectable purely
    for testing (no live network / no need for the real 20-creator
    registry in a unit test); both default to the real, live-backed
    values when omitted."""
    started_at = datetime.now(UTC)
    git = get_git_info()

    db = player_db if player_db is not None else PlayerDB.load()
    creators = registry if registry is not None else REGISTRY

    loaded = _load_extractions(extractions_dir)
    picks_by_creator, picks_used, picks_skipped = _build_picks(loaded)
    creator_weights = _creator_weights(creators)

    availability = filter_squad_candidates(db.all_players(), min_multiplier=0.0)
    pool = availability.eligible
    players_by_id = {p.player_id: p for p in pool}
    excluded_players = [
        ExcludedPlayer(player_id=v.player_id, reason=v.reason)
        for v in availability.verdicts.values()
        if v.availability is Availability.EXCLUDED
    ]
    availability_multipliers = {v.player_id: v.multiplier for v in availability.verdicts.values()}

    scores = score_picks(picks_by_creator, creator_weights, players_by_id)
    election = elect(picks_by_creator, creator_weights, eligible_player_ids=set(players_by_id))
    required = required_options(election)

    squad = build_squad(pool, scores, election.options, required, availability_multipliers)
    captain, vice = _pick_captain_vice(election, squad)

    finished_at = datetime.now(UTC)
    run_id, run_dir = _reserve_run_dir(runs_dir, _base_run_id(started_at, git))

    weights_used = {cid: creator_weights.get(cid, 0.0) for cid in picks_by_creator}
    record = RunRecord(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        git=git,
        inputs=[_extraction_input(path, raw, resolved) for path, raw, resolved in loaded],
        creator_weights=weights_used,
        excluded_players=excluded_players,
        counts=RunCounts(
            extractions_loaded=len(loaded),
            picks_used=picks_used,
            picks_skipped_unresolved=picks_skipped,
            players_in_pool=len(pool),
        ),
        solver=SolverOutcome(
            total_cost_m=squad.total_cost_m,
            formation=_formation(squad),
            captain=captain,
            vice_captain=vice,
            captain_options_required=squad.captain_options_required,
            relaxed_captaincy=squad.relaxed_captaincy,
        ),
    )

    (run_dir / "run.json").write_text(record.model_dump_json(indent=2))
    (run_dir / "squad.json").write_text(squad.model_dump_json(indent=2))
    (runs_dir / "latest.json").write_text(json.dumps({"run_id": run_id}, indent=2))

    return record, squad, run_dir


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Solve a squad from stored extractions")
    parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    record, squad, run_dir = run_pipeline()

    db = PlayerDB.load()

    def _name(player_id: int | None) -> str:
        if player_id is None:
            return "none"
        player = db.get(player_id)
        return player.web_name if player else f"id {player_id}"

    print(f"run_id: {record.run_id}")
    print(f"squad cost: £{squad.total_cost_m}m")
    print(f"formation: {record.solver.formation}")
    print(
        f"captain: {_name(record.solver.captain)}  vice-captain: {_name(record.solver.vice_captain)}"
    )
    if record.solver.relaxed_captaincy:
        print(
            f"captaincy constraint RELAXED to {record.solver.captain_options_required} "
            "required option(s) — see run.json"
        )
    print(
        f"extractions: {record.counts.extractions_loaded} loaded, "
        f"{record.counts.picks_used} picks used, "
        f"{record.counts.picks_skipped_unresolved} skipped unresolved, "
        f"{record.counts.players_in_pool} players in pool"
    )
    print(f"run record written to {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
