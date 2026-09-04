"""Tests for consensus/chips.py: tallying creators' own stated chip-timing
intentions. Pure logic, no network, no fixtures beyond hand-built
`VideoExtraction`/`ChipPlan` objects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fpl_oracle.consensus.chips import ChipConsensus, ChipVote, summarize_chip_plans
from fpl_oracle.extract.schemas import Chip, ChipPlan, Provenance, VideoExtraction


def _plan(
    chip: Chip = Chip.WILDCARD,
    target_gameweek: int | None = None,
    conviction: int = 3,
    reasoning: str = "test reasoning",
    provenance: Provenance = Provenance.PERSONAL,
) -> ChipPlan:
    return ChipPlan(
        chip=chip,
        target_gameweek=target_gameweek,
        conviction=conviction,
        reasoning=reasoning,
        provenance=provenance,
    )


def _extraction(
    creator_id: str,
    chip_plans: list[ChipPlan],
    video_id: str | None = None,
    gameweek: int | None = None,
) -> VideoExtraction:
    return VideoExtraction(
        creator_id=creator_id,
        video_id=video_id or f"{creator_id}-video",
        video_title="Test video",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        gameweek=gameweek,
        picks=[],
        chip_plans=chip_plans,
    )


# ---------------------------------------------------------------------------
# Basic shape: tallies always one per Chip, in enum order, even if empty.
# ---------------------------------------------------------------------------


class TestShape:
    def test_no_extractions_yields_one_empty_tally_per_chip_in_enum_order(self) -> None:
        consensus = summarize_chip_plans([], {}, gameweek=3)
        assert [t.chip for t in consensus.tallies] == list(Chip)
        for tally in consensus.tallies:
            assert tally.this_gameweek == []
            assert tally.later == []
            assert tally.unspecified == []
            assert tally.this_gameweek_weight == 0.0
            assert tally.this_gameweek_share == 0.0
        assert consensus.contributing_creators == 0
        assert consensus.contributing_weight == 0.0
        assert consensus.creators_with_plans == 0
        assert consensus.stale == []

    def test_extraction_with_no_chip_plans_still_counts_as_contributing(self) -> None:
        extraction = _extraction("alice", [])
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        assert consensus.contributing_creators == 1
        assert consensus.contributing_weight == 1.0
        assert consensus.creators_with_plans == 0


# ---------------------------------------------------------------------------
# Dedupe: one vote per (creator, chip).
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_highest_conviction_wins(self) -> None:
        extraction = _extraction(
            "alice",
            [
                _plan(target_gameweek=5, conviction=2),
                _plan(target_gameweek=6, conviction=5),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert wildcard.chip is Chip.WILDCARD
        votes = wildcard.later
        assert len(votes) == 1
        assert votes[0].conviction == 5
        assert votes[0].target_gameweek == 6

    def test_tie_break_prefers_target_equal_to_gameweek(self) -> None:
        extraction = _extraction(
            "alice",
            [
                _plan(target_gameweek=4, conviction=3),
                _plan(target_gameweek=3, conviction=3),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert len(wildcard.this_gameweek) == 1
        assert wildcard.this_gameweek[0].target_gameweek == 3
        assert wildcard.later == []

    def test_tie_break_then_prefers_lowest_non_none_target(self) -> None:
        extraction = _extraction(
            "alice",
            [
                _plan(target_gameweek=8, conviction=3),
                _plan(target_gameweek=5, conviction=3),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert len(wildcard.later) == 1
        assert wildcard.later[0].target_gameweek == 5

    def test_tie_break_finally_prefers_none_only_over_a_non_matching_dated_plan(self) -> None:
        """`None` is the last-resort tie-break preference, behind both a
        target matching `gameweek` and any non-None target — so a `None`
        plan only wins when it is the sole survivor of the earlier
        criteria, i.e. every other candidate at the same conviction is
        itself a `None`-target plan."""
        extraction = _extraction(
            "alice",
            [
                _plan(target_gameweek=None, conviction=3, reasoning="first"),
                _plan(target_gameweek=None, conviction=3, reasoning="second"),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert len(wildcard.unspecified) == 1
        # Deterministic pick among equal keys: the first one in input order.
        assert wildcard.unspecified[0].reasoning == "first"

    def test_dedupe_is_per_chip_not_global(self) -> None:
        extraction = _extraction(
            "alice",
            [
                _plan(chip=Chip.WILDCARD, target_gameweek=5, conviction=3),
                _plan(chip=Chip.FREE_HIT, target_gameweek=6, conviction=3),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        by_chip = {t.chip: t for t in consensus.tallies}
        assert len(by_chip[Chip.WILDCARD].later) == 1
        assert len(by_chip[Chip.FREE_HIT].later) == 1


# ---------------------------------------------------------------------------
# Stale handling.
# ---------------------------------------------------------------------------


class TestStale:
    def test_target_before_gameweek_is_stale_and_dropped_from_the_contest(self) -> None:
        extraction = _extraction(
            "alice",
            [
                _plan(target_gameweek=1, conviction=5),
                _plan(target_gameweek=5, conviction=1),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        assert len(consensus.stale) == 1
        assert consensus.stale[0].target_gameweek == 1
        wildcard = consensus.tallies[0]
        # The stale, higher-conviction plan must not win the contest even
        # though it has higher conviction than the surviving one.
        assert len(wildcard.later) == 1
        assert wildcard.later[0].target_gameweek == 5

    def test_multiple_stale_plans_for_the_same_chip_are_all_recorded_not_deduped(self) -> None:
        extraction = _extraction(
            "alice",
            [
                _plan(target_gameweek=1, conviction=5, reasoning="one"),
                _plan(target_gameweek=2, conviction=4, reasoning="two"),
            ],
        )
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        assert len(consensus.stale) == 2
        assert {v.reasoning for v in consensus.stale} == {"one", "two"}

    def test_a_creator_with_only_stale_plans_is_not_counted_in_creators_with_plans(
        self,
    ) -> None:
        extraction = _extraction("alice", [_plan(target_gameweek=1, conviction=5)])
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        assert consensus.creators_with_plans == 0
        assert len(consensus.stale) == 1

    def test_gameweek_none_means_nothing_is_stale(self) -> None:
        extraction = _extraction("alice", [_plan(target_gameweek=1, conviction=5)])
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=None)
        assert consensus.stale == []


# ---------------------------------------------------------------------------
# Bucketing + sort order.
# ---------------------------------------------------------------------------


class TestBucketing:
    def test_this_gameweek_bucket(self) -> None:
        extraction = _extraction("alice", [_plan(target_gameweek=3, conviction=3)])
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert len(wildcard.this_gameweek) == 1
        assert wildcard.later == []
        assert wildcard.unspecified == []

    def test_later_bucket(self) -> None:
        extraction = _extraction("alice", [_plan(target_gameweek=7, conviction=3)])
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert len(wildcard.later) == 1
        assert wildcard.this_gameweek == []

    def test_unspecified_bucket(self) -> None:
        extraction = _extraction("alice", [_plan(target_gameweek=None, conviction=3)])
        consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert len(wildcard.unspecified) == 1
        assert wildcard.this_gameweek == []
        assert wildcard.later == []

    def test_gameweek_none_this_gameweek_always_empty_and_dated_votes_go_later(self) -> None:
        extractions = [
            _extraction("alice", [_plan(target_gameweek=5, conviction=3)]),
            _extraction("bob", [_plan(target_gameweek=None, conviction=3)]),
        ]
        consensus = summarize_chip_plans(extractions, {"alice": 1.0, "bob": 1.0}, gameweek=None)
        wildcard = consensus.tallies[0]
        assert wildcard.this_gameweek == []
        assert [v.creator_id for v in wildcard.later] == ["alice"]
        assert [v.creator_id for v in wildcard.unspecified] == ["bob"]

    def test_later_sorted_by_target_gameweek_then_creator_id(self) -> None:
        extractions = [
            _extraction("zoe", [_plan(target_gameweek=6, conviction=3)]),
            _extraction("bob", [_plan(target_gameweek=5, conviction=3)]),
            _extraction("alice", [_plan(target_gameweek=5, conviction=3)]),
        ]
        consensus = summarize_chip_plans(
            extractions, {"zoe": 1.0, "bob": 1.0, "alice": 1.0}, gameweek=3
        )
        wildcard = consensus.tallies[0]
        assert [(v.creator_id, v.target_gameweek) for v in wildcard.later] == [
            ("alice", 5),
            ("bob", 5),
            ("zoe", 6),
        ]

    def test_this_gameweek_and_unspecified_sorted_by_weight_desc_then_creator_id(self) -> None:
        extractions = [
            _extraction("alice", [_plan(target_gameweek=3, conviction=3)]),
            _extraction("bob", [_plan(target_gameweek=3, conviction=3)]),
            _extraction("carl", [_plan(target_gameweek=3, conviction=3)]),
        ]
        weights = {"alice": 0.5, "bob": 0.9, "carl": 0.9}
        consensus = summarize_chip_plans(extractions, weights, gameweek=3)
        wildcard = consensus.tallies[0]
        # bob and carl tie on weight (0.9); creator_id breaks the tie.
        assert [v.creator_id for v in wildcard.this_gameweek] == ["bob", "carl", "alice"]


# ---------------------------------------------------------------------------
# Share arithmetic.
# ---------------------------------------------------------------------------


class TestShareArithmetic:
    def test_this_gameweek_share_is_weight_over_contributing_weight(self) -> None:
        extractions = [
            _extraction("alice", [_plan(target_gameweek=3, conviction=3)]),
            _extraction("bob", []),
        ]
        consensus = summarize_chip_plans(extractions, {"alice": 1.0, "bob": 3.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert consensus.contributing_weight == 4.0
        assert wildcard.this_gameweek_weight == 1.0
        assert wildcard.this_gameweek_share == 0.25

    def test_contributing_weight_zero_yields_zero_share_not_a_crash(self) -> None:
        extraction = _extraction("alice", [_plan(target_gameweek=3, conviction=3)])
        consensus = summarize_chip_plans([extraction], {"alice": 0.0}, gameweek=3)
        wildcard = consensus.tallies[0]
        assert consensus.contributing_weight == 0.0
        assert wildcard.this_gameweek_share == 0.0

    def test_unknown_creator_weight_defaults_to_zero(self) -> None:
        extraction = _extraction("mystery", [_plan(target_gameweek=3, conviction=3)])
        consensus = summarize_chip_plans([extraction], {}, gameweek=3)
        assert consensus.contributing_weight == 0.0
        wildcard = consensus.tallies[0]
        assert wildcard.this_gameweek[0].creator_weight == 0.0
        assert wildcard.this_gameweek_share == 0.0


# ---------------------------------------------------------------------------
# Determinism.
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_twice_yields_equal_output(self) -> None:
        extractions = [
            _extraction(
                "alice",
                [
                    _plan(chip=Chip.WILDCARD, target_gameweek=5, conviction=4),
                    _plan(chip=Chip.FREE_HIT, target_gameweek=None, conviction=2),
                ],
            ),
            _extraction("bob", [_plan(chip=Chip.WILDCARD, target_gameweek=1, conviction=5)]),
        ]
        weights = {"alice": 0.8, "bob": 0.4}
        first = summarize_chip_plans(extractions, weights, gameweek=3)
        second = summarize_chip_plans(extractions, weights, gameweek=3)
        assert first == second
        assert isinstance(first, ChipConsensus)


# ---------------------------------------------------------------------------
# Multiple extractions for the same creator pool their plans.
# ---------------------------------------------------------------------------


class TestMultipleExtractionsPerCreator:
    def test_plans_from_two_extractions_by_the_same_creator_are_pooled(self) -> None:
        extractions = [
            _extraction("alice", [_plan(target_gameweek=5, conviction=2)], video_id="vid1"),
            _extraction("alice", [_plan(target_gameweek=6, conviction=5)], video_id="vid2"),
        ]
        consensus = summarize_chip_plans(extractions, {"alice": 1.0}, gameweek=3)
        assert consensus.contributing_creators == 1
        wildcard = consensus.tallies[0]
        assert len(wildcard.later) == 1
        assert wildcard.later[0].target_gameweek == 6


def test_provenance_and_reasoning_carried_through_to_the_vote() -> None:
    extraction = _extraction(
        "alice",
        [
            _plan(
                target_gameweek=3,
                conviction=4,
                reasoning="locked in for the double",
                provenance=Provenance.GROUP,
            )
        ],
    )
    consensus = summarize_chip_plans([extraction], {"alice": 1.0}, gameweek=3)
    vote = consensus.tallies[0].this_gameweek[0]
    assert isinstance(vote, ChipVote)
    assert vote.provenance is Provenance.GROUP
    assert vote.reasoning == "locked in for the double"
