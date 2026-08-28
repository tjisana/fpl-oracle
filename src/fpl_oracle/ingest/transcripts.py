"""Transcript fetching and on-disk storage for YouTube videos.

All transcript network calls go through `fetch_transcript` (never ad hoc
`youtube_transcript_api` calls elsewhere). Fetched transcripts are saved
under `data/transcripts/{video_id}.json`, which doubles as a cache —
transcripts of already-published videos don't change, so `fetch_transcript`
checks the on-disk copy before hitting the network.

Proxy support (optional, off by default) is configured entirely through
environment variables — see `_proxy_config_from_env` and
`docs/transcript-blocking.md`. Unset, behaviour is unchanged: no proxy,
requests go out on this machine's own IP.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    RequestBlocked,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.proxies import GenericProxyConfig, ProxyConfig, WebshareProxyConfig

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


def _proxy_config_from_env() -> ProxyConfig | None:
    """Build a `youtube_transcript_api` proxy config from environment
    variables, or return None if none are set — the default, and today's
    behaviour: no proxy, all requests go out on this machine's own IP.

    Two backends, checked in this order (first one with enough config wins):

      - Webshare residential proxy — the backend `youtube_transcript_api`
        itself recommends for working around IP blocks:
        `YT_PROXY_WEBSHARE_USERNAME` / `YT_PROXY_WEBSHARE_PASSWORD`
        (+ optional `YT_PROXY_WEBSHARE_LOCATIONS`, a comma-separated list
        of location codes to restrict the IP pool to).
      - Any generic HTTP/HTTPS/SOCKS proxy: `YT_PROXY_HTTP_URL` and/or
        `YT_PROXY_HTTPS_URL` (at least one required).

    Credentials are read from the environment (loaded from `.env` via
    python-dotenv, per project convention) and are never logged — only the
    resulting `ProxyConfig` object is returned, never the raw values."""
    load_dotenv()

    webshare_username = os.environ.get("YT_PROXY_WEBSHARE_USERNAME")
    webshare_password = os.environ.get("YT_PROXY_WEBSHARE_PASSWORD")
    if webshare_username and webshare_password:
        locations_raw = os.environ.get("YT_PROXY_WEBSHARE_LOCATIONS")
        locations = (
            [loc.strip() for loc in locations_raw.split(",") if loc.strip()]
            if locations_raw
            else None
        )
        return WebshareProxyConfig(
            proxy_username=webshare_username,
            proxy_password=webshare_password,
            filter_ip_locations=locations,
        )

    http_url = os.environ.get("YT_PROXY_HTTP_URL")
    https_url = os.environ.get("YT_PROXY_HTTPS_URL")
    if http_url or https_url:
        return GenericProxyConfig(http_url=http_url, https_url=https_url)

    return None


def _select_transcript(video_id: str) -> Transcript | None:
    """Pick a transcript for `video_id`: prefer a manually-created English
    transcript, fall back to an auto-generated one. Returns None (after
    logging to stderr) if no English transcript is available at all.

    Any other `CouldNotRetrieveTranscript` subclass (transcripts disabled,
    age-restricted, unplayable, IP blocked, ...) propagates to the caller —
    `fetch_transcript` is what decides which of those are a per-video skip
    versus an environmental failure that should raise."""
    transcript_list = YouTubeTranscriptApi(proxy_config=_proxy_config_from_env()).list(video_id)

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
    """Write atomically: a transcript is the cache, and a half-written one
    is worse than a missing one — `load_transcript` would raise on the
    truncated JSON and take down both the warmer and the extractor rather
    than simply refetching. Killing a long harvest partway through is a
    routine event (it has happened), so tmp-then-rename is not paranoia."""
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / f"{t.video_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(t.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path


def load_transcript(video_id: str, dir: Path = DEFAULT_TRANSCRIPT_DIR) -> Transcript | None:
    """Return the stored transcript, or None if there isn't a usable one.

    A corrupt file counts as absent: it is a cache, so the right response
    is to refetch, not to crash a deadline-morning run."""
    path = dir / f"{video_id}.json"
    if not path.exists():
        return None
    try:
        return Transcript.model_validate_json(path.read_text())
    except (ValidationError, ValueError, OSError) as e:
        print(
            f"transcripts: discarding unreadable cached transcript {path} ({type(e).__name__})",
            file=sys.stderr,
        )
        return None
