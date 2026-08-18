"""Unit tests for fpl_oracle.roster.weights.

All expected values below are hand-derived from the module's own formulas
(log-rank scoring against the 10,000,000-size pool, the recency weights
[1.0, 0.6, 0.3], and the claim-tier shrinks/bonus/floor), not copied
from the implementation, so they'll catch a regression in the arithmetic.
"""

from __future__ import annotations

import math

import pytest

from fpl_oracle.roster.models import ClaimedFinish, PastFinish, Verification
from fpl_oracle.roster.registry import REGISTRY
from fpl_oracle.roster.weights import (
    DEFAULT_TIER2_WEIGHT,
    compute_documented_weight,
    compute_self_claimed_weight,
    compute_weight,
    weight_for,
)


class TestComputeWeight:
    def test_empty_list_returns_default(self) -> None:
        assert compute_weight([]) == DEFAULT_TIER2_WEIGHT

    def test_single_finish_uses_score_directly(self) -> None:
        # Only one recency weight (1.0) is in play, so the weighted average
        # collapses to that finish's raw rank score.
        finishes = [PastFinish(season_name="2023/24", rank=10)]
        assert compute_weight(finishes) == pytest.approx(0.8571428571428572)

    def test_two_finishes_weighted_average(self) -> None:
        finishes = [
            PastFinish(season_name="2022/23", rank=100),
            PastFinish(season_name="2023/24", rank=10),
        ]
        assert compute_weight(finishes) == pytest.approx(0.8035714285714286)

    def test_three_finishes_weighted_average(self) -> None:
        finishes = [
            PastFinish(season_name="2021/22", rank=1000),
            PastFinish(season_name="2022/23", rank=100),
            PastFinish(season_name="2023/24", rank=10),
        ]
        assert compute_weight(finishes) == pytest.approx(0.7669172932330828)

    def test_more_than_three_finishes_drops_oldest(self) -> None:
        # A 4th, oldest season (best rank of the lot) is present but must be
        # dropped entirely rather than diluting/boosting the recency window.
        finishes = [
            PastFinish(season_name="2020/21", rank=1),
            PastFinish(season_name="2021/22", rank=1000),
            PastFinish(season_name="2022/23", rank=100),
            PastFinish(season_name="2023/24", rank=10),
        ]
        assert compute_weight(finishes) == pytest.approx(0.7669172932330828)

    def test_none_rank_scores_zero(self) -> None:
        finishes = [PastFinish(season_name="2023/24", rank=None)]
        assert compute_weight(finishes) == pytest.approx(0.0)

    def test_rank_below_one_scores_zero(self) -> None:
        finishes = [PastFinish(season_name="2023/24", rank=0)]
        assert compute_weight(finishes) == pytest.approx(0.0)

    def test_rank_one_scores_maximum(self) -> None:
        finishes = [PastFinish(season_name="2023/24", rank=1)]
        assert compute_weight(finishes) == pytest.approx(1.0)

    def test_rank_at_pool_size_boundary_scores_zero(self) -> None:
        # log10(10_000_000) / log10(10_000_000) == 1.0 exactly -> score 0.0,
        # not negative.
        finishes = [PastFinish(season_name="2023/24", rank=10_000_000)]
        assert compute_weight(finishes) == pytest.approx(0.0)

    def test_rank_beyond_pool_size_clamped_to_zero(self) -> None:
        # Past the boundary the raw formula goes negative; max(0.0, ...)
        # must clamp it back to 0.0 rather than returning a negative weight.
        finishes = [PastFinish(season_name="2023/24", rank=50_000_000)]
        assert compute_weight(finishes) == pytest.approx(0.0)


class TestComputeDocumentedWeight:
    def test_empty_list_returns_default(self) -> None:
        assert compute_documented_weight([]) == DEFAULT_TIER2_WEIGHT

    def test_single_claim_no_consistency_bonus(self) -> None:
        # count=1 -> max(0, total_finishes - 1) == 0 -> no bonus, just the
        # rank score shrunk by DOCUMENTED_SHRINK.
        claims = [ClaimedFinish(description="x", rank=10, source_url="u", count=1)]
        assert compute_documented_weight(claims) == pytest.approx(0.7285714285714286)

    def test_floor_applies_for_weak_claim(self) -> None:
        # A near-bottom-of-pool rank scores ~0.0065 raw, well under
        # DEFAULT_TIER2_WEIGHT even before the shrink -- the floor must
        # bring it back up to DEFAULT_TIER2_WEIGHT rather than let a
        # documented creator score below "no evidence at all".
        claims = [ClaimedFinish(description="x", rank=9_000_000, source_url="u", count=1)]
        assert compute_documented_weight(claims) == pytest.approx(DEFAULT_TIER2_WEIGHT)

    def test_consistency_bonus_just_below_cap(self) -> None:
        # total_finishes=5 -> bonus = 0.03 * 4 = 0.12 (not yet at the 0.15 cap).
        claims = [ClaimedFinish(description="x", rank=100, source_url="u", count=5)]
        assert compute_documented_weight(claims) == pytest.approx(0.7091428571428572)

    def test_consistency_bonus_at_cap_boundary(self) -> None:
        # total_finishes=6 -> bonus = 0.03 * 5 = 0.15, exactly the cap.
        claims = [ClaimedFinish(description="x", rank=100, source_url="u", count=6)]
        assert compute_documented_weight(claims) == pytest.approx(0.7346428571428572)

    def test_consistency_bonus_plateaus_past_cap(self) -> None:
        # total_finishes=7 would compute to 0.18 uncapped; result must be
        # identical to the total_finishes=6 case, not higher.
        claims = [ClaimedFinish(description="x", rank=100, source_url="u", count=7)]
        assert compute_documented_weight(claims) == pytest.approx(0.7346428571428572)

    def test_best_score_and_bonus_capped_at_one_before_shrink(self) -> None:
        # rank=1 -> best score 1.0; count=6 -> bonus 0.15. Uncapped that's
        # 1.15, but min(1.0, ...) must clamp the sum to 1.0 before shrink,
        # giving exactly 1.0 * DOCUMENTED_SHRINK = 0.85, not 1.15 * 0.85.
        claims = [ClaimedFinish(description="x", rank=1, source_url="u", count=6)]
        assert compute_documented_weight(claims) == pytest.approx(0.85)

    def test_uses_best_ranked_claim_among_several(self) -> None:
        # Two claims with count=1 each (bonus 0) -- the weaker-ranked claim
        # listed first must not win; the better (lower) rank should.
        claims = [
            ClaimedFinish(description="weak", rank=1000, source_url="u", count=1),
            ClaimedFinish(description="strong", rank=10, source_url="u", count=1),
        ]
        assert compute_documented_weight(claims) == pytest.approx(0.7540714285714286)


class TestComputeSelfClaimedWeight:
    def test_empty_list_returns_default(self) -> None:
        assert compute_self_claimed_weight([]) == DEFAULT_TIER2_WEIGHT

    def test_single_claim_no_consistency_bonus(self) -> None:
        # Same shape as compute_documented_weight's equivalent test, but on
        # SELF_CLAIMED_SHRINK (0.75): (6/7) * 0.75.
        claims = [ClaimedFinish(description="x", rank=10, source_url="u", count=1)]
        assert compute_self_claimed_weight(claims) == pytest.approx(0.6428571428571429)

    def test_floor_applies_for_weak_claim(self) -> None:
        # Mirrors compute_documented_weight's floor case: a near-bottom-of-
        # pool rank must still floor at DEFAULT_TIER2_WEIGHT, not score
        # below "no evidence at all".
        claims = [ClaimedFinish(description="x", rank=9_000_000, source_url="u", count=1)]
        assert compute_self_claimed_weight(claims) == pytest.approx(DEFAULT_TIER2_WEIGHT)

    def test_best_score_and_bonus_capped_at_one_before_shrink(self) -> None:
        # Mirrors compute_documented_weight's cap case: rank=1 -> best score
        # 1.0, count=6 -> bonus 0.15, but min(1.0, ...) must clamp the sum to
        # 1.0 before shrink: exactly 1.0 * SELF_CLAIMED_SHRINK = 0.75, not
        # 1.15 * 0.75.
        claims = [ClaimedFinish(description="x", rank=1, source_url="u", count=6)]
        assert compute_self_claimed_weight(claims) == pytest.approx(0.75)


class TestEvidenceOrdering:
    def test_documented_beats_self_claimed_beats_default_for_same_claim(self) -> None:
        # Same single claim (rank=10, strong enough to clear the floor
        # under both shrinks) run through both claim-based tiers: the more
        # corroborated tier must score strictly higher, and even the
        # weaker (self-claimed) tier must still beat "no evidence at all".
        claims = [ClaimedFinish(description="x", rank=10, source_url="u", count=1)]
        documented = compute_documented_weight(claims)
        self_claimed = compute_self_claimed_weight(claims)
        assert documented > self_claimed > DEFAULT_TIER2_WEIGHT

    def test_ordering_partially_collapses_when_self_claimed_hits_floor(self) -> None:
        # In the band where the raw score clears the floor under the 0.85
        # shrink but not the 0.75 one, the ordering intentionally degrades
        # to documented > self_claimed == default. rank=200_000: raw score
        # 1 - log10(2e5)/7 ~= 0.24271; documented 0.24271*0.85 ~= 0.20630,
        # self-claimed 0.24271*0.75 ~= 0.18203 -> floored to 0.2.
        claims = [ClaimedFinish(description="x", rank=200_000, source_url="u", count=1)]
        assert compute_documented_weight(claims) == pytest.approx(0.2063035005265164)
        assert compute_self_claimed_weight(claims) == pytest.approx(DEFAULT_TIER2_WEIGHT)


class TestWeightFor:
    def test_api_verification_routes_to_compute_weight(self) -> None:
        finishes = [PastFinish(season_name="2023/24", rank=10)]
        assert weight_for(Verification.API, past_finishes=finishes) == compute_weight(finishes)

    def test_api_verification_with_no_finishes_uses_default(self) -> None:
        assert weight_for(Verification.API, past_finishes=None) == DEFAULT_TIER2_WEIGHT
        assert weight_for(Verification.API, past_finishes=[]) == DEFAULT_TIER2_WEIGHT

    def test_documented_verification_routes_to_compute_documented_weight(self) -> None:
        claims = [ClaimedFinish(description="x", rank=10, source_url="u", count=1)]
        assert weight_for(
            Verification.DOCUMENTED, documented_finishes=claims
        ) == compute_documented_weight(claims)

    def test_documented_verification_with_no_claims_uses_default(self) -> None:
        assert weight_for(Verification.DOCUMENTED, documented_finishes=None) == DEFAULT_TIER2_WEIGHT
        assert weight_for(Verification.DOCUMENTED, documented_finishes=[]) == DEFAULT_TIER2_WEIGHT

    def test_self_claimed_verification_routes_to_compute_self_claimed_weight(self) -> None:
        claims = [ClaimedFinish(description="x", rank=10, source_url="u", count=1)]
        assert weight_for(
            Verification.SELF_CLAIMED, self_claimed_finishes=claims
        ) == compute_self_claimed_weight(claims)

    def test_self_claimed_verification_with_no_claims_uses_default(self) -> None:
        assert (
            weight_for(Verification.SELF_CLAIMED, self_claimed_finishes=None)
            == DEFAULT_TIER2_WEIGHT
        )
        assert (
            weight_for(Verification.SELF_CLAIMED, self_claimed_finishes=[]) == DEFAULT_TIER2_WEIGHT
        )

    def test_self_claimed_verification_ignores_extraneous_other_tier_data(self) -> None:
        # Extraneous data for the "wrong" tiers must be ignored, not
        # consulted, when dispatching on SELF_CLAIMED.
        finishes = [PastFinish(season_name="2023/24", rank=1)]
        documented_claims = [ClaimedFinish(description="x", rank=1, source_url="u", count=10)]
        self_claims = [ClaimedFinish(description="x", rank=10, source_url="u", count=1)]
        assert weight_for(
            Verification.SELF_CLAIMED,
            past_finishes=finishes,
            documented_finishes=documented_claims,
            self_claimed_finishes=self_claims,
        ) == compute_self_claimed_weight(self_claims)

    def test_unverified_returns_default_regardless_of_inputs(self) -> None:
        finishes = [PastFinish(season_name="2023/24", rank=1)]
        claims = [ClaimedFinish(description="x", rank=1, source_url="u", count=10)]
        self_claims = [ClaimedFinish(description="x", rank=1, source_url="u", count=10)]
        assert weight_for(Verification.UNVERIFIED) == DEFAULT_TIER2_WEIGHT
        # Extraneous data for the "wrong" tier must be ignored, not consulted.
        assert (
            weight_for(
                Verification.UNVERIFIED,
                past_finishes=finishes,
                documented_finishes=claims,
                self_claimed_finishes=self_claims,
            )
            == DEFAULT_TIER2_WEIGHT
        )


def test_registry_creators_all_have_positive_finite_weight() -> None:
    assert REGISTRY, "registry should not be empty"
    for creator in REGISTRY:
        assert creator.weight is not None, f"{creator.creator_id} has no weight set"
        assert math.isfinite(creator.weight), f"{creator.creator_id} weight is not finite"
        assert creator.weight > 0.0, f"{creator.creator_id} weight is not positive"
