"""Tests for fpl_oracle.pipeline: stored extractions -> solved squad + an
immutable run record.

No network: `PlayerDB` and the creator registry are always injected as
small, hand-built fixtures via `run_pipeline`'s `player_db`/`registry`
params, never the real `PlayerDB.load()` / `REGISTRY`. Every call also
passes a fake `report_fn` (`_fake_report_fn` below) — `report.gameweek`
is a sibling module still being built concurrently, so these tests must
never trigger `run_pipeline`'s default lazy import of it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import Pick, PickAction, Provenance, VideoExtraction
from fpl_oracle.fpl.players import Player, PlayerDB, Position
from fpl_oracle.pipeline import (
    PipelineResult,
    diff_vs_previous,
    load_previous_run,
    run_pipeline,
)
from fpl_oracle.report.models import NuanceRecord, SquadNuance
from fpl_oracle.roster.models import Creator, Tier


def _fake_report_fn(**kwargs) -> str:
    """Stand-in for `report.gameweek.render_report` — matches its assumed
    keyword-only signature well enough to be a drop-in, without importing
    the real (concurrently-developed) module."""
    return f"# fake report ({kwargs.get('nuance')!r})"


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


def _write_extraction(
    directory: Path,
    creator_id: str,
    video_id: str,
    picks: list[Pick],
    published_at: datetime = datetime(2026, 8, 1, tzinfo=UTC),
) -> Path:
    extraction = VideoExtraction(
        creator_id=creator_id,
        video_id=video_id,
        video_title="Test video",
        published_at=published_at,
        gameweek=1,
        picks=picks,
    )
    resolved = ResolvedExtraction(extraction=extraction, resolutions=[])
    path = directory / f"{video_id}.json"
    path.write_text(resolved.model_dump_json())
    return path


# ---------------------------------------------------------------------------


class TestSupersededExtractions:
    """`data/extractions/` is keyed by video_id and append-only: a rerun
    writes a NEW file and never removes the old one. Since picks fan in by
    CREATOR, an unguarded load merges a creator's old and new videos into
    one ballot — and because scoring keeps the STRONGEST vote per (creator,
    player), a player he has just dropped keeps his full-strength earlier
    vote. This is the exact input the deadline rerun exists to refresh."""

    def test_only_the_creators_latest_video_is_used(self, tmp_path: Path) -> None:
        extractions = tmp_path / "extractions"
        extractions.mkdir()
        _write_extraction(
            extractions,
            "andy",
            "old_video",
            [_pick(1), _pick(2), _pick(3)],
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        _write_extraction(
            extractions,
            "andy",
            "new_video",
            [_pick(1)],
            published_at=datetime(2026, 8, 9, tzinfo=UTC),
        )

        result = run_pipeline(
            extractions_dir=extractions,
            runs_dir=tmp_path / "runs",
            player_db=_make_db(_legal_pool()),
            registry=[_creator("andy")],
            report_fn=_fake_report_fn,
        )

        # Only the newer video's single pick counts; players 2 and 3, dropped
        # overnight, must not survive on the strength of the older video.
        assert result.record.counts.picks_used == 1
        assert [i.video_id for i in result.record.inputs] == ["new_video"]

    def test_superseded_files_are_recorded_not_silently_dropped(self, tmp_path: Path) -> None:
        extractions = tmp_path / "extractions"
        extractions.mkdir()
        _write_extraction(
            extractions, "andy", "old_video", [_pick(1)], datetime(2026, 8, 1, tzinfo=UTC)
        )
        _write_extraction(
            extractions, "andy", "new_video", [_pick(1)], datetime(2026, 8, 9, tzinfo=UTC)
        )

        result = run_pipeline(
            extractions_dir=extractions,
            runs_dir=tmp_path / "runs",
            player_db=_make_db(_legal_pool()),
            registry=[_creator("andy")],
            report_fn=_fake_report_fn,
        )

        assert [i.video_id for i in result.record.superseded_inputs] == ["old_video"]
        # The file itself is untouched on disk — set aside, never deleted.
        assert (extractions / "old_video.json").exists()

    def test_different_creators_are_never_collapsed(self, tmp_path: Path) -> None:
        extractions = tmp_path / "extractions"
        extractions.mkdir()
        _write_extraction(extractions, "andy", "v1", [_pick(1)])
        _write_extraction(extractions, "focal", "v2", [_pick(2)])

        result = run_pipeline(
            extractions_dir=extractions,
            runs_dir=tmp_path / "runs",
            player_db=_make_db(_legal_pool()),
            registry=[_creator("andy"), _creator("focal")],
            report_fn=_fake_report_fn,
        )

        assert result.record.counts.extractions_loaded == 2
        assert result.record.superseded_inputs == []


class TestRunIdentity:
    def test_run_id_unique_and_never_overwrites(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        runs_dir = tmp_path / "runs"
        result1 = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=runs_dir,
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        result2 = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=runs_dir,
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        record1, dir1 = result1.record, result1.run_dir
        record2, dir2 = result2.record, result2.run_dir
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
        result = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice")],
            report_fn=_fake_report_fn,
        )
        record = result.record
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
        result = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice")],
            report_fn=_fake_report_fn,
        )
        record = result.record
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

        result = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice"), _creator("bob")],
            report_fn=_fake_report_fn,
        )
        record, squad = result.record, result.squad

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

        result = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice"), _creator("bob")],
            report_fn=_fake_report_fn,
        )
        record, squad = result.record, result.squad

        assert record.solver.relaxed_captaincy is True
        assert record.solver.captain_options_required < 2
        assert len(squad.players) == 15


class TestGracefulEmptyInputs:
    def test_missing_extractions_directory(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        result = run_pipeline(
            extractions_dir=tmp_path / "does-not-exist",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        record, squad = result.record, result.squad
        assert record.counts.extractions_loaded == 0
        assert record.counts.picks_used == 0
        assert record.counts.picks_skipped_unresolved == 0
        assert len(squad.players) == 15

    def test_empty_extractions_directory(self, tmp_path: Path) -> None:
        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        db = _make_db(_legal_pool())
        result = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        record, squad = result.record, result.squad
        assert record.counts.extractions_loaded == 0
        assert len(squad.players) == 15


# ---------------------------------------------------------------------------
# Phase 4: the PipelineResult shape, nuance/report seams, force_refresh.
# ---------------------------------------------------------------------------


class TestPipelineResultShape:
    def test_result_carries_scores_election_extractions_and_report(self, tmp_path: Path) -> None:
        extractions_dir = tmp_path / "extractions"
        extractions_dir.mkdir()
        _write_extraction(extractions_dir, "alice", "vid1", [_pick(1)])
        db = _make_db(_legal_pool())

        result = run_pipeline(
            extractions_dir=extractions_dir,
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("alice")],
            report_fn=_fake_report_fn,
        )

        assert isinstance(result, PipelineResult)
        assert 1 in result.scores
        assert result.election is not None
        assert len(result.extractions) == 1
        assert result.extractions[0].extraction.video_id == "vid1"
        assert result.report_path == result.run_dir / "report.md"
        assert result.report_path.read_text().startswith("# fake report")


class TestNuanceSeam:
    def test_nuance_fn_none_fires_no_nuance_call(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        result = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
            nuance_fn=None,
        )
        assert not (result.run_dir / "nuance.json").exists()

    def test_nuance_success_is_written_and_passed_to_report(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        captured_nuance: SquadNuance | None = None
        captured_creator_names: dict[str, str] = {}

        def fake_nuance_fn(run_id, squad, scores, election, extractions, db, creator_names):
            captured_creator_names.update(creator_names)
            return NuanceRecord(
                run_id=run_id,
                generated_at=datetime(2026, 8, 19, tzinfo=UTC),
                model="fake-model",
                nuance=SquadNuance(squad_notes=["ok"]),
            )

        def report_fn(*, nuance: SquadNuance | None = None, **kwargs) -> str:
            nonlocal captured_nuance
            captured_nuance = nuance
            return "# report"

        result = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[_creator("andy")],
            report_fn=report_fn,
            nuance_fn=fake_nuance_fn,
        )

        # Display names reach the nuance pass: the model must EMIT creator
        # ids (they are checked against who actually voted), but it reads
        # the evidence better when each id carries a human name.
        assert captured_creator_names == {"andy": "andy"}

        nuance_on_disk = json.loads((result.run_dir / "nuance.json").read_text())
        assert nuance_on_disk["failed"] is False
        assert nuance_on_disk["model"] == "fake-model"
        # report_fn's `nuance` kwarg is the bare `SquadNuance` (what the
        # real `report.gameweek.render_report` actually takes), not the
        # `NuanceRecord` audit wrapper.
        assert captured_nuance is not None
        assert captured_nuance.squad_notes == ["ok"]

    def test_nuance_failure_is_non_fatal_and_report_still_written(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        captured_nuance: SquadNuance | None = SquadNuance(
            squad_notes=["sentinel — must be cleared"]
        )

        def failing_nuance_fn(run_id, squad, scores, election, extractions, db, creator_names):
            raise ValueError("the LLM exploded")

        def report_fn(*, nuance: SquadNuance | None = None, **kwargs) -> str:
            nonlocal captured_nuance
            captured_nuance = nuance
            return "# report without nuance"

        result = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=report_fn,
            nuance_fn=failing_nuance_fn,
        )

        assert result.report_path is not None
        assert result.report_path.read_text() == "# report without nuance"
        nuance_on_disk = json.loads((result.run_dir / "nuance.json").read_text())
        assert nuance_on_disk["failed"] is True
        assert "the LLM exploded" in nuance_on_disk["failure_reason"]
        # the report itself must render WITHOUT the failed pass
        assert captured_nuance is None


class TestForceRefreshSeam:
    def test_force_refresh_reaches_player_db_load(self, tmp_path: Path, monkeypatch) -> None:
        db = _make_db(_legal_pool())
        captured: dict[str, object] = {}

        def fake_load(cls, force_refresh: bool = False) -> PlayerDB:
            captured["force_refresh"] = force_refresh
            return db

        monkeypatch.setattr(PlayerDB, "load", classmethod(fake_load))

        run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            registry=[],
            report_fn=_fake_report_fn,
            force_refresh=True,
        )

        assert captured["force_refresh"] is True

    def test_default_force_refresh_is_false(self, tmp_path: Path, monkeypatch) -> None:
        db = _make_db(_legal_pool())
        captured: dict[str, object] = {}

        def fake_load(cls, force_refresh: bool = False) -> PlayerDB:
            captured["force_refresh"] = force_refresh
            return db

        monkeypatch.setattr(PlayerDB, "load", classmethod(fake_load))

        run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            registry=[],
            report_fn=_fake_report_fn,
        )

        assert captured["force_refresh"] is False

    def test_injected_player_db_skips_load_entirely(self, tmp_path: Path, monkeypatch) -> None:
        db = _make_db(_legal_pool())

        def fake_load(cls, force_refresh: bool = False) -> PlayerDB:
            pytest.fail("PlayerDB.load must not be called when player_db is injected")

        monkeypatch.setattr(PlayerDB, "load", classmethod(fake_load))

        run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
            force_refresh=True,
        )


# ---------------------------------------------------------------------------
# Report-freshness gaps: bootstrap fetch timestamp, availability verdicts,
# and the squad diff — all now threaded through `PipelineResult` and into
# `render_report` (via `_fake_report_fn`/`report_fn`'s kwargs).
# ---------------------------------------------------------------------------


class TestBootstrapFetchedAt:
    def test_injected_player_db_yields_no_bootstrap_timestamp(self, tmp_path: Path) -> None:
        """`bootstrap_fetched_at` is only meaningful when this run actually
        loaded the real cache file — an injected `player_db` (every test in
        this module) never touched it, so the field must stay `None`
        rather than reporting a stale or fabricated timestamp."""
        db = _make_db(_legal_pool())
        result = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        assert result.record.bootstrap_fetched_at is None

    def test_old_run_json_without_the_new_fields_still_parses(self) -> None:
        """A real run.json written before `bootstrap_fetched_at` (on
        RunRecord) and `published_at` (on ExtractionInput) existed must
        still load — this is the exact shape `data/runs/*/run.json` had
        on disk before this change landed."""
        from fpl_oracle.pipeline import RunRecord

        old_json = json.dumps(
            {
                "run_id": "old-run",
                "started_at": "2026-08-19T00:00:00Z",
                "finished_at": "2026-08-19T00:00:05Z",
                "git": {"commit_sha": None, "short_sha": None, "branch": None, "dirty": None},
                "inputs": [
                    {
                        "path": "data/extractions/vid1.json",
                        "video_id": "vid1",
                        "creator_id": "alice",
                        "sha256": "a" * 64,
                    }
                ],
                "creator_weights": {"alice": 0.5},
                "excluded_players": [],
                "counts": {
                    "extractions_loaded": 1,
                    "picks_used": 1,
                    "picks_skipped_unresolved": 0,
                    "players_in_pool": 10,
                },
                "solver": {
                    "total_cost_m": 100.0,
                    "formation": "3-4-3",
                    "captain": None,
                    "vice_captain": None,
                    "captain_options_required": 0,
                    "relaxed_captaincy": False,
                },
            }
        )
        record = RunRecord.model_validate_json(old_json)
        assert record.bootstrap_fetched_at is None
        assert record.inputs[0].published_at is None


class TestPipelineResultDiffAndAvailability:
    def test_first_run_has_no_previous_and_diff_lists_everyone_in(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        result = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        assert result.diff.previous_run_id is None
        assert set(result.diff.players_in) == {p.player_id for p in result.squad.players}
        assert result.diff.players_out == []

    def test_second_run_diffs_against_the_first(self, tmp_path: Path) -> None:
        """`run_pipeline` reads `runs_dir/latest.json` for itself now (see
        its module docstring) — a bare, un-chained `run_pipeline` call must
        pick up the previous run's id without any caller doing extra work,
        which is exactly the gap `deadline.py` used to paper over alone."""
        db = _make_db(_legal_pool())
        runs_dir = tmp_path / "runs"
        first = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=runs_dir,
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        second = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=runs_dir,
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        assert second.diff.previous_run_id == first.record.run_id
        assert second.diff.new_captain == second.record.solver.captain

    def test_availability_verdicts_exposed_on_result(self, tmp_path: Path) -> None:
        pool = _legal_pool()
        db = _make_db(pool)
        result = run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=_fake_report_fn,
        )
        assert set(result.availability) == {p.player_id for p in pool}

    def test_diff_and_availability_reach_report_fn(self, tmp_path: Path) -> None:
        db = _make_db(_legal_pool())
        captured: dict = {}

        def report_fn(**kwargs) -> str:
            captured.update(kwargs)
            return "# report"

        run_pipeline(
            extractions_dir=tmp_path / "missing",
            runs_dir=tmp_path / "runs",
            player_db=db,
            registry=[],
            report_fn=report_fn,
        )

        assert "diff" in captured
        assert captured["diff"].previous_run_id is None
        assert "availability" in captured
        assert isinstance(captured["availability"], dict)


# ---------------------------------------------------------------------------
# Squad diff vs. the previous run — pure arithmetic over two on-disk
# squad.json / run.json files, no LLM, no network. Moved here from
# `deadline.py` (see `pipeline.py`'s module docstring for why); the tests
# below are relocated, not new, aside from `diff_vs_previous`'s signature
# now taking explicit `(player_ids, captain)` rather than a duck-typed
# `PipelineResult`-like object — `run_pipeline` already has the concrete
# `Squad`/captain at the point it calls this, so the decoupling Protocol
# `deadline.py` needed is no longer necessary here.
# ---------------------------------------------------------------------------


class TestLoadPreviousRun:
    def test_no_latest_pointer_returns_none(self, tmp_path: Path) -> None:
        assert load_previous_run(tmp_path / "runs") is None

    def test_latest_pointer_missing_files_returns_none(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        (runs_dir / "latest.json").write_text(json.dumps({"run_id": "ghost-run"}))
        assert load_previous_run(runs_dir) is None

    def test_reads_squad_and_captain_from_the_previous_run(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run123"
        run_dir.mkdir(parents=True)
        (run_dir / "squad.json").write_text(
            json.dumps({"players": [{"player_id": 1}, {"player_id": 2}]})
        )
        (run_dir / "run.json").write_text(json.dumps({"solver": {"captain": 1}}))
        (runs_dir / "latest.json").write_text(json.dumps({"run_id": "run123"}))

        result = load_previous_run(runs_dir)

        assert result == ("run123", {1, 2}, 1)


class TestPreviousRunRobustness:
    def test_corrupt_previous_squad_json_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """The previous run feeds a COSMETIC diff. A squad.json truncated by
        a Ctrl-C must never abort a run before it even starts."""
        runs = tmp_path / "runs"
        (runs / "run-old").mkdir(parents=True)
        (runs / "latest.json").write_text(json.dumps({"run_id": "run-old"}))
        (runs / "run-old" / "squad.json").write_text('{"players": [{"player_i')  # truncated
        (runs / "run-old" / "run.json").write_text(json.dumps({"solver": {"captain": 1}}))

        assert load_previous_run(runs) is None

    def test_previous_squad_missing_expected_keys_is_skipped(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        (runs / "run-old").mkdir(parents=True)
        (runs / "latest.json").write_text(json.dumps({"run_id": "run-old"}))
        (runs / "run-old" / "squad.json").write_text(json.dumps({"players": [{"nope": 1}]}))
        (runs / "run-old" / "run.json").write_text(json.dumps({"solver": {"captain": 1}}))

        assert load_previous_run(runs) is None


class TestDiffVsPrevious:
    def test_no_previous_run_reports_everyone_as_new_and_no_captain_change(self) -> None:
        diff = diff_vs_previous(None, {1, 2, 3}, 1)

        assert diff.previous_run_id is None
        assert diff.players_in == [1, 2, 3]
        assert diff.players_out == []
        assert diff.captain_changed is False
        assert diff.new_captain == 1

    def test_players_in_and_out_computed_correctly(self) -> None:
        previous = ("run0", {1, 2, 3}, 1)
        diff = diff_vs_previous(previous, {2, 3, 4}, 1)

        assert diff.players_in == [4]
        assert diff.players_out == [1]

    def test_captain_change_detected(self) -> None:
        previous = ("run0", {1, 2, 3}, 1)
        diff = diff_vs_previous(previous, {1, 2, 3}, 4)

        assert diff.captain_changed is True
        assert diff.previous_captain == 1
        assert diff.new_captain == 4

    def test_captain_unchanged_detected(self) -> None:
        previous = ("run0", {1, 2, 3}, 1)
        diff = diff_vs_previous(previous, {1, 2, 3}, 1)

        assert diff.captain_changed is False
