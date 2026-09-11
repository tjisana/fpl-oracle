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
`sourcing.fetch_transcript_from` (the read-through cache, defaulting to
Gemini — see that module's docstring for why) for the actual transcript
fetch.

    uv run python -m fpl_oracle.ingest.warm_cache --since 2026-08-22
"""

from __future__ import annotations

import logging
import random
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from youtube_transcript_api import RequestBlocked, YouTubeRequestFailed

from fpl_oracle.ingest.run_ingest import (
    eligible_creators,
    filter_min_duration,
    videos_for_creator,
)
from fpl_oracle.ingest.sourcing import (
    DEFAULT_TRANSCRIPT_SOURCE,
    SOURCE_ARG_HELP,
    TRANSCRIPT_SOURCES,
    TranscriptSource,
    fetch_transcript_from,
)
from fpl_oracle.ingest.transcripts import (
    DEFAULT_TRANSCRIPT_DIR,
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

# Seconds to wait between transcript fetches that actually hit the network
# (cached hits don't sleep). NOT arbitrary: the first live run pulled 34
# transcripts in 59 seconds and was IP-blocked immediately afterwards.
# Each transcript costs ~3 requests to youtube.com, so that burst ran at
# ~1.7 req/s sustained, unauthenticated, from one residential IP.
# ~2s with jitter puts us near 1 request every 2 seconds, which costs a
# few minutes across the whole roster and is the difference between a
# warm cache and a blocked one on deadline eve.
DEFAULT_FETCH_DELAY_SECONDS = 2.0
# Jitter fraction applied to the delay, so we don't hammer a metronome-
# regular pattern that is itself a bot signal.
FETCH_DELAY_JITTER = 0.4

# Circuit breaker. For `source="captions"`, `fetch_transcript_from` only
# re-raises RequestBlocked and YouTubeRequestFailed; other environmental
# anti-bot states (PoTokenRequired, YouTubeDataUnparsable — both
# CouldNotRetrieveTranscript subclasses) come back as a plain None,
# indistinguishable from "this video has no captions". For
# `source="gemini"` every failure comes back as None too (it never raises).
# Either way, a run in that state would mark EVERY video failed, never trip
# the block path, and exit 0 looking finished. So: if this many fetches
# fail in a row without a single success, stop and say so.
CONSECUTIVE_FAILURE_LIMIT = 6

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
        description="set when a block or the circuit breaker stopped the run early",
    )
    total_creators: int = Field(
        default=0,
        description=(
            "eligible creators the run intended to cover. On a blocked run this is the "
            "denominator the per-creator table lacks — without it the table reads as full "
            "coverage when most creators were never attempted."
        ),
    )

    @property
    def attempted(self) -> int:
        return len(self.creators)

    @property
    def never_attempted(self) -> int:
        return max(0, self.total_creators - self.attempted)

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


def _durations_for(videos: list[VideoInfo], creator: Creator, purpose: str) -> dict[str, int]:
    """Look up durations for `videos`, non-fatally. A quota blip, a
    transient API failure, or a transport-level error (connection refused,
    timeout — `httpx.HTTPError`, which `YouTubeApiError` does NOT subclass)
    must not zero out a warming run — see `filter_min_duration`'s "unknown
    is kept, never treated as too short" contract, which the Shorts-filter
    caller feeds; the gemini-timing caller just does without timing
    validation instead (see `gemini_transcripts`).

    `purpose` names what gets skipped in the warning, e.g. "the
    min-duration Shorts filter" or "Gemini timing validation" — the two
    current callers, `videos_to_warm` and `warm_creator`, mean different
    things by a duration-lookup failure and the log line should say which."""
    try:
        return get_video_durations([v.video_id for v in videos])
    except (YouTubeApiError, httpx.HTTPError) as e:
        logger.warning(
            "warm_cache: duration lookup failed (%s) — skipping %s for %s",
            e,
            purpose,
            creator.name,
        )
        return {}


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

    durations = _durations_for(since_filtered, creator, "the min-duration Shorts filter")
    selectable = filter_min_duration(since_filtered, durations)
    return selectable[:max_per_creator]


class _FailureStreak:
    """Counts consecutive transcript-fetch failures across the whole run.
    Any success resets it. Trips once `limit` failures occur back to back —
    the signal that we are being blocked rather than reading a run of
    caption-less videos."""

    def __init__(self, limit: int = CONSECUTIVE_FAILURE_LIMIT) -> None:
        self.limit = limit
        self.consecutive = 0

    def record_failure(self) -> bool:
        """Record a failure; return True if the breaker has now tripped."""
        self.consecutive += 1
        return self.consecutive >= self.limit

    def record_success(self) -> None:
        self.consecutive = 0


class WarmingInterrupted(Exception):
    """Raised out of `warm_creator` when the run must stop mid-creator —
    an environmental block, or the circuit breaker tripping. Carries the
    outcomes already completed for that creator so the caller can still
    report work that really happened (transcripts already saved to disk),
    instead of dropping the in-flight creator from the summary."""

    def __init__(self, reason: str, outcomes: list[VideoWarmOutcome]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcomes = outcomes


def _summarise(creator: Creator, outcomes: list[VideoWarmOutcome]) -> CreatorWarmSummary:
    return CreatorWarmSummary(
        creator_id=creator.creator_id,
        creator_name=creator.name,
        considered=len(outcomes),
        already_cached=sum(1 for o in outcomes if o.already_cached),
        newly_fetched=sum(1 for o in outcomes if o.newly_fetched),
        failed=sum(1 for o in outcomes if o.failed),
        videos=outcomes,
    )


def warm_creator(
    creator: Creator,
    registry: list[Creator],
    since: datetime,
    max_per_creator: int,
    dir: Path,
    dry_run: bool,
    source: TranscriptSource = DEFAULT_TRANSCRIPT_SOURCE,
    delay_seconds: float = DEFAULT_FETCH_DELAY_SECONDS,
    breaker: _FailureStreak | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CreatorWarmSummary:
    """Warm (or, in `dry_run`, just report on) one creator's candidate
    videos. Video-listing failures (`YouTubeApiError`) are caught and
    recorded as a creator-level failure — non-fatal, same discipline as
    `run_extract.py`.

    Stops the whole run, via `WarmingInterrupted` carrying the outcomes
    completed so far, when either:

    * `fetch_transcript_from` raises `RequestBlocked`/`YouTubeRequestFailed`
      (captions only) — environmental (this IP is blocked), not a fact
      about this creator; or
    * `breaker` trips, meaning enough fetches have failed in a row with no
      success that "these videos have no captions"/"gemini keeps failing"
      is no longer the plausible explanation (see
      `CONSECUTIVE_FAILURE_LIMIT`).

    Network fetches are throttled by `delay_seconds` with jitter, but only
    for `source="captions"` — that throttle exists to avoid YouTube scrape
    blocks; Gemini isn't rate-limited by IP the same way, and its own
    per-video latency (30-90s) is throttle enough on its own. Cached hits
    never sleep either way."""
    try:
        videos = videos_to_warm(creator, registry, since, max_per_creator)
    except YouTubeApiError as e:
        logger.warning("warm_cache: %s — video listing failed: %s", creator.creator_id, e)
        summary = _summarise(creator, [])
        summary.error = f"video listing failed: {e}"
        return summary

    # Gemini needs each video's duration to validate segment timings (see
    # gemini_transcripts' module docstring); captions doesn't. videos_to_warm
    # already looked durations up once for the Shorts filter, over a WIDER
    # candidate set, and discarded them — this is one extra cheap videos.list
    # call per creator, scoped to just the (much smaller) selected list.
    durations = (
        _durations_for(videos, creator, "Gemini timing validation") if source == "gemini" else {}
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

        # Throttle only real network work, and only for the caption scrape —
        # see this function's docstring. Sleeping BEFORE the fetch (not
        # after) means a run that is entirely cache hits never sleeps at all.
        if source == "captions" and delay_seconds > 0:
            jitter = delay_seconds * FETCH_DELAY_JITTER
            sleep(max(0.0, delay_seconds + random.uniform(-jitter, jitter)))

        try:
            transcript = fetch_transcript_from(
                video.video_id, source, dir=dir, duration_seconds=durations.get(video.video_id)
            )
        except (RequestBlocked, YouTubeRequestFailed) as e:
            raise WarmingInterrupted(
                f"{creator.creator_id}: {type(e).__name__}: {e}", outcomes
            ) from e

        if transcript is None:
            logger.info(
                "warm_cache: no transcript for '%s' (%s) — %s",
                video.title,
                video.video_id,
                creator.name,
            )
            failure_reason = (
                "gemini could not read the video"
                if source == "gemini"
                else "no transcript available"
            )
            outcomes.append(
                VideoWarmOutcome(
                    video_id=video.video_id,
                    title=video.title,
                    published_at=video.published_at,
                    already_cached=False,
                    failed=True,
                    failure_reason=failure_reason,
                )
            )
            if breaker is not None and breaker.record_failure():
                if source == "gemini":
                    breaker_reason = (
                        f"{breaker.limit} transcript fetches failed in a row with no success "
                        f"(last: {creator.creator_id}/{video.video_id}) — likely the Gemini API "
                        "is down or the free tier's 8-hours-of-video-per-day cap is exhausted"
                    )
                else:
                    breaker_reason = (
                        f"{breaker.limit} transcript fetches failed in a row with no success "
                        f"(last: {creator.creator_id}/{video.video_id}) — this looks like an "
                        f"environmental block, not {breaker.limit} caption-less videos"
                    )
                raise WarmingInterrupted(breaker_reason, outcomes)
            continue

        save_transcript(transcript, dir=dir)
        if breaker is not None:
            breaker.record_success()
        outcomes.append(
            VideoWarmOutcome(
                video_id=video.video_id,
                title=video.title,
                published_at=video.published_at,
                already_cached=False,
                newly_fetched=True,
            )
        )

    return _summarise(creator, outcomes)


def run_warm_cache(
    since: datetime,
    max_per_creator: int = DEFAULT_MAX_PER_CREATOR,
    dir: Path = DEFAULT_TRANSCRIPT_DIR,
    creator_ids: list[str] | None = None,
    dry_run: bool = False,
    source: TranscriptSource = DEFAULT_TRANSCRIPT_SOURCE,
    delay_seconds: float = DEFAULT_FETCH_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> WarmCacheRunSummary:
    """Warm the transcript cache across every eligible (optionally
    `creator_ids`-filtered) creator. Per-creator failures are non-fatal.

    The run stops early, reporting everything completed so far, when the
    environment turns against us: an explicit `RequestBlocked` /
    `YouTubeRequestFailed`, or the consecutive-failure circuit breaker
    tripping on anti-bot states that arrive disguised as ordinary
    per-video misses. The in-flight creator's completed work is preserved
    in the summary — those transcripts are on disk, and a report that
    omitted them would understate what tomorrow's extraction can use.

    Anything else unexpected (a connection reset, a DNS blip) is recorded
    against the run and also stops it, rather than escaping as a traceback
    that would lose the summary entirely."""
    creators = eligible_creators_filtered(creator_ids)
    summaries: list[CreatorWarmSummary] = []
    breaker = _FailureStreak()
    blocked = False
    blocked_reason: str | None = None

    for creator in creators:
        try:
            summary = warm_creator(
                creator,
                REGISTRY,
                since,
                max_per_creator,
                dir,
                dry_run,
                source=source,
                delay_seconds=delay_seconds,
                breaker=breaker,
                sleep=sleep,
            )
        except WarmingInterrupted as e:
            blocked = True
            blocked_reason = e.reason
            # The in-flight creator really did fetch these before we stopped.
            if e.outcomes:
                summaries.append(_summarise(creator, e.outcomes))
            logger.error("warm_cache: stopping run early — %s", blocked_reason)
            break
        except Exception as e:  # noqa: BLE001 - see docstring: never lose the summary
            blocked = True
            blocked_reason = f"{creator.creator_id}: unexpected {type(e).__name__}: {e}"
            logger.error("warm_cache: stopping run early — %s", blocked_reason)
            break
        summaries.append(summary)

    return WarmCacheRunSummary(
        creators=summaries,
        dry_run=dry_run,
        blocked=blocked,
        blocked_reason=blocked_reason,
        total_creators=len(creators),
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

    if summary.never_attempted:
        console.print(
            f"[bold yellow]{summary.attempted}/{summary.total_creators} creators attempted — "
            f"{summary.never_attempted} never reached.[/bold yellow] The table above is NOT "
            f"full coverage."
        )

    nothing = summary.creators_with_nothing
    if nothing:
        console.print(
            "\n[bold]Of the creators attempted, these have NOTHING on disk in this window "
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
    parser.add_argument(
        "--source",
        choices=TRANSCRIPT_SOURCES,
        default=DEFAULT_TRANSCRIPT_SOURCE,
        help=SOURCE_ARG_HELP,
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_FETCH_DELAY_SECONDS,
        help=(
            f"seconds to wait between transcript fetches that hit the network, applies to "
            f"--source captions only (default {DEFAULT_FETCH_DELAY_SECONDS}; 0 disables). "
            f"The first unthrottled captions run pulled 34 transcripts in 59s and was "
            f"IP-blocked."
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()

    since = parse_since(args.since) if args.since else default_since()

    # `extract/run_extract.py` calls `fetch_transcript_from` with no dir argument,
    # so it can only ever read DEFAULT_TRANSCRIPT_DIR. Warming somewhere else
    # produces a cache the paid extraction stage cannot see — silently.
    if args.dir.resolve() != DEFAULT_TRANSCRIPT_DIR.resolve():
        logger.warning(
            "warm_cache: --dir is %s, but extraction only reads %s — transcripts warmed "
            "here will NOT be visible to run_extract",
            args.dir,
            DEFAULT_TRANSCRIPT_DIR,
        )

    summary = run_warm_cache(
        since=since,
        max_per_creator=args.max_per_creator,
        dir=args.dir,
        creator_ids=args.creator_ids,
        dry_run=args.dry_run,
        source=args.source,
        delay_seconds=0.0 if args.dry_run else args.delay,
    )
    print_summary(summary)

    if summary.blocked:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
