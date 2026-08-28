"""Gameweek-agnostic transcript cache warmer.

Fetches and saves transcripts for every eligible creator's recent uploads,
so that the free/slow/rate-limitable YouTube+transcript stage is fully
decoupled from the paid LLM extraction stage that reads from the on-disk
cache afterwards. Run the morning-of extraction from whatever's already on
disk; run this the night before to make sure it's all there.

Reuses the same machinery as `run_ingest.py` / `run_extract.py` rather than
inventing new network code: `youtube_client.list_recent_videos` for
listings, `run_ingest.videos_for_creator` for shared-channel attribution,
`run_ingest.filter_min_duration` for the Shorts filter, and
`transcripts.fetch_transcript` (the read-through cache) for the actual
transcript fetch.

    uv run python -m fpl_oracle.ingest.warm_cache --since 2026-08-22
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from youtube_transcript_api import RequestBlocked, YouTubeRequestFailed

from fpl_oracle.ingest.run_ingest import (
    eligible_creators,
    filter_min_duration,
    videos_for_creator,
)
from fpl_oracle.ingest.transcripts import (
    DEFAULT_TRANSCRIPT_DIR,
    fetch_transcript,
    load_transcript,
    save_transcript,
)
from fpl_oracle.ingest.youtube_client import (
    VideoInfo,
    YouTubeApiError,
    get_video_durations,
    list_recent_videos,
)
from fpl_oracle.logging_setup import configure_logging
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY

logger = logging.getLogger(__name__)

DEFAULT_SINCE_DAYS = 7
DEFAULT_MAX_PER_CREATOR = 5

# How many of a channel's newest uploads to list before date/duration
# filtering and the --max-per-creator cap are applied. Generous headroom
# over the default cap (5) and the default lookback window (7 days) so a
# creator who posts more than a handful of times a week still has their
# whole window visible before capping — list_recent_videos already caches
# each listing for 6h, so a wider ask here costs nothing extra on a rerun.
VIDEOS_PER_CHANNEL = 25


class VideoWarmOutcome(BaseModel):
    """The result of considering one video for warming."""

    video_id: str
    title: str
    published_at: datetime
    already_cached: bool
    newly_fetched: bool = False
    failed: bool = False
    failure_reason: str | None = None


class CreatorWarmSummary(BaseModel):
    """Per-creator rollup of one `warm_creator` call."""

    creator_id: str
    creator_name: str
    considered: int
    already_cached: int
    newly_fetched: int
    failed: int
    videos: list[VideoWarmOutcome] = Field(default_factory=list)
    error: str | None = Field(
        default=None, description="creator-level failure, e.g. video listing failed"
    )

    @property
    def available_total(self) -> int:
        """How many transcripts for this creator are on disk after this
        run (whether already cached or newly fetched) — what tomorrow's
        extraction stage actually has to work with."""
        return self.already_cached + self.newly_fetched

    @property
    def yielded_nothing(self) -> bool:
        return self.available_total == 0


class WarmCacheRunSummary(BaseModel):
    """The outcome of one full `run_warm_cache` call across all creators."""

    creators: list[CreatorWarmSummary]
    dry_run: bool
    blocked: bool = False
    blocked_reason: str | None = Field(
        default=None,
        description="set when a RequestBlocked/YouTubeRequestFailed stopped the run early",
    )

    @property
    def considered(self) -> int:
        return sum(c.considered for c in self.creators)

    @property
    def already_cached(self) -> int:
        return sum(c.already_cached for c in self.creators)

    @property
    def newly_fetched(self) -> int:
        return sum(c.newly_fetched for c in self.creators)

    @property
    def failed(self) -> int:
        return sum(c.failed for c in self.creators)

    @property
    def creators_with_nothing(self) -> list[CreatorWarmSummary]:
        return [c for c in self.creators if c.yielded_nothing]


def default_since() -> datetime:
    """`DEFAULT_SINCE_DAYS` days ago, anchored to the start of that day in
    UTC — the same date-level granularity as an explicit `--since`."""
    day = (datetime.now(UTC) - timedelta(days=DEFAULT_SINCE_DAYS)).date()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def parse_since(value: str) -> datetime:
    """Parse a `--since YYYY-MM-DD` value into a UTC datetime at the start
    of that date."""
    parsed = datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007 - tzinfo attached below
    return parsed.replace(tzinfo=UTC)


def filter_since(videos: list[VideoInfo], since: datetime) -> list[VideoInfo]:
    """Keep only videos published on/after `since`. Pure — no I/O."""
    return [v for v in videos if v.published_at >= since]


def eligible_creators_filtered(creator_ids: list[str] | None) -> list[Creator]:
    """`eligible_creators(REGISTRY)`, optionally narrowed to `creator_ids`.
    Raises `ValueError` listing any id that isn't a known eligible creator —
    same discipline as `extract.run_extract.run_extraction`."""
    creators = eligible_creators(REGISTRY)
    if creator_ids is not None:
        known = {c.creator_id for c in creators}
        unknown = sorted(set(creator_ids) - known)
        if unknown:
            raise ValueError(f"unknown/ineligible creator id(s): {', '.join(unknown)}")
        creators = [c for c in creators if c.creator_id in creator_ids]
    return creators


def videos_to_warm(
    creator: Creator,
    registry: list[Creator],
    since: datetime,
    max_per_creator: int,
) -> list[VideoInfo]:
    """List, attribute, date-filter, Shorts-filter, and cap `creator`'s
    candidate videos for cache warming, newest first.

    Mirrors the pipeline in `run_ingest.ingest_one` / `run_extract.
    pick_gw1_video`: list -> shared-channel attribution -> Shorts filter,
    with a `since` date filter and a `max_per_creator` cap layered on top
    (both specific to this gameweek-agnostic warmer)."""
    assert creator.channel_id is not None
    videos = list_recent_videos(creator.channel_id, max_results=VIDEOS_PER_CHANNEL)
    co_creators = [
        c
        for c in registry
        if c.channel_id == creator.channel_id and c.creator_id != creator.creator_id
    ]
    attributed = videos_for_creator(creator, co_creators, videos)
    since_filtered = filter_since(attributed, since)

    try:
        durations = get_video_durations([v.video_id for v in since_filtered])
    except YouTubeApiError as e:
        logger.warning(
            "warm_cache: duration lookup failed (%s) — skipping the min-duration "
            "Shorts filter for %s, proceeding unfiltered",
            e,
            creator.name,
        )
        durations = {}
    selectable = filter_min_duration(since_filtered, durations)
    return selectable[:max_per_creator]


def warm_creator(
    creator: Creator,
    registry: list[Creator],
    since: datetime,
    max_per_creator: int,
    dir: Path,
    dry_run: bool,
) -> CreatorWarmSummary:
    """Warm (or, in `dry_run`, just report on) one creator's candidate
    videos. Video-listing failures (`YouTubeApiError`) are caught and
    recorded as a creator-level failure — non-fatal, same discipline as
    `run_extract.py`. `RequestBlocked` / `YouTubeRequestFailed` from
    `fetch_transcript` are deliberately NOT caught here: those are
    environmental (this IP/request is blocked), not a fact about this
    creator, and the caller decides whether to stop the whole run."""
    try:
        videos = videos_to_warm(creator, registry, since, max_per_creator)
    except YouTubeApiError as e:
        logger.warning("warm_cache: %s — video listing failed: %s", creator.creator_id, e)
        return CreatorWarmSummary(
            creator_id=creator.creator_id,
            creator_name=creator.name,
            considered=0,
            already_cached=0,
            newly_fetched=0,
            failed=0,
            error=f"video listing failed: {e}",
        )

    outcomes: list[VideoWarmOutcome] = []
    for video in videos:
        already = load_transcript(video.video_id, dir=dir) is not None

        if dry_run:
            outcomes.append(
                VideoWarmOutcome(
                    video_id=video.video_id,
                    title=video.title,
                    published_at=video.published_at,
                    already_cached=already,
                )
            )
            continue

        if already:
            outcomes.append(
                VideoWarmOutcome(
                    video_id=video.video_id,
                    title=video.title,
                    published_at=video.published_at,
                    already_cached=True,
                )
            )
            continue

        transcript = fetch_transcript(video.video_id, dir=dir)
        if transcript is None:
            logger.info(
                "warm_cache: no transcript for '%s' (%s) — %s",
                video.title,
                video.video_id,
                creator.name,
            )
            outcomes.append(
                VideoWarmOutcome(
                    video_id=video.video_id,
                    title=video.title,
                    published_at=video.published_at,
                    already_cached=False,
                    failed=True,
                    failure_reason="no transcript available",
                )
            )
            continue

        save_transcript(transcript, dir=dir)
        outcomes.append(
            VideoWarmOutcome(
                video_id=video.video_id,
                title=video.title,
                published_at=video.published_at,
                already_cached=False,
                newly_fetched=True,
            )
        )

    return CreatorWarmSummary(
        creator_id=creator.creator_id,
        creator_name=creator.name,
        considered=len(outcomes),
        already_cached=sum(1 for o in outcomes if o.already_cached),
        newly_fetched=sum(1 for o in outcomes if o.newly_fetched),
        failed=sum(1 for o in outcomes if o.failed),
        videos=outcomes,
    )


def run_warm_cache(
    since: datetime,
    max_per_creator: int = DEFAULT_MAX_PER_CREATOR,
    dir: Path = DEFAULT_TRANSCRIPT_DIR,
    creator_ids: list[str] | None = None,
    dry_run: bool = False,
) -> WarmCacheRunSummary:
    """Warm the transcript cache across every eligible (optionally
    `creator_ids`-filtered) creator. Per-creator failures are non-fatal.

    If `fetch_transcript` ever raises `RequestBlocked` / `YouTubeRequestFailed`
    (an environmental signal — this IP or request is blocked, not a fact
    about one video), the run stops immediately rather than continuing to
    hammer a block across the remaining creators; creators processed before
    the stop are still reported."""
    creators = eligible_creators_filtered(creator_ids)
    summaries: list[CreatorWarmSummary] = []
    blocked = False
    blocked_reason: str | None = None

    for creator in creators:
        try:
            summary = warm_creator(creator, REGISTRY, since, max_per_creator, dir, dry_run)
        except (RequestBlocked, YouTubeRequestFailed) as e:
            blocked = True
            blocked_reason = f"{creator.creator_id}: {type(e).__name__}: {e}"
            logger.error(
                "warm_cache: transcript fetching blocked — stopping run early: %s",
                blocked_reason,
            )
            break
        summaries.append(summary)

    return WarmCacheRunSummary(
        creators=summaries, dry_run=dry_run, blocked=blocked, blocked_reason=blocked_reason
    )


def print_summary(summary: WarmCacheRunSummary, console: Console | None = None) -> None:
    console = console or Console()
    title = "Transcript cache warm — dry run" if summary.dry_run else "Transcript cache warm"
    table = Table(title=title)
    table.add_column("Creator")
    table.add_column("Considered", justify="right")
    table.add_column("Already cached", justify="right")
    if summary.dry_run:
        table.add_column("Would fetch", justify="right")
    else:
        table.add_column("Newly fetched", justify="right")
    table.add_column("Failed", justify="right")

    for c in summary.creators:
        would_fetch_or_newly_fetched = (
            c.considered - c.already_cached - c.failed if summary.dry_run else c.newly_fetched
        )
        table.add_row(
            c.creator_name if c.error is None else f"{c.creator_name} (ERROR: {c.error})",
            str(c.considered),
            str(c.already_cached),
            str(would_fetch_or_newly_fetched),
            str(c.failed),
        )

    console.print(table)

    if summary.dry_run:
        would_fetch_total = summary.considered - summary.already_cached - summary.failed
        console.print(
            f"TOTAL: {summary.considered} considered, {summary.already_cached} already cached, "
            f"{would_fetch_total} would fetch, {summary.failed} failed"
        )
    else:
        console.print(
            f"TOTAL: {summary.considered} considered, {summary.already_cached} already cached, "
            f"{summary.newly_fetched} newly fetched, {summary.failed} failed"
        )

    nothing = summary.creators_with_nothing
    if nothing:
        console.print(
            "\n[bold]Creators with NOTHING on disk in this window "
            f"({len(nothing)}):[/bold] " + ", ".join(c.creator_id for c in nothing)
        )

    if summary.blocked:
        console.print(
            f"\n[bold red]STOPPED EARLY — transcript fetching appears blocked: "
            f"{summary.blocked_reason}[/bold red]"
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Warm the transcript cache for every eligible creator's recent uploads"
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=f"only videos published on/after this date (YYYY-MM-DD); "
        f"default {DEFAULT_SINCE_DAYS} days ago",
    )
    parser.add_argument(
        "--max-per-creator",
        type=int,
        default=DEFAULT_MAX_PER_CREATOR,
        help=f"cap videos considered per creator (default {DEFAULT_MAX_PER_CREATOR})",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_TRANSCRIPT_DIR,
        help=f"transcript output dir (default {DEFAULT_TRANSCRIPT_DIR})",
    )
    parser.add_argument(
        "--creator-ids",
        nargs="+",
        default=None,
        help="limit to these creator ids (default: all eligible creators)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be fetched without fetching any transcript",
    )
    args = parser.parse_args(argv)

    configure_logging()

    since = parse_since(args.since) if args.since else default_since()

    summary = run_warm_cache(
        since=since,
        max_per_creator=args.max_per_creator,
        dir=args.dir,
        creator_ids=args.creator_ids,
        dry_run=args.dry_run,
    )
    print_summary(summary)

    if summary.blocked:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
