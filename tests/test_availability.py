"""Tests for fpl_oracle.fpl.availability: the pre-solver FACTS VETO filter.

Fixtures are small hand-built `Player` instances (never touches `data/` or
the network) covering every observed `status` code, the `chance=0`
contradiction case, the doubtful-with-no-chance default, an unknown status
code, and the filter's threshold boundary.
"""

from __future__ import annotations

import pytest

from fpl_oracle.fpl.availability import (
    MIN_MULTIPLIER,
    Availability,
    assess,
    filter_squad_candidates,
)
from fpl_oracle.fpl.players import Player, Position


def _player(
    player_id: int = 1,
    status: str = "a",
    chance: int | None = None,
) -> Player:
    return Player(
        player_id=player_id,
        web_name=f"Player{player_id}",
        first_name="First",
        second_name=f"Last{player_id}",
        team_id=1,
        team_short="ARS",
        position=Position.MID,
        now_cost=100,
        status=status,
        chance_of_playing_next_round=chance,
    )


class TestAssessStatusCodes:
    def test_available_no_chance(self) -> None:
        v = assess(_player(status="a", chance=None))
        assert v.availability == Availability.AVAILABLE
        assert v.multiplier == 1.0

    def test_available_chance_100(self) -> None:
        v = assess(_player(status="a", chance=100))
        assert v.availability == Availability.AVAILABLE
        assert v.multiplier == 1.0

    def test_injured_excluded(self) -> None:
        v = assess(_player(status="i", chance=None))
        assert v.availability == Availability.EXCLUDED
        assert v.multiplier == 0.0

    def test_unavailable_excluded(self) -> None:
        v = assess(_player(status="u", chance=None))
        assert v.availability == Availability.EXCLUDED
        assert v.multiplier == 0.0

    def test_suspended_excluded(self) -> None:
        v = assess(_player(status="s", chance=None))
        assert v.availability == Availability.EXCLUDED
        assert v.multiplier == 0.0

    def test_doubtful_status_no_chance_defaults_half(self) -> None:
        v = assess(_player(status="d", chance=None))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.5
        assert "0.5" in v.reason or "50%" in v.reason

    def test_doubtful_status_with_chance_75(self) -> None:
        v = assess(_player(status="d", chance=75))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.75

    def test_available_status_with_partial_chance_is_doubtful(self) -> None:
        v = assess(_player(status="a", chance=25))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.25


class TestChanceZeroContradiction:
    def test_chance_zero_with_status_available_excludes(self) -> None:
        """An explicit chance=0 is a fact and vetoes a stale status='a'."""
        v = assess(_player(status="a", chance=0))
        assert v.availability == Availability.EXCLUDED
        assert v.multiplier == 0.0

    def test_chance_zero_with_status_doubtful_excludes(self) -> None:
        v = assess(_player(status="d", chance=0))
        assert v.availability == Availability.EXCLUDED
        assert v.multiplier == 0.0


class TestUnknownStatusCode:
    def test_unknown_status_is_doubtful_not_available(self) -> None:
        v = assess(_player(status="x", chance=None))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.5

    def test_unknown_status_reason_names_the_code(self) -> None:
        v = assess(_player(status="x", chance=None))
        assert "x" in v.reason


class TestVerdictCarriesRawFields:
    def test_verdict_carries_status_and_chance(self) -> None:
        v = assess(_player(status="d", chance=75))
        assert v.status == "d"
        assert v.chance_of_playing_next_round == 75

    def test_excluded_verdict_has_usable_reason(self) -> None:
        v = assess(_player(status="i", chance=None))
        assert isinstance(v.reason, str)
        assert len(v.reason) > 0


class TestFilterSquadCandidates:
    def test_threshold_boundary_exactly_075_passes(self) -> None:
        p = _player(status="d", chance=75)  # multiplier == 0.75 == MIN_MULTIPLIER
        eligible, excluded = filter_squad_candidates([p])
        assert eligible == [p]
        assert excluded == []

    def test_threshold_boundary_050_does_not_pass(self) -> None:
        p = _player(status="d", chance=50)  # multiplier == 0.5 < MIN_MULTIPLIER
        eligible, excluded = filter_squad_candidates([p])
        assert eligible == []
        assert len(excluded) == 1
        assert excluded[0].player_id == p.player_id

    def test_mixed_squad_splits_correctly(self) -> None:
        fit = _player(player_id=1, status="a", chance=None)
        hurt = _player(player_id=2, status="i", chance=None)
        doubtful_ok = _player(player_id=3, status="d", chance=100)
        doubtful_low = _player(player_id=4, status="a", chance=25)

        eligible, excluded = filter_squad_candidates([fit, hurt, doubtful_ok, doubtful_low])

        assert {p.player_id for p in eligible} == {1, 3}
        assert {v.player_id for v in excluded} == {2, 4}

    def test_custom_min_multiplier(self) -> None:
        p = _player(status="a", chance=25)  # multiplier 0.25
        eligible, _ = filter_squad_candidates([p], min_multiplier=0.2)
        assert eligible == [p]

    def test_default_threshold_constant_is_075(self) -> None:
        assert MIN_MULTIPLIER == 0.75


@pytest.mark.network
def test_live_bootstrap_availability_split_is_sane() -> None:
    """Smoke test against the real bootstrap-static payload: most players
    should be AVAILABLE, and a non-zero number should be EXCLUDED."""
    from fpl_oracle.fpl.players import PlayerDB

    db = PlayerDB.load()
    players = list(db._players.values())  # type: ignore[attr-defined]
    verdicts = [assess(p) for p in players]

    available = [v for v in verdicts if v.availability == Availability.AVAILABLE]
    excluded = [v for v in verdicts if v.availability == Availability.EXCLUDED]

    assert len(available) > len(verdicts) * 0.5
    assert len(excluded) > 0
