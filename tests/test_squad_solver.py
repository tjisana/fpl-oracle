"""Tests for solver/squad.py — pure ILP, no network."""

from __future__ import annotations

from collections import Counter

import pytest

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.players import Player, Position
from fpl_oracle.solver.squad import (
    BUDGET_M,
    MAX_PER_CLUB,
    POSITION_QUOTA,
    SQUAD_SIZE,
    XI_SIZE,
    InfeasibleSquadError,
    build_squad,
    coefficient,
)


def _player(pid: int, position: Position, price_m: float, team_id: int) -> Player:
    return Player(
        player_id=pid,
        web_name=f"P{pid}",
        first_name="F",
        second_name=f"P{pid}",
        team_id=team_id,
        team_short=f"T{team_id}",
        position=position,
        now_cost=int(price_m * 10),
        status="a",
        chance_of_playing_next_round=None,
    )


def _pool() -> list[Player]:
    """A legal, affordable pool: plenty of each position spread over enough
    clubs that the 3-per-club rule is satisfiable."""
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


def _score(pid: int, band_score: float) -> PlayerScore:
    return PlayerScore(
        player_id=pid,
        band="X",
        weighted_votes=band_score,
        band_score=band_score,
        backers=1,
        detractors=0,
        votes=[],
    )


class TestCoefficient:
    def test_price_anchored_with_bounded_consensus_lift(self) -> None:
        p = _player(1, Position.MID, 7.0, 1)
        assert coefficient(p, None) == pytest.approx(7.0)
        assert coefficient(p, _score(1, 1.0)) == pytest.approx(8.0)

    def test_consensus_cannot_promote_a_cheap_player_past_a_premium(self) -> None:
        # THE band-safety property: full consensus is worth one band width,
        # so a maximally-backed enabler never outranks an unbacked premium.
        enabler = coefficient(_player(1, Position.GK, 4.5, 1), _score(1, 1.0))
        premium = coefficient(_player(2, Position.FWD, 15.5, 2), None)
        assert enabler < premium


class TestBuildSquad:
    def test_obeys_every_fpl_squad_rule(self) -> None:
        pool = _pool()
        squad = build_squad(pool, {}, [], 0)
        by_id = {p.player_id: p for p in pool}
        positions = Counter(by_id[p.player_id].position for p in squad.players)
        clubs = Counter(by_id[p.player_id].team_id for p in squad.players)

        assert len(squad.players) == SQUAD_SIZE
        for position, quota in POSITION_QUOTA.items():
            assert positions[position] == quota
        assert squad.total_cost_m <= BUDGET_M
        assert max(clubs.values()) <= MAX_PER_CLUB

    def test_obeys_starting_xi_formation_rules(self) -> None:
        pool = _pool()
        squad = build_squad(pool, {}, [], 0)
        by_id = {p.player_id: p for p in pool}
        xi = Counter(by_id[p.player_id].position for p in squad.starting_xi)

        assert len(squad.starting_xi) == XI_SIZE
        assert xi[Position.GK] == 1
        assert xi[Position.DEF] >= 3
        assert xi[Position.FWD] >= 1
        assert len(squad.bench) == SQUAD_SIZE - XI_SIZE

    def test_starters_are_a_subset_of_the_squad(self) -> None:
        squad = build_squad(_pool(), {}, [], 0)
        squad_ids = {p.player_id for p in squad.players}
        assert {p.player_id for p in squad.starting_xi} <= squad_ids

    def test_money_goes_to_the_xi_not_the_bench(self) -> None:
        # The reason for optimising the XI rather than the flat 15: a
        # squad-wide objective spends real budget on players who never play.
        pool = _pool()
        squad = build_squad(pool, {_p.player_id: _score(_p.player_id, 0.9) for _p in pool}, [], 0)
        bench_avg = sum(p.price_m for p in squad.bench) / len(squad.bench)
        xi_avg = sum(p.price_m for p in squad.starting_xi) / len(squad.starting_xi)
        assert bench_avg < xi_avg

    def test_consensus_breaks_ties_within_a_price(self) -> None:
        # Consensus is the tiebreaker AMONG equally-priced players — it does
        # not (and must not) let a cheap player leapfrog a dearer one. Two
        # identical £6.0m midfielders at the same club-spread; only one is
        # backed, and he must be the one picked.
        pool = [p for p in _pool() if not (p.position is Position.MID and p.price_m == 6.0)]
        backed = _player(900, Position.MID, 6.0, team_id=6)
        unbacked = _player(901, Position.MID, 6.0, team_id=7)
        pool += [backed, unbacked]

        squad = build_squad(pool, {backed.player_id: _score(backed.player_id, 1.0)}, [], 0)
        chosen = {p.player_id for p in squad.players}
        assert backed.player_id in chosen
        assert unbacked.player_id not in chosen

    def test_captain_options_are_forced_into_the_squad(self) -> None:
        pool = _pool()
        # two expensive players nobody would otherwise pick
        options = [pool[0].player_id, pool[1].player_id]
        squad = build_squad(pool, {}, options, 2)
        chosen = {p.player_id for p in squad.players}
        assert set(options) <= chosen
        assert squad.captain_options_required == 2
        assert not squad.relaxed_captaincy

    def test_captaincy_relaxes_rather_than_failing(self) -> None:
        # An option that cannot fit (a GK when both GK slots are cheaper
        # elsewhere is still fittable, so force the issue: demand more
        # options than exist).
        pool = _pool()
        squad = build_squad(pool, {}, [pool[0].player_id], 2)
        assert squad.relaxed_captaincy is True
        assert squad.captain_options_required < 2

    def test_empty_pool_raises(self) -> None:
        with pytest.raises(InfeasibleSquadError, match="empty player pool"):
            build_squad([], {}, [], 0)

    def test_genuinely_infeasible_pool_raises(self) -> None:
        # Not enough goalkeepers to satisfy the quota.
        pool = [_player(i, Position.MID, 4.0, i % 8) for i in range(1, 20)]
        with pytest.raises(InfeasibleSquadError, match="no legal 15"):
            build_squad(pool, {}, [], 0)

    def test_unaffordable_pool_raises(self) -> None:
        pool = []
        pid = 1
        for position, count in (
            (Position.GK, 3),
            (Position.DEF, 6),
            (Position.MID, 6),
            (Position.FWD, 4),
        ):
            for _ in range(count):
                pool.append(_player(pid, position, 15.0, pid % 8))  # 15 * 15.0 = £225m
                pid += 1
        with pytest.raises(InfeasibleSquadError):
            build_squad(pool, {}, [], 0)
