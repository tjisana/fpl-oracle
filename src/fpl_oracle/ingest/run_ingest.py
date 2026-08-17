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
    """Creators with a personal channel_id, deduped by channel_id: any
    channel_id shared by more than one creator (the three Fantasy Football
    Hub personas, and the FPL Wire co-hosts) is dropped entirely, since it
    doesn't identify a single creator's own uploads. Ordered Tier.CORE
    first, then registry order (stable sort preserves ties within a tier)."""
    with_channel = [c for c in registry if c.channel_id is not None]
    channel_counts = Counter(c.channel_id for c in with_channel)
    unique = [c for c in with_channel if channel_counts[c.channel_id] == 1]
    return sorted(unique, key=lambda c: c.tier)


def ingest_one(creator: Creator) -> IngestedTranscript | None:
    """List a creator's recent videos and fetch+save a transcript for the
    most recent one that has an English transcript available, falling back
    through older videos in the listing before giving up on this creator."""
    assert creator.channel_id is not None
    videos = list_recent_videos(creator.channel_id, max_results=VIDEOS_PER_CHANNEL)
    if not videos:
        print(f"run_ingest: no videos found for {creator.name}", file=sys.stderr)
        return None

    for video in videos:
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
        result = ingest_one(creator)
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
