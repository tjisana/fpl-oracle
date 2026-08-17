"""Rank-based creator weights from verified FPL history.

Per the fpl-domain skill: skill is verified via `entry/{id}/history/`
past seasons, recency-weighted (last 3 seasons matter most). Popularity
(subscribers) is discovery signal only, never weight. Weights are frozen
for v1 — computed once here, not updated in-season.
"""

from __future__ import annotations

import math

from fpl_oracle.roster.models import ClaimedFinish, PastFinish, Verification

# Roughly the size of the FPL player pool a rank is drawn from — used only
# to normalize _rank_score onto a 0-1 scale, doesn't need to be exact.
_APPROX_PLAYER_POOL = 10_000_000

# Fallback weight for creators with no substantiated skill claim at all
# (Verification.UNVERIFIED). Deliberately low: it must sit below what a
# genuinely good verified history would score (see _rank_score), so
# verification is actually rewarded rather than being a wash. 0.2 sits
# below a top-400k (top ~4%) verified finish (_rank_score(400_000) ≈ 0.2)
# — "no evidence of skill", not "known-bad".
DEFAULT_TIER2_WEIGHT = 0.2

# Verification.DOCUMENTED (reputable source, no pullable ID) is weighted
# on the same log-rank scale as an API-verified finish, then discounted
# by this factor — strong evidence, but not independently auditable via
# our own client the way an API-verified history is.
DOCUMENTED_SHRINK = 0.85

# How many most-recent seasons count, and their relative recency weight
# (index 0 = most recent). Older seasons are dropped entirely.
_RECENCY_WEIGHTS = [1.0, 0.6, 0.3]


def _season_sort_key(finish: PastFinish) -> str:
    return finish.season_name


def _rank_score(rank: int | None) -> float:
    """0-1, higher is better. Log-scaled against the player pool size so
    the gap between rank 100 and 1,000 matters far more than the gap
    between rank 1,000,000 and 1,900,000."""
    if rank is None or rank < 1:
        return 0.0
    return max(0.0, 1.0 - math.log10(rank) / math.log10(_APPROX_PLAYER_POOL))


def compute_weight(past_finishes: list[PastFinish]) -> float:
    """Recency-weighted, log-rank-based weight from verified season
    history. Returns DEFAULT_TIER2_WEIGHT if there's no history to work
    from (e.g. an unverified creator)."""
    if not past_finishes:
        return DEFAULT_TIER2_WEIGHT

    recent = sorted(past_finishes, key=_season_sort_key, reverse=True)[: len(_RECENCY_WEIGHTS)]
    total_weight = 0.0
    weighted_score = 0.0
    for finish, recency in zip(recent, _RECENCY_WEIGHTS, strict=False):
        weighted_score += _rank_score(finish.rank) * recency
        total_weight += recency
    return weighted_score / total_weight if total_weight else DEFAULT_TIER2_WEIGHT


def compute_documented_weight(claims: list[ClaimedFinish]) -> float:
    """Weight for Verification.DOCUMENTED. Scores the single best-ranked
    claim on the same log-rank scale as an API-verified finish, adds a
    small bonus for corroborating repeat finishes (summed across claims'
    `count`, capped so repetition alone can't reach an API-tier score),
    then applies DOCUMENTED_SHRINK. Floored at DEFAULT_TIER2_WEIGHT: unlike
    an API-verified history (which can show real evidence of being bad),
    reaching this tier at all means a reputable source documented a real
    achievement — a weak one shouldn't end up scoring below "no evidence"."""
    if not claims:
        return DEFAULT_TIER2_WEIGHT
    best = max(_rank_score(c.rank) for c in claims)
    total_finishes = sum(c.count for c in claims)
    consistency_bonus = min(0.15, 0.03 * max(0, total_finishes - 1))
    return max(DEFAULT_TIER2_WEIGHT, min(1.0, best + consistency_bonus) * DOCUMENTED_SHRINK)


def weight_for(
    verification: Verification,
    past_finishes: list[PastFinish] | None = None,
    documented_finishes: list[ClaimedFinish] | None = None,
) -> float:
    """Single entry point: dispatches to the right weight calculation for
    a creator's verification tier."""
    if verification == Verification.API:
        return compute_weight(past_finishes or [])
    if verification == Verification.DOCUMENTED:
        return compute_documented_weight(documented_finishes or [])
    return DEFAULT_TIER2_WEIGHT
