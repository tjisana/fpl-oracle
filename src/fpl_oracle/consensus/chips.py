"""Tally creators' own stated chip-timing intentions (`ChipPlan`, on every
`VideoExtraction`) into a per-chip consensus: how many creators (and how
much weight) are playing a given chip THIS gameweek, versus later or with
no gameweek stated at all.

OBSERVATIONAL ONLY. Nothing here feeds the solver or `consensus.scoring` /
`consensus.captaincy` — a chip plan is a creator's strategy statement, not
a player opinion, and the report renders this section purely as
"N of M creators are wildcarding this week", never as an input to squad
selection.

Pure and deterministic: no IO, no network, no clock reads. This module
imports only from `fpl_oracle.extract.schemas` and pydantic — never from
`pipeline` or `report`, so it stays a leaf the pipeline can call into
without a dependency cycle.

DEDUPE. A creator can post more than one video with plans for the same
chip. Exactly one vote per (creator_id, chip) survives: the highest-
conviction plan, tie-broken by (1) `target_gameweek == gameweek`, but
ONLY when `gameweek` is not `None` — with `gameweek is None` this check
is skipped entirely rather than comparing `None == None`, which would
otherwise let an undated plan beat a dated one at equal conviction and
silently discard the more specific plan; (2) the lowest non-None target;
(3) `None` as the final fallback. A STALE plan (`target_gameweek <
gameweek`) is never a tie-break candidate: every stale plan is pulled out
first and recorded verbatim (not deduped) in `ChipConsensus.stale`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from fpl_oracle.extract.schemas import Chip, ChipPlan, Provenance, VideoExtraction


class ChipVote(BaseModel):
    """One creator's single, deduped stated intention for one chip."""

    creator_id: str
    creator_weight: float
    chip: Chip
    target_gameweek: int | None
    conviction: int
    provenance: Provenance
    reasoning: str


class ChipTally(BaseModel):
    """Every counted vote for one chip, split by when the creator plans to
    play it."""

    chip: Chip
    this_gameweek: list[ChipVote] = Field(description="target_gameweek == gameweek")
    later: list[ChipVote] = Field(
        description="target_gameweek > gameweek, or any dated target when gameweek is None"
    )
    unspecified: list[ChipVote] = Field(description="target_gameweek is None")
    this_gameweek_weight: float = Field(description="sum of creator_weight over this_gameweek")
    this_gameweek_share: float = Field(
        description=(
            "this_gameweek_weight / contributing_weight; 0.0 when contributing_weight == 0"
        )
    )


class ChipConsensus(BaseModel):
    """The full chip-plan tally for one pipeline run."""

    gameweek: int | None
    contributing_creators: int = Field(
        description=(
            "distinct creator_ids across the extractions passed in — the denominator basis"
        )
    )
    contributing_weight: float = Field(
        description="sum of creator_weights.get(cid, 0.0) over contributing_creators"
    )
    creators_with_plans: int = Field(
        description=(
            "distinct creators with >= 1 counted vote (any chip, any bucket), excluding stale"
        )
    )
    tallies: list[ChipTally] = Field(
        description="exactly one entry per Chip member, in enum definition order, even if empty"
    )
    stale: list[ChipVote] = Field(
        description="target_gameweek < gameweek — recorded for visibility, never counted"
    )


def _tie_break_key(plan: ChipPlan, gameweek: int | None) -> tuple[int, int, tuple[int, int]]:
    """Sort key selecting the single surviving plan among a creator's
    plans for one chip: highest conviction first, then (in order)
    `target_gameweek == gameweek`, then the lowest non-None target, then
    `None`. `min()` over this key picks the winner.

    The `target_gameweek == gameweek` check applies ONLY when `gameweek`
    is not `None`. With `gameweek is None`, that equality would compare
    `None == None` and let an undated plan beat a dated one at equal
    conviction — silently discarding the more specific plan's
    information. So a `None` gameweek skips straight to "lowest non-None
    target, then None", the same as any dated plan that doesn't match."""
    matches_gameweek = 0 if gameweek is not None and plan.target_gameweek == gameweek else 1
    if plan.target_gameweek is not None:
        target_rank = (0, plan.target_gameweek)
    else:
        target_rank = (1, 0)
    return (-plan.conviction, matches_gameweek, target_rank)


def summarize_chip_plans(
    extractions: list[VideoExtraction],
    creator_weights: dict[str, float],
    gameweek: int | None,
) -> ChipConsensus:
    """Tally every extraction's `chip_plans` into a `ChipConsensus`. Pure,
    deterministic: the same `extractions`/`creator_weights`/`gameweek`
    always produce an equal result."""
    plans_by_creator: dict[str, list[ChipPlan]] = {}
    for extraction in extractions:
        plans_by_creator.setdefault(extraction.creator_id, []).extend(extraction.chip_plans)

    # `sorted(...)` before summing keeps float-addition order (and thus the exact
    # result, in principle) independent of extraction insertion order — same
    # determinism concern `_modal_gameweek` (report/gameweek.py) documents for its
    # own tie-break.
    contributing_weight = sum(sorted(creator_weights.get(cid, 0.0) for cid in plans_by_creator))

    stale: list[ChipVote] = []
    votes_by_chip: dict[Chip, list[ChipVote]] = {chip: [] for chip in Chip}
    creators_with_plans: set[str] = set()

    for creator_id, plans in plans_by_creator.items():
        weight = creator_weights.get(creator_id, 0.0)

        non_stale: list[ChipPlan] = []
        for plan in plans:
            is_stale = (
                gameweek is not None
                and plan.target_gameweek is not None
                and plan.target_gameweek < gameweek
            )
            if is_stale:
                stale.append(
                    ChipVote(
                        creator_id=creator_id,
                        creator_weight=weight,
                        chip=plan.chip,
                        target_gameweek=plan.target_gameweek,
                        conviction=plan.conviction,
                        provenance=plan.provenance,
                        reasoning=plan.reasoning,
                    )
                )
            else:
                non_stale.append(plan)

        by_chip: dict[Chip, list[ChipPlan]] = {}
        for plan in non_stale:
            by_chip.setdefault(plan.chip, []).append(plan)

        for chip, group in by_chip.items():
            winner = min(group, key=lambda p: _tie_break_key(p, gameweek))
            votes_by_chip[chip].append(
                ChipVote(
                    creator_id=creator_id,
                    creator_weight=weight,
                    chip=chip,
                    target_gameweek=winner.target_gameweek,
                    conviction=winner.conviction,
                    provenance=winner.provenance,
                    reasoning=winner.reasoning,
                )
            )
            creators_with_plans.add(creator_id)

    tallies: list[ChipTally] = []
    for chip in Chip:
        votes = votes_by_chip[chip]
        this_gameweek_votes: list[ChipVote] = []
        later_votes: list[ChipVote] = []
        unspecified_votes: list[ChipVote] = []
        for vote in votes:
            if vote.target_gameweek is None:
                unspecified_votes.append(vote)
            elif gameweek is not None and vote.target_gameweek == gameweek:
                this_gameweek_votes.append(vote)
            else:
                # Either gameweek is None (every dated plan is `later`), or
                # gameweek is set and target_gameweek > gameweek — a target
                # < gameweek was already pulled into `stale` above and never
                # reaches here.
                later_votes.append(vote)

        this_gameweek_votes.sort(key=lambda v: (-v.creator_weight, v.creator_id))
        unspecified_votes.sort(key=lambda v: (-v.creator_weight, v.creator_id))
        later_votes.sort(key=lambda v: (v.target_gameweek, v.creator_id))

        this_gameweek_weight = sum(v.creator_weight for v in this_gameweek_votes)
        this_gameweek_share = (
            this_gameweek_weight / contributing_weight if contributing_weight > 0 else 0.0
        )

        tallies.append(
            ChipTally(
                chip=chip,
                this_gameweek=this_gameweek_votes,
                later=later_votes,
                unspecified=unspecified_votes,
                this_gameweek_weight=this_gameweek_weight,
                this_gameweek_share=this_gameweek_share,
            )
        )

    return ChipConsensus(
        gameweek=gameweek,
        contributing_creators=len(plans_by_creator),
        contributing_weight=contributing_weight,
        creators_with_plans=len(creators_with_plans),
        tallies=tallies,
        stale=stale,
    )
