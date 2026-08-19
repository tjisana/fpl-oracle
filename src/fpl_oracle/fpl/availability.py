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

The EXCLUDED veto is structural, not numeric: `filter_squad_candidates`
checks `verdict.availability` directly rather than inferring exclusion from
`multiplier == 0.0`, so a caller that relaxes `min_multiplier` (even to
`0.0`, "don't penalise doubt, just drop the unavailable") can never let an
EXCLUDED player slip through on the numeric comparison alone.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from fpl_oracle.fpl.players import Player

# A player must clear this multiplier (AND not be EXCLUDED — see
# `filter_squad_candidates`) to reach the solver. 0.75 is the threshold
# because a 75%-chance player is still a defensible pick a squad can be
# built around; below that is a coin flip the solver shouldn't spend
# budget on when a safer alternative exists in the same price band.
MIN_MULTIPLIER = 0.75

# Excluded statuses and their human-readable labels live in one dict so
# the status set and its labels can't drift apart from each other.
_EXCLUDED_STATUS_LABELS: dict[str, str] = {
    "i": "injured",
    "u": "unavailable",
    "s": "suspended",
}
_EXCLUDED_STATUSES = frozenset(_EXCLUDED_STATUS_LABELS)
_KNOWN_STATUSES = _EXCLUDED_STATUSES | {"a", "d"}

# Used for status == "d" when the API gives no `chance_of_playing_next_round`
# — a fact-backed status with no fact-backed number attached, so we fall
# back to a documented midpoint rather than guessing higher or lower.
_DOUBTFUL_DEFAULT_MULTIPLIER = 0.5
# Same fallback multiplier, reused for an unrecognised status code and for
# an out-of-range `chance_of_playing_next_round` — see the module
# docstring: neither case is ever silently treated as available (or, for
# an out-of-range chance, trusted at face value).
_UNKNOWN_STATUS_MULTIPLIER = 0.5


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    DOUBTFUL = "DOUBTFUL"
    EXCLUDED = "EXCLUDED"


class AvailabilityVerdict(BaseModel):
    player_id: int
    availability: Availability
    multiplier: float = Field(ge=0.0, le=1.0)
    reason: str
    status: str
    chance_of_playing_next_round: int | None


class AvailabilityFilter(BaseModel):
    """Result of `filter_squad_candidates`: the players cleared for the
    solver, plus a verdict for EVERY assessed player (not just the
    rejected ones) — so a `d`/75 player who clears the bar still carries
    its 0.75 multiplier where consensus/solver scoring can see and apply
    it, rather than reaching the solver indistinguishable from a fully
    fit player."""

    eligible: list[Player]
    verdicts: dict[int, AvailabilityVerdict]


def _bounded_ratio(chance: int) -> float | None:
    """`chance / 100` when `chance` is a plausible percentage (1-100
    inclusive — 0 is handled separately, upstream, as a hard veto), else
    `None` to signal an out-of-range value neither status branch ('a' nor
    'd') should trust at face value. Shared by both branches so a
    malformed `chance` (negative, or >100) can't produce a multiplier
    outside [0, 1] from one status path but not the other."""
    if 1 <= chance <= 100:
        return chance / 100
    return None


def assess(player: Player) -> AvailabilityVerdict:
    """Pure, no I/O: derive an `AvailabilityVerdict` from a `Player`'s
    `status` and `chance_of_playing_next_round` fields alone. See the
    module docstring for the FACTS VETO rule this implements."""
    status = player.status.strip().lower()
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

    # An explicit zero is a fact, checked before anything else (including
    # the unknown-status branch) — it vetoes any status, known or not.
    # A contradiction between status="a" and chance=0 favors the more
    # specific, more pessimistic signal.
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
        return verdict(
            Availability.EXCLUDED,
            0.0,
            f"status={status!r} ({_EXCLUDED_STATUS_LABELS[status]})",
        )

    if status == "d":
        if chance is None:
            return verdict(
                Availability.DOUBTFUL,
                _DOUBTFUL_DEFAULT_MULTIPLIER,
                "status='d' (doubtful) with no chance_of_playing_next_round given — "
                f"defaulting to {_DOUBTFUL_DEFAULT_MULTIPLIER:.0%}",
            )
        ratio = _bounded_ratio(chance)
        if ratio is None:
            return verdict(
                Availability.DOUBTFUL,
                _UNKNOWN_STATUS_MULTIPLIER,
                f"status='d' with an out-of-range chance_of_playing_next_round={chance!r} — "
                "treating as doubtful pending review",
            )
        return verdict(
            Availability.DOUBTFUL,
            ratio,
            f"status='d' (doubtful), chance_of_playing_next_round={chance}",
        )

    # status == "a" from here on (the only remaining member of
    # _KNOWN_STATUSES once excluded statuses and "d" are handled above).
    if chance is None or chance == 100:
        return verdict(Availability.AVAILABLE, 1.0, "status='a' (available), no fitness doubt")

    ratio = _bounded_ratio(chance)
    if ratio is None:
        return verdict(
            Availability.DOUBTFUL,
            _UNKNOWN_STATUS_MULTIPLIER,
            f"status='a' with an out-of-range chance_of_playing_next_round={chance!r} — "
            "treating as doubtful pending review",
        )
    return verdict(
        Availability.DOUBTFUL,
        ratio,
        f"status='a' but chance_of_playing_next_round={chance} — fitness doubt",
    )


def filter_squad_candidates(
    players: list[Player], min_multiplier: float = MIN_MULTIPLIER
) -> AvailabilityFilter:
    """Assess every player in `players` and split them into solver-eligible
    vs. not. A player is eligible only when BOTH hold: it is not EXCLUDED
    (checked structurally via `verdict.availability`, never inferred from
    `multiplier == 0.0` — see the module docstring) AND its multiplier
    clears `min_multiplier`. `verdicts` carries every assessed player's
    verdict, eligible or not, so downstream scoring/reporting can see the
    multiplier behind a DOUBTFUL pick that still made the squad."""
    eligible: list[Player] = []
    verdicts: dict[int, AvailabilityVerdict] = {}
    for player in players:
        v = assess(player)
        verdicts[player.player_id] = v
        if v.availability is not Availability.EXCLUDED and v.multiplier >= min_multiplier:
            eligible.append(player)
    return AvailabilityFilter(eligible=eligible, verdicts=verdicts)
