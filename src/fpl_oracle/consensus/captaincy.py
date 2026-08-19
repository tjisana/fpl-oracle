"""Captaincy election — deliberately separate from squad scoring.

Captaincy is a CEILING bet; squad selection is a FLOOR bet. Blending
them would let a safe, widely-owned player win the armband on inclusion
votes alone, so `captain` picks aggregate in their own table here and
never touch `consensus/scoring.py`'s numbers. (A captain pick still
counts as a plain inclusion over there — naming someone captain
obviously implies picking him — but the extra ceiling signal lives only
in this module.)

The solver consumes `captain_options()`: the squad must contain at least
2 of the top options, so the armband decision isn't stranded outside the
squad. Two guards on that list, both learned from the live data:

- A SUPPORT FLOOR, not just a top-K cut. The first live run produced 8
  captain picks across 3 players; third place was a single vote from one
  creator who cast two captain votes. Ranking by count alone would let a
  fringe name constrain the whole squad.
- OPTIONS ARE FILTERED FOR AVAILABILITY FIRST. An injured captain option
  would make the solver's >=2 constraint unsatisfiable — and the failure
  mode is "no squad at all on deadline morning", which is the worst
  outcome this project has.

THIN EVIDENCE WARNING: only 8 of ~14 creators' armbands were captured in
the first run. `CaptaincyElection.thin` says so, and the report must
surface it — the deadline-morning re-extraction matters more here than
anywhere else in the pipeline.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from fpl_oracle.consensus.scoring import CONVICTION_FACTOR
from fpl_oracle.extract.schemas import PickAction

# A vice-captain pick is a real but second-choice ceiling signal.
VICE_VALUE = 0.4

# An option must reach this share of the leader's weighted score to
# constrain the solver. Keeps a lone low-weight vote from forcing an
# expensive player into the squad.
SUPPORT_FLOOR_RATIO = 0.30

# Never constrain the squad with more than this many options.
MAX_OPTIONS = 3

# The election is flagged thin when fewer than this SHARE of the
# contributing creators named a captain. Deliberately relative, not an
# absolute count: the first live run captured 7 armbands from 14
# creators, and "half the roster didn't tell us who they're captaining"
# is thin regardless of whether 7 sounds like a lot.
THIN_EVIDENCE_RATIO = 0.6


class CaptainCandidate(BaseModel):
    player_id: int
    score: float = Field(description="weighted, conviction-adjusted captain support")
    voters: int = Field(description="distinct creators naming this player captain or vice")
    share: float = Field(description="score as a share of the leader's, 0-1")


class CaptaincyElection(BaseModel):
    candidates: list[CaptainCandidate] = Field(description="all candidates, best first")
    options: list[int] = Field(description="player_ids the solver must include >=2 of")
    total_voters: int = Field(description="distinct creators who named any captain")
    contributing_creators: int = Field(description="creators who submitted any pick at all")
    thin: bool = Field(description="too few creators voted — report must disclose")


def elect(
    picks_by_creator: dict[str, list[tuple[int, PickAction, int]]],
    creator_weights: dict[str, float],
    eligible_player_ids: set[int] | None = None,
) -> CaptaincyElection:
    """Run the captaincy election.

    `eligible_player_ids`, when given, is the post-availability pool —
    options are drawn only from it, so the solver can never be handed an
    unsatisfiable constraint naming an injured player. Candidates are
    still reported (the armband debate is interesting even when the
    favourite is out), but they cannot become options.
    """
    scores: dict[int, float] = defaultdict(float)
    voters: dict[int, set[str]] = defaultdict(set)

    for creator_id, picks in picks_by_creator.items():
        weight = creator_weights.get(creator_id, 0.0)
        # one captain and one vice per creator: if a creator names several
        # (they change their mind mid-video), the strongest survives
        best: dict[int, float] = {}
        for player_id, action, conviction in picks:
            if action is PickAction.CAPTAIN:
                value = weight * CONVICTION_FACTOR[conviction]
            elif action is PickAction.VICE:
                value = weight * CONVICTION_FACTOR[conviction] * VICE_VALUE
            else:
                continue
            best[player_id] = max(best.get(player_id, 0.0), value)
        for player_id, value in best.items():
            scores[player_id] += value
            voters[player_id].add(creator_id)

    if not scores:
        return CaptaincyElection(
            candidates=[], options=[], total_voters=0, contributing_creators=0, thin=True
        )

    leader = max(scores.values())
    candidates = [
        CaptainCandidate(
            player_id=player_id,
            score=score,
            voters=len(voters[player_id]),
            share=score / leader if leader else 0.0,
        )
        for player_id, score in scores.items()
    ]
    candidates.sort(key=lambda c: (-c.score, c.player_id))

    eligible = [
        c
        for c in candidates
        if c.share >= SUPPORT_FLOOR_RATIO
        and (eligible_player_ids is None or c.player_id in eligible_player_ids)
    ]
    options = [c.player_id for c in eligible[:MAX_OPTIONS]]

    all_voters = {
        cid
        for cid, picks in picks_by_creator.items()
        if any(a in (PickAction.CAPTAIN, PickAction.VICE) for _, a, _ in picks)
    }
    contributing = len([cid for cid, picks in picks_by_creator.items() if picks])
    return CaptaincyElection(
        candidates=candidates,
        options=options,
        total_voters=len(all_voters),
        contributing_creators=contributing,
        thin=(len(all_voters) < THIN_EVIDENCE_RATIO * contributing) if contributing else True,
    )


def required_options(election: CaptaincyElection) -> int:
    """How many of `election.options` the squad must contain.

    `min(2, len(options))` — the settled rule is ">=2", but demanding 2
    when only one credible option exists would make the solver infeasible
    and produce NO squad. Degrading to 1 (or 0) is always better than
    failing at the deadline; the report says which applied.
    """
    return min(2, len(election.options))
