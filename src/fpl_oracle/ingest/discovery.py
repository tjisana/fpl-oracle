"""Discovery: list each creator's recent uploads, upsert new videos into the
ledger, drop Shorts, classify.

The uploads source is injected (`UploadsClient`) so tests never touch the
network; `YouTubeUploadsClient` is the real one (1 YouTube API unit per
channel, plus 1 per 50 duration lookups). Shared-channel attribution reuses
`run_ingest.videos_for_creator`, and only creators `eligible_creators`
accepts are scanned.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from fpl_oracle.ingest.classify import Classifier, RegexClassifier, is_skipped_kind
from fpl_oracle.ingest.ledger import GameweekDeadline, Ledger, LedgerRow, assign_gameweek
from fpl_oracle.ingest.run_ingest import MIN_DURATION_S, eligible_creators, videos_for_creator
from fpl_oracle.ingest.youtube_client import (
    VideoInfo,
    YouTubeApiError,
    get_video_durations,
    list_recent_videos,
)
from fpl_oracle.roster.models import Creator

logger = logging.getLogger(__name__)

UPLOADS_PER_CHANNEL = 15
SHORT_MARK = f"short (<{MIN_DURATION_S}s)"
SKIPPED_KIND_MARK = "kind not harvested"


class UploadsClient(Protocol):
    def recent_uploads(self, channel_id: str) -> list[VideoInfo]: ...

    def durations(self, video_ids: Sequence[str]) -> dict[str, int]: ...


class YouTubeUploadsClient:
    """The real client: always refreshes the listing (a video posted an hour
    ago must be seen)."""

    def __init__(self, max_results: int = UPLOADS_PER_CHANNEL) -> None:
        self.max_results = max_results

    def recent_uploads(self, channel_id: str) -> list[VideoInfo]:
        return list_recent_videos(channel_id, max_results=self.max_results, force_refresh=True)

    def durations(self, video_ids: Sequence[str]) -> dict[str, int]:
        return get_video_durations(list(video_ids))


class DiscoveryResult(BaseModel):
    new_videos: int = 0
    channels_scanned: int = 0
    channels_failed: int = 0
    errors: list[str] = Field(default_factory=list)


def discover(
    ledger: Ledger,
    registry: Sequence[Creator],
    client: UploadsClient,
    deadlines: Sequence[GameweekDeadline],
    now: datetime,
    classifier: Classifier | None = None,
) -> DiscoveryResult:
    """Upsert every attributed upload of every eligible creator into
    `ledger` (mutated in place). Existing rows keep their status; they only
    gain a duration when one was missing (and a "short" row whose real
    duration turns out long, e.g. a finished premiere, is revived).

    One channel failing (API error / transport error) is recorded and
    skipped; it never aborts the scan."""
    classifier = classifier or RegexClassifier()
    result = DiscoveryResult()
    roster = list(registry)
    listings: dict[str, list[VideoInfo] | None] = {}

    for creator in eligible_creators(roster):
        assert creator.channel_id is not None
        if creator.channel_id not in listings:
            try:
                listings[creator.channel_id] = client.recent_uploads(creator.channel_id)
                result.channels_scanned += 1
            except (YouTubeApiError, httpx.HTTPError) as e:
                listings[creator.channel_id] = None
                result.channels_failed += 1
                result.errors.append(f"{creator.channel_id}: {type(e).__name__}: {e}")
                logger.warning("autopilot: listing %s failed: %s", creator.channel_id, e)
        videos = listings[creator.channel_id]
        if videos is None:
            continue
        co_creators = [
            c
            for c in roster
            if c.channel_id == creator.channel_id and c.creator_id != creator.creator_id
        ]
        attributed = [
            v
            for v in videos_for_creator(creator, co_creators, videos)
            if v.video_id not in ledger.rows or _needs_duration(ledger.rows[v.video_id])
        ]
        if not attributed:
            continue
        try:
            durations = client.durations([v.video_id for v in attributed])
        except (YouTubeApiError, httpx.HTTPError) as e:
            result.errors.append(f"durations: {type(e).__name__}: {e}")
            logger.warning("autopilot: duration lookup failed (%s); keeping unknown", e)
            durations = {}
        for video in attributed:
            duration = durations.get(video.video_id)
            existing = ledger.rows.get(video.video_id)
            if existing is not None:
                _refresh_duration(existing, duration)
                continue
            ledger.rows[video.video_id] = _new_row(
                video, creator, duration, deadlines, now, classifier
            )
            result.new_videos += 1
    return result


def _needs_duration(row: LedgerRow) -> bool:
    return row.duration_s is None or (row.duration_s < MIN_DURATION_S and row.status == "skipped")


def _new_row(
    video: VideoInfo,
    creator: Creator,
    duration: int | None,
    deadlines: Sequence[GameweekDeadline],
    now: datetime,
    classifier: Classifier,
) -> LedgerRow:
    kind = classifier.classify(video.title)
    status = "new"
    error: str | None = None
    if duration is not None and duration < MIN_DURATION_S:
        status, error = "skipped", SHORT_MARK
    elif is_skipped_kind(kind):
        status, error = "skipped", SKIPPED_KIND_MARK
    return LedgerRow(
        video_id=video.video_id,
        creator_id=creator.creator_id,
        title=video.title,
        published_at=video.published_at,
        duration_s=duration,
        gameweek=assign_gameweek(video.published_at, deadlines),
        kind=kind,
        first_seen_at=now,
        status=status,
        last_error=error,
    )


def _refresh_duration(row: LedgerRow, duration: int | None) -> None:
    if duration is None:
        return
    row.duration_s = duration
    if row.status == "skipped" and row.last_error == SHORT_MARK and duration >= MIN_DURATION_S:
        if is_skipped_kind(row.kind):
            row.last_error = SKIPPED_KIND_MARK
        else:
            row.status = "new"
            row.last_error = None
