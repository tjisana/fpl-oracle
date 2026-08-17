"""Pydantic models for the creator roster."""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel


class Tier(IntEnum):
    """Creator weighting tier. Tier 2 is the default fallback for creators
    whose skill can't be substantiated by any verification tier — they
    get a low default weight instead of a history-derived one. A creator
    can only be Tier.CORE if a real person's track record backs it —
    institutional/multi-person brands don't qualify."""

    CORE = 1
    SECONDARY = 2


class Verification(StrEnum):
    """How a creator's FPL skill claim was substantiated.

    - API: team ID verified, history pulled straight from
      `entry/{id}/history/` — the strongest signal.
    - DOCUMENTED: no verified ID yet, but a reputable third party (BBC,
      Fantasy Football Scout, ...) has publicly documented specific
      finishes for this person under their real name. Weighted strongly
      but shrunk relative to API since it isn't independently pullable.
    - UNVERIFIED: neither of the above. Default low weight.
    """

    API = "api"
    DOCUMENTED = "documented"
    UNVERIFIED = "unverified"


class PastFinish(BaseModel):
    """A season finish pulled directly from the FPL API (Verification.API)."""

    season_name: str
    rank: int | None


class ClaimedFinish(BaseModel):
    """A finish claimed by a reputable third-party source rather than
    pulled from the FPL API directly (Verification.DOCUMENTED). `rank` is
    a conservative representative value for a qualitative claim (e.g. "a
    top-10k finish" -> rank=10_000), not asserted as an exact rank.
    `count` is how many separate seasons/finishes the claim represents
    (e.g. BBC's "five top-10k finishes" -> one ClaimedFinish, count=5)."""

    description: str
    rank: int
    source_url: str
    count: int = 1


class Creator(BaseModel):
    creator_id: str
    name: str
    youtube_hint: str
    tier: Tier

    channel_id: str | None = None
    channel_title: str | None = None
    subscriber_count: int | None = None
    channel_match_flagged: bool = False

    # Shared-channel attribution (see `ingest.run_ingest.videos_for_creator`):
    # on a channel shared by multiple creators, a video belongs to a creator
    # with `title_filter` set iff EXACTLY ONE co-creator's filter (case-
    # insensitive substring) matches its title; `channel_primary` marks the
    # creator who ingests whatever's left unclaimed by any co-creator's
    # filter. At most one of these should be meaningfully set per creator.
    title_filter: str | None = None
    channel_primary: bool = False

    real_name: str | None = None
    fpl_team_id: int | None = None
    fpl_team_name: str | None = None
    verification: Verification = Verification.UNVERIFIED
    past_finishes: list[PastFinish] = []
    documented_finishes: list[ClaimedFinish] = []

    weight: float | None = None
    notes: str | None = None
