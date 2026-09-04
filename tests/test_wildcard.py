"""Wildcard gap tests.

Pure arithmetic module — every test builds its own tiny Player/PlayerDB/
PlayerScore/AvailabilityVerdict fixtures directly, mirroring
tests/test_transfers.py's pattern, rather than going through the pipeline.
"""

from __future__ import annotations

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict
from fpl_oracle.fpl.entry import OwnedPlayer
from fpl_oracle.fpl.players import Player, PlayerDB, Position
from fpl_oracle.solver.squad import BENCH_WEIGHT, Squad, SquadPlayer, coefficient
from fpl_oracle.solver.transfers import DeltaDistribution, TransferRecommendation, Verdict
from fpl_oracle.solver.wildcard import squad_objective, wildcard_gap


def _player(pid: int, pos: Position = Position.MID, cost: int = 50, team: int = 1) -> Player:
    return Player(
        player_id=pid,
        web_name=f"P{pid}",
        first_name=f"P{pid}",
        second_name=f"P{pid}",
        team_id=team,
        team_short=f"T{team}",
        position=pos,
        now_cost=cost,
        status="a",
        chance_of_playing_next_round=None,
    )


def _db(players: list[Player]) -> PlayerDB:
    by_id = {p.player_id: p for p in players}
    team_full_names = {p.team_id: p.team_short for p in players}
    metaphones = {p.player_id: ("", "") for p in players}
    return PlayerDB(players=by_id, team_full_names=team_full_names, player_metaphones=metaphones)


def _score(pid: int, band_score: float = 0.5, backers: int = 1, detractors: int = 0) -> PlayerScore:
    return PlayerScore(
        player_id=pid,
        band="MID-5",
        weighted_votes=backers * 1.0,
        band_score=band_score,
        backers=backers,
        detractors=detractors,
        votes=[],
    )


def _owned(pid: int, is_starting: bool = True, pos: Position = Position.MID) -> OwnedPlayer:
    return OwnedPlayer(
        player_id=pid,
        web_name=f"P{pid}",
        position=pos,
        is_starting=is_starting,
        is_captain=False,
        is_vice_captain=False,
        multiplier=1 if is_starting else 0,
        purchase_price=None,
        selling_price=None,
    )


def _squad_player(
    pid: int, starting: bool = True, price_m: float = 5.0, band_score: float = 0.5
) -> SquadPlayer:
    player = _player(pid, cost=int(price_m * 10))
    coef = coefficient(player, _score(pid, band_score=band_score))
    return SquadPlayer(
        player_id=pid,
        starting=starting,
        price_m=price_m,
        position=Position.MID,
        band_score=band_score,
        coefficient=coef,
    )


def _squad(players: list[SquadPlayer]) -> Squad:
    return Squad(
        players=players,
        total_cost_m=sum(p.price_m for p in players),
        objective=0.0,
        captain_options_included=[],
        captain_options_required=0,
        relaxed_captaincy=False,
    )


def _verdict(pid: int, multiplier: float) -> AvailabilityVerdict:
    return AvailabilityVerdict(
        player_id=pid,
        availability=Availability.AVAILABLE if multiplier > 0 else Availability.EXCLUDED,
        multiplier=multiplier,
        reason="test",
        status="a",
        chance_of_playing_next_round=None,
    )


class TestSquadObjective:
    def test_starting_players_count_at_full_weight(self) -> None:
        players = [_player(1)]
        db = _db(players)
        scores = {1: _score(1, band_score=0.5)}
        expected = coefficient(players[0], scores[1])
        assert squad_objective({1}, set(), db, scores, {}) == expected

    def test_bench_players_count_at_bench_weight(self) -> None:
        players = [_player(1)]
        db = _db(players)
        scores = {1: _score(1, band_score=0.5)}
        expected = coefficient(players[0], scores[1]) * BENCH_WEIGHT
        assert squad_objective(set(), {1}, db, scores, {}) == expected

    def test_unknown_player_id_contributes_zero_and_does_not_raise(self) -> None:
        db = _db([_player(1)])
        result = squad_objective({999}, set(), db, {}, {})
        assert result == 0.0

    def test_availability_multiplier_is_applied(self) -> None:
        players = [_player(1)]
        db = _db(players)
        scores = {1: _score(1, band_score=0.5)}
        verdicts = {1: _verdict(1, 0.75)}
        expected = coefficient(players[0], scores[1], 0.75)
        assert squad_objective({1}, set(), db, scores, verdicts) == expected

    def test_missing_verdict_defaults_to_full_multiplier(self) -> None:
        players = [_player(1)]
        db = _db(players)
        scores = {1: _score(1, band_score=0.5)}
        assert squad_objective({1}, set(), db, scores, {}) == squad_objective(
            {1}, set(), db, scores, {1: _verdict(1, 1.0)}
        )


class TestWildcardGap:
    def test_identical_squads_yield_zero_gap_and_full_keep(self) -> None:
        owned = [_owned(pid) for pid in range(1, 16)]
        squad_players = [_squad_player(pid) for pid in range(1, 16)]
        db = _db([_player(pid) for pid in range(1, 16)])
        scores = {pid: _score(pid) for pid in range(1, 16)}

        gap = wildcard_gap(
            owned=owned,
            solver_squad=_squad(squad_players),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
        )

        assert gap.gain == 0.0
        assert gap.my_objective == gap.solver_objective
        assert gap.kept_count == 15
        assert gap.changes_needed == 0
        assert gap.kept_player_ids == list(range(1, 16))

    def test_player_absent_from_db_contributes_zero_and_does_not_raise(self) -> None:
        owned = [_owned(1), _owned(999)]  # 999 not in the DB
        player1 = _player(1)
        db = _db([player1])
        scores = {1: _score(1)}
        squad_players = [_squad_player(1)]

        gap = wildcard_gap(
            owned=owned,
            solver_squad=_squad(squad_players),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
        )

        assert gap.my_objective == coefficient(player1, scores[1])

    def test_bench_weighted_at_bench_weight_in_the_gap(self) -> None:
        owned = [_owned(1, is_starting=False)]
        player1 = _player(1)
        db = _db([player1])
        scores = {1: _score(1, band_score=0.5)}

        gap = wildcard_gap(
            owned=owned,
            solver_squad=_squad([]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
        )

        assert gap.my_objective == coefficient(player1, scores[1]) * BENCH_WEIGHT

    def test_transfers_equivalent_none_when_no_transfer_rec(self) -> None:
        gap = wildcard_gap(
            owned=[_owned(1)],
            solver_squad=_squad([_squad_player(1)]),
            db=_db([_player(1)]),
            scores={1: _score(1)},
            verdicts={},
            transfer_rec=None,
        )
        assert gap.best_single_transfer_delta is None
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_none_when_delta_distribution_none(self) -> None:
        rec = TransferRecommendation(
            verdict=Verdict.ROLL,
            gameweek=1,
            free_transfers=1,
            recommended=None,
            shortlist=[],
            forced_moves=[],
            delta_distribution=None,
            reasoning="test",
            pricing_note="test",
        )
        gap = wildcard_gap(
            owned=[_owned(1)],
            solver_squad=_squad([_squad_player(1)]),
            db=_db([_player(1)]),
            scores={1: _score(1)},
            verdicts={},
            transfer_rec=rec,
        )
        assert gap.best_single_transfer_delta is None
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_none_when_best_not_positive(self) -> None:
        rec = TransferRecommendation(
            verdict=Verdict.ROLL,
            gameweek=1,
            free_transfers=1,
            recommended=None,
            shortlist=[],
            forced_moves=[],
            delta_distribution=DeltaDistribution(
                moves_considered=1,
                best=0.0,
                median=0.0,
                p90=0.0,
                bar_applied=0.5,
                cleared_bar=False,
            ),
            reasoning="test",
            pricing_note="test",
        )
        gap = wildcard_gap(
            owned=[_owned(1)],
            solver_squad=_squad([_squad_player(1)]),
            db=_db([_player(1)]),
            scores={1: _score(1)},
            verdicts={},
            transfer_rec=rec,
        )
        assert gap.best_single_transfer_delta == 0.0
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_computed_when_best_positive(self) -> None:
        owned = [_owned(pid) for pid in (1, 2)]
        solver_players = [_squad_player(pid, band_score=0.9) for pid in (3, 4)]
        db = _db([_player(pid) for pid in (1, 2, 3, 4)])
        scores = {pid: _score(pid, band_score=0.9) for pid in (1, 2, 3, 4)}
        rec = TransferRecommendation(
            verdict=Verdict.ROLL,
            gameweek=1,
            free_transfers=1,
            recommended=None,
            shortlist=[],
            forced_moves=[],
            delta_distribution=DeltaDistribution(
                moves_considered=1, best=0.5, median=0.5, p90=0.5, bar_applied=0.5, cleared_bar=True
            ),
            reasoning="test",
            pricing_note="test",
        )

        gap = wildcard_gap(
            owned=owned,
            solver_squad=_squad(solver_players),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=rec,
        )

        assert gap.best_single_transfer_delta == 0.5
        assert gap.transfers_equivalent == gap.gain / 0.5

    def test_unbacked_owned_ids_flags_no_score_and_zero_backers(self) -> None:
        owned = [_owned(1), _owned(2), _owned(3)]
        db = _db([_player(pid) for pid in (1, 2, 3)])
        scores = {
            1: _score(1, backers=2),  # backed
            2: _score(2, backers=0),  # scored but no backers
            # 3 has no PlayerScore entry at all
        }

        gap = wildcard_gap(
            owned=owned,
            solver_squad=_squad([]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
        )

        assert gap.unbacked_owned_ids == [2, 3]
