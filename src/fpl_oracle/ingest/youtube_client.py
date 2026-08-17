"""Thin client over the YouTube Data API v3.

All YouTube network calls go through here (never ad hoc `httpx` calls
elsewhere), with on-disk caching in `data/cache/youtube/` — channel
search and lookup results don't change fast enough to justify repeat
quota spend.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel

CACHE_DIR = Path("data/cache/youtube")
API_BASE = "https://www.googleapis.com/youtube/v3"

# Recent-video listings go stale, unlike channel lookups — ignore cache
# files older than this when deciding whether to hit the network again.
_UPLOADS_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60


class ChannelMatch(BaseModel):
    channel_id: str
    title: str
    description: str
    subscriber_count: int | None
    hidden_subscriber_count: bool


class VideoInfo(BaseModel):
    video_id: str
    channel_id: str
    title: str
    published_at: datetime
    description: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _cache_path(kind: str, key: str) -> Path:
    return CACHE_DIR / f"{kind}_{_slug(key)}.json"


def _read_cache(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text())
    return None


def _write_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _api_key() -> str:
    load_dotenv()
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY not set in environment/.env")
    return key


def search_top_channel(query: str) -> ChannelMatch | None:
    """Search for `query`, return the top channel result with subscriber
    count filled in (search.list doesn't include statistics, so this
    makes a follow-up channels.list call)."""
    cache_path = _cache_path("search", query)
    cached = _read_cache(cache_path)
    if cached is None:
        resp = httpx.get(
            f"{API_BASE}/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "channel",
                "maxResults": 1,
                "key": _api_key(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        cached = resp.json()
        _write_cache(cache_path, cached)

    items = cached.get("items", [])
    if not items:
        return None
    channel_id = items[0]["snippet"]["channelId"]
    return _get_channel_stats(channel_id)


def get_channel_by_handle(handle: str) -> ChannelMatch | None:
    """Look up a channel by its @handle directly — more precise than
    search when the handle is already known (e.g. from a creator's own
    bio link), since it skips YouTube's relevance ranking entirely."""
    handle = handle.lstrip("@")
    cache_path = _cache_path("handle", handle)
    cached = _read_cache(cache_path)
    if cached is None:
        resp = httpx.get(
            f"{API_BASE}/channels",
            params={
                "part": "snippet,statistics",
                "forHandle": handle,
                "key": _api_key(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        cached = resp.json()
        _write_cache(cache_path, cached)

    items = cached.get("items", [])
    if not items:
        return None
    item = items[0]
    stats = item.get("statistics", {})
    hidden = stats.get("hiddenSubscriberCount", False)
    return ChannelMatch(
        channel_id=item["id"],
        title=item["snippet"]["title"],
        description=item["snippet"].get("description", ""),
        subscriber_count=None if hidden else int(stats.get("subscriberCount", 0)),
        hidden_subscriber_count=hidden,
    )


def _parse_playlist_items(data: dict, channel_id: str) -> list[VideoInfo]:
    """Parse a `playlistItems.list` (part=snippet,contentDetails) response
    into `VideoInfo`s. Factored out from the HTTP call so it's testable
    offline against a canned fixture."""
    videos = []
    for item in data.get("items", []):
        snippet = item["snippet"]
        content_details = item.get("contentDetails", {})
        video_id = content_details.get("videoId") or snippet["resourceId"]["videoId"]
        published_at = content_details.get("videoPublishedAt") or snippet["publishedAt"]
        videos.append(
            VideoInfo(
                video_id=video_id,
                channel_id=channel_id,
                title=snippet["title"],
                published_at=published_at,
                description=snippet.get("description", ""),
            )
        )
    return videos


def _get_uploads_playlist_id(channel_id: str) -> str | None:
    """Resolve a channel's uploads playlist ID via `channels.list`
    part=contentDetails. Cached indefinitely — a channel's uploads
    playlist ID never changes."""
    cache_path = _cache_path("uploads-playlist", channel_id)
    cached = _read_cache(cache_path)
    if cached is None:
        resp = httpx.get(
            f"{API_BASE}/channels",
            params={
                "part": "contentDetails",
                "id": channel_id,
                "key": _api_key(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        cached = resp.json()
        _write_cache(cache_path, cached)

    items = cached.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_recent_videos(channel_id: str, max_results: int = 10) -> list[VideoInfo]:
    """List a channel's most recent uploads, newest first.

    Resolves the channel's uploads playlist (cached indefinitely — the
    playlist ID never changes) then lists items on it via
    `playlistItems.list`. Unlike channel lookups, this listing goes stale
    fast, so the read-through cache here has a freshness override: a
    cache file older than 6 hours is treated as a miss and refetched.
    This staleness rule is specific to this function, not the shared
    cache helpers.
    """
    playlist_id = _get_uploads_playlist_id(channel_id)
    if playlist_id is None:
        return []

    cache_path = _cache_path("uploads", channel_id)
    cache_is_fresh = (
        cache_path.exists()
        and (time.time() - cache_path.stat().st_mtime) < _UPLOADS_CACHE_MAX_AGE_SECONDS
    )
    cached = _read_cache(cache_path) if cache_is_fresh else None
    if cached is None:
        resp = httpx.get(
            f"{API_BASE}/playlistItems",
            params={
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": max_results,
                "key": _api_key(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        cached = resp.json()
        _write_cache(cache_path, cached)

    videos = _parse_playlist_items(cached, channel_id)
    videos.sort(key=lambda v: v.published_at, reverse=True)
    return videos[:max_results]


def _get_channel_stats(channel_id: str) -> ChannelMatch | None:
    cache_path = _cache_path("channel", channel_id)
    cached = _read_cache(cache_path)
    if cached is None:
        resp = httpx.get(
            f"{API_BASE}/channels",
            params={
                "part": "snippet,statistics",
                "id": channel_id,
                "key": _api_key(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        cached = resp.json()
        _write_cache(cache_path, cached)

    items = cached.get("items", [])
    if not items:
        return None
    item = items[0]
    stats = item.get("statistics", {})
    hidden = stats.get("hiddenSubscriberCount", False)
    return ChannelMatch(
        channel_id=item["id"],
        title=item["snippet"]["title"],
        description=item["snippet"].get("description", ""),
        subscriber_count=None if hidden else int(stats.get("subscriberCount", 0)),
        hidden_subscriber_count=hidden,
    )
