"""End-to-end transcript ingestion runner.

For a handful of creators from the roster registry, lists each creator's
most recent uploads and fetches+saves a transcript for one of them (falling
back to the next-most-recent video if the newest one has no transcript
available). Run as:

    uv run python -m fpl_oracle.ingest.run_ingest
"""

from __future__ import annotations

import sys
from collections import Counter

from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from fpl_oracle.ingest.transcripts import Transcript, fetch_transcript, save_transcript
from fpl_oracle.ingest.youtube_client import VideoInfo, list_recent_videos
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY

TARGET_TRANSCRIPT_COUNT = 3
VIDEOS_PER_CHANNEL = 10

console = Console()


class IngestedTranscript(BaseModel):
    creator: Creator
    video: VideoInfo
    transcript: Transcript
    saved_path: str


def eligible_creators(registry: list[Creator]) -> list[Creator]:
    """Creators eligible for ingestion: sole owner of their channel_id, OR
    has a `title_filter` (attributable slice of a shared channel), OR is
    `channel_primary` (ingests a shared channel's unclaimed videos). A
    co-host on a shared channel with neither (e.g. Zophar, Lateriser on The
    FPL Wire) is excluded — their content flows in via the channel's
    primary instead. Ordered Tier.CORE first, then registry order (stable
    sort preserves ties within a tier)."""
    with_channel = [c for c in registry if c.channel_id is not None]
    channel_counts = Counter(c.channel_id for c in with_channel)
    eligible = [
        c
        for c in with_channel
        if channel_counts[c.channel_id] == 1 or c.title_filter is not None or c.channel_primary
    ]
    return sorted(eligible, key=lambda c: c.tier)


def videos_for_creator(
    creator: Creator,
    co_creators_on_channel: list[Creator],
    videos: list[VideoInfo],
) -> list[VideoInfo]:
    """Attribute a shared channel's videos to `creator`, per the design in
    `roster.models.Creator`:

    - If `creator` has a `title_filter`: a video belongs to them iff their
      filter matches its title (case-insensitive substring) AND no other
      `co_creators_on_channel` filter also matches — a title matching two+
      personas' filters is ambiguous and goes to nobody.
    - Elif `creator` is `channel_primary`: they get every video that no
      co-creator's `title_filter` claims.
    - Otherwise (no co-creators sharing the channel, i.e. a sole owner):
      every video belongs to them, unfiltered.

    Pure function — no I/O — so it's unit-testable without hitting the
    YouTube API.
    """
    if creator.title_filter is not None:
        needle = creator.title_filter.lower()
        other_needles = [
            c.title_filter.lower() for c in co_creators_on_channel if c.title_filter is not None
        ]
        return [
            v
            for v in videos
            if needle in v.title.lower()
            and not any(other in v.title.lower() for other in other_needles)
        ]

    if creator.channel_primary:
        claim_needles = [
            c.title_filter.lower() for c in co_creators_on_channel if c.title_filter is not None
        ]
        return [v for v in videos if not any(needle in v.title.lower() for needle in claim_needles)]

    return list(videos)


def ingest_one(creator: Creator, registry: list[Creator]) -> IngestedTranscript | None:
    """List a creator's recent videos, attribute them per the shared-channel
    rule (a no-op for creators with a personal channel), and fetch+save a
    transcript for the most recent attributed one that has an English
    transcript available, falling back through older videos before giving
    up on this creator."""
    assert creator.channel_id is not None
    videos = list_recent_videos(creator.channel_id, max_results=VIDEOS_PER_CHANNEL)
    if not videos:
        print(f"run_ingest: no videos found for {creator.name}", file=sys.stderr)
        return None

    co_creators = [
        c
        for c in registry
        if c.channel_id == creator.channel_id and c.creator_id != creator.creator_id
    ]
    attributed = videos_for_creator(creator, co_creators, videos)
    if not attributed:
        print(
            f"run_ingest: no videos attributed to {creator.name} on its shared channel",
            file=sys.stderr,
        )
        return None

    for video in attributed:
        transcript = fetch_transcript(video.video_id)
        if transcript is None:
            print(
                f"run_ingest: no transcript for '{video.title}' ({video.video_id}) — "
                f"trying next video for {creator.name}",
                file=sys.stderr,
            )
            continue
        saved_path = save_transcript(transcript)
        return IngestedTranscript(
            creator=creator,
            video=video,
            transcript=transcript,
            saved_path=str(saved_path),
        )

    print(f"run_ingest: no video with a transcript found for {creator.name}", file=sys.stderr)
    return None


def print_summary(results: list[IngestedTranscript]) -> None:
    table = Table(title="Transcript ingestion summary")
    table.add_column("Creator")
    table.add_column("Video title")
    table.add_column("Published")
    table.add_column("Lang")
    table.add_column("Source")
    table.add_column("Segments", justify="right")
    table.add_column("Words", justify="right")
    table.add_column("Saved path")

    for result in results:
        table.add_row(
            result.creator.name,
            result.video.title,
            result.video.published_at.date().isoformat(),
            result.transcript.language,
            "auto" if result.transcript.is_generated else "manual",
            str(len(result.transcript.segments)),
            str(len(result.transcript.full_text.split())),
            result.saved_path,
        )

    console.print(table)


def main() -> int:
    candidates = eligible_creators(REGISTRY)
    results: list[IngestedTranscript] = []

    for creator in candidates:
        if len(results) >= TARGET_TRANSCRIPT_COUNT:
            break
        result = ingest_one(creator, REGISTRY)
        if result is not None:
            results.append(result)

    print_summary(results)

    if len(results) < TARGET_TRANSCRIPT_COUNT:
        print(
            f"run_ingest: only saved {len(results)}/{TARGET_TRANSCRIPT_COUNT} transcripts",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
