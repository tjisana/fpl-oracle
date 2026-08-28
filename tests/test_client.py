"""Tests for fpl_oracle.fpl.client: the bootstrap-static 6h cache
freshness override, and confirmation that it doesn't leak into the
entry-endpoint caches (those are meant to be cached indefinitely)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from fpl_oracle.fpl import client


class _FakeResponse:
    """Minimal stand-in for an httpx.Response — enough for the
    read-through cache callers in client.py."""

    def __init__(self, json_data: dict) -> None:
        self._json_data = json_data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._json_data


_CANNED_BOOTSTRAP = {"teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}], "elements": []}
_CANNED_ENTRY = {
    "name": "Test Team",
    "player_first_name": "Test",
    "player_last_name": "Manager",
    "summary_overall_rank": 12345,
}


class TestBootstrapStaticCacheFreshness:
    def test_stale_cache_triggers_refetch(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = tmp_path / "bootstrap_static.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"teams": [], "elements": [], "stale": True}))
        stale_mtime = time.time() - 7 * 60 * 60  # 7h old, past the 6h freshness window
        os.utime(cache_path, (stale_mtime, stale_mtime))

        calls: list[str] = []

        def fake_get(url: str, **kwargs) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse(_CANNED_BOOTSTRAP)

        monkeypatch.setattr(client.httpx, "get", fake_get)

        data = client.get_bootstrap_static()

        assert len(calls) == 1
        assert "bootstrap-static" in calls[0]
        assert data == _CANNED_BOOTSTRAP
        # the refetched payload overwrote the stale cache on disk
        assert json.loads(cache_path.read_text()) == _CANNED_BOOTSTRAP

    def test_fresh_cache_skips_network(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = tmp_path / "bootstrap_static.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(_CANNED_BOOTSTRAP))
        # Leave mtime as "now" — well within the 6h freshness window.

        def fail_get(*args, **kwargs):
            raise AssertionError("httpx.get should not be called when the cache is fresh")

        monkeypatch.setattr(client.httpx, "get", fail_get)

        data = client.get_bootstrap_static()

        assert data == _CANNED_BOOTSTRAP

    def test_missing_cache_fetches(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)

        calls: list[str] = []

        def fake_get(url: str, **kwargs) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse(_CANNED_BOOTSTRAP)

        monkeypatch.setattr(client.httpx, "get", fake_get)

        data = client.get_bootstrap_static()

        assert len(calls) == 1
        assert data == _CANNED_BOOTSTRAP


def _fake_get(calls: list[str], payload: dict):
    """httpx.get stand-in that records the URL and returns `payload`."""

    def fake_get(url: str, **kwargs) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(payload)

    return fake_get


class TestEntryCacheExpires:
    """These caches used to be pinned as never-expiring, and that was
    correct at the time: only `started_event` and past-season ranks were
    read from them, and neither rewrites itself.

    That premise died when league standings, `summary_overall_rank` and
    `summary_overall_points` were added to `EntrySummary` — all of which
    move every gameweek. A permanent cache then means a GW5 run reports
    GW1's rank and GW1's mini-league positions with nothing marking them
    stale. The old invariant is deliberately inverted here rather than
    deleted, so the reason is on the record.
    """

    def _ancient(self, path: Path) -> None:
        ancient = time.time() - 365 * 24 * 60 * 60
        os.utime(path, (ancient, ancient))

    def test_stale_entry_cache_is_refetched(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = client._cache_path("entry", 12345)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({**_CANNED_ENTRY, "name": "Stale Name"}))
        self._ancient(cache_path)

        calls: list[str] = []
        monkeypatch.setattr(client.httpx, "get", _fake_get(calls, _CANNED_ENTRY))

        entry = client.get_entry(12345)

        assert calls, "a year-old entry cache must be refetched, not served"
        assert entry is not None
        assert entry.team_name == "Test Team"

    def test_fresh_entry_cache_is_served_without_network(self, tmp_path: Path, monkeypatch) -> None:
        """The point is freshness, not defeating the cache: a deadline
        morning must not refetch on every call."""
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = client._cache_path("entry", 12345)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(_CANNED_ENTRY))
        # mtime is "now" — well inside the freshness window.

        def fail_get(*args, **kwargs):
            raise AssertionError("a fresh entry cache must not hit the network")

        monkeypatch.setattr(client.httpx, "get", fail_get)

        assert client.get_entry(12345) is not None

    def test_stale_history_cache_is_refetched(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = client._cache_path("history", 12345)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"past": []}))
        self._ancient(cache_path)

        payload = {"past": [{"season_name": "2023/24", "rank": 100}], "current": [], "chips": []}
        calls: list[str] = []
        monkeypatch.setattr(client.httpx, "get", _fake_get(calls, payload))

        history = client.get_entry_history(12345)

        assert calls, "a year-old history cache must be refetched"
        assert history is not None
        assert len(history.past) == 1


class TestEntryLeaguesAndPoints:
    """Job 1: `summary_overall_points` and the `leagues.classic[]` /
    `leagues.h2h[]` standings, parsed onto `EntrySummary`, plus the
    `private_leagues` convenience property (league_type == 'x' only —
    the global 's' pseudo-leagues are noise)."""

    def _entry_payload(self, **overrides: object) -> dict:
        base: dict = {
            "name": "Test Team",
            "player_first_name": "Test",
            "player_last_name": "Manager",
            "summary_overall_rank": 12345,
            "summary_overall_points": 180,
            "leagues": {
                "classic": [
                    {
                        "id": 1,
                        "name": "Overall",
                        "league_type": "s",
                        "entry_rank": 500000,
                        "entry_last_rank": 510000,
                        "rank_count": None,
                    },
                    {
                        "id": 2,
                        "name": "Rainmakers",
                        "league_type": "x",
                        "entry_rank": 2,
                        "entry_last_rank": 2,
                        "rank_count": 2,
                    },
                ],
                "h2h": [
                    {
                        "id": 3,
                        "name": "H2H League",
                        "league_type": "x",
                        "entry_rank": None,
                        "entry_last_rank": None,
                        "rank_count": 10,
                    },
                ],
            },
        }
        base.update(overrides)
        return base

    def test_parses_points_and_leagues(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            client.httpx, "get", lambda url, **kw: _FakeResponse(self._entry_payload())
        )

        entry = client.get_entry(42)

        assert entry is not None
        assert entry.summary_overall_points == 180
        assert len(entry.leagues_classic) == 2
        assert len(entry.leagues_h2h) == 1
        assert entry.leagues_classic[1].name == "Rainmakers"
        assert entry.leagues_classic[1].league_type == "x"
        assert entry.leagues_classic[1].is_private is True

    def test_private_leagues_excludes_system_leagues(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            client.httpx, "get", lambda url, **kw: _FakeResponse(self._entry_payload())
        )

        entry = client.get_entry(42)

        assert entry is not None
        names = {lg.name for lg in entry.private_leagues}
        assert names == {"Rainmakers", "H2H League"}
        assert "Overall" not in names

    def test_null_entry_rank_before_a_gameweek_is_scored(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            client.httpx, "get", lambda url, **kw: _FakeResponse(self._entry_payload())
        )

        entry = client.get_entry(42)

        assert entry is not None
        h2h_league = entry.leagues_h2h[0]
        assert h2h_league.entry_rank is None
        assert h2h_league.entry_last_rank is None
        assert h2h_league.rank_count == 10

    def test_missing_summary_overall_points_is_none_not_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        payload = self._entry_payload()
        del payload["summary_overall_points"]
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(payload))

        entry = client.get_entry(42)

        assert entry is not None
        assert entry.summary_overall_points is None

    def test_empty_leagues_block_parses_cleanly(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        payload = self._entry_payload(leagues={})
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(payload))

        entry = client.get_entry(42)

        assert entry is not None
        assert entry.leagues_classic == []
        assert entry.leagues_h2h == []
        assert entry.private_leagues == []

    def test_missing_leagues_key_entirely_parses_cleanly(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        payload = self._entry_payload()
        del payload["leagues"]
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(payload))

        entry = client.get_entry(42)

        assert entry is not None
        assert entry.leagues_classic == []
        assert entry.private_leagues == []


class TestCurrentCompletedGameweek:
    def test_returns_the_highest_finished_event_id(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        payload = {
            "teams": [],
            "elements": [],
            "events": [
                {"id": 1, "finished": True, "is_current": False, "is_next": False},
                {"id": 2, "finished": True, "is_current": True, "is_next": False},
                {"id": 3, "finished": False, "is_current": False, "is_next": True},
            ],
        }
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(payload))

        assert client.get_current_completed_gameweek() == 2

    def test_no_finished_gameweeks_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        payload = {
            "teams": [],
            "elements": [],
            "events": [
                {"id": 1, "finished": False, "is_current": True, "is_next": False},
            ],
        }
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(payload))

        assert client.get_current_completed_gameweek() is None

    def test_missing_events_key_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        payload = {"teams": [], "elements": []}
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(payload))

        assert client.get_current_completed_gameweek() is None


@pytest.mark.network
class TestBootstrapStaticLive:
    def test_live_fetch_parses(self) -> None:
        data = client.get_bootstrap_static()
        assert "elements" in data
        assert "teams" in data
        assert len(data["elements"]) > 400
