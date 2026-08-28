"""In-season transfer recommender: given the squad the owner ALREADY has,
is any single change worth making?

WHY THIS ISN'T `build_squad`. `solver/squad.py` answers a cold-start
question — "pick 15 from £100m" — which is asked once a season. It is
still the right tool for a wildcard or free hit. In-season the question
inverts: the owner holds 15 players and (usually) one free transfer, and
a fresh optimal 15 is unusable advice because it implies ten transfers
they cannot make.

WHY ENUMERATION AND NOT AN ILP. The real decision space with one free
transfer is small enough to enumerate exactly — 15 owned x the
same-position candidates, a few hundred evaluations. An ILP would buy no
extra search power here, and it would cost two things: its objective is a
squad-CONSTRUCTION prior whose price term rewards churning toward
expensive players, and a solver that proposes an N-transfer plan
implicitly claims it can price N x (-4), which nothing in this system
can. Enumeration also carries the explanation for free: every move keeps
the votes behind it.

WHY MOVES ARE RANKED BY COEFFICIENT DELTA, NOT BAND-SCORE DELTA. Band
scores are normalised WITHIN a (position, price) band, so they do not
compare across bands: a £4.0m band-winner and a £12.0m band-winner both
sit near 1.0 and their difference is noise. `solver.squad.coefficient` is
the project's one sanctioned cross-band yardstick, so the ranking key is
`coefficient(in) - coefficient(out)`.

WHAT THIS DELIBERATELY WILL NOT DO. It will not recommend taking a -4 on
consensus strength. Band scores have no points unit, so any threshold
claiming "this gain is worth 4 points" would be a fabricated conversion
wearing a constant's clothing. Hits are recommended ONLY when forced by
facts — an unavailable player the squad cannot cover. Moves that would
need a hit but carry real backing are SURFACED for the owner to judge,
with the evidence attached, and never endorsed by the system.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict
from fpl_oracle.fpl.players import Player, Position
from fpl_oracle.fpl.selling import SquadPricing
from fpl_oracle.solver.squad import coefficient

MAX_PER_CLUB = 3

# How many alternatives to keep per outgoing player before ranking, and
# how many moves to show. Enumeration is cheap; the cap is about a report
# a human will actually read.
SHORTLIST_SIZE = 5

# Minimum distinct creators backing an incoming player before a move can
# be RECOMMENDED (as opposed to merely listed). Consensus here is sparse
# — ~13 creators, ~20% of picks lost to name resolution — so a single
# enthusiastic voice is noise, not a signal.
MIN_BACKERS_TO_RECOMMEND = 3

# Coefficient-delta bar for spending a free transfer absent any negative
# signal on the outgoing player. Half of one band width (CONSENSUS_WEIGHT
# is 1.0). THIS NUMBER IS EMPIRICAL AND UNCALIBRATED — it encodes "clearly
# better, not marginally" and nothing more principled than that. It is
# stated here rather than buried so it can be tuned against logged
# outcomes instead of argued about.
MIN_DELTA_TO_RECOMMEND = 0.5


class Verdict(StrEnum):
    ROLL = "ROLL"
    USE_FREE_TRANSFER = "USE_FREE_TRANSFER"
    FORCED_MOVE = "FORCED_MOVE"
    NO_LEGAL_MOVE = "NO_LEGAL_MOVE"


class Affordability(StrEnum):
    AFFORDABLE = "AFFORDABLE"
    TOO_EXPENSIVE = "TOO_EXPENSIVE"
    UNVERIFIED = "UNVERIFIED"


class CandidateMove(BaseModel):
    """One single-transfer swap, with every input to the decision kept."""

    out_player_id: int
    out_name: str
    in_player_id: int
    in_name: str
    position: Position

    delta: float = Field(description="coefficient(in) - coefficient(out); the ranking key")
    price_delta_m: float
    consensus_delta: float = Field(description="band_score(in) - band_score(out)")

    in_backers: int
    in_detractors: int
    out_backers: int
    out_detractors: int

    affordability: Affordability
    budget_m: float | None = Field(
        description="selling price of the outgoing player plus bank; None if underivable"
    )
    out_availability: Availability
    in_availability: Availability
    forced: bool = Field(
        default=False, description="the outgoing player is EXCLUDED — this move fixes a hole"
    )
    notes: list[str] = Field(default_factory=list)


class TransferRecommendation(BaseModel):
    """Always fully populated, including when the answer is 'do nothing'.

    A roll is a real recommendation, not an empty result: it says the
    squad is fine, banks a transfer for next week, and still carries the
    armband call and the shortlist that was considered and rejected. An
    honest null beats a fabricated action — the same principle as
    `pipeline.NoConsensusEvidenceError`.
    """

    verdict: Verdict
    gameweek: int | None
    free_transfers: int
    recommended: CandidateMove | None
    shortlist: list[CandidateMove]
    forced_moves: list[CandidateMove]
    worth_considering: list[CandidateMove] = Field(
        default_factory=list,
        description=(
            "moves with real backing that this system will NOT endorse, because "
            "endorsing them would mean pricing a -4 in units it does not have"
        ),
    )
    reasoning: str
    pricing_note: str
    unavailable_owned: list[str] = Field(default_factory=list)


def _club_counts(owned: list[Player]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for p in owned:
        counts[p.team_id] = counts.get(p.team_id, 0) + 1
    return counts


def _respects_club_limit(out_player: Player, in_player: Player, counts: dict[int, int]) -> bool:
    if in_player.team_id == out_player.team_id:
        return True
    return counts.get(in_player.team_id, 0) + 1 <= MAX_PER_CLUB


def _availability_of(
    player_id: int, verdicts: dict[int, AvailabilityVerdict]
) -> tuple[Availability, float]:
    verdict = verdicts.get(player_id)
    if verdict is None:
        return Availability.AVAILABLE, 1.0
    return verdict.availability, verdict.multiplier


def build_moves(
    *,
    owned: list[Player],
    pool: list[Player],
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
    pricing: SquadPricing,
) -> list[CandidateMove]:
    """Every legal single transfer, ranked best-first by coefficient delta.

    Legality is three checks. Same position keeps the 2/5/5/3 quotas
    intact automatically and cannot break XI formation legality, so
    neither needs re-checking. Club limit is evaluated AFTER the swap.
    Affordability uses derived selling price, and is reported as
    UNVERIFIED rather than assumed when the derivation didn't reconcile.
    """
    owned_ids = {p.player_id for p in owned}
    counts = _club_counts(owned)
    moves: list[CandidateMove] = []

    for out_player in owned:
        out_avail, out_mult = _availability_of(out_player.player_id, verdicts)
        out_score = scores.get(out_player.player_id)
        out_coef = coefficient(out_player, out_score, out_mult)
        budget = pricing.budget_for(out_player.player_id)

        for in_player in pool:
            if in_player.player_id in owned_ids:
                continue
            if in_player.position is not out_player.position:
                continue
            if not _respects_club_limit(out_player, in_player, counts):
                continue

            in_avail, in_mult = _availability_of(in_player.player_id, verdicts)
            if in_avail is Availability.EXCLUDED:
                continue

            if budget is None:
                affordability = Affordability.UNVERIFIED
            elif in_player.now_cost > budget:
                affordability = Affordability.TOO_EXPENSIVE
            elif not pricing.verified:
                affordability = Affordability.UNVERIFIED
            else:
                affordability = Affordability.AFFORDABLE

            if affordability is Affordability.TOO_EXPENSIVE:
                continue

            in_score = scores.get(in_player.player_id)
            in_coef = coefficient(in_player, in_score, in_mult)

            notes: list[str] = []
            if affordability is Affordability.UNVERIFIED:
                notes.append(
                    "affordability unverified — confirm in the FPL app before transferring"
                )

            moves.append(
                CandidateMove(
                    out_player_id=out_player.player_id,
                    out_name=out_player.web_name,
                    in_player_id=in_player.player_id,
                    in_name=in_player.web_name,
                    position=out_player.position,
                    delta=in_coef - out_coef,
                    price_delta_m=in_player.price_m - out_player.price_m,
                    consensus_delta=(in_score.band_score if in_score else 0.0)
                    - (out_score.band_score if out_score else 0.0),
                    in_backers=in_score.backers if in_score else 0,
                    in_detractors=in_score.detractors if in_score else 0,
                    out_backers=out_score.backers if out_score else 0,
                    out_detractors=out_score.detractors if out_score else 0,
                    affordability=affordability,
                    budget_m=None if budget is None else budget / 10,
                    out_availability=out_avail,
                    in_availability=in_avail,
                    forced=out_avail is Availability.EXCLUDED,
                    notes=notes,
                )
            )

    moves.sort(key=lambda m: m.delta, reverse=True)
    return moves


def best_per_outgoing(moves: list[CandidateMove], limit: int) -> list[CandidateMove]:
    """One row per outgoing player, best-first.

    The raw ranking clusters: an unavailable or poorly-backed player has a
    depressed coefficient, so every swap involving them outranks everything
    else and the top of the list becomes five ways to replace one person.
    That is the right ANSWER but a poor SHORTLIST — the owner wants to see
    which of their fifteen is the weak link, not five variations on one.
    """
    seen: set[int] = set()
    out: list[CandidateMove] = []
    for move in moves:
        if move.out_player_id in seen:
            continue
        seen.add(move.out_player_id)
        out.append(move)
        if len(out) == limit:
            break
    return out


def recommend_transfer(
    *,
    owned: list[Player],
    pool: list[Player],
    scores: dict[int, PlayerScore],
    verdicts: dict[int, AvailabilityVerdict],
    pricing: SquadPricing,
    free_transfers: int | None,
    gameweek: int | None = None,
) -> TransferRecommendation:
    """The whole decision: roll, use the free transfer, or fix a hole.

    `free_transfers=None` means the derivation refused to guess; treat it
    as 1, the true lower bound, since accrual guarantees at least one
    every gameweek. Say so in the output rather than silently assuming.
    """
    ft_note = ""
    if free_transfers is None:
        free_transfers = 1
        ft_note = " Free-transfer count could not be derived; assuming 1 (the guaranteed minimum)."

    moves = build_moves(owned=owned, pool=pool, scores=scores, verdicts=verdicts, pricing=pricing)
    unavailable = [
        p.web_name
        for p in owned
        if _availability_of(p.player_id, verdicts)[0] is Availability.EXCLUDED
    ]
    forced = [m for m in moves if m.forced]
    shortlist = best_per_outgoing(moves, SHORTLIST_SIZE)

    if not moves:
        return TransferRecommendation(
            verdict=Verdict.NO_LEGAL_MOVE,
            gameweek=gameweek,
            free_transfers=free_transfers,
            recommended=None,
            shortlist=[],
            forced_moves=[],
            reasoning=(
                "No legal single transfer was found at all. With a full squad that usually "
                "means the candidate pool is empty — check that extraction actually ran for "
                "this gameweek." + ft_note
            ),
            pricing_note=pricing.note,
            unavailable_owned=unavailable,
        )

    # Facts first: an unavailable player is a hole in the XI, and fixing
    # it is the one case where a move is compelled rather than judged.
    if forced:
        best_forced = forced[0]
        return TransferRecommendation(
            verdict=Verdict.FORCED_MOVE,
            gameweek=gameweek,
            free_transfers=free_transfers,
            recommended=best_forced,
            shortlist=shortlist,
            forced_moves=forced[:SHORTLIST_SIZE],
            reasoning=(
                f"{best_forced.out_name} is unavailable, so this is a move forced by facts "
                f"rather than a judgement call on form. Best available replacement is "
                f"{best_forced.in_name} ({best_forced.in_backers} creator backers)." + ft_note
            ),
            pricing_note=pricing.note,
            unavailable_owned=unavailable,
        )

    best = moves[0]
    out_is_disliked = best.out_detractors > 0 or best.out_availability is Availability.DOUBTFUL
    well_backed = best.in_backers >= MIN_BACKERS_TO_RECOMMEND
    clears_bar = best.delta >= MIN_DELTA_TO_RECOMMEND or out_is_disliked

    if well_backed and clears_bar:
        return TransferRecommendation(
            verdict=Verdict.USE_FREE_TRANSFER,
            gameweek=gameweek,
            free_transfers=free_transfers,
            recommended=best,
            shortlist=shortlist,
            forced_moves=[],
            reasoning=(
                f"{best.in_name} is backed by {best.in_backers} creators and improves the "
                f"squad's coefficient by {best.delta:+.2f} over {best.out_name}"
                + (
                    f", who has {best.out_detractors} creator(s) actively against them"
                    if best.out_detractors
                    else ""
                )
                + "."
                + ft_note
            ),
            pricing_note=pricing.note,
            unavailable_owned=unavailable,
        )

    # Nothing cleared the bar. That is a real answer, and rolling banks
    # the transfer — worth more than a marginal move made for its own sake.
    considering = [m for m in moves[:SHORTLIST_SIZE] if m.in_backers >= MIN_BACKERS_TO_RECOMMEND]
    why = (
        f"best available move ({best.out_name} -> {best.in_name}) has only "
        f"{best.in_backers} creator backer(s)"
        if not well_backed
        else (
            f"best available move ({best.out_name} -> {best.in_name}) gains only "
            f"{best.delta:+.2f}, below the {MIN_DELTA_TO_RECOMMEND} bar, and nobody is "
            f"arguing against {best.out_name}"
        )
    )
    return TransferRecommendation(
        verdict=Verdict.ROLL,
        gameweek=gameweek,
        free_transfers=free_transfers,
        recommended=None,
        shortlist=shortlist,
        forced_moves=[],
        worth_considering=considering,
        reasoning=(
            f"Roll the transfer: {why}. Banking it leaves "
            f"{min(free_transfers + 1, 5)} for next gameweek." + ft_note
        ),
        pricing_note=pricing.note,
        unavailable_owned=unavailable,
    )
