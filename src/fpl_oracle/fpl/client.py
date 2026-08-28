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

# `entry/{id}/` and `entry/{id}/history/` were cached forever on the
# reasoning that a manager's history doesn't rewrite itself. That was true
# when only `started_event` and past seasons were read from them. They now
# also carry `summary_overall_points`, `summary_overall_rank` and the whole
# league-standings block — all of which move every gameweek. One hour keeps
# a deadline-morning run cheap while guaranteeing the standings are this
# gameweek's.
_ENTRY_CACHE_MAX_AGE_SECONDS = 60 * 60


class LeagueStanding(BaseModel):
    """One entry of `entry/{id}/`'s `leagues.classic[]` or `leagues.h2h[]`
    array: the manager's standing in one league. `league_type` is the raw
    API code — "s" for a system/global league (the "Overall" and
    per-gameweek pseudo-leagues every manager is auto-enrolled in — noise
    for this purpose) and "x" for a private league someone actually
    created and the owner cares about; see `EntrySummary.private_leagues`.
    `entry_rank`/`entry_last_rank` are `None` before a gameweek has been
    scored (pre-season, or a brand-new league) — never fabricated."""

    league_id: int
    name: str
    league_type: str
    entry_rank: int | None
    entry_last_rank: int | None
    rank_count: int | None

    @property
    def is_private(self) -> bool:
        return self.league_type == "x"


def _parse_league_standings(entries: list[dict]) -> list[LeagueStanding]:
    return [
        LeagueStanding(
            league_id=e["id"],
            name=e["name"],
            league_type=e.get("league_type", ""),
            entry_rank=e.get("entry_rank"),
            entry_last_rank=e.get("entry_last_rank"),
            rank_count=e.get("rank_count"),
        )
        for e in entries
    ]


class EntrySummary(BaseModel):
    entry_id: int
    team_name: str
    player_first_name: str
    player_last_name: str
    summary_overall_rank: int | None
    # The season points total. `None` pre-season / before any gameweek has
    # been scored for this entry — same defensive posture as
    # `summary_overall_rank`, never fabricated as 0.
    summary_overall_points: int | None
    started_event: int
    # Team value and bank as of the manager's most recent deadline, in
    # tenths of a million (matching `now_cost`'s convention) — `None`
    # pre-season, before any deadline has passed for this entry. Despite
    # the name, `last_deadline_value` is the TOTAL squad value (15 players'
    # current selling-adjusted worth), not a per-player figure.
    last_deadline_bank: int | None
    last_deadline_value: int | None
    # Every league this entry is a member of, split the same way the API
    # splits them. Defaults to `[]` so older cached `entry_*.json` files
    # (fetched before this field existed) still parse.
    leagues_classic: list[LeagueStanding] = []
    leagues_h2h: list[LeagueStanding] = []

    @property
    def bank_m(self) -> float | None:
        return None if self.last_deadline_bank is None else self.last_deadline_bank / 10

    @property
    def team_value_m(self) -> float | None:
        return None if self.last_deadline_value is None else self.last_deadline_value / 10

    @property
    def private_leagues(self) -> list[LeagueStanding]:
        """Only the PRIVATE leagues (`league_type == "x"`) — the ones the
        owner actually cares about. Excludes the global "Overall" /
        per-gameweek pseudo-leagues (`league_type == "s"`) every manager
        is auto-enrolled in, which are noise for this purpose."""
        return [lg for lg in (*self.leagues_classic, *self.leagues_h2h) if lg.is_private]


class PastSeason(BaseModel):
    season_name: str
    rank: int | None


class GameweekHistory(BaseModel):
    """One entry of `entry/{id}/history/`'s `current` array: this
    season's per-gameweek summary, including the transfer bookkeeping
    (`event_transfers`, `event_transfers_cost`) that `fpl.entry`'s
    free-transfer derivation replays."""

    event: int
    points: int
    total_points: int
    rank: int | None
    overall_rank: int | None
    bank: int
    value: int
    event_transfers: int
    event_transfers_cost: int
    points_on_bench: int


class ChipPlay(BaseModel):
    """One entry of `entry/{id}/history/`'s `chips` array: a chip the
    manager played this season and which gameweek it was played in.
    `name` is the API's raw lowercase chip id ("wildcard", "freehit",
    "bboost", "3xc"), not a display label."""

    name: str
    event: int


class EntryHistory(BaseModel):
    entry_id: int
    past: list[PastSeason]
    # `current` and `chips` default to `[]` for older cached payloads (and
    # test fixtures) that predate this module — see fpl.entry, the only
    # consumer that needs them. Existing callers reading only `.past` are
    # unaffected.
    current: list[GameweekHistory] = []
    chips: list[ChipPlay] = []


def _cache_path(kind: str, entry_id: int) -> Path:
    return CACHE_DIR / f"{kind}_{entry_id}.json"


_NOT_FOUND = {"not_found": True}


def _get_json(url: str, cache_path: Path, max_age_seconds: float | None = None) -> dict | None:
    """Returns None for a cached-or-live 404 (bad ID), the JSON body otherwise.

    `max_age_seconds=None` caches forever, which is right for genuinely
    immutable payloads. Anything carrying a rank, a points total, or a
    league position MUST pass a max age: those change every gameweek, and
    a permanent cache turns them into last-week's numbers presented with
    no staleness marker — the silent-wrong-answer failure this project
    treats as the worst outcome available."""
    if cache_path.exists():
        fresh = (
            max_age_seconds is None or (time.time() - cache_path.stat().st_mtime) < max_age_seconds
        )
        if fresh:
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


def get_next_gameweek() -> int | None:
    """The gameweek whose deadline is next, per `bootstrap-static`'s
    `events` array — i.e. the one a squad is being picked FOR.

    NOTE THE ROLLOVER: `is_next` flips to GW(N+1) the instant GW N's
    deadline passes. A run started after the deadline therefore targets
    the following gameweek, which is correct for planning and wrong for
    "what did I just submit". Callers that mean a specific gameweek should
    pass it explicitly rather than rely on this."""
    data = get_bootstrap_static()
    for event in data.get("events", []):
        if event.get("is_next"):
            return event["id"]
    return None


def get_current_completed_gameweek() -> int | None:
    """The most recently FINISHED gameweek, per `bootstrap-static`'s
    `events` array (each event carries `id`, `finished`, `is_current`,
    `is_next`). `finished=True` means every fixture in that gameweek has
    been played and its points are final — this is deliberately the more
    conservative "completed" reading (as opposed to `is_current`, which
    can point at a gameweek whose matches are still being played), since
    `fpl.entry`'s my-squad fetch is meant to record a settled squad, not
    one mid-gameweek. Returns `None` before any gameweek has finished
    (pre-season, or partway through GW1)."""
    data = get_bootstrap_static()
    finished_ids = [e["id"] for e in data.get("events", []) if e.get("finished")]
    return max(finished_ids) if finished_ids else None


def get_entry(entry_id: int) -> EntrySummary | None:
    """Fetch a manager's public profile. Returns None on 404 (bad ID)."""
    data = _get_json(
        f"{API_BASE}/entry/{entry_id}/",
        _cache_path("entry", entry_id),
        max_age_seconds=_ENTRY_CACHE_MAX_AGE_SECONDS,
    )
    if data is None:
        return None
    leagues = data.get("leagues") or {}
    return EntrySummary(
        entry_id=entry_id,
        team_name=data["name"],
        player_first_name=data["player_first_name"],
        player_last_name=data["player_last_name"],
        summary_overall_rank=data.get("summary_overall_rank"),
        summary_overall_points=data.get("summary_overall_points"),
        started_event=data.get("started_event", 1),
        last_deadline_bank=data.get("last_deadline_bank"),
        last_deadline_value=data.get("last_deadline_value"),
        leagues_classic=_parse_league_standings(leagues.get("classic", [])),
        leagues_h2h=_parse_league_standings(leagues.get("h2h", [])),
    )


def get_entry_history(entry_id: int) -> EntryHistory | None:
    data = _get_json(
        f"{API_BASE}/entry/{entry_id}/history/",
        _cache_path("history", entry_id),
        max_age_seconds=_ENTRY_CACHE_MAX_AGE_SECONDS,
    )
    if data is None:
        return None
    return EntryHistory(
        entry_id=entry_id,
        past=[
            PastSeason(season_name=p["season_name"], rank=p.get("rank"))
            for p in data.get("past", [])
        ],
        current=[GameweekHistory.model_validate(gw) for gw in data.get("current", [])],
        chips=[ChipPlay.model_validate(c) for c in data.get("chips", [])],
    )


class PickEntry(BaseModel):
    """One of the 15 elements in a picks payload's `picks` array.
    `position` is the SQUAD ORDER (1-11 starting XI in formation order,
    12-15 bench order) — it is NOT the player's pitch position
    (GK/DEF/MID/FWD); that comes from `fpl.players.Player.position` via
    the `element` id. `multiplier` is 0 for a benched player, 1 for a
    normal starter, 2 for the captain, 3 for the captain under Triple
    Captain."""

    element: int
    position: int
    multiplier: int
    is_captain: bool
    is_vice_captain: bool


class EntryHistoryEvent(BaseModel):
    """The `entry_history` object nested in a picks payload: this
    specific gameweek's points/rank/bank/value/transfer snapshot, as of
    THAT gameweek's deadline — not necessarily the manager's latest.
    Preferred over `EntrySummary.last_deadline_bank/value` when building
    a squad for a particular gameweek, since it's exact for that gw."""

    event: int
    points: int
    total_points: int
    rank: int | None
    overall_rank: int | None
    bank: int
    value: int
    event_transfers: int
    event_transfers_cost: int
    points_on_bench: int


class PicksResponse(BaseModel):
    active_chip: str | None
    entry_history: EntryHistoryEvent
    picks: list[PickEntry]


def _picks_cache_path(entry_id: int, gameweek: int) -> Path:
    return CACHE_DIR / f"picks_{entry_id}_gw{gameweek}.json"


def get_picks(entry_id: int, gameweek: int) -> PicksResponse | None:
    """Fetch a manager's 15 picks for `gameweek`: starting/bench split,
    captain/vice, and `multiplier`.

    Returns None when the picks aren't public yet — this endpoint 404s
    (or otherwise 4xx's) for any gameweek whose deadline hasn't passed;
    the data appears in place the moment it does, without the URL
    changing. That is a genuinely different kind of "not found" than
    `get_entry`'s (a permanently bad id), so unlike `_get_json`, a miss
    here is NEVER written to disk as a cached negative — doing so would
    permanently poison every pre-deadline check into thinking the
    gameweek can never have picks, including the deadline-morning rerun
    this data exists to serve. A HIT is still cached indefinitely: once a
    gameweek's picks go public they are final and never change.
    5xx and non-HTTP errors are NOT treated as "not available yet" — those
    are real failures and propagate, same as every other client function
    here.
    """
    cache_path = _picks_cache_path(entry_id, gameweek)
    if cache_path.exists():
        return PicksResponse.model_validate(json.loads(cache_path.read_text()))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = httpx.get(
            f"{API_BASE}/entry/{entry_id}/event/{gameweek}/picks/", headers=HEADERS, timeout=15
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if 400 <= e.response.status_code < 500:
            return None
        raise
    data = resp.json()
    cache_path.write_text(json.dumps(data, indent=2))
    return PicksResponse.model_validate(data)


class TransferRecord(BaseModel):
    element_in: int
    element_in_cost: int
    element_out: int
    element_out_cost: int
    entry: int
    event: int
    time: str


# `entry/{id}/transfers/` grows every time the owner acts, unlike
# entry/history (immutable once fetched) — so, like bootstrap-static, its
# cache has a freshness override rather than caching forever. Transfers
# happen at most a few times a week (never within a single session the
# way prices/injury flags move), so an hour-old list is never stale enough
# to misinform a free-transfer derivation in practice.
_TRANSFERS_CACHE_MAX_AGE_SECONDS = 60 * 60


def get_transfers(entry_id: int, force_refresh: bool = False) -> list[TransferRecord]:
    """Fetch every transfer `entry_id` has made this season (oldest
    first, per the API). `force_refresh` bypasses the freshness check,
    same convention as `get_bootstrap_static`."""
    cache_path = _cache_path("transfers", entry_id)
    cache_is_fresh = (
        not force_refresh
        and cache_path.exists()
        and (time.time() - cache_path.stat().st_mtime) < _TRANSFERS_CACHE_MAX_AGE_SECONDS
    )
    if cache_is_fresh:
        data = json.loads(cache_path.read_text())
    else:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        resp = httpx.get(f"{API_BASE}/entry/{entry_id}/transfers/", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        cache_path.write_text(json.dumps(data, indent=2))
    return [TransferRecord.model_validate(t) for t in data]
