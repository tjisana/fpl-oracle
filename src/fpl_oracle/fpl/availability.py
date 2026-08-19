"""Pre-solver availability filter: FACTS VETO, OPINIONS VOTE (see the
fpl-domain skill, "Consensus & solver design rules"). Consensus scoring
aggregates creator opinions on who to pick; this module runs after that
scoring and before the solver, and has the final word on who the solver
is even allowed to see. No amount of creator conviction can put an
injured/suspended/unavailable player in front of the solver — availability
here is read straight off the FPL API's `status` and
`chance_of_playing_next_round` fields on `Player`, never off a vote count.

Verified against a live bootstrap-static payload: `status` observed values
were `a` (available, 480 players), `i` (injured, 47), `u` (unavailable, 37),
`d` (doubtful, 28), `s` (suspended, 3) — treated as an OPEN set, since the
game can and does add codes. `chance_of_playing_next_round` observed values
were `None` (467 — the common case for fit players), `0` (87), `75` (24),
`100` (13), `25` (2), `50` (2).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from fpl_oracle.fpl.players import Player

# A player must clear this multiplier to reach the solver. 0.75 is the
# threshold because a 75%-chance player is still a defensible pick a squad
# can be built around; below that is a coin flip the solver shouldn't spend
# budget on when a safer alternative exists in the same price band.
MIN_MULTIPLIER = 0.75

_EXCLUDED_STATUSES = {"i", "u", "s"}
_KNOWN_STATUSES = _EXCLUDED_STATUSES | {"a", "d"}

# Used for status == "d" when the API gives no `chance_of_playing_next_round`
# — a fact-backed status with no fact-backed number attached, so we fall
# back to a documented midpoint rather than guessing higher or lower.
_DOUBTFUL_DEFAULT_MULTIPLIER = 0.5
# Same fallback multiplier, reused for an unrecognised status code — see
# the module docstring: an unknown code must never be silently treated as
# available.
_UNKNOWN_STATUS_MULTIPLIER = 0.5


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    DOUBTFUL = "DOUBTFUL"
    EXCLUDED = "EXCLUDED"


class AvailabilityVerdict(BaseModel):
    player_id: int
    availability: Availability
    multiplier: float
    reason: str
    status: str
    chance_of_playing_next_round: int | None


def assess(player: Player) -> AvailabilityVerdict:
    """Pure, no I/O: derive an `AvailabilityVerdict` from a `Player`'s
    `status` and `chance_of_playing_next_round` fields alone. See the
    module docstring for the FACTS VETO rule this implements."""
    status = player.status
    chance = player.chance_of_playing_next_round

    def verdict(availability: Availability, multiplier: float, reason: str) -> AvailabilityVerdict:
        return AvailabilityVerdict(
            player_id=player.player_id,
            availability=availability,
            multiplier=multiplier,
            reason=reason,
            status=status,
            chance_of_playing_next_round=chance,
        )

    # An explicit zero is a fact, regardless of what the status string
    # says — a contradiction between status="a" and chance=0 favors the
    # more specific, more pessimistic signal.
    if chance == 0:
        return verdict(
            Availability.EXCLUDED,
            0.0,
            f"chance_of_playing_next_round is 0 (status={status!r})",
        )

    if status not in _KNOWN_STATUSES:
        return verdict(
            Availability.DOUBTFUL,
            _UNKNOWN_STATUS_MULTIPLIER,
            f"unrecognised FPL status code {status!r} — treating as doubtful pending review",
        )

    if status in _EXCLUDED_STATUSES:
        _LABELS = {"i": "injured", "u": "unavailable", "s": "suspended"}
        return verdict(
            Availability.EXCLUDED,
            0.0,
            f"status={status!r} ({_LABELS[status]})",
        )

    if status == "d":
        if chance is None:
            return verdict(
                Availability.DOUBTFUL,
                _DOUBTFUL_DEFAULT_MULTIPLIER,
                "status='d' (doubtful) with no chance_of_playing_next_round given — "
                f"defaulting to {_DOUBTFUL_DEFAULT_MULTIPLIER:.0%}",
            )
        return verdict(
            Availability.DOUBTFUL,
            chance / 100,
            f"status='d' (doubtful), chance_of_playing_next_round={chance}",
        )

    # status == "a" from here on.
    if chance is None or chance == 100:
        return verdict(Availability.AVAILABLE, 1.0, "status='a' (available), no fitness doubt")

    if 1 <= chance <= 99:
        return verdict(
            Availability.DOUBTFUL,
            chance / 100,
            f"status='a' but chance_of_playing_next_round={chance} — fitness doubt",
        )

    # chance is negative or >100: not an observed value, but not silently
    # trusted either.
    return verdict(
        Availability.DOUBTFUL,
        _UNKNOWN_STATUS_MULTIPLIER,
        f"status='a' with an out-of-range chance_of_playing_next_round={chance!r} — "
        "treating as doubtful pending review",
    )


def filter_squad_candidates(
    players: list[Player], min_multiplier: float = MIN_MULTIPLIER
) -> tuple[list[Player], list[AvailabilityVerdict]]:
    """Split `players` into those that clear `min_multiplier` (eligible for
    the solver) and the verdicts for everyone who didn't (for the gameweek
    report to explain the exclusions). Verdict order follows input order."""
    eligible: list[Player] = []
    excluded_verdicts: list[AvailabilityVerdict] = []
    for player in players:
        v = assess(player)
        if v.multiplier >= min_multiplier:
            eligible.append(player)
        else:
            excluded_verdicts.append(v)
    return eligible, excluded_verdicts
