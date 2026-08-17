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
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel

CACHE_DIR = Path("data/cache/youtube")
API_BASE = "https://www.googleapis.com/youtube/v3"


class ChannelMatch(BaseModel):
    channel_id: str
    title: str
    description: str
    subscriber_count: int | None
    hidden_subscriber_count: bool


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
