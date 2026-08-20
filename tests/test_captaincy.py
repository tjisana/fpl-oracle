"""Tests for consensus/captaincy.py — pure, no network."""

from __future__ import annotations

import pytest

from fpl_oracle.consensus.captaincy import (
    MAX_OPTIONS,
    SUPPORT_FLOOR_RATIO,
    elect,
    required_options,
)
from fpl_oracle.extract.schemas import PickAction

CAP = PickAction.CAPTAIN
VICE = PickAction.VICE
INC = PickAction.SQUAD_INCLUDE


class TestElect:
    def test_only_captain_and_vice_picks_count(self) -> None:
        # squad_include must contribute NOTHING here — that is the whole
        # point of a separate election. A widely-included safe player must
        # not win the armband on inclusion votes.
        election = elect(
            {"a": [(1, INC, 5), (1, PickAction.WATCHLIST, 5), (2, CAP, 5)]},
            {"a": 0.4},
        )
        assert [c.player_id for c in election.candidates] == [2]

    def test_vice_counts_less_than_captain(self) -> None:
        election = elect({"a": [(1, CAP, 5)], "b": [(2, VICE, 5)]}, {"a": 0.4, "b": 0.4})
        by_id = {c.player_id: c.score for c in election.candidates}
        assert by_id[1] > by_id[2]

    def test_weighted_by_creator_and_conviction(self) -> None:
        election = elect(
            {"strong": [(1, CAP, 5)], "weak": [(2, CAP, 1)]}, {"strong": 0.5, "weak": 0.5}
        )
        by_id = {c.player_id: c.score for c in election.candidates}
        assert by_id[1] > by_id[2]

    def test_creator_changing_their_mind_counts_once(self) -> None:
        # A creator who names two captains mid-video contributes their
        # strongest signal per player, not a doubled vote.
        one = elect({"a": [(1, CAP, 5)]}, {"a": 0.4})
        twice = elect({"a": [(1, CAP, 5), (1, CAP, 3)]}, {"a": 0.4})
        assert twice.candidates[0].score == pytest.approx(one.candidates[0].score)
        assert twice.candidates[0].voters == 1

    def test_support_floor_excludes_fringe_candidates_from_options(self) -> None:
        # THE live case: the first run had a third candidate on a single
        # low-weight vote (4% of the leader). Fringe names must not
        # constrain the whole squad.
        election = elect(
            {
                "a": [(1, CAP, 5)],
                "b": [(1, CAP, 5)],
                "c": [(1, CAP, 5)],
                "d": [(99, CAP, 1)],
            },
            {"a": 0.5, "b": 0.5, "c": 0.5, "d": 0.1},
        )
        fringe = next(c for c in election.candidates if c.player_id == 99)
        assert fringe.share < SUPPORT_FLOOR_RATIO
        assert 99 not in election.options  # reported, but never constrains
        assert 1 in election.options

    def test_options_capped(self) -> None:
        picks = {f"c{i}": [(i, CAP, 5)] for i in range(6)}
        weights = {f"c{i}": 0.4 for i in range(6)}
        election = elect(picks, weights)
        assert len(election.options) <= MAX_OPTIONS

    def test_unavailable_candidates_cannot_become_options(self) -> None:
        # An injured captain option would make the solver's >=2 constraint
        # unsatisfiable — "no squad at all on deadline morning".
        election = elect(
            {"a": [(1, CAP, 5)], "b": [(1, CAP, 5)], "c": [(2, CAP, 5)]},
            {"a": 0.4, "b": 0.4, "c": 0.4},
            eligible_player_ids={2},  # player 1 filtered out by availability
        )
        assert 1 not in election.options
        assert election.options == [2]
        # ...but he is still REPORTED, since the armband debate is useful
        assert 1 in [c.player_id for c in election.candidates]

    def test_thin_evidence_is_relative_to_contributing_creators(self) -> None:
        # 7 of 14 creators naming a captain is thin even though 7 sounds
        # like plenty — this is the live-run shape.
        picks: dict[str, list[tuple[int, PickAction, int]]] = {
            f"voter{i}": [(1, CAP, 5)] for i in range(7)
        }
        picks.update({f"silent{i}": [(2, INC, 5)] for i in range(7)})
        weights = {cid: 0.4 for cid in picks}
        election = elect(picks, weights)
        assert election.total_voters == 7
        assert election.contributing_creators == 14
        assert election.thin is True

    def test_broad_participation_is_not_thin(self) -> None:
        picks: dict[str, list[tuple[int, PickAction, int]]] = {
            f"voter{i}": [(1, CAP, 5)] for i in range(9)
        }
        picks["silent"] = [(2, INC, 5)]
        election = elect(picks, {cid: 0.4 for cid in picks})
        assert election.thin is False

    def test_no_captain_picks_at_all(self) -> None:
        election = elect({"a": [(1, INC, 5)]}, {"a": 0.4})
        assert election.candidates == []
        assert election.options == []
        assert election.thin is True


class TestRequiredOptions:
    def test_two_when_two_or_more_options(self) -> None:
        election = elect(
            {"a": [(1, CAP, 5)], "b": [(2, CAP, 5)]},
            {"a": 0.4, "b": 0.4},
        )
        assert len(election.options) == 2
        assert required_options(election) == 2

    def test_degrades_rather_than_making_the_solver_infeasible(self) -> None:
        # One credible option: demanding 2 would produce NO squad, which is
        # strictly worse than a squad with a weaker captaincy guarantee.
        single = elect({"a": [(1, CAP, 5)]}, {"a": 0.4})
        assert required_options(single) == 1
        none = elect({"a": [(1, INC, 5)]}, {"a": 0.4})
        assert required_options(none) == 0


class TestOneArmbandPerCreator:
    """A creator has ONE captain. Naming several is one opinion recorded
    several times — not several opinions. The earlier implementation
    deduped per player then summed across players, so a creator naming two
    captains funded both at full weight and appeared in the `voters` count
    for each."""

    def test_creator_naming_two_captains_funds_only_the_strongest(self) -> None:
        picks = {
            "andy": [
                (1, PickAction.CAPTAIN, 5),
                (2, PickAction.CAPTAIN, 2),
            ]
        }
        election = elect(picks, {"andy": 1.0})

        scored = {c.player_id: c for c in election.candidates}
        assert 1 in scored, "the strongest armband pick must survive"
        assert 2 not in scored, "the weaker rival must not also be funded"
        assert scored[1].voters == 1
        assert election.total_voters == 1

    def test_a_captain_pick_outranks_that_creators_own_vice_pick(self) -> None:
        picks = {"andy": [(1, PickAction.VICE, 5), (2, PickAction.CAPTAIN, 5)]}
        election = elect(picks, {"andy": 1.0})

        ids = [c.player_id for c in election.candidates]
        assert ids == [2], "a vice is a second choice, not a second vote"

    def test_distinct_creators_still_accumulate_normally(self) -> None:
        picks = {
            "andy": [(1, PickAction.CAPTAIN, 5)],
            "focal": [(1, PickAction.CAPTAIN, 5)],
        }
        election = elect(picks, {"andy": 1.0, "focal": 1.0})

        assert election.candidates[0].voters == 2
        assert election.total_voters == 2
