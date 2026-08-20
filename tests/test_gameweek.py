"""Tests for report/gameweek.py — markdown rendering only.

No network: `PlayerDB` is always hand-built from a small fixture (never
`PlayerDB.load()`), mirroring the pattern in tests/test_pipeline.py and
tests/test_squad_solver.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpl_oracle.consensus.captaincy import CaptainCandidate, CaptaincyElection
from fpl_oracle.consensus.scoring import CreatorVote, PlayerScore
from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import Pick, PickAction, Provenance, VideoExtraction
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict
from fpl_oracle.fpl.players import Player, PlayerDB, Position
from fpl_oracle.pipeline import (
    ExcludedPlayer,
    ExtractionInput,
    GitInfo,
    RunCounts,
    RunRecord,
    SolverOutcome,
    SquadDiff,
)
from fpl_oracle.report import gameweek as gameweek_mod
from fpl_oracle.report.gameweek import render_report, write_report
from fpl_oracle.report.models import Concern, ConcernKind, PlayerNuance, SquadNuance
from fpl_oracle.roster.models import Creator, Tier
from fpl_oracle.solver.squad import Squad, SquadPlayer

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _player(pid: int, position: Position, price_m: float, team_id: int) -> Player:
    return Player(
        player_id=pid,
        web_name=f"Player{pid}",
        first_name="First",
        second_name=f"Last{pid}",
        team_id=team_id,
        team_short=f"T{team_id}",
        position=position,
        now_cost=int(price_m * 10),
        status="a",
        chance_of_playing_next_round=None,
    )


def _db(players: list[Player]) -> PlayerDB:
    by_id = {p.player_id: p for p in players}
    team_full_names = {p.team_id: p.team_short for p in players}
    metaphones = {p.player_id: ("", "") for p in players}
    return PlayerDB(players=by_id, team_full_names=team_full_names, player_metaphones=metaphones)


def _squad_player(player: Player, starting: bool, band_score: float = 0.5) -> SquadPlayer:
    return SquadPlayer(
        player_id=player.player_id,
        starting=starting,
        price_m=player.price_m,
        position=player.position,
        band_score=band_score,
        coefficient=player.price_m + band_score,
    )


def _basic_pool() -> list[Player]:
    players = []
    pid = 1
    for position, count in (
        (Position.GK, 2),
        (Position.DEF, 3),
        (Position.MID, 3),
        (Position.FWD, 2),
    ):
        for i in range(count):
            players.append(_player(pid, position, 4.5 + i, team_id=pid % 5))
            pid += 1
    return players


def _basic_squad(players: list[Player]) -> Squad:
    # first of each position group starts, rest bench, mirroring the
    # real solver's own (not starting, position, -coefficient) sort.
    starters = {
        players[0].player_id,
        players[2].player_id,
        players[5].player_id,
        players[8].player_id,
    }
    squad_players = [_squad_player(p, starting=p.player_id in starters) for p in players]
    squad_players.sort(key=lambda p: (not p.starting, p.position, -p.coefficient))
    return Squad(
        players=squad_players,
        total_cost_m=round(sum(p.price_m for p in squad_players), 1),
        objective=1.0,
        captain_options_included=[],
        captain_options_required=0,
        relaxed_captaincy=False,
    )


def _record(
    *,
    creator_weights: dict[str, float] | None = None,
    excluded_players: list[ExcludedPlayer] | None = None,
    captain: int | None = None,
    vice_captain: int | None = None,
    relaxed_captaincy: bool = False,
    captain_options_required: int = 0,
    inputs: list[ExtractionInput] | None = None,
    git: GitInfo | None = None,
    bootstrap_fetched_at: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id="20260819T120000Z-abc1234",
        started_at=datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, 12, 0, 5, tzinfo=UTC),
        bootstrap_fetched_at=bootstrap_fetched_at,
        git=git
        if git is not None
        else GitInfo(commit_sha=None, short_sha=None, branch=None, dirty=None),
        inputs=inputs or [],
        creator_weights=creator_weights or {},
        excluded_players=excluded_players or [],
        counts=RunCounts(
            extractions_loaded=1, picks_used=2, picks_skipped_unresolved=1, players_in_pool=10
        ),
        solver=SolverOutcome(
            total_cost_m=50.0,
            formation="3-4-3",
            captain=captain,
            vice_captain=vice_captain,
            captain_options_required=captain_options_required,
            relaxed_captaincy=relaxed_captaincy,
        ),
    )


def _creator(creator_id: str, name: str, channel_id: str | None = None) -> Creator:
    """A creator with a resolved channel of their own — i.e. eligible for
    ingestion under `ingest.run_ingest.eligible_creators`, which is what
    the report's participation counts are measured against. `channel_id`
    defaults to one derived from the id so each fixture creator is the
    sole owner of their own channel."""
    return Creator(
        creator_id=creator_id,
        name=name,
        youtube_hint=name,
        tier=Tier.CORE,
        channel_id=channel_id if channel_id is not None else f"UC_{creator_id}",
    )


def _pick(
    player_id: int | None,
    action: PickAction = PickAction.SQUAD_INCLUDE,
    conviction: int = 5,
    reasoning: str = "solid pick",
    provenance: Provenance = Provenance.PERSONAL,
) -> Pick:
    return Pick(
        player_name_raw="Some Player",
        team_inferred=None,
        position_inferred=None,
        player_id=player_id,
        action=action,
        conviction=conviction,
        time_horizon=1,
        reasoning=reasoning,
        provenance=provenance,
    )


def _extraction(
    creator_id: str,
    video_id: str,
    picks: list[Pick],
    gameweek: int | None = 1,
    published_at: datetime = datetime(2026, 8, 1, tzinfo=UTC),
) -> ResolvedExtraction:
    extraction = VideoExtraction(
        creator_id=creator_id,
        video_id=video_id,
        video_title="Test video",
        published_at=published_at,
        gameweek=gameweek,
        picks=picks,
    )
    return ResolvedExtraction(extraction=extraction, resolutions=[])


def _score(player_id: int, band: str, votes: list[CreatorVote]) -> PlayerScore:
    total = sum(v.value for v in votes)
    return PlayerScore(
        player_id=player_id,
        band=band,
        weighted_votes=total,
        band_score=0.8,
        backers=sum(1 for v in votes if v.value > 0),
        detractors=sum(1 for v in votes if v.value < 0),
        votes=votes,
    )


def _vote(
    creator_id: str, weight: float, action: PickAction, conviction: int, value: float
) -> CreatorVote:
    return CreatorVote(
        creator_id=creator_id,
        creator_weight=weight,
        action=action,
        conviction=conviction,
        value=value,
    )


def _empty_election() -> CaptaincyElection:
    return CaptaincyElection(
        candidates=[], options=[], total_voters=0, contributing_creators=0, thin=True
    )


# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_render_twice_is_identical(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"alice": 0.5})
        election = _empty_election()
        extractions = [_extraction("alice", "v1", [_pick(pool[0].player_id)])]
        creators = [_creator("alice", "Alice")]

        first = render_report(
            record=record,
            squad=squad,
            scores={},
            election=election,
            db=db,
            extractions=extractions,
            creators=creators,
        )
        second = render_report(
            record=record,
            squad=squad,
            scores={},
            election=election,
            db=db,
            extractions=extractions,
            creators=creators,
        )
        assert first == second


class TestSquadTables:
    def test_captain_and_vice_markers(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        starter_ids = [p.player_id for p in squad.starting_xi]
        captain_id, vice_id = starter_ids[0], starter_ids[1]
        record = _record(captain=captain_id, vice_captain=vice_id)
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        captain_player = db.get(captain_id)
        vice_player = db.get(vice_id)
        assert captain_player is not None
        assert vice_player is not None
        assert f"{captain_player.web_name} (C)" in report
        assert f"{vice_player.web_name} (VC)" in report


class TestCaptaincyDisclosures:
    def test_thin_warning_rendered_when_thin(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        election = CaptaincyElection(
            candidates=[
                CaptainCandidate(player_id=pool[0].player_id, score=1.0, voters=1, share=1.0)
            ],
            options=[pool[0].player_id],
            total_voters=1,
            contributing_creators=10,
            thin=True,
        )
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=election,
            db=db,
            extractions=[],
            creators=[],
        )
        assert "thin captaincy evidence" in report.lower()

    def test_no_thin_warning_when_not_thin(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        election = CaptaincyElection(
            candidates=[
                CaptainCandidate(player_id=pool[0].player_id, score=1.0, voters=9, share=1.0)
            ],
            options=[pool[0].player_id],
            total_voters=9,
            contributing_creators=10,
            thin=False,
        )
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=election,
            db=db,
            extractions=[],
            creators=[],
        )
        assert "thin captaincy evidence" not in report.lower()

    def test_relaxed_captaincy_notice(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        election = CaptaincyElection(
            candidates=[
                CaptainCandidate(player_id=pool[0].player_id, score=1.0, voters=2, share=1.0),
                CaptainCandidate(player_id=pool[1].player_id, score=0.9, voters=2, share=0.9),
            ],
            options=[pool[0].player_id, pool[1].player_id],
            total_voters=4,
            contributing_creators=4,
            thin=False,
        )
        record = _record(relaxed_captaincy=True, captain_options_required=1)
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=election,
            db=db,
            extractions=[],
            creators=[],
        )
        assert "captaincy constraint relaxed" in report.lower()
        assert "relaxed to 1" in report.lower()

    def test_no_relaxed_notice_when_not_relaxed(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(relaxed_captaincy=False),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "captaincy constraint relaxed" not in report.lower()


class TestDissent:
    def test_detractor_vote_appears_with_reasoning(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        target = squad.players[0].player_id

        negative_vote = _vote("bob", 0.4, PickAction.AVOID, 3, value=-0.24)
        score = _score(target, "band", [negative_vote])
        extractions = [
            _extraction(
                "bob",
                "v1",
                [
                    _pick(
                        target,
                        action=PickAction.AVOID,
                        conviction=3,
                        reasoning="rotation risk this week",
                    )
                ],
            )
        ]
        creators = [_creator("bob", "Bob Creator")]
        report = render_report(
            record=_record(),
            squad=squad,
            scores={target: score},
            election=_empty_election(),
            db=db,
            extractions=extractions,
            creators=creators,
        )
        assert "Bob Creator" in report
        assert "rotation risk this week" in report
        assert "Nobody argued against this squad." not in report

    def test_no_dissent_line_when_no_detractors(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "Nobody argued against this squad." in report


class TestNuance:
    def test_none_renders_did_not_run_line(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            nuance=None,
        )
        assert "nuance pass did not run" in report.lower()

    def test_populated_nuance_renders_resolved_creator_names(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        target = squad.players[0].player_id
        nuance = SquadNuance(
            players=[
                PlayerNuance(
                    player_id=target,
                    summary="Backed for his set-piece role.",
                    concerns=[
                        Concern(
                            kind=ConcernKind.ROTATION,
                            note="Might be rotated against weaker sides.",
                            creator_ids=["alice"],
                            severity=2,
                        )
                    ],
                )
            ],
            captaincy_note="Two creators flagged the captain's tough fixture.",
            squad_notes=["Heavy reliance on one club's fixtures."],
        )
        creators = [_creator("alice", "Alice Analyst")]
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=creators,
            nuance=nuance,
        )
        assert "Alice Analyst" in report
        assert "Might be rotated against weaker sides." in report
        assert "Two creators flagged the captain's tough fixture." in report
        assert "Heavy reliance on one club's fixtures." in report
        assert "nuance pass did not run" not in report.lower()


class TestVetoedByAvailability:
    def test_excluded_player_with_votes_appears(self) -> None:
        pool = _basic_pool()
        excluded_player = _player(9001, Position.MID, 4.0, team_id=99)
        db = _db([*pool, excluded_player])
        squad = _basic_squad(pool)

        extractions = [
            _extraction("alice", "v1", [_pick(9001, action=PickAction.SQUAD_INCLUDE, conviction=5)])
        ]
        record = _record(
            creator_weights={"alice": 0.5},
            excluded_players=[ExcludedPlayer(player_id=9001, reason="status='i' (injured)")],
        )
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=extractions,
            creators=[_creator("alice", "Alice")],
        )
        assert "Player9001" in report
        assert "injured" in report

    def test_excluded_player_with_no_votes_absent(self) -> None:
        pool = _basic_pool()
        excluded_player = _player(9002, Position.MID, 4.0, team_id=99)
        db = _db([*pool, excluded_player])
        squad = _basic_squad(pool)

        record = _record(
            excluded_players=[ExcludedPlayer(player_id=9002, reason="status='i' (injured)")],
        )
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "Player9002" not in report
        assert "No creator-backed player was removed by the availability veto." in report


class TestCreatorNameResolution:
    def test_unknown_creator_id_falls_back_without_crashing(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        target = squad.players[0].player_id
        negative_vote = _vote("ghost", 0.4, PickAction.AVOID, 3, value=-0.24)
        score = _score(target, "band", [negative_vote])
        extractions = [
            _extraction("ghost", "v1", [_pick(target, action=PickAction.AVOID, conviction=3)])
        ]
        # note: NO creators list entry for "ghost" — should fall back to the raw id.
        report = render_report(
            record=_record(),
            squad=squad,
            scores={target: score},
            election=_empty_election(),
            db=db,
            extractions=extractions,
            creators=[],
        )
        assert "ghost" in report

    def test_known_creator_id_resolves_display_name(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"alice": 0.6})
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[_creator("alice", "Alice Analyst")],
        )
        assert "Alice Analyst" in report


class TestEvidenceQualityAndProvenance:
    def test_counts_and_weights_rendered(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"alice": 0.6, "bob": 0.3})
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[_creator("alice", "Alice"), _creator("bob", "Bob")],
        )
        assert "Extractions loaded: 1" in report
        assert "Picks used: 2" in report
        assert "Independence disclosure" in report
        assert "Alice: 0.600" in report
        assert "Bob: 0.300" in report

    def test_git_all_none_handled(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "git: unavailable" in report

    def test_git_present_rendered(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        git = GitInfo(commit_sha="abc123def456", short_sha="abc123d", branch="main", dirty=False)
        report = render_report(
            record=_record(git=git),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "abc123d" in report
        assert "main" in report

    def test_input_files_listed_with_abbreviated_sha(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        inputs = [
            ExtractionInput(
                path="data/extractions/vid1.json",
                video_id="vid1",
                creator_id="alice",
                sha256="a" * 64,
                published_at=datetime(2026, 8, 15, tzinfo=UTC),
            )
        ]
        report = render_report(
            record=_record(inputs=inputs),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "a" * 12 in report
        assert "a" * 64 not in report
        assert "vid1" in report
        assert "2026-08-15" in report

    def test_input_with_no_published_at_renders_unknown_not_a_crash(self) -> None:
        """ExtractionInput.published_at defaults to None so a run.json
        written before the field existed still parses — the renderer must
        handle that gracefully, not call `.date()` on None."""
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        inputs = [
            ExtractionInput(
                path="data/extractions/vid1.json",
                video_id="vid1",
                creator_id="alice",
                sha256="a" * 64,
            )
        ]
        report = render_report(
            record=_record(inputs=inputs),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "published unknown" in report


class TestWriteReport:
    def test_writes_report_md(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run1"
        path = write_report("# hello\n", run_dir)
        assert path == run_dir / "report.md"
        assert path.read_text() == "# hello\n"


class TestExcludedBackers:
    """`_excluded_backers` re-implements `score_picks`'s one-vote-per-
    creator (strongest by absolute value) arithmetic for players that
    never reach `scores` at all. These pin it to that same semantics so
    a future edit to either side shows up as a test failure, not a
    silent drift."""

    def test_multiple_picks_from_one_creator_collapse_to_strongest_by_absolute_value(
        self,
    ) -> None:
        # alice has two picks for the same excluded player: a confident
        # squad_include and a half-hearted avoid. score_picks would keep
        # only the one further from neutral — here that's the include —
        # so this must count as ONE positive vote, not two, and not let
        # the weaker avoid win.
        weights = {"alice": 0.8}
        include_value = gameweek_mod.vote_value(0.8, PickAction.SQUAD_INCLUDE, 5)
        avoid_value = gameweek_mod.vote_value(0.8, PickAction.AVOID, 1)
        assert abs(include_value) > abs(avoid_value)  # sanity: include is strongest

        extractions = [
            _extraction(
                "alice",
                "v1",
                [
                    _pick(9001, action=PickAction.SQUAD_INCLUDE, conviction=5),
                    _pick(9001, action=PickAction.AVOID, conviction=1),
                ],
            )
        ]
        assert gameweek_mod._excluded_backers(extractions, 9001, weights) == 1

    def test_creator_whose_strongest_vote_is_negative_does_not_count_as_backer(self) -> None:
        weights = {"bob": 0.8}
        extractions = [
            _extraction("bob", "v1", [_pick(9001, action=PickAction.AVOID, conviction=5)])
        ]
        assert gameweek_mod._excluded_backers(extractions, 9001, weights) == 0

    def test_multiple_distinct_creators_sum_correctly(self) -> None:
        weights = {"alice": 0.8, "bob": 0.6, "carol": 0.5}
        extractions = [
            _extraction(
                "alice", "v1", [_pick(9001, action=PickAction.SQUAD_INCLUDE, conviction=5)]
            ),
            _extraction("bob", "v2", [_pick(9001, action=PickAction.SQUAD_INCLUDE, conviction=3)]),
            _extraction("carol", "v3", [_pick(9001, action=PickAction.AVOID, conviction=4)]),
        ]
        assert gameweek_mod._excluded_backers(extractions, 9001, weights) == 2


class TestFindPick:
    def test_no_candidates_returns_none(self) -> None:
        index = gameweek_mod._build_pick_index([])
        assert gameweek_mod._find_pick(index, "alice", 1, PickAction.SQUAD_INCLUDE) is None

    def test_prefers_highest_conviction_among_action_matching_candidates(self) -> None:
        # Two squad_include picks from the same creator for the same player,
        # at different convictions. score_picks keeps only the higher-
        # conviction vote, so _find_pick must pair that vote with THIS
        # pick's reasoning/provenance, not the first squad_include seen —
        # otherwise a strong vote gets captioned with a weaker pick's words.
        weak = _pick(1, action=PickAction.SQUAD_INCLUDE, conviction=2, reasoning="lukewarm take")
        strong = _pick(1, action=PickAction.SQUAD_INCLUDE, conviction=5, reasoning="nailed on")
        index = gameweek_mod._build_pick_index([_extraction("alice", "v1", [weak, strong])])

        pick = gameweek_mod._find_pick(index, "alice", 1, PickAction.SQUAD_INCLUDE)
        assert pick is strong

    def test_falls_back_to_highest_conviction_when_no_action_matches(self) -> None:
        low = _pick(1, action=PickAction.WATCHLIST, conviction=2, reasoning="keeping an eye")
        high = _pick(1, action=PickAction.BENCH, conviction=4, reasoning="bench fodder")
        index = gameweek_mod._build_pick_index([_extraction("alice", "v1", [low, high])])

        # the vote's action (squad_include) matches neither recorded pick
        pick = gameweek_mod._find_pick(index, "alice", 1, PickAction.SQUAD_INCLUDE)
        assert pick is high


class TestModalGameweekTieBreak:
    def test_tie_breaks_on_smallest_gameweek(self) -> None:
        extractions = [
            _extraction("alice", "v1", [], gameweek=3),
            _extraction("bob", "v2", [], gameweek=1),
        ]
        assert gameweek_mod._modal_gameweek(extractions) == 1


# ---------------------------------------------------------------------------
# Data-freshness header: bootstrap fetch age + source video date range.
# ---------------------------------------------------------------------------


class TestBootstrapFreshness:
    def test_missing_timestamp_says_unavailable_rather_than_omitting(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(bootstrap_fetched_at=None),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "FPL data: fetch time unavailable" in report

    def test_present_timestamp_renders_age_relative_to_run_start(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        # _record()'s started_at is fixed at 2026-08-19 12:00:00 UTC.
        fetched_at = datetime(2026, 8, 19, 11, 48, 0, tzinfo=UTC)  # 12 minutes earlier
        report = render_report(
            record=_record(bootstrap_fetched_at=fetched_at),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "FPL data: fetched 2026-08-19 11:48:00 UTC" in report
        assert "12 minutes before this run" in report


class TestSourceVideoRange:
    def test_range_rendered_from_extraction_published_dates(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        extractions = [
            _extraction("alice", "v1", [], published_at=datetime(2026, 8, 10, tzinfo=UTC)),
            _extraction("bob", "v2", [], published_at=datetime(2026, 8, 19, tzinfo=UTC)),
        ]
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=extractions,
            creators=[],
        )
        assert "Source videos: 2 videos published 2026-08-10 to 2026-08-19" in report

    def test_no_extractions_omits_the_line(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
        )
        assert "Source videos:" not in report


# ---------------------------------------------------------------------------
# Availability: a DOUBTFUL squad member must be flagged, not just tabulated.
# ---------------------------------------------------------------------------


def _verdict(
    player_id: int,
    availability: Availability = Availability.DOUBTFUL,
    multiplier: float = 0.5,
    chance: int | None = 50,
    status: str = "d",
) -> AvailabilityVerdict:
    return AvailabilityVerdict(
        player_id=player_id,
        availability=availability,
        multiplier=multiplier,
        reason=f"status={status!r} (doubtful), chance_of_playing_next_round={chance}",
        status=status,
        chance_of_playing_next_round=chance,
    )


class TestAvailability:
    def test_doubtful_starter_is_called_out_near_the_top(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        target = squad.starting_xi[0].player_id
        availability = {target: _verdict(target, chance=25, multiplier=0.25)}
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            availability=availability,
        )
        assert "## Availability" in report
        target_player = db.get(target)
        assert target_player is not None
        target_name = target_player.web_name
        # The callout must land BEFORE the squad tables, not only inside them.
        callout_index = report.index("## Availability")
        table_index = report.index("## Starting XI")
        assert callout_index < table_index
        assert f"{target_name}" in report[callout_index:table_index]
        assert "25%" in report[callout_index:table_index]
        assert "Every squad member is fully available." not in report

    def test_fully_available_squad_says_so_in_one_line(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            availability={},
        )
        assert "Every squad member is fully available." in report

    def test_doubtful_player_appears_in_squad_table_row(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        target = squad.starting_xi[0].player_id
        availability = {target: _verdict(target, chance=25, multiplier=0.25)}
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            availability=availability,
        )
        assert "Availability" in report  # column header
        assert "doubtful (25%, 0.25x)" in report.lower()


# ---------------------------------------------------------------------------
# Creator participation: contributing vs. eligible, non-contributors named,
# per-creator resolved/total pick counts.
# ---------------------------------------------------------------------------


class TestCreatorParticipation:
    def test_shared_channel_cohosts_are_not_reported_as_failures(self) -> None:
        """Zophar and Lateriser share The FPL Wire's channel with Pras and
        are deliberately never ingested alone — their views flow in through
        the channel's primary. Counting them as non-contributors reports
        the intended architecture as a failure."""
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"pras": 0.6})
        creators = [
            _creator("pras", "Pras", channel_id="UC_wire"),
            _creator("zophar", "Zophar", channel_id="UC_wire"),
            _creator("lateriser", "Lateriser", channel_id="UC_wire"),
        ]
        creators[0] = creators[0].model_copy(update={"channel_primary": True})

        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=creators,
        )

        # Only Pras is eligible; the co-hosts are represented, not missing.
        assert "1 of 1 eligible creators" in report
        assert "3 on the roster" in report
        assert "Not ingested by design" in report
        assert "Zophar" in report and "Lateriser" in report
        assert "Eligible but contributed nothing" not in report

    def test_creator_with_no_channel_is_reported_as_a_roster_gap(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"alice": 0.6})
        creators = [
            _creator("alice", "Alice"),
            Creator(creator_id="ghost", name="Ghost", youtube_hint="Ghost", tier=Tier.CORE),
        ]

        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=creators,
        )

        assert "No channel resolved" in report
        assert "Ghost" in report

    def test_non_contributing_eligible_creator_is_named(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"alice": 0.6})
        creators = [_creator("alice", "Alice"), _creator("ghost", "Ghost Creator")]
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=creators,
        )
        assert "1 of 2 eligible creators" in report
        assert "Ghost Creator" in report

    def test_full_participation_lists_nobody(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        record = _record(creator_weights={"alice": 0.6})
        creators = [_creator("alice", "Alice")]
        report = render_report(
            record=record,
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=creators,
        )
        assert "1 of 1 eligible creators" in report
        assert "Contributed nothing this run" not in report

    def test_per_creator_pick_counts_rendered(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        extractions = [
            _extraction("alice", "v1", [_pick(1), _pick(None)]),  # 1/2 resolved
        ]
        report = render_report(
            record=_record(creator_weights={"alice": 0.6}),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=extractions,
            creators=[_creator("alice", "Alice")],
        )
        assert "Alice: 1/2 (50%)" in report


# ---------------------------------------------------------------------------
# Squad diff: present (first section) vs. absent (omitted silently).
# ---------------------------------------------------------------------------


class TestDiffSection:
    def test_diff_is_the_first_section_when_present(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        in_id = squad.players[0].player_id
        out_id = 9999
        diff = SquadDiff(
            previous_run_id="run-old",
            players_in=[in_id],
            players_out=[out_id],
            previous_captain=None,
            new_captain=squad.players[0].player_id,
            captain_changed=True,
        )
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            diff=diff,
        )
        assert "## Squad diff vs previous run" in report
        diff_index = report.index("## Squad diff vs previous run")
        header_index = report.index("# Gameweek Report")
        table_index = report.index("## Starting XI")
        assert header_index < diff_index < table_index
        assert "run-old" in report
        assert "Captain changed" in report

    def test_diff_absent_when_none(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            diff=None,
        )
        assert "Squad diff vs previous run" not in report

    def test_diff_with_no_previous_run_says_so(self) -> None:
        pool = _basic_pool()
        db = _db(pool)
        squad = _basic_squad(pool)
        diff = SquadDiff(
            previous_run_id=None,
            players_in=sorted(p.player_id for p in squad.players),
            players_out=[],
            previous_captain=None,
            new_captain=None,
            captain_changed=False,
        )
        report = render_report(
            record=_record(),
            squad=squad,
            scores={},
            election=_empty_election(),
            db=db,
            extractions=[],
            creators=[],
            diff=diff,
        )
        assert "No previous run to diff against" in report
