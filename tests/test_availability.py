"""Tests for fpl_oracle.fpl.availability: the pre-solver FACTS VETO filter.

Fixtures are small hand-built `Player` instances (never touches `data/` or
the network) covering every observed `status` code, the `chance=0`
contradiction case (including combined with an unknown status, per review
finding 3), the doubtful-with-no-chance default, an unknown status code,
out-of-range `chance` values on both the 'a' and 'd' paths, and the
filter's threshold boundary — including that the EXCLUDED veto in
`filter_squad_candidates` is structural and can't be bypassed by relaxing
`min_multiplier` (review finding 1).
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

    def test_chance_zero_with_unknown_status_still_excludes(self) -> None:
        """Review finding 3: the chance=0 veto must run BEFORE the
        unknown-status branch, so an unrecognised code carrying an
        explicit chance=0 is EXCLUDED, not DOUBTFUL. Pins the precedence
        a mutation (swapping the two checks) previously slipped past."""
        v = assess(_player(status="x", chance=0))
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

    def test_status_normalised_case_and_whitespace(self) -> None:
        v = assess(_player(status=" A ", chance=100))
        assert v.availability == Availability.AVAILABLE
        assert v.status == "a"


class TestOutOfRangeChance:
    """Review nit 4/5: an out-of-range chance must fall back to DOUBTFUL
    0.5 on BOTH the 'a' and 'd' status paths, never trusted at face value
    (a naive `chance / 100` would produce multiplier 1.5 or negative)."""

    def test_available_status_chance_above_100(self) -> None:
        v = assess(_player(status="a", chance=150))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.5

    def test_available_status_negative_chance(self) -> None:
        v = assess(_player(status="a", chance=-10))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.5

    def test_doubtful_status_chance_above_100(self) -> None:
        v = assess(_player(status="d", chance=150))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.5

    def test_doubtful_status_negative_chance(self) -> None:
        v = assess(_player(status="d", chance=-50))
        assert v.availability == Availability.DOUBTFUL
        assert v.multiplier == 0.5


class TestVerdictCarriesRawFields:
    def test_verdict_carries_status_and_chance(self) -> None:
        v = assess(_player(status="d", chance=75))
        assert v.status == "d"
        assert v.chance_of_playing_next_round == 75

    def test_excluded_verdict_has_usable_reason(self) -> None:
        v = assess(_player(status="i", chance=None))
        assert isinstance(v.reason, str)
        assert len(v.reason) > 0

    def test_multiplier_always_within_unit_interval(self) -> None:
        for status, chance in [
            ("a", None),
            ("a", 100),
            ("a", 25),
            ("a", 0),
            ("a", -10),
            ("a", 150),
            ("d", None),
            ("d", 75),
            ("d", 0),
            ("d", -10),
            ("d", 150),
            ("i", None),
            ("u", None),
            ("s", None),
            ("x", None),
            ("x", 0),
        ]:
            v = assess(_player(status=status, chance=chance))
            assert 0.0 <= v.multiplier <= 1.0


class TestFilterSquadCandidates:
    def test_threshold_boundary_exactly_075_passes(self) -> None:
        p = _player(status="d", chance=75)  # multiplier == 0.75 == MIN_MULTIPLIER
        result = filter_squad_candidates([p])
        assert result.eligible == [p]
        assert result.verdicts[p.player_id].availability == Availability.DOUBTFUL

    def test_threshold_boundary_050_does_not_pass(self) -> None:
        p = _player(status="d", chance=50)  # multiplier == 0.5 < MIN_MULTIPLIER
        result = filter_squad_candidates([p])
        assert result.eligible == []
        assert result.verdicts[p.player_id].multiplier == 0.5

    def test_mixed_squad_splits_correctly(self) -> None:
        fit = _player(player_id=1, status="a", chance=None)
        hurt = _player(player_id=2, status="i", chance=None)
        doubtful_ok = _player(player_id=3, status="d", chance=100)
        doubtful_low = _player(player_id=4, status="a", chance=25)

        result = filter_squad_candidates([fit, hurt, doubtful_ok, doubtful_low])

        assert {p.player_id for p in result.eligible} == {1, 3}
        # Verdicts are returned for EVERY assessed player, not just the
        # rejected ones (review finding 2).
        assert set(result.verdicts.keys()) == {1, 2, 3, 4}

    def test_custom_min_multiplier(self) -> None:
        p = _player(status="a", chance=25)  # multiplier 0.25
        result = filter_squad_candidates([p], min_multiplier=0.2)
        assert result.eligible == [p]

    def test_default_threshold_constant_is_075(self) -> None:
        assert MIN_MULTIPLIER == 0.75

    def test_verdicts_returned_for_eligible_players_too(self) -> None:
        """Review finding 2: a `d`/75 player that clears the bar must
        still have its 0.75 multiplier visible in `verdicts`, not just
        appear in `eligible` indistinguishable from a fully fit player."""
        p = _player(status="d", chance=75)
        result = filter_squad_candidates([p])
        assert p in result.eligible
        v = result.verdicts[p.player_id]
        assert v.multiplier == 0.75
        assert v.availability == Availability.DOUBTFUL

    def test_excluded_never_bypassed_by_relaxed_threshold(self) -> None:
        """Review finding 1: min_multiplier=0.0 must NOT admit EXCLUDED
        players. The EXCLUDED veto is structural (checked via
        verdict.availability), not inferred from multiplier >= 0.0."""
        hurt = _player(player_id=1, status="i", chance=None)
        suspended = _player(player_id=2, status="s", chance=None)
        fit = _player(player_id=3, status="a", chance=None)

        result = filter_squad_candidates([hurt, suspended, fit], min_multiplier=0.0)

        assert {p.player_id for p in result.eligible} == {3}
        assert result.verdicts[1].availability == Availability.EXCLUDED
        assert result.verdicts[2].availability == Availability.EXCLUDED


@pytest.mark.network
def test_live_bootstrap_availability_invariants() -> None:
    """Smoke test against the real bootstrap-static payload. Checks the
    load-bearing invariants rather than loose ratios: every player whose
    status is injured/unavailable/suspended, or whose chance is exactly
    0, must be EXCLUDED; every verdict's multiplier must stay in [0, 1];
    and the AVAILABLE/EXCLUDED split should still look like a real
    Premier League roster (mostly fit, a real number hurt)."""
    from fpl_oracle.fpl.players import PlayerDB

    db = PlayerDB.load()
    players = db.all_players()
    verdicts = {p.player_id: assess(p) for p in players}

    for player in players:
        v = verdicts[player.player_id]
        assert 0.0 <= v.multiplier <= 1.0
        if player.status in {"i", "u", "s"} or player.chance_of_playing_next_round == 0:
            assert v.availability == Availability.EXCLUDED, (
                f"player {player.player_id} ({player.web_name}): status={player.status!r} "
                f"chance={player.chance_of_playing_next_round!r} should be EXCLUDED, "
                f"got {v.availability}"
            )

    available = [v for v in verdicts.values() if v.availability == Availability.AVAILABLE]
    excluded = [v for v in verdicts.values() if v.availability == Availability.EXCLUDED]
    assert len(available) > len(verdicts) * 0.5
    assert len(excluded) > 0
