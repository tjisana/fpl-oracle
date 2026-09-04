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
from fpl_oracle.solver.wildcard import best_xi, squad_objective, wildcard_gap

# A real FPL squad's position shape: 2 GK, 5 DEF, 5 MID, 3 FWD.
_LEGAL_POSITIONS: list[Position] = (
    [Position.GK] * 2 + [Position.DEF] * 5 + [Position.MID] * 5 + [Position.FWD] * 3
)


def _player(pid: int, pos: Position = Position.MID, price_m: float = 4.0, team: int = 1) -> Player:
    return Player(
        player_id=pid,
        web_name=f"P{pid}",
        first_name=f"P{pid}",
        second_name=f"P{pid}",
        team_id=team,
        team_short=f"T{team}",
        position=pos,
        now_cost=int(price_m * 10),
        status="a",
        chance_of_playing_next_round=None,
    )


def _db(players: list[Player]) -> PlayerDB:
    by_id = {p.player_id: p for p in players}
    team_full_names = {p.team_id: p.team_short for p in players}
    metaphones = {p.player_id: ("", "") for p in players}
    return PlayerDB(players=by_id, team_full_names=team_full_names, player_metaphones=metaphones)


def _score(pid: int, band_score: float = 0.0, backers: int = 1, detractors: int = 0) -> PlayerScore:
    return PlayerScore(
        player_id=pid,
        band="MID-4",
        weighted_votes=backers * 1.0,
        band_score=band_score,
        backers=backers,
        detractors=detractors,
        votes=[],
    )


def _owned(pid: int) -> OwnedPlayer:
    # `position` and `is_starting` are placeholders here: wildcard_gap
    # ignores `is_starting` entirely (see the module docstring) and
    # re-derives position from `db.get(pid)`, not from this field.
    return OwnedPlayer(
        player_id=pid,
        web_name=f"P{pid}",
        position=Position.MID,
        is_starting=True,
        is_captain=False,
        is_vice_captain=False,
        multiplier=1,
        purchase_price=None,
        selling_price=None,
    )


def _squad_player(pid: int, price_m: float = 5.0) -> SquadPlayer:
    # `position`, `band_score`, `coefficient` and `starting` are all
    # placeholders: wildcard_gap re-derives everything it needs from
    # `db`/`scores`/`verdicts` via the player_id alone.
    return SquadPlayer(
        player_id=pid,
        starting=True,
        price_m=price_m,
        position=Position.MID,
        band_score=0.0,
        coefficient=0.0,
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


def _legal_squad(ids: list[int], band_score: float = 0.0) -> list[Player]:
    """15 players shaped like a real FPL squad: 2 GK, 5 DEF, 5 MID, 3 FWD."""
    assert len(ids) == 15
    return [_player(pid, pos=pos) for pid, pos in zip(ids, _LEGAL_POSITIONS, strict=True)]


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

    def test_excluded_player_at_multiplier_zero_contributes_zero(self) -> None:
        # A player can carry a strong PlayerScore and still be worth
        # nothing here once they're ruled out: EXCLUDED always ships with
        # multiplier 0.0, which must zero the whole coefficient out
        # regardless of price or band_score.
        players = [_player(1, price_m=12.0)]
        db = _db(players)
        scores = {1: _score(1, band_score=0.9)}
        verdicts = {1: _verdict(1, 0.0)}
        assert squad_objective({1}, set(), db, scores, verdicts) == 0.0
        assert squad_objective(set(), {1}, db, scores, verdicts) == 0.0


class TestBestXi:
    def test_maximises_total_and_breaks_ties_on_lower_id(self) -> None:
        # GK 1 and 2 tie at the top — 1 must win.
        # DEF 11-15 all tie at 4.0 — the top 4 by (coef, id) start, 15 benches.
        # MID 21-25 all tie at 4.0 — deep enough that filling MID to 5 and
        # DEF to only 4 beats filling DEF to 5 and MID to 4 (both cost one
        # slot to a 4.0 player, so it's a pure tie — the loop order
        # (DEF outer, MID inner) means DEF=4/MID=5 is found first and
        # never displaced by the later, equal-value DEF=5/MID=4).
        # FWD 31/32 tie at 3.0 (weaker than the 4.0 uniform value, so only
        # one forward is ever worth starting) — 31 must win the tie; 33/34
        # at 1.0 never make the cut.
        players = (
            [_player(1, Position.GK, price_m=4.0), _player(2, Position.GK, price_m=4.0)]
            + [_player(pid, Position.DEF, price_m=4.0) for pid in (11, 12, 13, 14, 15)]
            + [_player(pid, Position.MID, price_m=4.0) for pid in (21, 22, 23, 24, 25)]
            + [_player(31, Position.FWD, price_m=3.0), _player(32, Position.FWD, price_m=3.0)]
            + [_player(33, Position.FWD, price_m=1.0), _player(34, Position.FWD, price_m=1.0)]
        )
        db = _db(players)
        ids = {p.player_id for p in players}

        starting, bench = best_xi(ids, db, {}, {})

        assert starting == {1, 11, 12, 13, 14, 21, 22, 23, 24, 25, 31}
        assert bench == {2, 15, 32, 33, 34}
        assert len(starting) == 11

    def test_unknown_ids_are_ignored(self) -> None:
        ids15 = list(range(1, 16))
        db = _db(_legal_squad(ids15))
        starting, bench = best_xi({*ids15, 9999}, db, {}, {})
        assert 9999 not in starting
        assert 9999 not in bench
        assert starting | bench == set(ids15)

    def test_no_gk_means_nothing_can_start(self) -> None:
        players = [_player(pid, Position.DEF) for pid in (1, 2, 3, 4, 5)]
        db = _db(players)
        ids = {p.player_id for p in players}
        starting, bench = best_xi(ids, db, {}, {})
        assert starting == set()
        assert bench == ids

    def test_no_legal_formation_benches_everyone_including_the_gk(self) -> None:
        # A single GK and nothing else: no outfield formation can ever
        # reach 10, so even the lone GK has nowhere legal to start.
        players = [_player(1, Position.GK)]
        db = _db(players)
        starting, bench = best_xi({1}, db, {}, {})
        assert starting == set()
        assert bench == {1}


class TestWildcardGap:
    def test_identical_squads_yield_zero_gap_and_full_keep(self) -> None:
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores = {pid: _score(pid) for pid in ids}
        owned = [_owned(pid) for pid in ids]
        solver_squad = _squad([_squad_player(pid) for pid in ids])

        gap = wildcard_gap(
            owned=owned,
            solver_squad=solver_squad,
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
            my_squad_value_m=100.0,
        )

        assert gap.gain == 0.0
        assert gap.my_objective == gap.solver_objective
        assert gap.kept_count == 15
        assert gap.changes_needed == 0
        assert gap.kept_player_ids == ids
        assert gap.my_squad_value_m == 100.0
        assert gap.solver_squad_cost_m == solver_squad.total_cost_m

    def test_player_absent_from_db_contributes_zero_and_does_not_raise(self) -> None:
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores = {pid: _score(pid) for pid in ids}
        # The owner's squad carries an extra id the DB has never heard of
        # (e.g. a departed player) alongside their real 15.
        owned = [_owned(pid) for pid in (*ids, 999)]
        solver_squad = _squad([_squad_player(pid) for pid in ids])

        gap = wildcard_gap(
            owned=owned,
            solver_squad=solver_squad,
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
            my_squad_value_m=100.0,
        )

        # The unknown id contributes nothing, so the two otherwise-identical
        # squads still score identically.
        assert gap.gain == 0.0
        assert gap.kept_player_ids == ids
        assert gap.kept_count == 15
        assert gap.changes_needed == 0

    def test_changes_needed_positive_when_squads_share_nothing(self) -> None:
        owned_ids = list(range(1, 16))
        solver_ids = list(range(101, 116))
        db = _db(_legal_squad(owned_ids) + _legal_squad(solver_ids))
        scores = {pid: _score(pid) for pid in (*owned_ids, *solver_ids)}

        gap = wildcard_gap(
            owned=[_owned(pid) for pid in owned_ids],
            solver_squad=_squad([_squad_player(pid) for pid in solver_ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
            my_squad_value_m=100.0,
        )

        assert gap.kept_count == 0
        assert gap.changes_needed == 15

    def test_negative_gain_when_owned_squad_scores_higher(self) -> None:
        owned_ids = list(range(1, 16))
        solver_ids = list(range(101, 116))
        db = _db(_legal_squad(owned_ids) + _legal_squad(solver_ids))
        # The owner's squad is much better backed than the solver's here
        # (a contrived setup — never true when both draw from the same
        # scoreboard — but it pins that a negative gain is representable
        # and doesn't blow up the transfers-equivalent guard).
        scores = {pid: _score(pid, band_score=0.9) for pid in owned_ids}
        scores.update({pid: _score(pid, band_score=0.0) for pid in solver_ids})
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
            owned=[_owned(pid) for pid in owned_ids],
            solver_squad=_squad([_squad_player(pid) for pid in solver_ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=rec,
            my_squad_value_m=100.0,
        )

        assert gap.gain < 0
        # Guarded on gain > 0 as well as best > 0 — a positive best-single
        # -transfer delta must not leak a transfers_equivalent when the
        # owner is already ahead.
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_none_when_no_transfer_rec(self) -> None:
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores = {pid: _score(pid) for pid in ids}
        gap = wildcard_gap(
            owned=[_owned(pid) for pid in ids],
            solver_squad=_squad([_squad_player(pid) for pid in ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
            my_squad_value_m=100.0,
        )
        assert gap.best_single_transfer_delta is None
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_none_when_delta_distribution_none(self) -> None:
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores = {pid: _score(pid) for pid in ids}
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
            owned=[_owned(pid) for pid in ids],
            solver_squad=_squad([_squad_player(pid) for pid in ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=rec,
            my_squad_value_m=100.0,
        )
        assert gap.best_single_transfer_delta is None
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_none_when_best_not_positive(self) -> None:
        owned_ids = list(range(1, 16))
        solver_ids = list(range(101, 116))
        db = _db(_legal_squad(owned_ids) + _legal_squad(solver_ids))
        scores = {pid: _score(pid) for pid in (*owned_ids, *solver_ids)}
        scores.update({pid: _score(pid, band_score=0.5) for pid in solver_ids})
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
            owned=[_owned(pid) for pid in owned_ids],
            solver_squad=_squad([_squad_player(pid) for pid in solver_ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=rec,
            my_squad_value_m=100.0,
        )
        assert gap.best_single_transfer_delta == 0.0
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_none_when_gain_not_positive(self) -> None:
        # Identical squads (gain == 0) with a transfer_rec that DOES carry
        # a positive best delta: the gain > 0 guard, not just best > 0,
        # must be what suppresses transfers_equivalent here.
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores = {pid: _score(pid) for pid in ids}
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
            owned=[_owned(pid) for pid in ids],
            solver_squad=_squad([_squad_player(pid) for pid in ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=rec,
            my_squad_value_m=100.0,
        )
        assert gap.gain == 0.0
        assert gap.best_single_transfer_delta == 0.5
        assert gap.transfers_equivalent is None

    def test_transfers_equivalent_computed_when_gain_and_best_positive(self) -> None:
        owned_ids = list(range(1, 16))
        solver_ids = list(range(101, 116))
        db = _db(_legal_squad(owned_ids) + _legal_squad(solver_ids))
        scores = {pid: _score(pid, band_score=0.0) for pid in owned_ids}
        scores.update({pid: _score(pid, band_score=0.9) for pid in solver_ids})
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
            owned=[_owned(pid) for pid in owned_ids],
            solver_squad=_squad([_squad_player(pid) for pid in solver_ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=rec,
            my_squad_value_m=100.0,
        )

        assert gap.gain > 0
        assert gap.best_single_transfer_delta == 0.5
        assert gap.transfers_equivalent == gap.gain / 0.5

    def test_unbacked_owned_ids_flags_no_score_and_zero_backers(self) -> None:
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores = {1: _score(1, backers=2)}  # backed
        scores[2] = _score(2, backers=0)  # scored but no backers
        # id 3 has no PlayerScore entry at all

        gap = wildcard_gap(
            owned=[_owned(pid) for pid in ids],
            solver_squad=_squad([_squad_player(pid) for pid in ids]),
            db=db,
            scores=scores,
            verdicts={},
            transfer_rec=None,
            my_squad_value_m=100.0,
        )

        assert 2 in gap.unbacked_owned_ids
        assert 3 in gap.unbacked_owned_ids
        assert 1 not in gap.unbacked_owned_ids

    def test_unbacked_owned_ids_excludes_excluded_players(self) -> None:
        # An EXCLUDED (injured/suspended) player never earns a PlayerScore
        # either, but that's a fact about availability, not an absence of
        # creator backing — they must not appear in the unbacked list.
        ids = list(range(1, 16))
        db = _db(_legal_squad(ids))
        scores: dict[int, PlayerScore] = {}  # nobody has a score this week
        verdicts = {2: _verdict(2, 0.0)}  # id 2 is EXCLUDED

        gap = wildcard_gap(
            owned=[_owned(pid) for pid in ids],
            solver_squad=_squad([_squad_player(pid) for pid in ids]),
            db=db,
            scores=scores,
            verdicts=verdicts,
            transfer_rec=None,
            my_squad_value_m=100.0,
        )

        assert 2 not in gap.unbacked_owned_ids
        assert 1 in gap.unbacked_owned_ids
