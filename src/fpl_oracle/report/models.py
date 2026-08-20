"""Shared Pydantic contract for the Phase 4 nuance pass and the gameweek
report.

Two rules shape everything here, and both exist so the report stays an
aggregation of CREATOR expertise rather than an LLM's own football take:

GROUNDING. Every `Concern` is meant to come from something a creator
actually said in a pick's `reasoning` field, and the model is given
those strings and nothing else about the players — no stats, no
fixtures — so it has nothing else to draw on. But only the ATTRIBUTION
is mechanically enforced: `creator_ids` are validated against the set of
creators that actually voted on that player (`nuance.validate_nuance`),
and a fabricated id is dropped, never rendered, exactly as an unresolved
player name is never stored. The WORDING is not checked — a `Concern`'s
`note`, and all of `SquadNuance.captaincy_note` and `squad_notes`, are
free prose the model writes and `validate_nuance` passes through
untouched. Grounding is a prompt-level constraint on that prose, not a
mechanical one.

MECHANICAL DISSENT STAYS MECHANICAL. Who voted `avoid`/`transfer_out` on
a squad player is arithmetic over `PlayerScore.votes` — the report
computes it directly and never asks the model. The nuance pass adds only
what arithmetic can't: the WORDS behind a vote.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ConcernKind(StrEnum):
    """The recurring shapes creator caveats take. `OTHER` is the escape
    hatch — a forced taxonomy would push real concerns into wrong bins."""

    ROTATION = "rotation"
    FIXTURES = "fixtures"
    INJURY_DOUBT = "injury_doubt"
    ROLE_CHANGE = "role_change"
    PRICE_VALUE = "price_value"
    FORM = "form"
    DISSENT = "dissent"
    OTHER = "other"


class Concern(BaseModel):
    """One caveat voiced about a squad player, attributed to the creators
    who voiced it."""

    kind: ConcernKind
    note: str = Field(
        max_length=240,
        description="the concern in one plain sentence, paraphrasing what the creators said",
    )
    creator_ids: list[str] = Field(
        description="creators who voiced this concern; must be creators who voted on this player"
    )
    severity: int = Field(
        ge=1,
        le=3,
        description="1 = worth knowing, 2 = would change a close call, 3 = would change the pick",
    )


class PlayerNuance(BaseModel):
    """The nuance pass's read on one squad member."""

    player_id: int
    summary: str = Field(
        max_length=240,
        description="one sentence on why the creators backed this player",
    )
    concerns: list[Concern] = Field(default_factory=list)


class SquadNuance(BaseModel):
    """The nuance pass's output for one solved squad."""

    players: list[PlayerNuance] = Field(default_factory=list)
    captaincy_note: str | None = Field(
        default=None,
        max_length=600,
        description="what the creators said about the armband, including the case against",
    )
    squad_notes: list[str] = Field(
        default_factory=list,
        description="cross-cutting observations (club stacking, fixture clustering) creators raised",
    )


class NuanceRecord(BaseModel):
    """Audit wrapper written to `data/runs/{run_id}/nuance.json`, so a
    shipped report ties back to the exact model and inputs behind it."""

    run_id: str
    generated_at: datetime
    model: str
    nuance: SquadNuance
    dropped_creator_ids: list[str] = Field(
        default_factory=list,
        description="creator ids the model attributed that did not vote on that player — dropped",
    )
    failed: bool = Field(
        default=False,
        description="the nuance call failed; the report renders without it rather than not at all",
    )
    failure_reason: str | None = None
