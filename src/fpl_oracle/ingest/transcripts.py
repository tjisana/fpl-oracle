"""Transcript fetching and on-disk storage for YouTube videos.

All transcript network calls go through `fetch_transcript` (never ad hoc
`youtube_transcript_api` calls elsewhere). Fetched transcripts are saved
under `data/transcripts/{video_id}.json`, which doubles as a cache —
transcripts of already-published videos don't change, so `fetch_transcript`
checks the on-disk copy before hitting the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    RequestBlocked,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)

DEFAULT_TRANSCRIPT_DIR = Path("data/transcripts")


class TranscriptSegment(BaseModel):
    text: str
    start: float
    duration: float


class Transcript(BaseModel):
    video_id: str
    language: str
    is_generated: bool
    segments: list[TranscriptSegment]

    @property
    def full_text(self) -> str:
        return " ".join(segment.text for segment in self.segments)


def _select_transcript(video_id: str) -> Transcript | None:
    """Pick a transcript for `video_id`: prefer a manually-created English
    transcript, fall back to an auto-generated one. Returns None (after
    logging to stderr) if no English transcript is available at all.

    Any other `CouldNotRetrieveTranscript` subclass (transcripts disabled,
    age-restricted, unplayable, IP blocked, ...) propagates to the caller —
    `fetch_transcript` is what decides which of those are a per-video skip
    versus an environmental failure that should raise."""
    transcript_list = YouTubeTranscriptApi().list(video_id)

    try:
        found = transcript_list.find_manually_created_transcript(["en"])
    except NoTranscriptFound:
        try:
            found = transcript_list.find_generated_transcript(["en"])
        except NoTranscriptFound:
            print(
                f"transcripts: no English transcript available for video {video_id}",
                file=sys.stderr,
            )
            return None

    fetched = found.fetch()
    return Transcript(
        video_id=video_id,
        language=fetched.language_code,
        is_generated=fetched.is_generated,
        segments=[
            TranscriptSegment(text=s.text, start=s.start, duration=s.duration) for s in fetched
        ],
    )


def fetch_transcript(video_id: str, dir: Path = DEFAULT_TRANSCRIPT_DIR) -> Transcript | None:
    """Fetch the transcript for `video_id`, preferring a manually-created
    English transcript and falling back to auto-generated English.

    Checks the on-disk store first (`load_transcript`) since transcripts of
    published videos don't change. Returns None for any per-video reason a
    transcript can't be produced — disabled, age-restricted, unplayable, no
    English transcript, etc: anything in youtube_transcript_api's
    `CouldNotRetrieveTranscript` hierarchy. `RequestBlocked` (and its
    `IpBlocked` subclass) and `YouTubeRequestFailed` are re-raised instead
    of swallowed — those mean YouTube is blocking this IP or a request
    itself failed, which isn't a fact about this particular video and
    shouldn't be treated as "no transcript, try the next video". Any other
    unexpected error is left to raise too.
    """
    cached = load_transcript(video_id, dir=dir)
    if cached is not None:
        return cached

    try:
        return _select_transcript(video_id)
    except RequestBlocked:
        raise
    except YouTubeRequestFailed:
        raise
    except CouldNotRetrieveTranscript as e:
        print(
            f"transcripts: no transcript available for video {video_id} ({type(e).__name__})",
            file=sys.stderr,
        )
        return None


def save_transcript(t: Transcript, dir: Path = DEFAULT_TRANSCRIPT_DIR) -> Path:
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / f"{t.video_id}.json"
    path.write_text(t.model_dump_json(indent=2))
    return path


def load_transcript(video_id: str, dir: Path = DEFAULT_TRANSCRIPT_DIR) -> Transcript | None:
    path = dir / f"{video_id}.json"
    if not path.exists():
        return None
    return Transcript.model_validate_json(path.read_text())
