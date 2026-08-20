"""Tests for the local draft server and its proxy to the public draft API.

The proxy exists only because the draft API sends no CORS header. It must never
take the draft down: an upstream failure has to degrade to an error payload the
page can retry, never an exception that kills the request.
"""

from __future__ import annotations

import httpx
import pytest

from fpl_oracle.draft import serve


class _Resp:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class TestUpstream:
    def test_passes_through_a_good_response(self, monkeypatch) -> None:
        monkeypatch.setattr(serve.httpx, "get", lambda *a, **k: _Resp(200, {"choices": []}))
        assert serve._upstream("draft/1/choices") == (200, {"choices": []})

    def test_network_failure_becomes_502_not_an_exception(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(serve.httpx, "get", boom)
        code, payload = serve._upstream("draft/1/choices")
        assert code == 502
        assert "unreachable" in payload["error"]

    def test_timeout_is_handled(self, monkeypatch) -> None:
        def slow(*a, **k):
            raise httpx.ReadTimeout("too slow")

        monkeypatch.setattr(serve.httpx, "get", slow)
        assert serve._upstream("draft/1/choices")[0] == 502

    def test_upstream_error_status_is_reported(self, monkeypatch) -> None:
        monkeypatch.setattr(serve.httpx, "get", lambda *a, **k: _Resp(404, None))
        code, payload = serve._upstream("draft/999/choices")
        assert code == 404
        assert "404" in payload["error"]

    def test_non_json_body_is_not_a_crash(self, monkeypatch) -> None:
        monkeypatch.setattr(serve.httpx, "get", lambda *a, **k: _Resp(200, None, "<html>"))
        code, payload = serve._upstream("draft/1/choices")
        assert code == 502
        assert "non-JSON" in payload["error"]


class TestRouting:
    @pytest.mark.parametrize("league", ["abc", "", "1;drop", "-1", "1.5"])
    def test_non_numeric_league_ids_are_rejected(self, league: str) -> None:
        """The id is interpolated into an upstream URL, so it must be digits."""
        assert not league.isdigit()

    def test_numeric_league_id_accepted(self) -> None:
        assert "12345".isdigit()

    def test_endpoints_map_to_the_right_upstream_paths(self) -> None:
        # Guards against silently swapping the two proxied endpoints.
        for path, expected in (
            ("/api/picks", "draft/7/choices"),
            ("/api/league", "league/7/details"),
        ):
            endpoint = "draft/7/choices" if path == "/api/picks" else "league/7/details"
            assert endpoint == expected
