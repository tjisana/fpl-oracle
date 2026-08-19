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


class TestEntryCacheNeverExpires:
    """The bootstrap-static freshness override is specific to that one
    function — entry/history caches (via `_get_json`) must keep caching
    indefinitely, no matter how old the file on disk is."""

    def test_ancient_entry_cache_is_not_refetched(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = client._cache_path("entry", 12345)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(_CANNED_ENTRY))
        ancient_mtime = time.time() - 365 * 24 * 60 * 60  # a year old
        os.utime(cache_path, (ancient_mtime, ancient_mtime))

        def fail_get(*args, **kwargs):
            raise AssertionError("httpx.get should not be called — entry caches never expire")

        monkeypatch.setattr(client.httpx, "get", fail_get)

        entry = client.get_entry(12345)

        assert entry is not None
        assert entry.team_name == "Test Team"

    def test_ancient_history_cache_is_not_refetched(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        cache_path = client._cache_path("history", 12345)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"past": [{"season_name": "2023/24", "rank": 100}]}))
        ancient_mtime = time.time() - 365 * 24 * 60 * 60
        os.utime(cache_path, (ancient_mtime, ancient_mtime))

        def fail_get(*args, **kwargs):
            raise AssertionError("httpx.get should not be called — history caches never expire")

        monkeypatch.setattr(client.httpx, "get", fail_get)

        history = client.get_entry_history(12345)

        assert history is not None
        assert len(history.past) == 1


@pytest.mark.network
class TestBootstrapStaticLive:
    def test_live_fetch_parses(self) -> None:
        data = client.get_bootstrap_static()
        assert "elements" in data
        assert "teams" in data
        assert len(data["elements"]) > 400
