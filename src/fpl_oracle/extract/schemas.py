"""Pydantic schemas for LLM pick extraction from creator transcripts.

These models are the extraction contract. The structured-output shape
handed to Claude is the pick-level content only (`picks` + `gameweek`) —
the pipeline fills in `creator_id`/`video_id`/`video_title`/`published_at`
itself, so the LLM never parrots back identifiers it could mangle. The
`Field` descriptions on LLM-emitted fields are written for the extracting
model, not just for human readers.

Design decisions carried in here:

- COMPOSITE KEY: a pick is identified by (player_name_raw, team_inferred,
  position_inferred), never by the bare name — "Gabriel" alone is
  ambiguous across three Arsenal players. `player_id` stays None at
  extraction time; it is filled in post-extraction by the resolver in
  `fpl/players.py` (rapidfuzz against the FPL player DB). An unresolved
  name must never be stored as if it were a player.
- PROVENANCE (from the Phase 1 shared-channel attribution review): every
  pick records whether it is the attributed creator's own stated view or
  a group/joint-discussion position. Shared-channel videos (e.g. The FPL
  Wire ingested via Pras) make this distinction load-bearing, so the
  field is required — the extractor must commit to one.
- `time_horizon` captures multi-week strategy talk ("for the run of
  fixtures", "Palmer in by GW4") so future gameweeks can be steered by it.
- DEFERRED: player-less strategy statements (pure chip-timing plans like
  "wildcard GW8" with no player attached) have no home in this schema —
  every Pick requires a player name. Chip-plan modeling is out of v1
  extraction scope; such statements are knowingly dropped.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class PickAction(StrEnum):
    """What the creator is saying to do with the player."""

    SQUAD_INCLUDE = "squad_include"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    CAPTAIN = "captain"
    VICE = "vice"
    BENCH = "bench"
    AVOID = "avoid"
    WATCHLIST = "watchlist"


class Provenance(StrEnum):
    """Whose opinion a pick represents on a shared or collaborative channel."""

    PERSONAL = "personal"
    GROUP = "group"


class Pick(BaseModel):
    player_name_raw: str = Field(
        min_length=1,
        description=(
            "The player's name verbatim as it appears in the transcript, "
            "including any mis-transcription (e.g. 'Sacca', 'Hall and'). "
            "Do not correct or normalize it."
        ),
    )
    team_inferred: str | None = Field(
        description=(
            "Premier League club the creator means, inferred from context "
            "or PL knowledge (e.g. 'Arsenal'). None only if genuinely "
            "undeterminable."
        ),
    )
    position_inferred: Literal["GK", "DEF", "MID", "FWD"] | None = Field(
        description=(
            "FPL position inferred from context or PL knowledge. None only "
            "if genuinely undeterminable."
        ),
    )
    player_id: int | None = Field(
        default=None,
        description=(
            "FPL element id — always None at extraction time; resolved "
            "later against the FPL player DB."
        ),
    )
    action: PickAction = Field(
        description=(
            "The creator's FINAL stated position in this video. If they "
            "change their mind mid-video ('I had X but switched to Y'), "
            "extract only the final stance. Hedged non-picks ('if you "
            "already own him, hold') are 'watchlist', not 'transfer_in'."
        ),
    )
    conviction: int = Field(
        ge=1,
        le=5,
        description=(
            "1-5, judged from language: 'nailed', 'locked in' = 5; "
            "'punt' with clear intent = 3-4; 'maybe', 'monitoring' = 1-2."
        ),
    )
    time_horizon: int = Field(
        ge=1,
        le=38,
        description=(
            "Horizon in gameweeks: 1 = this-week move, larger for stated "
            "plans ('for the run of fixtures' ~4-6, 'wildcard GW8' = "
            "gameweeks until then)."
        ),
    )
    reasoning: str = Field(
        max_length=300,
        description="The creator's own logic for this pick, one sentence max.",
    )
    provenance: Provenance = Field(
        description=(
            "'personal' if this is the attributed creator's own stated "
            "view; 'group' if it emerged from a joint discussion / shared "
            "channel without a clear individual owner."
        ),
    )


class VideoExtraction(BaseModel):
    """All picks extracted from a single video, keyed to creator + video."""

    creator_id: str
    video_id: str
    video_title: str
    published_at: datetime
    gameweek: int | None = Field(
        ge=1,
        le=38,
        description=(
            "The gameweek the video is about, if stated or inferable from "
            "title/context; None otherwise."
        ),
    )
    picks: list[Pick]
