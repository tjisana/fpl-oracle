"""Thin client over the public FPL API.

All FPL network calls go through here, with on-disk caching in
`data/cache/fpl/` and a browser-like User-Agent — the API 403s generic
UAs.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache/fpl")
API_BASE = "https://fantasy.premierleague.com/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# bootstrap-static changes throughout the day (prices, injury flags) unlike
# the entry endpoints below, so its cache goes stale — a cache file older
# than this is treated as a miss and refetched. Same spirit as the
# youtube_client uploads-listing freshness override.
_BOOTSTRAP_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60


class EntrySummary(BaseModel):
    entry_id: int
    team_name: str
    player_first_name: str
    player_last_name: str
    summary_overall_rank: int | None


class PastSeason(BaseModel):
    season_name: str
    rank: int | None


class EntryHistory(BaseModel):
    entry_id: int
    past: list[PastSeason]


def _cache_path(kind: str, entry_id: int) -> Path:
    return CACHE_DIR / f"{kind}_{entry_id}.json"


_NOT_FOUND = {"not_found": True}


def _get_json(url: str, cache_path: Path) -> dict | None:
    """Returns None for a cached-or-live 404 (bad ID), the JSON body otherwise."""
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        return None if data == _NOT_FOUND else data

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            cache_path.write_text(json.dumps(_NOT_FOUND))
            return None
        raise
    data = resp.json()
    cache_path.write_text(json.dumps(data, indent=2))
    return data


# The FPL API is at its flakiest (429/503 under load) in the final hours
# before a deadline — exactly when the deadline-morning rerun forces a
# refetch and cannot fall back to the cache. A single blip must not be the
# difference between a squad and no squad, so the fetch retries with
# backoff rather than propagating the first error.
_BOOTSTRAP_FETCH_ATTEMPTS = 3
_BOOTSTRAP_RETRY_BACKOFF_SECONDS = 2.0


def _fetch_bootstrap_with_retries() -> dict:
    last_error: Exception | None = None
    for attempt in range(_BOOTSTRAP_FETCH_ATTEMPTS):
        try:
            resp = httpx.get(f"{API_BASE}/bootstrap-static/", headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            last_error = e
            if attempt == _BOOTSTRAP_FETCH_ATTEMPTS - 1:
                break
            delay = _BOOTSTRAP_RETRY_BACKOFF_SECONDS * (2**attempt)
            logger.warning(
                "bootstrap-static fetch failed (attempt %d/%d): %s — retrying in %.0fs",
                attempt + 1,
                _BOOTSTRAP_FETCH_ATTEMPTS,
                e,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"bootstrap-static unavailable after {_BOOTSTRAP_FETCH_ATTEMPTS} attempts: {last_error}. "
        "To solve on the cached (possibly stale) player data instead, run without "
        "--force-refresh: uv run python -m fpl_oracle.pipeline"
    ) from last_error


def _bootstrap_cache_path() -> Path:
    return CACHE_DIR / "bootstrap_static.json"


def get_bootstrap_static(force_refresh: bool = False) -> dict:
    """Fetch the `bootstrap-static/` payload: all players, teams,
    gameweeks, current prices/positions/ownership.

    Unlike the entry endpoints (`_get_json`, cached indefinitely — a
    manager's history doesn't rewrite itself), this payload changes
    throughout the day, so the on-disk cache has a freshness override: a
    cache file older than 6 hours is treated as a miss and refetched.

    `force_refresh` bypasses that freshness check entirely and always
    refetches live, regardless of how fresh the cache file is. This
    exists for the deadline-morning rerun (`fpl_oracle.deadline`):
    injury/availability flags move in the final hours before a deadline,
    and serving a cache up to 6 hours old is exactly wrong at that
    moment — a squad could ship containing a player ruled out that
    morning. Every other caller leaves this at the default, and default
    behaviour (including what's on disk afterwards) is unchanged from
    before this flag existed.
    """
    cache_path = _bootstrap_cache_path()
    cache_is_fresh = (
        not force_refresh
        and cache_path.exists()
        and (time.time() - cache_path.stat().st_mtime) < _BOOTSTRAP_CACHE_MAX_AGE_SECONDS
    )
    if cache_is_fresh:
        return json.loads(cache_path.read_text())

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = _fetch_bootstrap_with_retries()
    cache_path.write_text(json.dumps(data, indent=2))
    return data


def get_entry(entry_id: int) -> EntrySummary | None:
    """Fetch a manager's public profile. Returns None on 404 (bad ID)."""
    data = _get_json(f"{API_BASE}/entry/{entry_id}/", _cache_path("entry", entry_id))
    if data is None:
        return None
    return EntrySummary(
        entry_id=entry_id,
        team_name=data["name"],
        player_first_name=data["player_first_name"],
        player_last_name=data["player_last_name"],
        summary_overall_rank=data.get("summary_overall_rank"),
    )


def get_entry_history(entry_id: int) -> EntryHistory | None:
    data = _get_json(f"{API_BASE}/entry/{entry_id}/history/", _cache_path("history", entry_id))
    if data is None:
        return None
    return EntryHistory(
        entry_id=entry_id,
        past=[
            PastSeason(season_name=p["season_name"], rank=p.get("rank"))
            for p in data.get("past", [])
        ],
    )
