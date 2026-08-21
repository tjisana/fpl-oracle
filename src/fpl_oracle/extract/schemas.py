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
- CHIP PLANS (added for in-season, 2026-08-20): player-less strategy
  statements ("wildcard GW8", "bench boost on the double") were knowingly
  dropped in v1, because every Pick requires a player name and GW1 content
  is squad reveals. In-season that inverts — creators publish entire
  videos on chip timing, and a chip is worth more than any single
  transfer. `ChipPlan` gives them a home alongside `picks`, deliberately
  as a SEPARATE list rather than a Pick with a null player: a chip is not
  an opinion about a footballer, and forcing it into Pick would put a
  synthetic player-less row through a resolver whose entire job is to
  refuse rows without a real player.
- URGENCY (added for in-season, 2026-08-20): "get him in before he rises
  tonight" and "I will decide on Saturday" are different instructions
  with the same action and the same player. Prices move nightly, so the
  distinction is the difference between acting today and losing 0.1m of
  team value. Optional, because most statements genuinely carry no
  urgency signal and forcing the model to invent one would be noise.
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


class Urgency(StrEnum):
    """WHEN the creator says to act. Distinct from `time_horizon`, which is
    about how many gameweeks a pick is meant to pay off over: a player can
    be a long-horizon hold that must nonetheless be bought TONIGHT to beat
    a price rise."""

    BEFORE_PRICE_CHANGE = "before_price_change"
    THIS_DEADLINE = "this_deadline"
    NO_RUSH = "no_rush"


class Chip(StrEnum):
    """FPL chips. Verify the current season's chip set at season start —
    the game adds and renames chips some years (see the `fpl-domain`
    skill); an unrecognised chip name must not be silently coerced into a
    neighbouring one."""

    WILDCARD = "wildcard"
    FREE_HIT = "free_hit"
    BENCH_BOOST = "bench_boost"
    TRIPLE_CAPTAIN = "triple_captain"


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
    urgency: Urgency | None = Field(
        default=None,
        description=(
            "WHEN to act, if the creator says: 'before_price_change' for 'get him in "
            "tonight before he goes up', 'this_deadline' for a move meant for this "
            "gameweek, 'no_rush' when they explicitly say it can wait. Null when they "
            "give no timing signal at all — most picks. Do not infer urgency from "
            "enthusiasm; only from an actual statement about timing."
        ),
    )


class ChipPlan(BaseModel):
    """A stated intention to play a chip in a particular gameweek.

    Player-less by nature, which is exactly why it is not a `Pick`. A chip
    swings more points than any single transfer, so an in-season system
    that reads only player opinions is deaf to the biggest calls the
    creators make.
    """

    chip: Chip = Field(description="which chip the creator plans to play")
    target_gameweek: int | None = Field(
        default=None,
        ge=1,
        le=38,
        description=(
            "The gameweek they intend to play it in, if stated. None when they name the "
            "chip but not a gameweek ('wildcard at some point soon') — do not guess a number."
        ),
    )
    conviction: int = Field(
        ge=1,
        le=5,
        description=(
            "How firm the plan is, 1-5, from their language: 'locked in', 'definitely "
            "this week' = 5; 'leaning towards', 'probably' = 3; 'might', 'considering' = 1-2."
        ),
    )
    reasoning: str = Field(
        max_length=300, description="the creator's own stated reason, one sentence"
    )
    provenance: Provenance = Field(
        description="whether this is the attributed creator's own plan or a group position"
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
    chip_plans: list[ChipPlan] = Field(
        default_factory=list,
        description=(
            "Player-less chip-timing intentions stated in the video. Defaults to empty so "
            "every extraction file written before this field existed still parses."
        ),
    )
