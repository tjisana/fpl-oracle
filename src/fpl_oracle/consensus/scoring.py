"""Weighted consensus scoring: resolved picks -> per-player scores.

Design (settled in the fpl-domain skill, formulation advised by the
architect pass and decided here):

PRICE BANDS. Votes only compare WITHIN a price band, because cheap
players attract more votes purely by being affordable — 7 creators
backing a £4.5m keeper means "best budget keeper", never "better than
Haaland". A band is (position, £1.0m bucket). `band_score` is the
band-relative number; turning that into a solver objective is the
SOLVER's job (it anchors on price), not this module's. Keeping the two
apart is what stops cross-band comparison creeping back in.

ONE VOTE PER CREATOR PER PLAYER. A creator who says "Haaland is in my
squad" and later "Haaland is my captain" produced two picks for one
opinion — 17 such pairs in the first live run. We take the strongest
signal, never the sum, or repetition would inflate a single view.

CAPTAIN PICKS COUNT AS INCLUSION HERE. "Captaincy never blends into
general scores" means the captaincy CEILING signal lives only in the
separate election (`consensus/captaincy.py`) — but a creator naming
someone captain has self-evidently included him, so the pick is worth a
plain squad_include to this score and no more.

AVOID IS A NEGATIVE SCORE, NOT AN EXCLUSION. Constraints are for rules,
preferences are for scores. Only availability facts veto. A hard
exclusion would hand a minority a veto over exactly the polarising
premiums where the decision matters most (Haaland: 11 backers, 2
captain votes, and 2 avoids).

NO HERDING DISCOUNT IN V1 — deliberately. With 14 creators and one
gameweek, vote correlation cannot be told apart from convergent correct
inference: everyone watched the same fixtures at the same prices, so a
correlation penalty mostly punishes agreeing with the consensus, which
is the signal we are farming. The worst correlation was already removed
structurally in Phase 1 (The FPL Wire's three co-hosts count once via
Pras; FFH's personas are title-split). The gameweek report must still
disclose that effective independent opinions are fewer than the headline
creator count. Revisit with real data in v2.
"""

from __future__ import annotations

import math
from collections import defaultdict

from pydantic import BaseModel, Field

from fpl_oracle.extract.schemas import PickAction
from fpl_oracle.fpl.players import Player

# How much of a creator's weight each action carries. squad_include is the
# unit. captain/vice count as inclusion here (their ceiling signal lives in
# the captaincy election). watchlist is real but soft interest; avoid is
# negative but cheaper than -1.0: an avoid commits no budget, and
# "trap team" content is its own engagement genre, so avoids are
# structurally cheaper talk than includes.
ACTION_VALUE: dict[PickAction, float] = {
    PickAction.SQUAD_INCLUDE: 1.0,
    PickAction.TRANSFER_IN: 1.0,
    PickAction.CAPTAIN: 1.0,
    PickAction.VICE: 1.0,
    PickAction.BENCH: 0.6,
    PickAction.WATCHLIST: 0.3,
    PickAction.TRANSFER_OUT: -0.5,
    PickAction.AVOID: -0.6,
}

# Saturating, NOT linear. Conviction is itself an LLM's read of a
# creator's rhetoric — the noisiest field in the extraction schema — so a
# linear 1-5 multiplier would give the noisiest signal 5x leverage. This
# caps the spread at 4x and keeps the busiest region (conviction 2 vs 4)
# at a defensible 1.8x.
CONVICTION_FACTOR: dict[int, float] = {1: 0.25, 2: 0.5, 3: 0.75, 4: 0.9, 5: 1.0}

# Price bucket width for banding, in £m.
BAND_WIDTH_M = 1.0

# Shrinkage in the band-score denominator, in units of weighted vote —
# roughly one median creator weight. This is what makes thin bands safe
# WITHOUT merging them: a band's score is W/(band_max + c), so a lone
# occupant is scored against himself plus the shrinkage. A well-backed
# singleton lands ~0.90, a barely-backed one ~0.40 — the guard we need,
# with no cross-band contamination.
#
# An earlier draft merged thin bands down into the next cheaper band. On
# real data that walked Haaland (£15.5m, the only voted player in his
# bracket) four bands down to be scored against £6.0m forwards — exactly
# the cross-band comparison banding exists to prevent, just inverted.
# Shrinkage alone does the job; merging does harm.
BAND_SHRINKAGE = 0.3


class CreatorVote(BaseModel):
    """One creator's single strongest opinion on one player."""

    creator_id: str
    creator_weight: float
    action: PickAction
    conviction: int
    value: float = Field(description="creator_weight * action_value * conviction_factor")


class PlayerScore(BaseModel):
    """Consensus outcome for one player. `band_score` is the number the
    solver consumes; `weighted_votes` and `votes` drive the report."""

    player_id: int
    band: str
    weighted_votes: float
    band_score: float = Field(description="0-1 within the price band; >0.5 is well backed")
    backers: int = Field(description="creators with a positive-valued opinion")
    detractors: int = Field(description="creators with a negative-valued opinion")
    votes: list[CreatorVote]


def vote_value(creator_weight: float, action: PickAction, conviction: int) -> float:
    """One (creator, player) opinion's contribution, before aggregation."""
    return creator_weight * ACTION_VALUE[action] * CONVICTION_FACTOR[conviction]


def band_of(player: Player) -> str:
    """(position, £1.0m bucket) — e.g. 'MID:7.0'. Bucket by floor so
    £7.0m and £7.5m share a band, which is the granularity creators
    actually reason about."""
    bucket = math.floor(player.price_m / BAND_WIDTH_M) * BAND_WIDTH_M
    return f"{player.position.value}:{bucket:.1f}"


def score_picks(
    picks_by_creator: dict[str, list[tuple[int, PickAction, int]]],
    creator_weights: dict[str, float],
    players: dict[int, Player],
) -> dict[int, PlayerScore]:
    """Aggregate resolved picks into per-player consensus scores.

    `picks_by_creator` maps creator_id -> [(player_id, action, conviction)].
    Only players present in `players` are scored (callers pass the resolved,
    availability-filtered pool).
    """
    # 1. one strongest vote per (creator, player)
    strongest: dict[tuple[str, int], CreatorVote] = {}
    for creator_id, picks in picks_by_creator.items():
        weight = creator_weights.get(creator_id, 0.0)
        for player_id, action, conviction in picks:
            if player_id not in players:
                continue
            value = vote_value(weight, action, conviction)
            key = (creator_id, player_id)
            current = strongest.get(key)
            # "strongest" = furthest from neutral, so a lone avoid still
            # registers, but an include beats a watchlist for the same player
            if current is None or abs(value) > abs(current.value):
                strongest[key] = CreatorVote(
                    creator_id=creator_id,
                    creator_weight=weight,
                    action=action,
                    conviction=conviction,
                    value=value,
                )

    # 2. sum into per-player totals
    votes_by_player: dict[int, list[CreatorVote]] = defaultdict(list)
    for (_creator_id, player_id), vote in strongest.items():
        votes_by_player[player_id].append(vote)
    weighted: dict[int, float] = {
        pid: sum(v.value for v in votes) for pid, votes in votes_by_player.items()
    }

    # 3. the best-backed player in each band sets that band's denominator
    band_max: dict[str, float] = defaultdict(float)
    for player_id in votes_by_player:
        band = band_of(players[player_id])
        band_max[band] = max(band_max[band], weighted[player_id])

    # 4. band-relative score
    scores: dict[int, PlayerScore] = {}
    for player_id, player_votes in votes_by_player.items():
        ranked: list[CreatorVote] = sorted(player_votes, key=lambda vote: -vote.value)
        band = band_of(players[player_id])
        denominator = band_max[band] + BAND_SHRINKAGE
        total = weighted[player_id]
        scores[player_id] = PlayerScore(
            player_id=player_id,
            band=band,
            weighted_votes=total,
            # negatives clamp to 0: a disliked player is "no consensus
            # support", and the solver's objective has no meaning below zero
            band_score=max(0.0, total / denominator) if denominator > 0 else 0.0,
            backers=sum(1 for v in player_votes if v.value > 0),
            detractors=sum(1 for v in player_votes if v.value < 0),
            votes=ranked,
        )
    return scores
