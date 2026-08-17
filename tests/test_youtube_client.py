"""Unit tests for fpl_oracle.ingest.youtube_client's error handling —
specifically that the YouTube API key never leaks into an exception
message when a request fails (CLAUDE.md: never print secrets)."""

from __future__ import annotations

import httpx
import pytest

from fpl_oracle.ingest import youtube_client


class TestKeyRedaction:
    def test_key_redacted_from_url(self) -> None:
        url = "https://www.googleapis.com/youtube/v3/channels?id=abc&key=SECRETVALUE"
        assert "SECRETVALUE" not in youtube_client._redact_key(url)
        assert "key=REDACTED" in youtube_client._redact_key(url)

    def test_key_redacted_when_key_is_the_first_param(self) -> None:
        url = "https://www.googleapis.com/youtube/v3/channels?key=SECRETVALUE&id=abc"
        assert "SECRETVALUE" not in youtube_client._redact_key(url)

    def test_non_key_params_untouched(self) -> None:
        url = "https://www.googleapis.com/youtube/v3/channels?id=abc&key=SECRETVALUE"
        redacted = youtube_client._redact_key(url)
        assert "id=abc" in redacted


class TestGetRaisesRedactedError:
    def test_http_error_does_not_leak_key_but_keeps_status_code(self, monkeypatch) -> None:
        url = "https://www.googleapis.com/youtube/v3/channels?id=abc&key=SECRETVALUE"
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request, json={"error": {"message": "Forbidden"}})

        def fake_get(url: str, **kwargs) -> httpx.Response:
            return response

        monkeypatch.setattr(youtube_client.httpx, "get", fake_get)

        with pytest.raises(youtube_client.YouTubeApiError) as exc_info:
            youtube_client._get(url, params={"id": "abc", "key": "SECRETVALUE"})

        message = str(exc_info.value)
        assert "SECRETVALUE" not in message
        assert "403" in message
        # Endpoint should still be visible for debugging.
        assert "channels" in message

    def test_success_response_returns_normally(self, monkeypatch) -> None:
        url = "https://www.googleapis.com/youtube/v3/channels?id=abc&key=SECRETVALUE"
        request = httpx.Request("GET", url)
        response = httpx.Response(200, request=request, json={"items": []})

        def fake_get(url: str, **kwargs) -> httpx.Response:
            return response

        monkeypatch.setattr(youtube_client.httpx, "get", fake_get)

        result = youtube_client._get(url, params={"id": "abc", "key": "SECRETVALUE"})
        assert result.json() == {"items": []}
