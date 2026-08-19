"""Thin client over the public FPL API.

All FPL network calls go through here, with on-disk caching in
`data/cache/fpl/` and a browser-like User-Agent — the API 403s generic
UAs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

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


def _bootstrap_cache_path() -> Path:
    return CACHE_DIR / "bootstrap_static.json"


def get_bootstrap_static() -> dict:
    """Fetch the `bootstrap-static/` payload: all players, teams,
    gameweeks, current prices/positions/ownership.

    Unlike the entry endpoints (`_get_json`, cached indefinitely — a
    manager's history doesn't rewrite itself), this payload changes
    throughout the day, so the on-disk cache has a freshness override: a
    cache file older than 6 hours is treated as a miss and refetched.
    """
    cache_path = _bootstrap_cache_path()
    cache_is_fresh = (
        cache_path.exists()
        and (time.time() - cache_path.stat().st_mtime) < _BOOTSTRAP_CACHE_MAX_AGE_SECONDS
    )
    if cache_is_fresh:
        return json.loads(cache_path.read_text())

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(f"{API_BASE}/bootstrap-static/", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
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
