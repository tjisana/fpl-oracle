"""Single pipeline entry point: stored extractions -> solved squad, plus an
immutable run record.

    uv run python -m fpl_oracle.pipeline
    uv run python -m fpl_oracle.pipeline --nuance --force-refresh

Stages: load the FPL player DB -> load every stored `ResolvedExtraction`
from `data/extractions/` -> build per-creator picks + weights from
`roster/` -> apply the availability facts-veto -> weighted consensus
scoring -> captaincy election -> the PuLP squad solve -> (optional) LLM
nuance pass -> gameweek report. Every run is written to
`data/runs/{run_id}/` (never overwritten — see `_reserve_run_dir`)
alongside a `RunRecord` that ties the shipped squad back to the exact
input files (path + sha256) and code version (git SHA/branch/dirty) that
produced it, so a deadline-morning re-run is auditable rather than a
silent overwrite.

NETWORK: the only network access here is the existing cached FPL client
(`fpl.players.PlayerDB.load`, 6h-fresh bootstrap-static cache by default;
pass `force_refresh=True` — or `--force-refresh` on the CLI — to bypass
that cache and refetch live, which the deadline-morning rerun needs so
injury/availability flags from the last few hours aren't missed). This
module deliberately does NOT run YouTube ingestion or LLM extraction —
those cost API money and are run separately via `ingest.run_ingest` /
`extract.run_extract` (see `fpl_oracle.deadline`, which chains all three).
The nuance pass (`--nuance` / `nuance_fn`) is the one thing in THIS module
that costs money: an LLM call over the solved squad's picks. It is never
fired unless explicitly opted into.

REPORT FRESHNESS/DIFF: this module also owns the two mechanical checks a
report needs to be trusted at a glance — neither is a football opinion, so
neither belongs in `report.gameweek` (which stays a pure renderer). It
stamps `RunRecord.bootstrap_fetched_at` (the FPL cache file's mtime, so the
report can show how old the underlying player data is) and computes the
`SquadDiff` against whatever `data/runs/latest.json` pointed to before this
run started (`load_previous_run` / `diff_vs_previous`), so every run — not
just a `deadline.py`-chained one — gets a diff section in its report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from fpl_oracle.consensus.captaincy import CaptaincyElection, elect, required_options
from fpl_oracle.consensus.scoring import PlayerScore, score_picks
from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import PickAction
from fpl_oracle.fpl import client as fpl_client
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict, filter_squad_candidates
from fpl_oracle.fpl.client import CACHE_DIR, LeagueStanding
from fpl_oracle.fpl.entry import (
    EntryIdNotConfiguredError,
    OwnedPlayer,
    fetch_my_squad,
    get_entry_id,
)
from fpl_oracle.fpl.players import Player, PlayerDB, Position
from fpl_oracle.fpl.selling import derive_squad_pricing
from fpl_oracle.fpl.snapshot import derive_purchase_prices
from fpl_oracle.logging_setup import configure_logging
from fpl_oracle.report.models import NuanceRecord, SquadNuance
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY
from fpl_oracle.roster.weights import weight_for
from fpl_oracle.solver.squad import Squad, build_squad
from fpl_oracle.solver.transfers import TransferRecommendation, recommend_transfer
from fpl_oracle.solver.wildcard import WildcardGap, wildcard_gap

logger = logging.getLogger(__name__)

# Seam signatures for the two Phase-4 modules this pipeline wires in
# (`report/nuance.py`, `report/gameweek.py`) — injectable so tests never
# need the real LLM call, and lazily imported so neither is a module-scope
# dependency; see `run_pipeline`'s `nuance_fn`/`report_fn` params.
#
# The trailing `creator_names` is the creator_id -> display-name map. The
# nuance model must EMIT ids (they are checked mechanically against who
# actually voted), but it reads better evidence when the ids it is reading
# carry a human name alongside them, so the map is threaded through rather
# than left to default empty.
NuanceFn = Callable[
    [
        str,
        Squad,
        dict[int, PlayerScore],
        CaptaincyElection,
        list[ResolvedExtraction],
        PlayerDB,
        dict[str, str],
    ],
    NuanceRecord,
]
# report_fn is called with keyword arguments only (record, squad, scores,
# election, db, extractions, creators, availability, nuance, diff) — see
# `_render_report`.
ReportFn = Callable[..., str]

# Seam signature for the my-squad fetch (Job 2, ROADMAP.md "My-squad
# awareness"): a network-touching call, so — same spirit as
# `report_fn`/`nuance_fn` above — it's injectable so tests never need it
# to actually reach fantasy.premierleague.com. Unlike `nuance_fn` (opt-in,
# `None` means "skip"), the REAL fetch (`fetch_my_squad_summary`) is meant
# to run on every real pipeline invocation — see `main()`, which is the
# one place that wires the real function in by default. A bare
# `run_pipeline()` call (every existing test, and any other direct
# caller) leaves `my_squad_fn=None` and gets no my-squad section, exactly
# like an injected `player_db`/`registry` bypasses the real network loads
# those parameters would otherwise trigger.
MySquadFn = Callable[[PlayerDB], "tuple[MySquadSummary | None, str]"]

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
    published_at: datetime | None = Field(
        default=None,
        description=(
            "the source video's publish time, so the report can show evidence age. "
            "Defaults to None so run.json files written before this field existed still parse "
            "— see RunRecord.bootstrap_fetched_at for the same pattern."
        ),
    )


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


class MySquadSummary(BaseModel):
    """What the owner actually owns, as of the current completed
    gameweek, plus how they're doing — attached to `RunRecord` for the
    transfer recommender (a separate, not-yet-built task) to consume.

    OBSERVATIONAL ONLY. Nothing here feeds the solver, consensus scoring,
    or captaincy election above — those already ran by the time this is
    fetched, and this pipeline's job is still "pick 15 from the
    consensus", unchanged. See `run_pipeline`'s `my_squad_fn` seam.
    """

    entry_id: int
    team_name: str
    gameweek: int = Field(description="the current completed gameweek this squad was fetched for")
    players: list[OwnedPlayer] = Field(
        description="all 15 owned players, with position/bench flags"
    )
    bank: int = Field(description="tenths of a million, matching now_cost's convention")
    team_value: int = Field(description="total squad value, tenths of a million")
    free_transfers: int | None = Field(
        description="derived free transfers available for gameweek + 1; None if undeterminable"
    )
    free_transfers_note: str = Field(description="how free_transfers was derived, or why it's None")
    overall_rank: int | None
    overall_points: int | None
    private_leagues: list[LeagueStanding] = Field(
        description="leagues.classic[]/leagues.h2h[] entries with league_type == 'x' only"
    )


class RunRecord(BaseModel):
    """The immutable audit record for one pipeline run."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    bootstrap_fetched_at: datetime | None = Field(
        default=None,
        description=(
            "mtime of data/cache/fpl/bootstrap_static.json at the point this run read it — "
            "the closest thing to 'when was the underlying FPL data fetched' without "
            "fpl.client exposing a timestamp of its own. None when `player_db` was injected "
            "(tests) rather than loaded from the real cache, or when the cache file "
            "couldn't be stat'd. Defaults to None so run.json files written before this "
            "field existed still parse."
        ),
    )
    git: GitInfo
    inputs: list[ExtractionInput]
    gameweek: int | None = Field(
        default=None,
        description=(
            "the gameweek this run solved for. Extractions about a DIFFERENT gameweek were "
            "excluded from the vote (see `excluded_wrong_gameweek`); None means no filter "
            "was applied and every stored extraction was loaded."
        ),
    )
    excluded_wrong_gameweek: list[str] = Field(
        default_factory=list,
        description=(
            "extraction files excluded because they are about another gameweek. Recorded "
            "because a stale creator vote is invisible otherwise: such a creator was never "
            "selected for extraction, so they appear in no match-quality report either."
        ),
    )
    superseded_inputs: list[ExtractionInput] = Field(
        default_factory=list,
        description=(
            "extraction files on disk that were NOT used: an older video from a creator who "
            "has since posted a newer one (see `_latest_per_creator`). Recorded, not deleted."
        ),
    )
    creator_weights: dict[str, float] = Field(description="weights actually used this run")
    excluded_players: list[ExcludedPlayer]
    counts: RunCounts
    solver: SolverOutcome
    my_squad: MySquadSummary | None = Field(
        default=None,
        description=(
            "the owner's actual squad for the current completed gameweek, plus overall "
            "rank/points and private-league standings (Job 2, ROADMAP.md 'My-squad "
            "awareness') — OBSERVATIONAL ONLY, never fed into the solver/consensus above. "
            "None when FPL_ENTRY_ID isn't configured, no gameweek has completed yet, the "
            "picks aren't public yet, or the fetch failed — see my_squad_note for which. "
            "Defaults to None so run.json files written before this field existed still parse."
        ),
    )
    transfer_recommendation: TransferRecommendation | None = Field(
        default=None,
        description=(
            "the in-season answer: roll, use the free transfer, or fix a forced hole. "
            "None when no owned squad was available to reason about."
        ),
    )
    transfer_note: str = Field(
        default="",
        description="the recommendation's reasoning, or why there isn't one",
    )
    my_squad_note: str = Field(
        default="",
        description="why my_squad is None, or a short confirmation of what was fetched",
    )
    wildcard_gap: WildcardGap | None = Field(
        default=None,
        description=(
            "the owner's squad vs the solver's £100m squad on the transfer section's "
            "yardstick (starters 1.0, bench BENCH_WEIGHT). Observational. Defaults to "
            "None so older run.json files parse."
        ),
    )
    wildcard_note: str = Field(
        default="", description="why wildcard_gap is None, or a one-line summary"
    )


# ---------------------------------------------------------------------------
# Squad diff vs. the previous run — pure arithmetic over two on-disk
# squad.json / run.json files, no LLM, no network. Lives here (not in
# deadline.py, which historically owned it) so BOTH `deadline.py` and
# `report.gameweek` can read it without one importing the other: pipeline.py
# is already a dependency of both, and neither of them is a dependency of
# it, so this is the one shared home that adds no import edge.
# ---------------------------------------------------------------------------


class SquadDiff(BaseModel):
    """What changed vs. the run that `data/runs/latest.json` pointed to
    BEFORE this rerun started. `previous_run_id=None` means there was no
    previous run to diff against (e.g. the very first run ever) — that is
    not an error, just nothing to report."""

    previous_run_id: str | None
    players_in: list[int] = Field(description="player_ids in the new squad, not the previous one")
    players_out: list[int] = Field(description="player_ids in the previous squad, not the new one")
    previous_captain: int | None
    new_captain: int | None
    captain_changed: bool = Field(
        description="False whenever there is no previous run to compare against"
    )


def load_previous_run(runs_dir: Path) -> tuple[str, set[int], int | None] | None:
    """Read `runs_dir/latest.json` and the run it points to, BEFORE this
    rerun writes anything — `run_pipeline` calls this before it touches
    `latest.json` itself. Returns `(run_id, player_ids, captain)`, or
    `None` if there is no previous run (missing pointer, or a pointer to a
    run directory that's since gone missing its own files — either way,
    "no previous run to diff against", not an error)."""
    latest_path = runs_dir / "latest.json"
    if not latest_path.exists():
        return None
    try:
        latest = json.loads(latest_path.read_text())
    except json.JSONDecodeError:
        return None
    run_id = latest.get("run_id")
    if not run_id:
        return None
    run_dir = runs_dir / run_id
    squad_path = run_dir / "squad.json"
    run_path = run_dir / "run.json"
    if not squad_path.exists() or not run_path.exists():
        return None
    # Nothing here is worth failing the run over: this feeds a COSMETIC
    # diff. A previous run's squad.json truncated by a Ctrl-C, or written
    # by an older schema, would otherwise raise out of run_pipeline before
    # anything else even started — killing a run over a nicety. The
    # docstring's promise ("not an error") has to hold for the file
    # contents too, not just the pointer.
    try:
        squad_data = json.loads(squad_path.read_text())
        run_data = json.loads(run_path.read_text())
        player_ids = {p["player_id"] for p in squad_data["players"]}
        captain = run_data.get("solver", {}).get("captain")
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
        logger.warning("pipeline: unreadable previous run %s (%s) — skipping the diff", run_id, e)
        return None
    return run_id, player_ids, captain


def diff_vs_previous(
    previous: tuple[str, set[int], int | None] | None,
    new_player_ids: set[int],
    new_captain: int | None,
) -> SquadDiff:
    """Diff the just-solved squad/captain against `previous` (from
    `load_previous_run`, captured BEFORE this run). Pure — no I/O."""
    if previous is None:
        return SquadDiff(
            previous_run_id=None,
            players_in=sorted(new_player_ids),
            players_out=[],
            previous_captain=None,
            new_captain=new_captain,
            captain_changed=False,
        )

    prev_run_id, prev_ids, prev_captain = previous
    return SquadDiff(
        previous_run_id=prev_run_id,
        players_in=sorted(new_player_ids - prev_ids),
        players_out=sorted(prev_ids - new_player_ids),
        previous_captain=prev_captain,
        new_captain=new_captain,
        captain_changed=prev_captain != new_captain,
    )


class PipelineResult(BaseModel):
    """Everything one `run_pipeline` call produced — the audit record and
    solved squad, plus the consensus artefacts that produced them
    (`scores`, `election`) and the raw inputs (`extractions`), so a
    caller (the gameweek report, the deadline-morning summary, a human at
    a REPL) never has to re-derive or re-load what the pipeline already
    computed. `report_path` is `None` only if report rendering was never
    reached (it currently always is; the field stays optional in case a
    future caller wants a report-less run).

    `diff` and `availability` are the same values threaded into the
    report — `diff` is ALWAYS present (never `None`), even on the very
    first run ever (`diff.previous_run_id` is `None` in that case, not
    the whole field) — so any caller (e.g. `deadline.py`'s own terminal
    summary) can read it here instead of re-deriving it."""

    model_config = {"arbitrary_types_allowed": True}

    record: RunRecord
    squad: Squad
    run_dir: Path
    scores: dict[int, PlayerScore]
    election: CaptaincyElection
    extractions: list[ResolvedExtraction]
    availability: dict[int, AvailabilityVerdict]
    diff: SquadDiff
    report_path: Path | None = None


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


_BOOTSTRAP_CACHE_FILENAME = "bootstrap_static.json"


def _bootstrap_fetched_at() -> datetime | None:
    """Best-effort mtime of `fpl.client`'s bootstrap-static cache file —
    the closest thing to "when was the underlying FPL data fetched"
    without `fpl.client` exposing a timestamp of its own (it writes the
    payload straight to disk with no fetched-at field inside it). Read-
    only, never fatal: a missing/unreadable cache file just yields `None`,
    same spirit as `get_git_info` above. Only meaningful right after
    `PlayerDB.load()` has run — call this before anything else touches
    that file."""
    path = CACHE_DIR / _BOOTSTRAP_CACHE_FILENAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=UTC)


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


class NoConsensusEvidenceError(RuntimeError):
    """Raised when a run would solve on zero creator picks.

    The solver's objective is `price + band_score`. With no picks every
    band_score is zero, so it degenerates to "spend £100m" and still
    returns a perfectly legal, confident-looking 15 — with no captain,
    because nobody voted for one. That output is indistinguishable from a
    real recommendation to anyone reading squad.json, and it is pure
    noise. Refusing is the only safe behaviour: an empty answer the user
    can see is worth far more than a fabricated one they can't detect.

    Reached when extraction produced nothing, when every stored extraction
    belongs to another gameweek, or when name resolution dropped every
    pick."""


def _load_extractions(
    directory: Path, gameweek: int | None = None
) -> tuple[list[tuple[Path, bytes, ResolvedExtraction]], list[tuple[Path, int | None]]]:
    """Load + parse every extraction file, keeping only those that are
    about `gameweek`. Returns `(loaded, wrong_gameweek)`.

    THE GAMEWEEK FILTER IS NOT OPTIONAL HYGIENE. `data/extractions/` is
    append-only and keyed by video_id, and `_latest_per_creator` dedupes
    only by publish date — it has no notion of which gameweek a pick was
    about. So a creator who posted a GW1 team but no GW2 video keeps
    voting, with last week's squad, forever. On the GW2 run that was two
    real creators (fpl-family, fpl-hints) casting stale GW1 votes into the
    GW2 consensus, silently: they were never *selected* for extraction, so
    they never appeared in the match-quality report either.

    `gameweek=None` disables the filter (every stored extraction loads),
    which is the old behaviour and is what tests and ad-hoc re-solves of a
    historical run want.

    An extraction whose own `gameweek` is None — the model could not tell —
    is KEPT rather than dropped: absence of evidence isn't evidence of the
    wrong gameweek, and `render_match_quality` already flags those for a
    human. Only an explicit mismatch is excluded.

    A file that fails to parse is skipped with a warning rather than
    aborting the whole run — consistent with `run_extract.py`'s own
    per-item-non-fatal philosophy."""
    loaded: list[tuple[Path, bytes, ResolvedExtraction]] = []
    wrong_gameweek: list[tuple[Path, int | None]] = []
    for path in _list_extraction_files(directory):
        raw = path.read_bytes()
        try:
            resolved = ResolvedExtraction.model_validate_json(raw)
        except ValidationError as e:
            logger.warning("pipeline: skipping unparseable extraction file %s: %s", path, e)
            continue
        found = resolved.extraction.gameweek
        if gameweek is not None and found is not None and found != gameweek:
            logger.warning(
                "pipeline: EXCLUDING %s (%s) — it is a GW%s extraction, this run is GW%s",
                path.name,
                resolved.extraction.creator_id,
                found,
                gameweek,
            )
            wrong_gameweek.append((path, found))
            continue
        loaded.append((path, raw, resolved))
    return loaded, wrong_gameweek


def _latest_per_creator(
    loaded: list[tuple[Path, bytes, ResolvedExtraction]],
) -> tuple[
    list[tuple[Path, bytes, ResolvedExtraction]], list[tuple[Path, bytes, ResolvedExtraction]]
]:
    """Keep ONE extraction per creator — the most recently published — and
    return `(kept, superseded)`.

    `data/extractions/` is keyed by video_id and is append-only: a
    re-extraction writes a NEW file and never removes the old one. Without
    this, a creator who posts "MY FINAL GW1 TEAM" on deadline morning is
    loaded twice, and because `_build_picks` fans picks in by CREATOR, the
    two videos merge into one ballot. The old opinions do not lose to the
    new ones — `score_picks` keeps the strongest vote per (creator,
    player), so a player he has just DROPPED keeps his full-strength
    earlier vote, and `elect` would count him as a voter for his old
    captain and his new one at once. The creator's latest video is his
    final word, which is exactly the rule extraction already applies
    WITHIN a video ("the creator's FINAL stated position only").

    Superseded files are returned, not deleted — they stay on disk and are
    recorded in the `RunRecord` so the audit trail shows what was set
    aside and why. Ties on `published_at` (same-day reposts with identical
    timestamps) break on video_id, purely for determinism.
    """
    best: dict[str, tuple[Path, bytes, ResolvedExtraction]] = {}
    superseded: list[tuple[Path, bytes, ResolvedExtraction]] = []

    def _rank(item: tuple[Path, bytes, ResolvedExtraction]) -> tuple[datetime, str]:
        return (item[2].extraction.published_at, item[2].extraction.video_id)

    for item in loaded:
        creator_id = item[2].extraction.creator_id
        incumbent = best.get(creator_id)
        if incumbent is None:
            best[creator_id] = item
            continue
        if _rank(item) > _rank(incumbent):
            best[creator_id] = item
            superseded.append(incumbent)
        else:
            superseded.append(item)

    for _path, _raw, resolved in superseded:
        logger.info(
            "pipeline: superseded extraction for %s — video %s (published %s) is not the "
            "creator's latest",
            resolved.extraction.creator_id,
            resolved.extraction.video_id,
            resolved.extraction.published_at.date(),
        )

    # Identity, not equality: two files could carry byte-identical content,
    # and `in` over Pydantic models would then keep the wrong one. Order
    # follows `loaded` (sorted by path) so the run record stays stable.
    kept_ids = {id(item) for item in best.values()}
    kept = [item for item in loaded if id(item) in kept_ids]
    return kept, superseded


def _extraction_input(path: Path, raw: bytes, resolved: ResolvedExtraction) -> ExtractionInput:
    return ExtractionInput(
        path=str(path),
        video_id=resolved.extraction.video_id,
        creator_id=resolved.extraction.creator_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        published_at=resolved.extraction.published_at,
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
    computed rankings; no solver/consensus logic changes.

    STARTERS FIRST. The solver's captaincy constraint puts election options
    into the 15, not the XI (`solver/squad.py`), so a candidate the
    price-anchored objective decided to bench can still top this ranking —
    and an armband on a benched player is simply thrown away. Candidates
    are therefore ranked within the starting XI first, and only fall back
    to the wider squad if the election put nobody in the XI at all (in
    which case a benched captain, flagged in the report, still beats no
    captain)."""
    starter_ids = {p.player_id for p in squad.starting_xi}
    squad_ids = {p.player_id for p in squad.players}
    ranked = [c.player_id for c in election.candidates if c.player_id in starter_ids]
    if not ranked:
        ranked = [c.player_id for c in election.candidates if c.player_id in squad_ids]
    captain = ranked[0] if ranked else None
    vice = ranked[1] if len(ranked) > 1 else None
    return captain, vice


def _formation(squad: Squad) -> str:
    starters = Counter(p.position for p in squad.starting_xi)
    return f"{starters[Position.DEF]}-{starters[Position.MID]}-{starters[Position.FWD]}"


# ---------------------------------------------------------------------------
# My-squad fetch (Job 2, ROADMAP.md "My-squad awareness") — observational
# only; see MySquadSummary's docstring. This is the one place in this
# module that reaches `fpl.entry`/`fpl.client`'s entry-endpoint fetches;
# everything above it (extractions, consensus, solver) is untouched by it.
# ---------------------------------------------------------------------------


def fetch_my_squad_summary(player_db: PlayerDB) -> tuple[MySquadSummary | None, str]:
    """Best-effort, network-touching fetch of the owner's my-squad
    summary for `run_pipeline`'s `my_squad_fn` seam. NEVER raises — Job
    2's degrade-gracefully requirement: a missing `FPL_ENTRY_ID`, an
    entry with no completed gameweek yet, or any fetch failure all return
    `(None, <reason>)` rather than costing the run its squad/report.
    """
    try:
        entry_id = get_entry_id()
    except EntryIdNotConfiguredError:
        return None, "FPL_ENTRY_ID not configured — my-squad section skipped"
    except ValueError as e:
        # get_entry_id also raises a bare ValueError for a non-integer id.
        # This block sits outside the broad guard below, so without this
        # clause a typo'd FPL_ENTRY_ID escapes a function whose docstring
        # promises it never raises.
        return None, f"FPL_ENTRY_ID is malformed ({e}) — my-squad section skipped"

    try:
        gameweek = fpl_client.get_current_completed_gameweek()
        if gameweek is None:
            return None, "no gameweek has completed yet — my-squad section skipped"

        # Distinguish "wrong id" from "right id, picks not public yet".
        # fetch_my_squad tries picks first and falls back to a placeholder
        # team name, so a typo'd id produced "picks aren't public yet" —
        # sending the owner off to wait for a deadline instead of fixing
        # their .env.
        if fpl_client.get_entry(entry_id) is None:
            return (
                None,
                f"FPL_ENTRY_ID={entry_id} does not match any FPL manager — check .env",
            )

        squad = fetch_my_squad(entry_id, gameweek, player_db)
        if squad is None:
            return (
                None,
                f"picks for GW{gameweek} aren't public yet — my-squad section skipped",
            )

        entry_summary = fpl_client.get_entry(entry_id)
        overall_rank = entry_summary.summary_overall_rank if entry_summary else None
        overall_points = entry_summary.summary_overall_points if entry_summary else None
        private_leagues = entry_summary.private_leagues if entry_summary else []

        summary = MySquadSummary(
            entry_id=entry_id,
            team_name=squad.team_name,
            gameweek=gameweek,
            players=squad.players,
            bank=squad.bank,
            team_value=squad.team_value,
            free_transfers=squad.free_transfers,
            free_transfers_note=squad.free_transfers_note,
            overall_rank=overall_rank,
            overall_points=overall_points,
            private_leagues=private_leagues,
        )
        return summary, f"fetched squad for GW{gameweek}"
    except Exception as e:  # noqa: BLE001 - my-squad must never cost the run its squad
        logger.warning("pipeline: my-squad fetch failed: %s", e)
        return None, f"my-squad fetch failed: {e}"


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def _run_nuance(
    nuance_fn: NuanceFn,
    run_id: str,
    squad: Squad,
    scores: dict[int, PlayerScore],
    election: CaptaincyElection,
    extractions: list[ResolvedExtraction],
    db: PlayerDB,
    creator_names: dict[str, str],
    run_dir: Path,
) -> NuanceRecord:
    """Call `nuance_fn` and always return a `NuanceRecord` — a failure
    (LLM error, bad response, anything) is captured as `failed=True`
    rather than propagated, per the same per-item-non-fatal philosophy
    `extract.run_extract` uses: a nuance-pass failure must never cost the
    deadline-morning rerun its squad or report. Caller writes the result
    to `run_dir/nuance.json` either way."""
    try:
        return nuance_fn(run_id, squad, scores, election, extractions, db, creator_names)
    except Exception as e:  # noqa: BLE001 - deliberately broad; see docstring
        logger.warning("pipeline: nuance pass failed: %s", e)
        return NuanceRecord(
            run_id=run_id,
            generated_at=datetime.now(UTC),
            model="unavailable",
            nuance=SquadNuance(),
            failed=True,
            failure_reason=str(e),
        )


def _render_report(
    report_fn: ReportFn | None,
    *,
    record: RunRecord,
    squad: Squad,
    scores: dict[int, PlayerScore],
    election: CaptaincyElection,
    db: PlayerDB,
    extractions: list[ResolvedExtraction],
    creators: list[Creator],
    availability: dict[int, AvailabilityVerdict],
    nuance: SquadNuance | None,
    diff: SquadDiff,
    run_dir: Path,
) -> Path:
    """Render + write the gameweek report. `nuance` is the bare
    `SquadNuance` (not the `NuanceRecord` audit wrapper) — that's the type
    `report.gameweek.render_report` actually takes; the caller
    (`run_pipeline`) is the one that unwraps `NuanceRecord.nuance`, and
    passes `None` outright on a failed/skipped nuance pass. `report_fn=None`
    means "use the
    real renderer" (`report.gameweek.render_report`), lazily imported so
    this module never depends on that concurrently-developed file at
    import time — only when a real (non-test) run actually reaches this
    point without an injected fake. Either way, this function itself owns
    writing `run_dir/report.md`, so a test-injected `report_fn` never
    needs a matching fake `write_report` too."""
    if report_fn is None:
        from fpl_oracle.report.gameweek import render_report

        report_fn = render_report
    markdown = report_fn(
        record=record,
        squad=squad,
        scores=scores,
        election=election,
        db=db,
        extractions=extractions,
        creators=creators,
        availability=availability,
        nuance=nuance,
        diff=diff,
    )
    report_path = run_dir / "report.md"
    report_path.write_text(markdown)
    return report_path


def _recommend_transfer_for(
    *,
    my_squad: MySquadSummary,
    db: PlayerDB,
    pool: list[Player],
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
    gameweek: int | None,
) -> TransferRecommendation:
    """Adapt pipeline state into `solver.transfers.recommend_transfer`.

    Selling prices are DERIVED here (see `fpl.selling`) rather than read:
    the public picks endpoint doesn't carry them, but purchase price is
    recoverable from the transfer log and season-start prices, and the
    selling rule is deterministic from there. `derive_squad_pricing`
    reconciles its own total against FPL's reported squad value, so a
    derivation that doesn't add up degrades to UNVERIFIED affordability
    instead of a confident wrong number.
    """
    owned_ids = [p.player_id for p in my_squad.players]
    owned = [player for player in (db.get(pid) for pid in owned_ids) if player is not None]
    elements_by_id = {e["id"]: e for e in fpl_client.get_bootstrap_static().get("elements", [])}
    pricing = derive_squad_pricing(
        owned_player_ids=owned_ids,
        elements_by_id=elements_by_id,
        purchase_from_transfers=derive_purchase_prices(my_squad.entry_id).known,
        bank=my_squad.bank,
        reported_squad_value=my_squad.team_value,
    )
    return recommend_transfer(
        starting_player_ids={p.player_id for p in my_squad.players if p.is_starting},
        owned=owned,
        pool=pool,
        scores=scores,
        verdicts=verdicts,
        pricing=pricing,
        free_transfers=my_squad.free_transfers,
        gameweek=gameweek,
    )


def run_pipeline(
    extractions_dir: Path = DEFAULT_EXTRACTIONS_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    player_db: PlayerDB | None = None,
    registry: list[Creator] | None = None,
    *,
    force_refresh: bool = False,
    nuance_fn: NuanceFn | None = None,
    report_fn: ReportFn | None = None,
    my_squad_fn: MySquadFn | None = fetch_my_squad_summary,
    gameweek: int | None = None,
    allow_no_evidence: bool = False,
) -> PipelineResult:
    """Run the full stored-extractions -> solved-squad -> report pipeline
    and write an immutable run record. `player_db`/`registry` are
    injectable purely for testing (no live network / no need for the real
    20-creator registry in a unit test); both default to the real,
    live-backed values when omitted.

    `force_refresh` bypasses the FPL bootstrap-static 6h cache (only takes
    effect when `player_db` is omitted — an injected `player_db` is
    already "loaded" and this has nothing left to bypass). See
    `fpl.client.get_bootstrap_static` for why this exists.

    `nuance_fn` is OPT-IN: `None` (the default) means the nuance LLM pass
    never runs — no call, no cost, no `nuance.json` written. Pass a
    callable matching `NuanceFn` (see `report.nuance.generate_nuance`) to
    enable it; a failure there is caught and recorded, never fatal (see
    `_run_nuance`).

    `report_fn` controls the gameweek report renderer; `None` uses the
    real one (`report.gameweek.render_report`), lazily imported. A test
    fake only needs to match `ReportFn`'s signature — the write-to-disk
    step lives in this module, not in the fake.

    `my_squad_fn` DEFAULTS ON, unlike `nuance_fn`. The two are not
    analogous: nuance is genuinely optional (it costs an LLM call and has
    a --no-nuance flag), whereas my-squad is free and is meant to run on
    every real invocation. Defaulting it off made the library's default
    behaviour the wrong behaviour, and correctness then depended on every
    caller remembering the parameter — a caller that forgot got
    `my_squad=None` with the note "not requested for this run", which
    reads as a deliberate choice rather than an omission. Tests and
    offline callers pass `my_squad_fn=None` explicitly to stay
    network-free. Historic note, since this docstring used to say the
    opposite:
    `None` (the default) means no my-squad fetch happens and
    `RunRecord.my_squad` stays `None` — this is what every existing
    caller (this module's own test suite, and any other direct
    `run_pipeline()` call) gets without changes. `main()` is the one
    place that wires in the real fetch (`fetch_my_squad_summary`) by
    default, so a real CLI run always attempts it without needing a
    flag — see Job 2 in ROADMAP.md ("My-squad awareness"). Pass a
    callable matching `MySquadFn` to enable it elsewhere (e.g.
    `deadline.py`'s stage 3). The real fetch never raises on its own
    (see `fetch_my_squad_summary`), but this function is still per-item
    non-fatal: a `my_squad_fn` that raises degrades to `(None, note)`
    rather than losing the solved squad.
    """
    started_at = datetime.now(UTC)
    git = get_git_info()

    # Captured BEFORE anything below touches `runs_dir/latest.json` (that
    # write happens near the end of this function) — this is "the run
    # before this one", the baseline the squad-diff section is measured
    # against. Doing this here, inside run_pipeline itself, rather than
    # leaving it to a caller (as `deadline.py` used to), is what makes a
    # bare `pipeline.py` run also get a diff in its report — previously
    # only `deadline.py` ever computed one, so a `pipeline --force-refresh`
    # run had no diff at all.
    previous_run = load_previous_run(runs_dir)

    db = player_db if player_db is not None else PlayerDB.load(force_refresh=force_refresh)
    # Only meaningful when this run actually loaded the real cache file
    # (not an injected `player_db`, e.g. in a test) — see `_bootstrap_
    # fetched_at`'s docstring.
    bootstrap_fetched_at = None if player_db is not None else _bootstrap_fetched_at()
    creators = registry if registry is not None else REGISTRY

    all_loaded, wrong_gameweek = _load_extractions(extractions_dir, gameweek)
    loaded, superseded = _latest_per_creator(all_loaded)
    picks_by_creator, picks_used, picks_skipped = _build_picks(loaded)
    if picks_used == 0 and not allow_no_evidence:
        raise NoConsensusEvidenceError(
            f"no usable creator picks for GW{gameweek}: {len(all_loaded)} extraction "
            f"file(s) parsed, {len(wrong_gameweek)} excluded as another gameweek's, "
            f"{picks_skipped} pick(s) dropped in name resolution. Refusing to solve — "
            f"the result would be a price-anchored squad with no creator support behind it."
        )
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
    diff = diff_vs_previous(previous_run, {p.player_id for p in squad.players}, captain)

    # Job 2, ROADMAP.md "My-squad awareness" — purely observational, and
    # computed independently of everything above: it can never change the
    # squad/captain/diff already decided. `my_squad_fn=None` (every
    # existing caller) skips it entirely; see run_pipeline's docstring.
    # Belt-and-braces non-fatal even though `fetch_my_squad_summary`
    # itself never raises — a test-injected `my_squad_fn` might.
    my_squad_summary: MySquadSummary | None = None
    my_squad_note = "my-squad fetch not requested for this run"
    if my_squad_fn is not None:
        try:
            my_squad_summary, my_squad_note = my_squad_fn(db)
        except Exception as e:  # noqa: BLE001 - my-squad must never cost the run its squad
            logger.warning("pipeline: my_squad_fn raised: %s", e)
            my_squad_summary, my_squad_note = None, f"my-squad fetch failed: {e}"

    # The in-season answer. Depends on my-squad, so it can only run when
    # that succeeded; like my-squad itself it is computed AFTER the solve
    # and cannot influence it. `build_squad`'s output stays the
    # wildcard/free-hit answer; this is the "given what you own" one.
    transfer_rec: TransferRecommendation | None = None
    transfer_note = "no owned squad available — transfer recommendation skipped"
    if my_squad_summary is not None:
        try:
            transfer_rec = _recommend_transfer_for(
                my_squad=my_squad_summary,
                db=db,
                pool=pool,
                scores=scores,
                verdicts=availability.verdicts,
                gameweek=gameweek,
            )
            transfer_note = transfer_rec.reasoning
        except Exception as e:  # noqa: BLE001 - never cost the run its squad
            logger.warning("pipeline: transfer recommendation failed: %s", e)
            transfer_note = f"transfer recommendation failed: {e}"

    # How far the owner's actual squad sits from the solver's £100m
    # answer, on the transfer section's yardstick. Depends on
    # `transfer_rec` for the transfers-equivalent conversion, so it runs
    # after that block.
    wildcard: WildcardGap | None = None
    wildcard_note = "no owned squad available — wildcard gap skipped"
    if my_squad_summary is not None:
        try:
            wildcard = wildcard_gap(
                owned=my_squad_summary.players,
                solver_squad=squad,
                db=db,
                scores=scores,
                verdicts=availability.verdicts,
                transfer_rec=transfer_rec,
                my_squad_value_m=my_squad_summary.team_value / 10,
            )
            wildcard_note = f"gap {wildcard.gain:+.2f}; solver keeps {wildcard.kept_count}/15"
        except Exception as e:  # noqa: BLE001 - never cost the run its squad
            logger.warning("pipeline: wildcard gap failed: %s", e)
            wildcard_note = f"wildcard gap failed: {e}"

    finished_at = datetime.now(UTC)
    run_id, run_dir = _reserve_run_dir(runs_dir, _base_run_id(started_at, git))

    weights_used = {cid: creator_weights.get(cid, 0.0) for cid in picks_by_creator}
    record = RunRecord(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        bootstrap_fetched_at=bootstrap_fetched_at,
        git=git,
        inputs=[_extraction_input(path, raw, resolved) for path, raw, resolved in loaded],
        gameweek=gameweek,
        transfer_recommendation=transfer_rec,
        transfer_note=transfer_note,
        excluded_wrong_gameweek=[f"{path.name} (GW{found})" for path, found in wrong_gameweek],
        superseded_inputs=[
            _extraction_input(path, raw, resolved) for path, raw, resolved in superseded
        ],
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
        my_squad=my_squad_summary,
        my_squad_note=my_squad_note,
        wildcard_gap=wildcard,
        wildcard_note=wildcard_note,
    )

    (run_dir / "run.json").write_text(record.model_dump_json(indent=2))
    (run_dir / "squad.json").write_text(squad.model_dump_json(indent=2))
    (runs_dir / "latest.json").write_text(json.dumps({"run_id": run_id}, indent=2))

    extractions = [resolved for _path, _raw, resolved in loaded]

    # Nuance is opt-in: nuance_fn=None means it never runs (no call, no
    # cost, no nuance.json). When it does run, a failure is captured
    # rather than propagated (`_run_nuance`) and — per spec — the report
    # is rendered WITHOUT the failed pass rather than with a half-failed
    # one, so `nuance_for_report` is deliberately None on failure even
    # though the failed NuanceRecord itself is still written to disk for
    # the audit trail. `report.gameweek.render_report` takes the bare
    # `SquadNuance`, not the `NuanceRecord` audit wrapper, so this is
    # where that unwrapping happens.
    nuance_for_report: SquadNuance | None = None
    if nuance_fn is not None:
        nuance_record = _run_nuance(
            nuance_fn,
            run_id,
            squad,
            scores,
            election,
            extractions,
            db,
            {c.creator_id: c.name for c in creators},
            run_dir,
        )
        (run_dir / "nuance.json").write_text(nuance_record.model_dump_json(indent=2))
        if not nuance_record.failed:
            nuance_for_report = nuance_record.nuance

    report_path = _render_report(
        report_fn,
        record=record,
        squad=squad,
        scores=scores,
        election=election,
        db=db,
        extractions=extractions,
        creators=creators,
        availability=availability.verdicts,
        nuance=nuance_for_report,
        diff=diff,
        run_dir=run_dir,
    )

    return PipelineResult(
        record=record,
        squad=squad,
        run_dir=run_dir,
        scores=scores,
        election=election,
        extractions=extractions,
        availability=availability.verdicts,
        diff=diff,
        report_path=report_path,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Solve a squad from stored extractions")
    parser.add_argument(
        "--nuance",
        action="store_true",
        help="run the LLM nuance pass over the solved squad (costs money — an Anthropic API call)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help=(
            "bypass the 6h bootstrap-static cache and refetch FPL availability live "
            "(use on deadline morning; free, no LLM cost)"
        ),
    )
    parser.add_argument(
        "--gameweek",
        type=int,
        default=None,
        help=(
            "gameweek to solve for; extractions about another gameweek are excluded from "
            "the vote. Defaults to the FPL API's next event. Pass explicitly when running "
            "AFTER a deadline has passed, since `is_next` will have rolled on by then."
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()

    nuance_fn = None
    if args.nuance:
        from fpl_oracle.report.nuance import generate_nuance

        nuance_fn = generate_nuance

    # Unlike nuance, my-squad has no flag: it's free (no LLM call), so a
    # real CLI run always attempts it — see run_pipeline's docstring for
    # why `run_pipeline()` itself defaults this to off (test isolation).
    gameweek = args.gameweek
    if gameweek is None:
        gameweek = fpl_client.get_next_gameweek()
        logger.info("pipeline: solving for GW%s (from the FPL API's next event)", gameweek)

    try:
        result = run_pipeline(
            force_refresh=args.force_refresh,
            nuance_fn=nuance_fn,
            my_squad_fn=fetch_my_squad_summary,
            gameweek=gameweek,
        )
    except NoConsensusEvidenceError as e:
        print(f"\nREFUSING TO SOLVE: {e}", file=sys.stderr)
        print(
            "Run the extraction stage first "
            f"(uv run python -m fpl_oracle.extract.run_extract --gameweek {gameweek}), "
            "or pass an explicit --gameweek if you meant a different one.",
            file=sys.stderr,
        )
        return 1
    record, squad, run_dir = result.record, result.squad, result.run_dir

    # Deliberately NOT force_refresh: this only resolves ids to names for
    # the lines printed below. run_pipeline already refreshed the cache
    # seconds ago, so forcing again would re-download the whole payload AND
    # risk printing names from a different snapshot than the one solved on.
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
    if result.report_path is not None:
        print(f"report written to {result.report_path}")

    if record.my_squad is not None:
        ms = record.my_squad
        print(
            f"my squad (GW{ms.gameweek}): {ms.team_name} — "
            f"bank £{ms.bank / 10:.1f}m, value £{ms.team_value / 10:.1f}m, "
            f"free transfers: {ms.free_transfers if ms.free_transfers is not None else 'unknown'}"
        )
        if ms.overall_rank is not None:
            print(f"  overall rank: {ms.overall_rank:,}  overall points: {ms.overall_points}")
        for lg in ms.private_leagues:
            rank = lg.entry_rank if lg.entry_rank is not None else "?"
            count = lg.rank_count if lg.rank_count is not None else "?"
            print(f"  {lg.name}: {rank}/{count}")
    else:
        print(f"my squad: not recorded — {record.my_squad_note}")
    # The in-season answer, printed last because it is the thing to act on.
    rec = record.transfer_recommendation
    if rec is not None:
        print(f"\n=== GW{rec.gameweek} TRANSFER: {rec.verdict} ===")
        print(f"  {rec.reasoning}")
        if rec.unavailable_owned:
            print(f"  UNAVAILABLE IN YOUR SQUAD: {', '.join(rec.unavailable_owned)}")
        if rec.recommended is not None:
            m = rec.recommended
            print(
                f"  -> {m.out_name} OUT, {m.in_name} IN  "
                f"({m.delta:+.2f}, {m.price_delta_m:+.1f}m, "
                f"{m.in_backers} backers, {m.affordability})"
            )
            for note in m.notes:
                print(f"     note: {note}")
        if rec.shortlist:
            print("  also considered:")
            for m in rec.shortlist:
                print(
                    f"    {m.out_name:16} -> {m.in_name:16} {m.delta:+.2f}  "
                    f"backers {m.out_backers}->{m.in_backers}  {m.affordability}"
                )
        if rec.hit_candidates:
            print("  if you'll take a -4 (system will NOT price the hit for you):")
            for m in rec.hit_candidates:
                print(
                    f"    {m.out_name:16} -> {m.in_name:16} {m.delta:+.2f}  {m.in_backers} backers"
                )
        d = rec.delta_distribution
        if d is not None and d.moves_considered:
            print(
                f"  judged {d.moves_considered} legal moves vs bar {d.bar_applied:.2f} "
                f"(best {d.best:+.2f}, p90 {d.p90:+.2f}, median {d.median:+.2f})"
            )
        print(f"  pricing: {rec.pricing_note}")
    elif record.transfer_note:
        print(f"\ntransfer recommendation: {record.transfer_note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
