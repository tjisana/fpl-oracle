"""Unit tests for fpl_oracle.ingest.youtube_client: error handling
(specifically that the YouTube API key never leaks into an exception
message when a request fails — CLAUDE.md: never print secrets) and the
pure ISO-8601 duration parser used by the min-duration Shorts filter."""

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


class TestParseIso8601Duration:
    def test_minutes_and_seconds(self) -> None:
        assert youtube_client.parse_iso8601_duration("PT1M30S") == 90

    def test_hours_minutes_seconds(self) -> None:
        assert youtube_client.parse_iso8601_duration("PT1H2M3S") == 3723

    def test_seconds_only(self) -> None:
        assert youtube_client.parse_iso8601_duration("PT45S") == 45

    def test_hours_only(self) -> None:
        assert youtube_client.parse_iso8601_duration("PT2H") == 7200

    def test_minutes_only(self) -> None:
        assert youtube_client.parse_iso8601_duration("PT10M") == 600

    def test_days_component(self) -> None:
        assert youtube_client.parse_iso8601_duration("P1DT2H") == 86400 + 7200

    def test_zero_duration(self) -> None:
        assert youtube_client.parse_iso8601_duration("PT0S") == 0

    def test_unrecognized_format_raises(self) -> None:
        with pytest.raises(ValueError):
            youtube_client.parse_iso8601_duration("not-a-duration")
