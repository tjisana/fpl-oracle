"""Tests for consensus/scoring.py — pure, no network, no data/ reads."""

from __future__ import annotations

import pytest

from fpl_oracle.consensus.scoring import (
    ACTION_VALUE,
    BAND_SHRINKAGE,
    CONVICTION_FACTOR,
    band_of,
    score_picks,
    vote_value,
)
from fpl_oracle.extract.schemas import PickAction
from fpl_oracle.fpl.players import Player, Position


def _player(player_id: int, price_m: float, position: Position = Position.MID) -> Player:
    return Player(
        player_id=player_id,
        web_name=f"P{player_id}",
        first_name="First",
        second_name=f"P{player_id}",
        team_id=1,
        team_short="ARS",
        position=position,
        now_cost=int(price_m * 10),
        status="a",
        chance_of_playing_next_round=None,
    )


class TestVoteValue:
    def test_multiplies_weight_action_and_conviction(self) -> None:
        assert vote_value(0.4, PickAction.SQUAD_INCLUDE, 5) == pytest.approx(0.4)
        assert vote_value(0.4, PickAction.WATCHLIST, 5) == pytest.approx(0.4 * 0.3)

    def test_conviction_is_saturating_not_linear(self) -> None:
        # A linear 1-5 scale would make conviction 5 worth 5x conviction 1 —
        # far too much leverage for the noisiest field in the schema.
        ratio = CONVICTION_FACTOR[5] / CONVICTION_FACTOR[1]
        assert ratio == pytest.approx(4.0)
        assert CONVICTION_FACTOR[4] / CONVICTION_FACTOR[2] == pytest.approx(1.8)

    def test_avoid_is_negative_but_cheaper_than_an_include(self) -> None:
        assert ACTION_VALUE[PickAction.AVOID] < 0
        assert abs(ACTION_VALUE[PickAction.AVOID]) < ACTION_VALUE[PickAction.SQUAD_INCLUDE]

    def test_captain_counts_as_inclusion(self) -> None:
        # The captaincy CEILING signal lives in the separate election; here a
        # captain pick is worth exactly a squad_include and no more.
        assert ACTION_VALUE[PickAction.CAPTAIN] == ACTION_VALUE[PickAction.SQUAD_INCLUDE]


class TestBandOf:
    def test_buckets_by_position_and_whole_million(self) -> None:
        assert band_of(_player(1, 7.5, Position.MID)) == "MID:7.0"
        assert band_of(_player(2, 7.0, Position.MID)) == "MID:7.0"
        assert band_of(_player(3, 7.5, Position.FWD)) == "FWD:7.0"  # position separates
        assert band_of(_player(4, 4.5, Position.GK)) == "GK:4.0"


class TestScorePicks:
    def test_one_vote_per_creator_per_player_takes_the_strongest(self) -> None:
        # The live-data bug: 17 (creator, player) pairs had BOTH a
        # squad_include and a captain pick. Summing would double-count one
        # opinion; we take the strongest signal only.
        players = {1: _player(1, 7.5)}
        both = score_picks(
            {"c": [(1, PickAction.SQUAD_INCLUDE, 5), (1, PickAction.CAPTAIN, 5)]},
            {"c": 0.4},
            players,
        )
        once = score_picks({"c": [(1, PickAction.SQUAD_INCLUDE, 5)]}, {"c": 0.4}, players)
        assert both[1].weighted_votes == pytest.approx(once[1].weighted_votes)
        assert len(both[1].votes) == 1

    def test_watchlist_loses_to_squad_include_for_the_same_creator(self) -> None:
        players = {1: _player(1, 7.5)}
        scores = score_picks(
            {"c": [(1, PickAction.WATCHLIST, 5), (1, PickAction.SQUAD_INCLUDE, 5)]},
            {"c": 0.4},
            players,
        )
        assert scores[1].votes[0].action is PickAction.SQUAD_INCLUDE

    def test_avoid_registers_even_against_a_weaker_positive(self) -> None:
        # "Strongest" is distance from neutral, so a firm avoid beats a
        # lukewarm watchlist from the same creator.
        players = {1: _player(1, 7.5)}
        scores = score_picks(
            {"c": [(1, PickAction.WATCHLIST, 1), (1, PickAction.AVOID, 5)]},
            {"c": 0.4},
            players,
        )
        assert scores[1].votes[0].action is PickAction.AVOID
        assert scores[1].weighted_votes < 0
        assert scores[1].detractors == 1

    def test_negative_consensus_clamps_to_zero_band_score(self) -> None:
        players = {1: _player(1, 7.5)}
        scores = score_picks({"c": [(1, PickAction.AVOID, 5)]}, {"c": 0.4}, players)
        assert scores[1].weighted_votes < 0
        assert scores[1].band_score == 0.0

    def test_band_score_is_relative_within_a_band(self) -> None:
        players = {1: _player(1, 7.5), 2: _player(2, 7.0)}  # same band
        scores = score_picks(
            {
                "a": [(1, PickAction.SQUAD_INCLUDE, 5), (2, PickAction.SQUAD_INCLUDE, 5)],
                "b": [(1, PickAction.SQUAD_INCLUDE, 5)],
            },
            {"a": 0.4, "b": 0.4},
            players,
        )
        assert scores[1].band_score > scores[2].band_score
        assert scores[1].band == scores[2].band

    def test_cheap_player_does_not_outscore_premium_across_bands(self) -> None:
        # THE point of banding: a £4.5m keeper with many backers is "best
        # budget keeper", not "better than the premium". Both can top their
        # own band; neither is compared to the other.
        players = {1: _player(1, 4.5, Position.GK), 2: _player(2, 15.5, Position.FWD)}
        scores = score_picks(
            {
                "a": [(1, PickAction.SQUAD_INCLUDE, 5), (2, PickAction.SQUAD_INCLUDE, 5)],
                "b": [(1, PickAction.SQUAD_INCLUDE, 5)],
                "c": [(1, PickAction.SQUAD_INCLUDE, 5)],
            },
            {"a": 0.4, "b": 0.4, "c": 0.4},
            players,
        )
        assert scores[1].band != scores[2].band
        # The invariant: piling MORE votes onto the cheap keeper must not
        # move the premium's score at all — they are never compared.
        more_votes = score_picks(
            {
                "a": [(1, PickAction.SQUAD_INCLUDE, 5), (2, PickAction.SQUAD_INCLUDE, 5)],
                "b": [(1, PickAction.SQUAD_INCLUDE, 5)],
                "c": [(1, PickAction.SQUAD_INCLUDE, 5)],
                "d": [(1, PickAction.SQUAD_INCLUDE, 5)],
                "e": [(1, PickAction.SQUAD_INCLUDE, 5)],
            },
            {"a": 0.4, "b": 0.4, "c": 0.4, "d": 0.4, "e": 0.4},
            players,
        )
        # the premium (2) is untouched by the keeper's extra votes...
        assert more_votes[2].band_score == pytest.approx(scores[2].band_score)
        # ...while the keeper's own score moves, since he sets his own band's max
        assert more_votes[1].band_score > scores[1].band_score

    def test_thin_band_is_not_contaminated_by_a_cheaper_band(self) -> None:
        # REGRESSION: an earlier draft merged thin bands down into the next
        # cheaper band. On live data that scored Haaland (£15.5m, alone in
        # his bracket) against £6.0m forwards — the exact cross-band
        # comparison banding exists to prevent. A lone premium must be
        # scored in his OWN band.
        players = {1: _player(1, 15.5, Position.FWD), 2: _player(2, 6.5, Position.FWD)}
        scores = score_picks(
            {
                "a": [(1, PickAction.SQUAD_INCLUDE, 5), (2, PickAction.SQUAD_INCLUDE, 5)],
                "b": [(2, PickAction.SQUAD_INCLUDE, 5)],
                "c": [(2, PickAction.SQUAD_INCLUDE, 5)],
            },
            {"a": 0.4, "b": 0.4, "c": 0.4},
            players,
        )
        assert scores[1].band == "FWD:15.0"
        assert scores[2].band == "FWD:6.0"
        # the premium has ONE backer vs the cheaper player's three, but is
        # judged only against himself + shrinkage, so he stays well scored
        assert scores[1].band_score == pytest.approx(0.4 / (0.4 + BAND_SHRINKAGE))

    def test_weakly_backed_singleton_does_not_get_a_free_perfect_score(self) -> None:
        # The guard shrinkage buys us: alone in a band is not the same as
        # "unanimously backed".
        players = {1: _player(1, 12.0)}
        scores = score_picks({"c": [(1, PickAction.WATCHLIST, 1)]}, {"c": 0.2}, players)
        assert scores[1].band_score < 0.2

    def test_unknown_player_ids_are_ignored(self) -> None:
        scores = score_picks({"c": [(99, PickAction.SQUAD_INCLUDE, 5)]}, {"c": 0.4}, {})
        assert scores == {}

    def test_unknown_creator_contributes_nothing(self) -> None:
        players = {1: _player(1, 7.5)}
        scores = score_picks({"ghost": [(1, PickAction.SQUAD_INCLUDE, 5)]}, {}, players)
        assert scores[1].weighted_votes == 0.0

    def test_backers_and_detractors_counted_separately(self) -> None:
        players = {1: _player(1, 7.5)}
        scores = score_picks(
            {
                "a": [(1, PickAction.SQUAD_INCLUDE, 5)],
                "b": [(1, PickAction.SQUAD_INCLUDE, 4)],
                "c": [(1, PickAction.AVOID, 4)],
            },
            {"a": 0.4, "b": 0.3, "c": 0.3},
            players,
        )
        assert scores[1].backers == 2
        assert scores[1].detractors == 1
        assert len(scores[1].votes) == 3
