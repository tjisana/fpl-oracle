"""Tests for fpl_oracle.pipeline: stored extractions -> solved squad + an
immutable run record.

No network: `PlayerDB` and the creator registry are always injected as
small, hand-built fixtures via `run_pipeline`'s `player_db`/`registry`
params, never the real `PlayerDB.load()` / `REGISTRY`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import Pick, PickAction, Provenance, VideoExtraction
from fpl_oracle.fpl.players import Player, Position
from fpl_oracle.pipeline import run_pipeline
from fpl_oracle.roster.models import Creator, Tier

# ---------------------------------------------------------------------------
# Fixtures — deliberately mirroring tests/test_squad_solver.py's small,
# hand-built pool pattern (a legal, affordable spread across positions and
# clubs) so build_squad always has a feasible base to work with.
# ---------------------------------------------------------------------------


def _player(
    pid: int,
    position: Position,
    price_m: float,
    team_id: int,
    status: str = "a",
    chance: int | None = None,
) -> Player:
    return Player(
        player_id=pid,
        web_name=f"P{pid}",
        first_name="F",
        second_name=f"P{pid}",
        team_id=team_id,
        team_short=f"T{team_id}",
        position=position,
        now_cost=int(price_m * 10),
        status=status,
        chance_of_playing_next_round=chance,
    )


def _legal_pool() -> list[Player]:
    players, pid = [], 1
    for position, count in (
        (Position.GK, 6),
        (Position.DEF, 12),
        (Position.MID, 12),
        (Position.FWD, 8),
    ):
        for i in range(count):
            players.append(_player(pid, position, 4.0 + (i % 5), team_id=pid % 8))
            pid += 1
    return players


def _make_db(players: list[Player]):
    from fpl_oracle.fpl.players import PlayerDB

    by_id = {p.player_id: p for p in players}
    team_full_names = {p.team_id: p.team_short for p in players}
    metaphones = {p.player_id: ("", "") for p in players}
    return PlayerDB(players=by_id, team_full_names=team_full_names, player_metaphones=metaphones)


def _creator(creator_id: str) -> Creator:
    return Creator(creator_id=creator_id, name=creator_id, youtube_hint=creator_id, tier=Tier.CORE)


def _pick(
    player_id: int | None, action: PickAction = PickAction.SQUAD_INCLUDE, conviction: int = 5
) -> Pick:
    return Pick(
        player_name_raw="Some Player",
        team_inferred=None,
        position_inferred=None,
        player_id=player_id,
        action=action,
        conviction=conviction,
        time_horizon=1,
        reasoning="test",
        provenance=Provenance.PERSONAL,
    )


def _write_extraction(directory: Path, creator_id: str, video_id: str, picks: list[Pick]) -> Path:
    extraction = VideoExtraction(
        creator_id=creator_id,
        video_id=video_id,
        video_title="Test video",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        gameweek=1,
        picks=picks,
    )
    resolved = ResolvedExtraction(extraction=extraction, resolutions=[])
    path = directory / f"{video_id}.json"
    path.write_text(resolved.model_dump_json())
    return path


# ---------------------------------------------------------------------------


class TestRunIdentity:
    def test_run_id_unique_and_never_overwrites(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        runs_dir = tmp_path / "runs"
        record1, _, dir1 = run_pipeline(
            extractions_dir=tmp_path / "missing", runs_dir=runs_dir, player_db=db, registry=[]
        )
        record2, _, dir2 = run_pipeline(
            extractions_dir=tmp_path / "missing", runs_dir=runs_dir, player_db=db, registry=[]
        )
        assert record1.run_id != record2.run_id
        assert dir1 != dir2
        assert (dir1 / "run.json").exists()
        assert (dir2 / "run.json").exists()
        # the first run's record must still be intact — never overwritten
        first_on_disk = json.loads((dir1 / "run.json").read_text())
        assert first_on_disk["run_id"] == record1.run_id

        latest = json.loads((runs_dir / "latest.json").read_text())
        assert latest["run_id"] == record2.run_id


class TestInputProvenance:
    def test_extraction_sha256_recorded(self, tmp_path: Path) -> None:
        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        path = _write_extraction(extractions_dir, "alice", "vid1", [_pick(1)])
        db = _make_db(_legal_pool())
        record, _, _ = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice")],
        )
        assert len(record.inputs) == 1
        entry = record.inputs[0]
        assert entry.video_id == "vid1"
        assert entry.creator_id == "alice"
        assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


class TestPickResolution:
    def test_unresolved_picks_skipped_and_counted(self, tmp_path: Path) -> None:
        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        _write_extraction(
            extractions_dir,
            "alice",
            "vid1",
            [_pick(1), _pick(None)],  # one resolved, one unresolved
        )
        db = _make_db(_legal_pool())
        record, _, _ = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice")],
        )
        assert record.counts.picks_used == 1
        assert record.counts.picks_skipped_unresolved == 1


class TestAvailabilityVeto:
    def test_excluded_players_never_reach_the_solver(self, tmp_path: Path) -> None:
        pool = _legal_pool()
        # Cheap AND heavily backed — would be an obvious pick if not
        # structurally vetoed by its "injured" status.
        excluded = _player(9001, Position.MID, 4.0, team_id=99, status="i")
        db = _make_db([*pool, excluded])

        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        _write_extraction(
            extractions_dir, "alice", "v1", [_pick(9001, action=PickAction.CAPTAIN, conviction=5)]
        )
        _write_extraction(
            extractions_dir,
            "bob",
            "v2",
            [_pick(9001, action=PickAction.SQUAD_INCLUDE, conviction=5)],
        )

        record, squad, _ = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice"), _creator("bob")],
        )

        assert 9001 not in {p.player_id for p in squad.players}
        assert any(e.player_id == 9001 for e in record.excluded_players)
        assert any(e.player_id == 9001 and e.reason for e in record.excluded_players)


class TestCaptaincyRelaxation:
    def test_relaxed_flag_surfaces_when_both_options_cant_fit(self, tmp_path: Path) -> None:
        pool = _legal_pool()
        # Two equally-backed captain options that together (or even singly,
        # against the rest of a legal 15) blow the £100m budget — the
        # solver must relax the >=2 captaincy guarantee rather than fail.
        premium_a = _player(9101, Position.FWD, 60.0, team_id=50)
        premium_b = _player(9102, Position.FWD, 60.0, team_id=51)
        db = _make_db([*pool, premium_a, premium_b])

        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        _write_extraction(
            extractions_dir, "alice", "v1", [_pick(9101, action=PickAction.CAPTAIN, conviction=5)]
        )
        _write_extraction(
            extractions_dir, "bob", "v2", [_pick(9102, action=PickAction.CAPTAIN, conviction=5)]
        )

        record, squad, _ = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice"), _creator("bob")],
        )

        assert record.solver.relaxed_captaincy is True
        assert record.solver.captain_options_required < 2
        assert len(squad.players) == 15


class TestGracefulEmptyInputs:
    def test_missing_extractions_directory(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        record, squad, _ = run_pipeline(
            extractions_dir=tmp_path / "does-not-exist",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
        )
        assert record.counts.extractions_loaded == 0
        assert record.counts.picks_used == 0
        assert record.counts.picks_skipped_unresolved == 0
        assert len(squad.players) == 15

    def test_empty_extractions_directory(self, tmp_path: Path) -> None:
        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        db = _make_db(_legal_pool())
        record, squad, _ = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
        )
        assert record.counts.extractions_loaded == 0
        assert len(squad.players) == 15
