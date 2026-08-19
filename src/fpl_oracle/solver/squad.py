"""PuLP integer program: the optimal 15 under FPL's rules.

FORMULATION (advised by the architect pass, decided here).

Two variable sets, not one:

    x_p in {0,1}   player p is in the 15-man squad
    y_p in {0,1}   player p starts in the XI          y_p <= x_p

    maximize  sum(coef_p * y_p)  +  BENCH_WEIGHT * sum(coef_p * x_p)

Optimising the XI rather than the flat 15 is the whole point. Only 11
players score in a gameweek, so an objective summing over all 15 will
happily spend real money on bench players — the classic beginner error.
With a separate `y`, budget flows to starters and the bench becomes the
cheap feasibility filler it should be. The small BENCH_WEIGHT term keeps
the bench from being arbitrary (a backed £4.5m defender beats an
unbacked one) without pretending to model autosub value.

THE OBJECTIVE COEFFICIENT is price-anchored:

    coef_p = price_m(p) + CONSENSUS_WEIGHT * band_score(p)

Price is the game designers' own public expected-points prior, and it is
the one cross-band yardstick creator herding cannot distort. Consensus
then perturbs a player by at most CONSENSUS_WEIGHT (one band width), so
a well-backed £4.5m enabler tops out around 5.5 — best-in-class, never
"better than Haaland". That is what lets band-relative scores drive a
single comparable objective without re-introducing the cross-band
comparison the bands exist to prevent.

Rejected alternative: using band_score alone as the coefficient. Every
band's best player scores ~1.0, so price carries no value at all and the
optimum is a squad of band-winners with tens of millions unspent.

CONSTRAINTS ARE FOR RULES, PREFERENCES ARE FOR SCORES. Hard constraints
here are only the actual game rules (squad size, positional quotas,
budget, 3-per-club) plus the captaincy-options rule. Everything else —
conviction, avoids, consensus strength — is priced into the objective.
Availability is not a constraint either: unavailable players are removed
from the pool BEFORE the solve (facts veto).
"""

from __future__ import annotations

from collections import defaultdict

import pulp
from pydantic import BaseModel, Field

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.players import Player, Position

BUDGET_M = 100.0
SQUAD_SIZE = 15
POSITION_QUOTA: dict[Position, int] = {
    Position.GK: 2,
    Position.DEF: 5,
    Position.MID: 5,
    Position.FWD: 3,
}
MAX_PER_CLUB = 3

XI_SIZE = 11
XI_MIN: dict[Position, int] = {Position.GK: 1, Position.DEF: 3, Position.FWD: 1}
XI_MAX: dict[Position, int] = {Position.GK: 1}

# £m-equivalent that full consensus support is worth. One band width, so
# consensus can promote a player past at most one price bracket — the
# cleanest rule to defend in the report.
CONSENSUS_WEIGHT = 1.0

# Bench players count this much in the objective. Non-zero so the bench
# isn't arbitrary; small so it never justifies real spending.
BENCH_WEIGHT = 0.1


class SquadPlayer(BaseModel):
    player_id: int
    starting: bool
    price_m: float
    position: Position
    band_score: float
    coefficient: float


class Squad(BaseModel):
    players: list[SquadPlayer]
    total_cost_m: float
    objective: float
    captain_options_included: list[int]
    captain_options_required: int
    relaxed_captaincy: bool = Field(
        default=False,
        description="the captaincy constraint had to be relaxed to stay feasible",
    )

    @property
    def starting_xi(self) -> list[SquadPlayer]:
        return [p for p in self.players if p.starting]

    @property
    def bench(self) -> list[SquadPlayer]:
        return [p for p in self.players if not p.starting]


class InfeasibleSquadError(RuntimeError):
    """No legal squad exists for the given pool — the pool is too small or
    too expensive. Raised only after the captaincy constraint has already
    been relaxed away, so it always means a real feasibility problem."""


def coefficient(player: Player, score: PlayerScore | None) -> float:
    """Objective coefficient: price anchor plus bounded consensus lift."""
    band_score = score.band_score if score else 0.0
    return player.price_m + CONSENSUS_WEIGHT * band_score


def _solver() -> pulp.LpSolver:
    """The CBC binding to solve with.

    PuLP deprecates PULP_CBC_CMD in favour of COIN_CMD (removal in PuLP
    4.0), but COIN_CMD needs a separately-installed CBC binary. Installing
    the `pulp[cbc]` extra makes `listSolvers(onlyAvailable=True)` REPORT
    COIN_CMD while the binary still fails to execute — so availability is
    not a usable signal and auto-selecting COIN_CMD breaks every solve.
    We therefore pin the bundled binding that actually works and accept
    the deprecation warning (filtered in pyproject). Revisit when PuLP 4.0
    lands and the packaged CBC is reliable.
    """
    return pulp.PULP_CBC_CMD(msg=False)


def _solve(
    players: list[Player],
    coefficients: dict[int, float],
    captain_options: list[int],
    required_options: int,
) -> tuple[dict[int, int], dict[int, int]] | None:
    problem = pulp.LpProblem("fpl_squad", pulp.LpMaximize)
    # prob.add_variable, not LpVariable(...) directly — the latter is
    # deprecated and removed in PuLP 4.0.
    x = {p.player_id: problem.add_variable(f"x_{p.player_id}", cat="Binary") for p in players}
    y = {p.player_id: problem.add_variable(f"y_{p.player_id}", cat="Binary") for p in players}

    problem += pulp.lpSum(
        coefficients[p.player_id] * y[p.player_id]
        + BENCH_WEIGHT * coefficients[p.player_id] * x[p.player_id]
        for p in players
    )

    # squad-level game rules
    problem += pulp.lpSum(x.values()) == SQUAD_SIZE
    problem += pulp.lpSum(p.price_m * x[p.player_id] for p in players) <= BUDGET_M
    for position, quota in POSITION_QUOTA.items():
        problem += pulp.lpSum(x[p.player_id] for p in players if p.position == position) == quota
    by_club: dict[int, list[Player]] = defaultdict(list)
    for p in players:
        by_club[p.team_id].append(p)
    for club_players in by_club.values():
        problem += pulp.lpSum(x[p.player_id] for p in club_players) <= MAX_PER_CLUB

    # starting XI
    for p in players:
        problem += y[p.player_id] <= x[p.player_id]
    problem += pulp.lpSum(y.values()) == XI_SIZE
    for position, minimum in XI_MIN.items():
        problem += pulp.lpSum(y[p.player_id] for p in players if p.position == position) >= minimum
    for position, maximum in XI_MAX.items():
        problem += pulp.lpSum(y[p.player_id] for p in players if p.position == position) <= maximum

    # captaincy: the armband decision must not be stranded outside the squad
    if required_options > 0 and captain_options:
        problem += pulp.lpSum(x[pid] for pid in captain_options if pid in x) >= required_options

    problem.solve(_solver())
    if pulp.LpStatus[problem.status] != "Optimal":
        return None
    return (
        {pid: int(round(var.value() or 0)) for pid, var in x.items()},
        {pid: int(round(var.value() or 0)) for pid, var in y.items()},
    )


def build_squad(
    players: list[Player],
    scores: dict[int, PlayerScore],
    captain_options: list[int],
    required_options: int,
) -> Squad:
    """Pick the optimal 15 (and the XI within it).

    `players` must ALREADY be availability-filtered — this function does
    not re-check, because facts veto before the solver by design.

    If the captaincy constraint makes the problem infeasible, it is
    relaxed (to 1, then dropped) and the result is flagged rather than
    failing: shipping a squad with a weaker captaincy guarantee always
    beats shipping none at the deadline.
    """
    if not players:
        raise InfeasibleSquadError("empty player pool")

    coefficients = {p.player_id: coefficient(p, scores.get(p.player_id)) for p in players}

    relaxed = False
    solution = None
    for attempt in range(required_options, -1, -1):
        solution = _solve(players, coefficients, captain_options, attempt)
        if solution is not None:
            relaxed = attempt < required_options
            required_options = attempt
            break
    if solution is None:
        raise InfeasibleSquadError(
            f"no legal 15 from a pool of {len(players)} even without the captaincy constraint"
        )

    x_values, y_values = solution
    by_id = {p.player_id: p for p in players}
    chosen = [
        SquadPlayer(
            player_id=pid,
            starting=bool(y_values[pid]),
            price_m=by_id[pid].price_m,
            position=by_id[pid].position,
            band_score=scores[pid].band_score if pid in scores else 0.0,
            coefficient=coefficients[pid],
        )
        for pid, in_squad in x_values.items()
        if in_squad
    ]
    chosen.sort(key=lambda p: (not p.starting, p.position, -p.coefficient))

    return Squad(
        players=chosen,
        total_cost_m=round(sum(p.price_m for p in chosen), 1),
        objective=round(
            sum(p.coefficient for p in chosen if p.starting)
            + BENCH_WEIGHT * sum(p.coefficient for p in chosen),
            3,
        ),
        captain_options_included=[pid for pid in captain_options if x_values.get(pid)],
        captain_options_required=required_options,
        relaxed_captaincy=relaxed,
    )
