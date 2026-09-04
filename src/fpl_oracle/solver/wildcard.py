"""Wildcard gap: how far the owner's actual squad sits from the solver's
best-15-from-£100m squad, on the transfer section's yardstick (starters
count at 1.0, bench at BENCH_WEIGHT — see `solver.squad.coefficient`,
which `solver/transfers.py` documents as the project's one sanctioned
cross-band measure).

BOTH SIDES' STARTING XI ARE RE-OPTIMISED HERE (`best_xi`), never read off
a stored "starting" flag. The solver's own `SquadPlayer.starting` is
itself an argmax over its squad's coefficients — the LP's `y` variables.
`OwnedPlayer.is_starting`, by contrast, is nothing like that: it is just
whatever lineup the owner last submitted, which may be stale, defensive,
or simply not the XI a fresh optimisation of the same 15 names would
pick. Comparing the solver's argmax against the owner's last team sheet
would inflate (or deflate) the gap with a lineup decision the consensus
never actually made. Re-deriving the XI the same way on both sides with
`best_xi` keeps the comparison like-for-like. This matters more than it
might look: starters count 10x bench in `squad_objective` (1.0 vs
BENCH_WEIGHT), so which 11 of the 15 are "starting" dominates the score.

OBSERVATIONAL ONLY, same as `MySquadSummary` and `TransferRecommendation`:
this never feeds back into the solve. Pure arithmetic, no IO,
deterministic.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict
from fpl_oracle.fpl.entry import OwnedPlayer
from fpl_oracle.fpl.players import PlayerDB, Position
from fpl_oracle.solver.squad import BENCH_WEIGHT, Squad, coefficient
from fpl_oracle.solver.transfers import TransferRecommendation

# Legal outfield formations: DEF 3-5, MID 2-5, FWD 1-3, summing to 10
# (the GK slot is fixed at 1 and chosen separately — FPL never plays two
# keepers). Same shape as `solver.squad.XI_MIN`/`XI_MAX`, restated here
# rather than imported because this module enumerates formations instead
# of encoding them as ILP bounds.
_DEF_RANGE = range(3, 6)
_MID_RANGE = range(2, 6)
_FWD_RANGE = range(1, 4)


class WildcardGap(BaseModel):
    """How far the owner's actual squad sits from the solver's £100m
    squad, on the transfer section's yardstick. Landed on `RunRecord` and
    written to every run.json, so every field carries its own
    description rather than relying on this docstring at read time.

    Both squads' starting XI are re-optimised by `best_xi` before scoring
    — see the module docstring for why the stored "starting" flags on
    either side are not used directly.
    """

    my_objective: float = Field(
        description="owner's 15 on the shared yardstick, with the XI re-optimised by best_xi"
    )
    solver_objective: float = Field(
        description="solver's 15 on the same yardstick, with the XI re-optimised by best_xi"
    )
    gain: float = Field(description="solver_objective - my_objective")
    my_squad_value_m: float = Field(
        description="owner's squad value in £m (MySquadSummary.team_value / 10)"
    )
    solver_squad_cost_m: float = Field(
        description="the solver's squad total cost in £m (Squad.total_cost_m)"
    )
    best_single_transfer_delta: float | None = Field(
        description=(
            "transfer_rec.delta_distribution.best, when a transfer recommendation was "
            "computed this run; None otherwise"
        )
    )
    transfers_equivalent: float | None = Field(
        description=(
            "gain / best_single_transfer_delta, only when both the gain and the best "
            "single-transfer delta are positive; None otherwise (a wildcard cannot be "
            "worth a negative number of transfers)"
        )
    )
    kept_player_ids: list[int] = Field(
        description="owned player ids that are also in the solver's squad, sorted"
    )
    kept_count: int = Field(description="len(kept_player_ids)")
    changes_needed: int = Field(description="len(solver squad) - kept_count")
    unbacked_owned_ids: list[int] = Field(
        description=(
            "owned players with no PlayerScore or zero backers this week, EXCLUDING "
            "players the availability filter has marked EXCLUDED — an injured player "
            "never earns a PlayerScore, and reporting them as 'no creator backing' "
            "would misrepresent a fact as an absence of consensus. Sorted."
        )
    )


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
    player must not take down the whole comparison. Ids are summed in
    sorted order so the total is reproducible regardless of set
    iteration order.
    """
    total = 0.0
    for pid in sorted(player_ids_starting):
        player = db.get(pid)
        if player is None:
            continue
        multiplier = verdicts[pid].multiplier if pid in verdicts else 1.0
        total += coefficient(player, scores.get(pid), multiplier)
    for pid in sorted(player_ids_bench):
        player = db.get(pid)
        if player is None:
            continue
        multiplier = verdicts[pid].multiplier if pid in verdicts else 1.0
        total += coefficient(player, scores.get(pid), multiplier) * BENCH_WEIGHT
    return total


def best_xi(
    player_ids: set[int],
    db: PlayerDB,
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
) -> tuple[set[int], set[int]]:
    """Re-optimise the starting XI out of a 15-player pool: the single
    highest-coefficient GK, plus the best-N per outfield position under
    whichever legal formation (DEF 3-5, MID 2-5, FWD 1-3, summing to 10)
    maximises total coefficient. See the module docstring for why this
    is necessary rather than trusting either side's stored "starting"
    flag.

    Ids unknown to the DB are ignored entirely — they are excluded from
    both the returned starting set and the bench. Ties within a position
    break on the lower player_id, for a result that never depends on set
    iteration order. Returns (starting, bench); if no legal formation
    fits the pool (e.g. no GK at all), everything known falls to the
    bench and starting is empty.
    """
    ranked: dict[Position, list[tuple[float, int]]] = {p: [] for p in Position}
    for pid in sorted(player_ids):
        player = db.get(pid)
        if player is None:
            continue
        multiplier = verdicts[pid].multiplier if pid in verdicts else 1.0
        coef = coefficient(player, scores.get(pid), multiplier)
        ranked[player.position].append((coef, pid))

    for players in ranked.values():
        players.sort(key=lambda pair: (-pair[0], pair[1]))

    known_ids = {pid for players in ranked.values() for _, pid in players}

    gk = ranked[Position.GK][:1]
    if not gk:
        return set(), known_ids

    best_total: float | None = None
    best_starting: set[int] = set()
    for def_n in _DEF_RANGE:
        for mid_n in _MID_RANGE:
            for fwd_n in _FWD_RANGE:
                if def_n + mid_n + fwd_n != 10:
                    continue
                defs = ranked[Position.DEF][:def_n]
                mids = ranked[Position.MID][:mid_n]
                fwds = ranked[Position.FWD][:fwd_n]
                if len(defs) < def_n or len(mids) < mid_n or len(fwds) < fwd_n:
                    continue
                total = (
                    gk[0][0]
                    + sum(c for c, _ in defs)
                    + sum(c for c, _ in mids)
                    + sum(c for c, _ in fwds)
                )
                if best_total is None or total > best_total:
                    best_total = total
                    best_starting = (
                        {gk[0][1]}
                        | {pid for _, pid in defs}
                        | {pid for _, pid in mids}
                        | {pid for _, pid in fwds}
                    )

    if best_total is None:
        # No legal formation fits this pool (e.g. too few defenders) —
        # nothing can legally start.
        return set(), known_ids

    return best_starting, known_ids - best_starting


def _has_no_backing(player_id: int, scores: dict[int, PlayerScore]) -> bool:
    score = scores.get(player_id)
    return score is None or score.backers == 0


def _is_excluded(player_id: int, verdicts: dict[int, AvailabilityVerdict]) -> bool:
    verdict = verdicts.get(player_id)
    return verdict is not None and verdict.availability is Availability.EXCLUDED


def wildcard_gap(
    *,
    owned: list[OwnedPlayer],
    solver_squad: Squad,
    db: PlayerDB,
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
    transfer_rec: TransferRecommendation | None,
    my_squad_value_m: float,
) -> WildcardGap:
    owned_ids = {p.player_id for p in owned}
    solver_ids = {p.player_id for p in solver_squad.players}

    owned_starting, owned_bench = best_xi(owned_ids, db, scores, verdicts)
    solver_starting, solver_bench = best_xi(solver_ids, db, scores, verdicts)

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
        if best_single_transfer_delta is not None and best_single_transfer_delta > 0 and gain > 0
        else None
    )

    kept_player_ids = sorted(owned_ids & solver_ids)

    unbacked_owned_ids = sorted(
        p.player_id
        for p in owned
        if _has_no_backing(p.player_id, scores) and not _is_excluded(p.player_id, verdicts)
    )

    return WildcardGap(
        my_objective=my_objective,
        solver_objective=solver_objective,
        gain=gain,
        my_squad_value_m=my_squad_value_m,
        solver_squad_cost_m=solver_squad.total_cost_m,
        best_single_transfer_delta=best_single_transfer_delta,
        transfers_equivalent=transfers_equivalent,
        kept_player_ids=kept_player_ids,
        kept_count=len(kept_player_ids),
        changes_needed=len(solver_squad.players) - len(kept_player_ids),
        unbacked_owned_ids=unbacked_owned_ids,
    )
