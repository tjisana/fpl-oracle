"""Unit tests for the FPL Draft value board.

Focused on the decisions that are easy to get wrong and silently produce a
plausible-looking but useless board: the starter-vs-rostered baseline, the
rank-orders-magnitude projection, and value-over-next-available being measured
downward rather than against the best player left.
"""

from __future__ import annotations

import pytest

from fpl_oracle.draft.board import (
    ROSTER_SLOTS,
    STARTERS,
    build_board,
    squad_needs,
    value_over_next_available,
)
from fpl_oracle.draft.expert_rankings import ALL_BOARDS, ExpertBoard
from fpl_oracle.fpl.players import Position

ELEMENT_TYPE = {Position.GK: 1, Position.DEF: 2, Position.MID: 3, Position.FWD: 4}


def _get(board, player_id: int):
    """Fetch a player and narrow away the Optional, so tests fail loudly."""
    p = board.by_id(player_id)
    assert p is not None, f"player {player_id} missing from board"
    return p


def _element(pid: int, name: str, pos: Position, rank: int, points: int, **kw) -> dict:
    return {
        "id": pid,
        "web_name": name,
        "first_name": name,
        "second_name": f"{name}son",
        "team": 1,
        "element_type": ELEMENT_TYPE[pos],
        "draft_rank": rank,
        "total_points": points,
        "minutes": kw.get("minutes", 2000),
        "status": kw.get("status", "a"),
        "news": kw.get("news", ""),
    }


def _payload(elements: list[dict]) -> dict:
    return {"elements": elements, "teams": [{"id": 1, "short_name": "TST"}]}


def _pool(per_position: int = 60) -> dict:
    """A pool big enough that every positional baseline lands on a real player."""
    els: list[dict] = []
    pid = 1
    for pos in Position:
        for i in range(per_position):
            els.append(_element(pid, f"{pos}{i:02d}", pos, rank=pid, points=300 - i * 5))
            pid += 1
    return _payload(els)


class TestBaseline:
    def test_baseline_is_the_last_starter_not_the_last_rostered(self) -> None:
        """The bug that would have you drafting a keeper in round one.

        A 10-team league rosters 2 keepers each, so a replacement-level
        baseline is the 21st keeper — a backup on near-zero points. Only one
        keeper starts, so the baseline must be the 10th.
        """
        board = build_board(_pool(), teams=10)
        gk_curve = sorted(
            (p.last_season_points for p in board.players if p.position == Position.GK),
            reverse=True,
        )
        assert board.baselines[Position.GK] == gk_curve[10 - 1]
        assert board.baselines[Position.GK] != gk_curve[20 - 1]

    def test_baseline_scales_with_league_size(self) -> None:
        small = build_board(_pool(), teams=6)
        large = build_board(_pool(), teams=12)
        # More managers -> the replacement starter is worse -> baseline drops.
        assert large.baselines[Position.FWD] < small.baselines[Position.FWD]

    def test_baselines_use_the_configured_starter_counts(self) -> None:
        board = build_board(_pool(), teams=10)
        for pos in Position:
            curve = sorted(
                (p.last_season_points for p in board.players if p.position == pos),
                reverse=True,
            )
            assert board.baselines[pos] == curve[STARTERS[pos] * 10 - 1]


class TestProjection:
    def test_rank_supplies_order_and_the_curve_supplies_magnitude(self) -> None:
        """An injured star keeps his rank's projection, not his own bad total.

        Isak was FPL's 5th-ranked player on 41 points from 694 minutes.
        Projecting him on his own total would bury him; he should inherit the
        points of the Nth-best forward instead.
        """
        els = [
            _element(1, "Fit", Position.FWD, rank=2, points=200),
            _element(2, "Injured", Position.FWD, rank=1, points=41, minutes=694),
            _element(3, "Filler", Position.FWD, rank=3, points=100),
        ]
        board = build_board(_payload(els), teams=2)
        injured = board.by_id(2)
        assert injured is not None
        # Top-ranked, so he inherits the best total in the pool.
        assert injured.position_rank == 1
        assert injured.projected_points == 200.0
        assert injured.last_season_points == 41

    def test_projection_preserves_the_positional_points_curve(self) -> None:
        board = build_board(_pool(), teams=10)
        for pos in Position:
            group = sorted(
                (p for p in board.players if p.position == pos),
                key=lambda p: p.position_rank,
            )
            curve = sorted((p.last_season_points for p in group), reverse=True)
            assert [p.projected_points for p in group] == [float(x) for x in curve]


class TestExpertBlend:
    def test_creator_rank_pulls_a_player_up_the_board(self) -> None:
        els = [
            _element(1, "Alpha", Position.MID, rank=1, points=200),
            _element(2, "Sleeper", Position.MID, rank=3, points=150),
            _element(3, "Beta", Position.MID, rank=2, points=100),
        ]
        experts = [
            ExpertBoard(
                creator="Tester",
                source_url="https://example.invalid",
                complete=False,
                ranks={"Sleeper": 1},
            )
        ]
        plain = build_board(_payload(els), teams=2)
        blended = build_board(_payload(els), teams=2, expert_boards=experts)
        assert _get(plain, 2).position_rank == 3
        assert _get(blended, 2).position_rank == 2

    def test_absence_from_a_partial_list_is_not_a_vote_against(self) -> None:
        """A top-20 that omits a player says nothing about him."""
        els = [
            _element(1, "Listed", Position.MID, rank=1, points=200),
            _element(2, "Unlisted", Position.MID, rank=2, points=150),
        ]
        experts = [
            ExpertBoard(
                creator="Tester",
                source_url="https://example.invalid",
                complete=False,
                ranks={"Listed": 1},
            )
        ]
        board = build_board(_payload(els), teams=2, expert_boards=experts)
        unlisted = board.by_id(2)
        assert unlisted is not None
        assert unlisted.expert_ranks == {}
        assert unlisted.consensus_rank == 2.0  # unchanged, FPL rank alone

    def test_unresolvable_creator_name_is_reported_never_guessed(self) -> None:
        els = [_element(1, "Real", Position.MID, rank=1, points=200)]
        experts = [
            ExpertBoard(
                creator="Tester",
                source_url="https://example.invalid",
                complete=False,
                ranks={"Nobody At All": 1},
            )
        ]
        board = build_board(_payload(els), teams=2, expert_boards=experts)
        assert board.unresolved_expert_names == {"Tester": ["Nobody At All"]}
        assert _get(board, 1).expert_ranks == {}

    def test_ambiguous_surname_is_refused_not_guessed(self) -> None:
        """Two Palmers exist in the real pool: Cole (MID) and Alex (GK).

        A bare surname must not silently promote the wrong one — that would
        hand a backup keeper a top-5 creator rank.
        """
        els = [
            {
                **_element(1, "Palmer", Position.MID, rank=4, points=200),
                "first_name": "Cole",
                "second_name": "Palmer",
            },
            {
                **_element(2, "Palmer", Position.GK, rank=570, points=5),
                "first_name": "Alex",
                "second_name": "Palmer",
            },
        ]
        ambiguous = [
            ExpertBoard(
                creator="T", source_url="https://e.invalid", complete=False, ranks={"Palmer": 4}
            )
        ]
        board = build_board(_payload(els), teams=2, expert_boards=ambiguous)
        assert board.unresolved_expert_names == {"T": ["Palmer"]}
        assert all(p.expert_ranks == {} for p in board.players)

        qualified = [
            ExpertBoard(
                creator="T",
                source_url="https://e.invalid",
                complete=False,
                ranks={"Cole Palmer": 4},
            )
        ]
        board = build_board(_payload(els), teams=2, expert_boards=qualified)
        assert board.unresolved_expert_names == {}
        assert _get(board, 1).expert_ranks == {"T": 4}
        assert _get(board, 2).expert_ranks == {}

    def test_shipped_creator_boards_resolve_against_the_real_pool(self) -> None:
        """Guards the checked-in rankings against name drift in the FPL data."""
        els, pid = [], 1
        for name in [
            "Haaland",
            "B.Fernandes",
            "Isak",
            "Palmer",
            "Semenyo",
            "Saka",
            "Gabriel",
            "Thiago",
            "Watkins",
            "Rice",
            "Gibbs-White",
            "Sesko",
            "Mbeumo",
            "Havertz",
            "Szoboszlai",
            "Calvert-Lewin",
            "Cunha",
            "Wirtz",
            "Cherki",
        ]:
            els.append(_element(pid, name, Position.MID, rank=pid, points=200 - pid))
            pid += 1
        for full in [
            "João Pedro",
            "Morgan Rogers",
            "Bruno Guimarães",
            "Gyökeres",
            "Cole Palmer",
        ]:
            first, _, last = full.rpartition(" ")
            els.append(
                {
                    **_element(pid, last or full, Position.MID, rank=pid, points=100),
                    "first_name": first or full,
                    "second_name": last or full,
                }
            )
            pid += 1
        board = build_board(_payload(els), teams=2, expert_boards=ALL_BOARDS)
        assert board.unresolved_expert_names == {}


class TestValueOverNextAvailable:
    def test_measured_against_the_man_below_not_the_best_left(self) -> None:
        """The bug caught in the first live run: VONA went negative.

        Comparing against the best available at the position makes every player
        below the top look worthless. The question is what remains if you pass.
        """
        els = [
            _element(1, "Best", Position.FWD, rank=1, points=200),
            _element(2, "Middle", Position.FWD, rank=2, points=150),
            _element(3, "Worst", Position.FWD, rank=3, points=100),
        ]
        board = build_board(_payload(els), teams=2)
        avail = board.players
        middle = board.by_id(2)
        assert middle is not None
        assert value_over_next_available(middle, avail) == pytest.approx(50.0)

    def test_last_man_at_a_position_is_worth_his_whole_projection(self) -> None:
        els = [_element(1, "Only", Position.FWD, rank=1, points=120)]
        board = build_board(_payload(els), teams=2)
        only = board.by_id(1)
        assert only is not None
        assert value_over_next_available(only, board.players) == pytest.approx(120.0)


class TestSquadNeeds:
    def test_needs_start_at_the_full_roster(self) -> None:
        assert squad_needs([]) == ROSTER_SLOTS
        assert sum(ROSTER_SLOTS.values()) == 15

    def test_needs_decrement_as_players_are_drafted(self) -> None:
        board = build_board(_pool(), teams=10)
        forwards = [p for p in board.players if p.position == Position.FWD][:3]
        need = squad_needs(forwards)
        assert need[Position.FWD] == 0
        assert need[Position.MID] == ROSTER_SLOTS[Position.MID]


class TestGuards:
    def test_rejects_a_nonsense_league_size(self) -> None:
        with pytest.raises(ValueError, match="at least 2 teams"):
            build_board(_pool(), teams=1)

    def test_empty_pool_does_not_crash(self) -> None:
        board = build_board(_payload([]), teams=10)
        assert board.players == []
        assert all(v == 0.0 for v in board.baselines.values())
