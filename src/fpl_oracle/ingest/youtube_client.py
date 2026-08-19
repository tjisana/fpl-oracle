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

# playlistItems.list and videos.list both cap out at 50 ids/items per
# request — used to page/batch requests that need more than that.
_API_PAGE_SIZE = 50

_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class YouTubeApiError(RuntimeError):
    """Raised for a non-2xx YouTube Data API response. Always constructed
    via `_get` so the message never contains the raw `key=...` query param —
    see that function's docstring."""


def _redact_key(url: str) -> str:
    """Strip a `key=...` query param's value out of `url` so it's safe to
    put in an exception message / log line. CLAUDE.md forbids printing
    secrets, and `httpx.HTTPStatusError`'s default message embeds the full
    request URL, key included."""
    return re.sub(r"([?&]key=)[^&]*", r"\1REDACTED", url)


def _get(url: str, params: dict) -> httpx.Response:
    """The one place every YouTube Data API GET goes through. Wraps
    `httpx.get` + `raise_for_status`, re-raising `httpx.HTTPStatusError` as
    a `YouTubeApiError` whose message has the API key redacted out of the
    URL — status code and endpoint stay visible, just not the secret."""
    resp = httpx.get(url, params=params, timeout=15)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise YouTubeApiError(
            f"YouTube API request failed: {resp.status_code} {_redact_key(str(e.request.url))}"
        ) from e
    return resp


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


def _slug_preserving_case(text: str) -> str:
    """Like `_slug`, but doesn't lowercase and keeps `-`/`_` distinct — for
    cache keys (like YouTube video ids, which are case-sensitive base64url
    strings using both `-` and `_`) where `_slug`'s normalization could
    collide two distinct keys onto the same cache file."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")


def _cache_path_preserving_case(kind: str, key: str) -> Path:
    return CACHE_DIR / f"{kind}_{_slug_preserving_case(key)}.json"


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
        resp = _get(
            f"{API_BASE}/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "channel",
                "maxResults": 1,
                "key": _api_key(),
            },
        )
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
        resp = _get(
            f"{API_BASE}/channels",
            params={
                "part": "snippet,statistics",
                "forHandle": handle,
                "key": _api_key(),
            },
        )
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
        resp = _get(
            f"{API_BASE}/channels",
            params={
                "part": "contentDetails",
                "id": channel_id,
                "key": _api_key(),
            },
        )
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
        resp = _get(
            f"{API_BASE}/playlistItems",
            params={
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": max_results,
                "key": _api_key(),
            },
        )
        cached = resp.json()
        _write_cache(cache_path, cached)

    videos = _parse_playlist_items(cached, channel_id)
    videos.sort(key=lambda v: v.published_at, reverse=True)
    return videos[:max_results]


def _uploads_cache_kind(max_results: int, stop_before: datetime | None) -> str:
    if stop_before is None:
        return f"uploads-deep-{max_results}"
    return f"uploads-deep-{max_results}-cutoff-{stop_before.date().isoformat()}"


def list_uploads(
    channel_id: str,
    max_results: int = 75,
    stop_before: datetime | None = None,
) -> list[VideoInfo]:
    """List up to `max_results` of a channel's uploads, newest first,
    paging through `playlistItems.list` (each page holds at most
    `_API_PAGE_SIZE` items, the API's max) until either `max_results` is
    reached, the channel runs out of uploads, or (when `stop_before` is
    given) a fetched page's oldest item predates `stop_before`.

    `stop_before` is a quota-saving early exit for callers with a known
    "won't need anything older than this" cutoff — e.g. the SELF_CLAIMED
    harvest sweep in `roster/harvest.py` only cares about the current
    season-review window, so it doesn't need to page back through a
    daily uploader's entire history. It does NOT filter the returned
    videos — a page that straddles the cutoff still has all of its items
    included in the result — it only stops requesting further pages once
    it's seen one that's aged past the cutoff.

    A sibling to `list_recent_videos` for callers that need to look
    further back than the freshest handful — e.g. that same harvest
    sweep, which looks for season-review videos that are typically 2-3
    months old by the time it runs, well outside `list_recent_videos`'s
    default page.

    Same 6h-freshness cache override as `list_recent_videos`, but this is
    an EXACT-match cache, not a smart one: it's keyed by the literal
    `(channel_id, max_results, stop_before)` triple, so a call with any
    different `max_results` or `stop_before` — shallower OR deeper —
    always misses and refetches from scratch. It never trims a deeper
    cached listing down to serve a shallower request, or extends a
    shallower one to serve a deeper request.
    """
    playlist_id = _get_uploads_playlist_id(channel_id)
    if playlist_id is None:
        return []

    cache_path = _cache_path(_uploads_cache_kind(max_results, stop_before), channel_id)
    cache_is_fresh = (
        cache_path.exists()
        and (time.time() - cache_path.stat().st_mtime) < _UPLOADS_CACHE_MAX_AGE_SECONDS
    )
    cached = _read_cache(cache_path) if cache_is_fresh else None
    if cached is None:
        items: list[dict] = []
        page_token: str | None = None
        while len(items) < max_results:
            params = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": min(_API_PAGE_SIZE, max_results - len(items)),
                "key": _api_key(),
            }
            if page_token:
                params["pageToken"] = page_token
            resp = _get(f"{API_BASE}/playlistItems", params=params)
            page = resp.json()
            page_items = page.get("items", [])
            items.extend(page_items)
            page_token = page.get("nextPageToken")

            if stop_before is not None and page_items:
                page_videos = _parse_playlist_items({"items": page_items}, channel_id)
                if min(v.published_at for v in page_videos) < stop_before:
                    break
            if not page_token:
                break
        cached = {"items": items}
        _write_cache(cache_path, cached)

    videos = _parse_playlist_items(cached, channel_id)
    videos.sort(key=lambda v: v.published_at, reverse=True)
    return videos[:max_results]


def parse_iso8601_duration(duration: str) -> int:
    """Parse a YouTube `contentDetails.duration` ISO-8601 duration string
    (e.g. "PT1M30S", "PT1H2M3S", "PT45S") into whole seconds.

    Pure, no I/O — YouTube durations only ever carry day/hour/minute/
    second components (no months/years, videos aren't that long), so
    that's all this parses; anything else raises `ValueError`.

    Note: YouTube reports "P0D" (parses to 0s here) for a live/upcoming
    broadcast that hasn't finished airing yet — a livestream in progress
    has no fixed duration. Landing on 0s is desirable, not a bug: it
    falls below `run_ingest.MIN_DURATION_S`, so the Shorts filter drops
    it too, which is correct for a different reason — no transcript
    exists yet for a stream that hasn't ended.
    """
    match = _ISO8601_DURATION_RE.match(duration)
    if not match:
        raise ValueError(f"Unrecognized ISO-8601 duration: {duration!r}")
    parts = match.groupdict()
    days = int(parts["days"] or 0)
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def get_video_durations(video_ids: list[str]) -> dict[str, int]:
    """Fetch each video's duration in seconds via `videos.list`
    part=contentDetails. Cached indefinitely per video id, like channel
    lookups — a published video's duration never changes.

    Ids already cached are served from disk; the rest are batched into
    `videos.list` calls of up to `_API_PAGE_SIZE` ids each (the API's
    max for a single request). Returns a dict rather than a list since a
    video that YouTube doesn't return (deleted/private) simply won't have
    a key — callers shouldn't assume every input id comes back.
    """
    durations: dict[str, int] = {}
    missing: list[str] = []
    for video_id in video_ids:
        cache_path = _cache_path_preserving_case("duration", video_id)
        cached = _read_cache(cache_path)
        if cached is not None and "duration_s" in cached:
            durations[video_id] = cached["duration_s"]
        else:
            missing.append(video_id)

    for i in range(0, len(missing), _API_PAGE_SIZE):
        batch = missing[i : i + _API_PAGE_SIZE]
        resp = _get(
            f"{API_BASE}/videos",
            params={"part": "contentDetails", "id": ",".join(batch), "key": _api_key()},
        )
        data = resp.json()
        for item in data.get("items", []):
            video_id = item["id"]
            seconds = parse_iso8601_duration(item["contentDetails"]["duration"])
            durations[video_id] = seconds
            _write_cache(_cache_path_preserving_case("duration", video_id), {"duration_s": seconds})

    return durations


def _get_channel_stats(channel_id: str) -> ChannelMatch | None:
    cache_path = _cache_path("channel", channel_id)
    cached = _read_cache(cache_path)
    if cached is None:
        resp = _get(
            f"{API_BASE}/channels",
            params={
                "part": "snippet,statistics",
                "id": channel_id,
                "key": _api_key(),
            },
        )
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
