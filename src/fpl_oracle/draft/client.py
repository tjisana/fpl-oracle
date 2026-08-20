"""Thin client over the official FPL Draft API.

Separate host and separate payload from the classic API in `fpl/client.py`:
`draft.premierleague.com/api/bootstrap-static` carries a `draft_rank` per
player (FPL's own forward-looking draft board) and, in preseason, a
`total_points` that is still LAST season's final total. Both are load-bearing
for the board in `draft/board.py`.

Cached on disk like every other network call in this project.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

CACHE_DIR = Path("data/cache/draft")
API_BASE = "https://draft.premierleague.com/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Injury flags and news move during the day, and on draft day that matters
# more than usual — keep this shorter than the classic client's 6h.
_BOOTSTRAP_CACHE_MAX_AGE_SECONDS = 60 * 60


def _fresh(path: Path, max_age: int) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age


def fetch_bootstrap(*, refresh: bool = False) -> dict:
    """The draft bootstrap: elements, teams, element_types, settings.

    `refresh=True` forces a refetch — use it right before a draft so injury
    news is current.
    """
    cache = CACHE_DIR / "bootstrap-static.json"
    if not refresh and _fresh(cache, _BOOTSTRAP_CACHE_MAX_AGE_SECONDS):
        return json.loads(cache.read_text())

    resp = httpx.get(f"{API_BASE}/bootstrap-static", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(resp.text)
    return resp.json()


def fetch_game() -> dict:
    """Season state: `current_event` is None before the season starts.

    Worth checking, because the meaning of `total_points` in the bootstrap
    depends on it — preseason it is last season's total, in-season it is this
    season's running tally, which would silently break the projection.
    """
    resp = httpx.get(f"{API_BASE}/game", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_element_status(league_id: int) -> dict:
    """Live per-player ownership state: draft/{league}/choices.

    Transaction-aware — reflects trades and waiver claims made after the
    original draft, unlike `choices` in the same payload (which is only ever
    the original draft-day pick order). Deliberately NOT cached: a waiver
    decision needs today's ownership truth, not up-to-an-hour-old truth (see
    the same choice in draft/serve.py's `_upstream`).
    """
    resp = httpx.get(f"{API_BASE}/draft/{league_id}/choices", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_league_entries(league_id: int) -> dict:
    """Managers in the league: league/{league_id}/details. Also not cached, same reasoning."""
    resp = httpx.get(f"{API_BASE}/league/{league_id}/details", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()
