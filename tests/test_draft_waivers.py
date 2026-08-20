"""Unit tests for the in-season waiver-wire recommender.

Focused on the decisions that are easy to get wrong: the legal-formation
enumeration in `best_xi` (never an illegal shape, never silently picking an
infeasible one), the XI-delta (not draft-day value) definition of a claim's
worth, the same-position-swap constraint `recommend_claims` applies, and the
fitness/trade filters on the live-data helpers.
"""

from __future__ import annotations

import pytest

from fpl_oracle.draft.board import DraftPlayer
from fpl_oracle.draft.waivers import (
    MIN_GAIN_WORTH_PRIORITY,
    best_xi,
    evaluate_claim,
    free_agents_from_element_status,
    recommend_claims,
    squad_from_element_status,
)
from fpl_oracle.fpl.players import Position


def _player(pid: int, name: str, pos: Position, points: float, status: str = "a") -> DraftPlayer:
    return DraftPlayer(
        player_id=pid,
        web_name=name,
        first_name=name,
        second_name=f"{name}son",
        team_short="TST",
        position=pos,
        fpl_draft_rank=pid,
        last_season_points=int(points),
        minutes=2000,
        status=status,
        projected_points=points,
    )


class TestBestXI:
    def test_picks_the_legal_optimal_formation(self) -> None:
        """3-4-3 should be forced/correct when DEF is scarce but MID/FWD are deep."""
        squad = [
            _player(1, "GK1", Position.GK, 50),
            _player(2, "GK2", Position.GK, 10),
            _player(3, "D1", Position.DEF, 40),
            _player(4, "D2", Position.DEF, 35),
            _player(5, "D3", Position.DEF, 30),
            _player(6, "M1", Position.MID, 60),
            _player(7, "M2", Position.MID, 55),
            _player(8, "M3", Position.MID, 50),
            _player(9, "M4", Position.MID, 45),
            _player(10, "M5", Position.MID, 40),
            _player(11, "F1", Position.FWD, 70),
            _player(12, "F2", Position.FWD, 65),
            _player(13, "F3", Position.FWD, 60),
        ]
        xi = best_xi(squad)
        assert xi.formation == {Position.GK: 1, Position.DEF: 3, Position.MID: 4, Position.FWD: 3}
        assert len(xi.starters) == 11
        assert len(xi.bench) == len(squad) - 11

    def test_exactly_one_gk_even_with_multiple_available(self) -> None:
        squad = [
            _player(1, "GK1", Position.GK, 80),
            _player(2, "GK2", Position.GK, 70),
            _player(3, "D1", Position.DEF, 40),
            _player(4, "D2", Position.DEF, 35),
            _player(5, "D3", Position.DEF, 30),
            _player(6, "M1", Position.MID, 60),
            _player(7, "M2", Position.MID, 55),
            _player(8, "M3", Position.MID, 50),
            _player(9, "M4", Position.MID, 45),
            _player(10, "M5", Position.MID, 40),
            _player(11, "F1", Position.FWD, 70),
            _player(12, "F2", Position.FWD, 65),
            _player(13, "F3", Position.FWD, 60),
        ]
        xi = best_xi(squad)
        gk_starters = [p for p in xi.starters if p.position == Position.GK]
        assert len(gk_starters) == 1
        assert gk_starters[0].player_id == 1  # the higher-projected keeper
        assert xi.formation[Position.GK] == 1
        # the backup keeper must be benched, never started as a second GK
        assert any(p.player_id == 2 for p in xi.bench)

    def test_two_mid_is_the_forced_minimum(self) -> None:
        """With only 2 MID in the squad, the formation must use MID=2."""
        squad = [
            _player(1, "GK1", Position.GK, 50),
            _player(2, "D1", Position.DEF, 40),
            _player(3, "D2", Position.DEF, 35),
            _player(4, "D3", Position.DEF, 30),
            _player(5, "D4", Position.DEF, 25),
            _player(6, "D5", Position.DEF, 20),
            _player(7, "M1", Position.MID, 60),
            _player(8, "M2", Position.MID, 55),
            _player(9, "F1", Position.FWD, 70),
            _player(10, "F2", Position.FWD, 65),
            _player(11, "F3", Position.FWD, 60),
        ]
        xi = best_xi(squad)
        assert xi.formation[Position.MID] == 2
        mid_starters = [p for p in xi.starters if p.position == Position.MID]
        assert len(mid_starters) == 2

    def test_raises_on_a_squad_that_cannot_fill_any_formation(self) -> None:
        squad = [
            _player(1, "GK1", Position.GK, 50),
            _player(2, "M1", Position.MID, 60),
            _player(3, "F1", Position.FWD, 70),
        ]
        with pytest.raises(ValueError, match="no legal starting XI formation"):
            best_xi(squad)


def _base_squad() -> list[DraftPlayer]:
    """A minimal legal squad: 2 GK, 5 DEF, 5 MID, 3 FWD, enough to field 1-3-4-3."""
    return [
        _player(1, "GK1", Position.GK, 50),
        _player(2, "GK2", Position.GK, 10),
        _player(3, "D1", Position.DEF, 40),
        _player(4, "D2", Position.DEF, 35),
        _player(5, "D3", Position.DEF, 30),
        _player(6, "D4", Position.DEF, 10),
        _player(7, "D5", Position.DEF, 5),
        _player(8, "M1", Position.MID, 60),
        _player(9, "M2", Position.MID, 55),
        _player(10, "M3", Position.MID, 50),
        _player(11, "M4", Position.MID, 45),
        _player(12, "M5", Position.MID, 10),
        _player(13, "F1", Position.FWD, 70),
        _player(14, "F2", Position.FWD, 65),
        _player(15, "F3", Position.FWD, 20),
    ]


class TestEvaluateClaim:
    def test_bench_only_improvement_scores_zero_gain(self) -> None:
        """A free agent that only beats the bench doesn't crack the XI."""
        squad = _base_squad()
        # D5 (5 pts, on the bench in the base formation) is the weakest DEF.
        drop = next(p for p in squad if p.player_id == 7)  # D5, 5 pts
        # Better than the bench DEF (5) but worse than the weakest STARTER DEF (30).
        add = _player(99, "NewDef", Position.DEF, 15)
        evaluation = evaluate_claim(squad, add, drop)
        assert evaluation.gain == 0
        assert evaluation.changes_xi is False

    def test_claim_that_improves_the_xi_scores_the_true_delta(self) -> None:
        squad = _base_squad()
        before = best_xi(squad)
        drop = next(p for p in squad if p.player_id == 15)  # F3, 20 pts (a starter)
        add = _player(99, "NewFwd", Position.FWD, 90)
        evaluation = evaluate_claim(squad, add, drop)
        new_squad = [p for p in squad if p.player_id != 15] + [add]
        after = best_xi(new_squad)
        expected_gain = after.total_points - before.total_points
        assert evaluation.gain == pytest.approx(expected_gain)
        assert expected_gain == pytest.approx(90 - 20)
        assert evaluation.changes_xi is True

    def test_worth_priority_threshold(self) -> None:
        squad = _base_squad()
        drop = next(p for p in squad if p.player_id == 15)  # F3, 20 pts (a starter)

        small_gain_add = _player(98, "SmallGain", Position.FWD, 21)  # +1, below threshold
        small = evaluate_claim(squad, small_gain_add, drop)
        assert small.gain == pytest.approx(1.0)
        assert small.gain < MIN_GAIN_WORTH_PRIORITY
        assert small.worth_priority is False
        assert small.note != ""

        big_gain_add = _player(97, "BigGain", Position.FWD, 25)  # +5, above threshold
        big = evaluate_claim(squad, big_gain_add, drop)
        assert big.gain >= MIN_GAIN_WORTH_PRIORITY
        assert big.worth_priority is True
        assert big.note == ""


class TestRecommendClaims:
    def test_excludes_unavailable_free_agents_but_includes_and_flags_doubt(self) -> None:
        squad = _base_squad()
        free_agents = [
            _player(101, "Unavailable", Position.FWD, 200, status="u"),
            _player(102, "Injured", Position.FWD, 200, status="i"),
            _player(103, "Suspended", Position.FWD, 200, status="s"),
            _player(104, "Doubtful", Position.FWD, 200, status="d"),
        ]
        claims = recommend_claims(squad, free_agents)
        added_ids = {c.add.player_id for c in claims}
        assert 101 not in added_ids
        assert 102 not in added_ids
        assert 103 not in added_ids
        assert 104 in added_ids
        doubt_claim = next(c for c in claims if c.add.player_id == 104)
        assert doubt_claim.availability_flag == "DOUBT"

    def test_pairs_only_within_the_same_position(self) -> None:
        squad = _base_squad()
        free_agents = [_player(101, "BigFwd", Position.FWD, 500)]
        claims = recommend_claims(squad, free_agents)
        assert all(c.drop.position == Position.FWD for c in claims)

    def test_empty_free_agent_pool_does_not_crash(self) -> None:
        squad = _base_squad()
        assert recommend_claims(squad, []) == []

    def test_results_sorted_best_gain_first(self) -> None:
        squad = _base_squad()
        free_agents = [
            _player(101, "SmallUpgrade", Position.FWD, 25),
            _player(102, "BigUpgrade", Position.FWD, 300),
        ]
        claims = recommend_claims(squad, free_agents)
        assert len(claims) >= 2
        gains = [c.gain for c in claims]
        assert gains == sorted(gains, reverse=True)


class TestElementStatusHelpers:
    def _status_list(self) -> list[dict]:
        return [
            {"element": 1, "owner": 199528, "status": "o", "in_accepted_trade": False},
            {"element": 2, "owner": 199528, "status": "o", "in_accepted_trade": False},
            {"element": 3, "owner": 300000, "status": "o", "in_accepted_trade": False},
            {"element": 4, "owner": None, "status": "a", "in_accepted_trade": False},
            {"element": 5, "owner": None, "status": "a", "in_accepted_trade": False},
            {"element": 6, "owner": None, "status": "l", "in_accepted_trade": False},
            {"element": 7, "owner": None, "status": "a", "in_accepted_trade": True},
            {"element": 8, "owner": 199528, "status": "o", "in_accepted_trade": True},
            {"element": 999, "owner": 199528, "status": "o", "in_accepted_trade": False},
        ]

    def _players_by_id(self) -> dict[int, DraftPlayer]:
        return {
            1: _player(1, "Mine1", Position.GK, 50),
            2: _player(2, "Mine2", Position.DEF, 40),
            3: _player(3, "Rival", Position.DEF, 40),
            4: _player(4, "Free1", Position.MID, 30),
            5: _player(5, "Free2", Position.FWD, 20),
            6: _player(6, "Locked", Position.FWD, 20),
            7: _player(7, "TradeFree", Position.FWD, 20),
            8: _player(8, "TradeMine", Position.FWD, 20),
            # note: 999 deliberately absent from players_by_id
        }

    def test_squad_from_element_status_splits_by_owner(self) -> None:
        squad = squad_from_element_status(self._status_list(), self._players_by_id(), 199528)
        ids = {p.player_id for p in squad}
        assert ids == {1, 2}  # not player 3 (different owner), not 8 (in_accepted_trade)

    def test_free_agents_from_element_status_only_owner_none_status_a(self) -> None:
        agents = free_agents_from_element_status(self._status_list(), self._players_by_id())
        ids = {p.player_id for p in agents}
        assert ids == {4, 5}  # not 6 (locked), not 7 (in_accepted_trade)

    def test_missing_bootstrap_entry_is_skipped_not_crashed(self) -> None:
        # element 999 is in element_status but absent from players_by_id
        squad = squad_from_element_status(self._status_list(), self._players_by_id(), 199528)
        assert 999 not in {p.player_id for p in squad}
