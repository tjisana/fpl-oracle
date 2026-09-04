"""Wildcard gap: how far the owner's actual squad sits from the solver's
best-15-from-£100m squad, on the solver's own yardstick.

`solver/transfers.py` documents `solver.squad.coefficient` as the
project's one sanctioned cross-band measure (price-anchored, consensus
perturbs by at most one band width). This module reuses that same
measure to compare two whole squads — the owner's 15 and the solver's 15
— rather than a single swap, so the report can answer "is a wildcard
worth it" with one number instead of leaving the owner to eyeball two
separate squad listings.

OBSERVATIONAL ONLY, same as `MySquadSummary` and `TransferRecommendation`:
this never feeds back into the solve. Pure arithmetic, no IO,
deterministic.
"""

from __future__ import annotations

from pydantic import BaseModel

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.availability import AvailabilityVerdict
from fpl_oracle.fpl.entry import OwnedPlayer
from fpl_oracle.fpl.players import PlayerDB
from fpl_oracle.solver.squad import BENCH_WEIGHT, Squad, coefficient
from fpl_oracle.solver.transfers import TransferRecommendation


class WildcardGap(BaseModel):
    my_objective: float
    solver_objective: float
    gain: float
    best_single_transfer_delta: float | None
    transfers_equivalent: float | None
    kept_player_ids: list[int]
    kept_count: int
    changes_needed: int
    unbacked_owned_ids: list[int]


def squad_objective(
    player_ids_starting: set[int],
    player_ids_bench: set[int],
    db: PlayerDB,
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
) -> float:
    """sum over players of coefficient(player, scores.get(pid), multiplier) *
    (1.0 if starting else BENCH_WEIGHT). An id the DB does not know
    contributes 0.0 rather than raising — a stale or unresolved owned
    player must not take down the whole comparison."""
    total = 0.0
    for pid, weight in (
        *((pid, 1.0) for pid in player_ids_starting),
        *((pid, BENCH_WEIGHT) for pid in player_ids_bench),
    ):
        player = db.get(pid)
        if player is None:
            continue
        multiplier = verdicts[pid].multiplier if pid in verdicts else 1.0
        total += coefficient(player, scores.get(pid), multiplier) * weight
    return total


def wildcard_gap(
    *,
    owned: list[OwnedPlayer],
    solver_squad: Squad,
    db: PlayerDB,
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
    transfer_rec: TransferRecommendation | None,
) -> WildcardGap:
    owned_starting = {p.player_id for p in owned if p.is_starting}
    owned_bench = {p.player_id for p in owned if not p.is_starting}
    solver_starting = {p.player_id for p in solver_squad.players if p.starting}
    solver_bench = {p.player_id for p in solver_squad.players if not p.starting}

    my_objective = squad_objective(owned_starting, owned_bench, db, scores, verdicts)
    solver_objective = squad_objective(solver_starting, solver_bench, db, scores, verdicts)
    gain = solver_objective - my_objective

    best_single_transfer_delta = (
        transfer_rec.delta_distribution.best
        if transfer_rec is not None and transfer_rec.delta_distribution is not None
        else None
    )
    transfers_equivalent = (
        gain / best_single_transfer_delta
        if best_single_transfer_delta is not None and best_single_transfer_delta > 0
        else None
    )

    owned_ids = {p.player_id for p in owned}
    solver_ids = {p.player_id for p in solver_squad.players}
    kept_player_ids = sorted(owned_ids & solver_ids)

    unbacked_owned_ids = sorted(
        p.player_id
        for p in owned
        if scores.get(p.player_id) is None or scores[p.player_id].backers == 0
    )

    return WildcardGap(
        my_objective=my_objective,
        solver_objective=solver_objective,
        gain=gain,
        best_single_transfer_delta=best_single_transfer_delta,
        transfers_equivalent=transfers_equivalent,
        kept_player_ids=kept_player_ids,
        kept_count=len(kept_player_ids),
        changes_needed=len(solver_squad.players) - len(kept_player_ids),
        unbacked_owned_ids=unbacked_owned_ids,
    )
