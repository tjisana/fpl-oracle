"""Picks which backend reads a video's transcript: Gemini (default) or the
YouTube caption scraper (opt-in).

WHY GEMINI IS DEFAULT. `ingest/transcripts.fetch_transcript` scrapes
YouTube's internal caption endpoint — unsupported access that has IP-blocked
this machine three gameweeks running, each time costing real creators from a
deadline harvest. `ingest/gemini_transcripts.fetch_transcript_via_gemini`
reads the same video through Gemini's YouTube-URL input, which is supported
access with no bot detection and no IP block. It also watches the video, so
it can read a name off an on-screen team sheet where the audio is ambiguous
— on GW2 that resolved 96% of picks by name versus 80% for the caption
scrape. Together that makes Gemini the safer AND more accurate default.

WHAT CAPTIONS STILL BUYS. The caption track carries real per-word timing;
Gemini's segment timings are usually unusable and get discarded (see
`gemini_transcripts`'s module docstring), so a Gemini-sourced transcript
degrades the report's timestamped YouTube deep links to "no timestamp"
rather than a wrong one. Captions remain available as an explicit opt-in
(`source="captions"`) for callers that need those deep links and are willing
to risk the block.

Import order is one-way: this module imports from `transcripts` and
`gemini_transcripts`; neither of those may import this module, or the two
would form a cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fpl_oracle.ingest.gemini_transcripts import fetch_transcript_via_gemini
from fpl_oracle.ingest.transcripts import (
    DEFAULT_TRANSCRIPT_DIR,
    Transcript,
    fetch_transcript,
    load_transcript,
)

TranscriptSource = Literal["gemini", "captions"]

DEFAULT_TRANSCRIPT_SOURCE: TranscriptSource = "gemini"

TRANSCRIPT_SOURCES: tuple[TranscriptSource, ...] = ("gemini", "captions")

# Shared --source CLI help text (warm_cache.py and extract/run_extract.py
# both expose this flag and should describe it identically).
SOURCE_ARG_HELP = (
    "gemini (default): read the video via the Gemini API, never blocked, timestamps "
    "usually unavailable. captions: scrape YouTube's caption track — real timestamps, "
    "but has IP-blocked this machine three gameweeks running."
)


def fetch_transcript_from(
    video_id: str,
    source: TranscriptSource = DEFAULT_TRANSCRIPT_SOURCE,
    *,
    dir: Path = DEFAULT_TRANSCRIPT_DIR,
    duration_seconds: float | None = None,
) -> Transcript | None:
    """Fetch `video_id`'s transcript via `source`, checking the on-disk
    cache first regardless of which backend originally produced it — a
    cached transcript is a cached transcript, whichever way it was read.

    On a cache miss: `source="captions"` delegates to `fetch_transcript`,
    which may raise `RequestBlocked` / `YouTubeRequestFailed` — left to
    propagate, since callers already handle those as an environmental
    block. `source="gemini"` delegates to `fetch_transcript_via_gemini`,
    which never raises and returns `None` on failure instead."""
    cached = load_transcript(video_id, dir=dir)
    if cached is not None:
        return cached

    if source == "captions":
        return fetch_transcript(video_id, dir=dir)
    return fetch_transcript_via_gemini(video_id, duration_seconds)
