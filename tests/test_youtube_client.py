"""Unit tests for fpl_oracle.ingest.youtube_client: error handling
(specifically that the YouTube API key never leaks into an exception
message when a request fails — CLAUDE.md: never print secrets), the pure
ISO-8601 duration parser used by the min-duration Shorts filter, the
case-preserving cache key used for per-video durations, and
`list_uploads`'s early-stop paging against a `stop_before` cutoff."""

from __future__ import annotations

from datetime import UTC, datetime

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

    def test_p0d_live_upcoming_marker_parses_to_zero(self) -> None:
        # YouTube reports "P0D" for a live/upcoming broadcast that
        # hasn't finished airing — 0s is correct, not a parse failure,
        # and it's desirable: it falls below MIN_DURATION_S, so the
        # Shorts filter drops it too (no transcript exists yet either).
        assert youtube_client.parse_iso8601_duration("P0D") == 0


class TestDurationCacheKeyCasePreserved:
    def test_case_distinct_video_ids_get_different_cache_paths(self) -> None:
        # Regression: YouTube video ids are case-sensitive. The plain
        # `_cache_path` lowercases via `_slug`, which would let
        # "dQw4w9WgXcQ" and "dqw4w9wgxcq" collide onto the same cache
        # file and serve each other's duration. The duration cache must
        # use the case-preserving path instead.
        path_a = youtube_client._cache_path_preserving_case("duration", "dQw4w9WgXcQ")
        path_b = youtube_client._cache_path_preserving_case("duration", "dqw4w9wgxcq")
        assert path_a != path_b

    def test_plain_cache_path_would_have_collided(self) -> None:
        # Documents the bug this fixes: the case-insensitive `_cache_path`
        # used for other (non-duration) caches DOES collide these ids —
        # that's exactly why durations need the preserving variant.
        assert youtube_client._cache_path("duration", "dQw4w9WgXcQ") == youtube_client._cache_path(
            "duration", "dqw4w9wgxcq"
        )


def _playlist_item(video_id: str, published_at: str) -> dict:
    return {
        "snippet": {
            "publishedAt": published_at,
            "title": f"Video {video_id}",
            "description": "",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {
            "videoId": video_id,
            "videoPublishedAt": published_at,
        },
    }


class _FakeJsonResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class TestListUploadsStopBeforeCutoff:
    def test_stops_paging_once_a_page_predates_the_cutoff(self, monkeypatch, tmp_path) -> None:
        channel_id = "UCsomechannel"
        monkeypatch.setattr(youtube_client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            youtube_client, "_get_uploads_playlist_id", lambda channel_id: "PLuploads"
        )

        # Page 1 already contains an item older than the cutoff, so
        # list_uploads should stop right there and never request page 2.
        page_1 = {
            "items": [
                _playlist_item("v_new1", "2026-06-01T00:00:00Z"),
                _playlist_item("v_new2", "2026-04-20T00:00:00Z"),  # predates cutoff
            ],
            "nextPageToken": "page2token",
        }
        page_2 = {"items": [_playlist_item("v_old1", "2025-01-01T00:00:00Z")]}

        requested_page_tokens: list[str | None] = []

        def fake_get(url: str, params: dict) -> _FakeJsonResponse:
            requested_page_tokens.append(params.get("pageToken"))
            if params.get("pageToken") is None:
                return _FakeJsonResponse(page_1)
            return _FakeJsonResponse(page_2)

        monkeypatch.setattr(youtube_client, "_get", fake_get)

        cutoff = datetime(2026, 5, 1, tzinfo=UTC)
        videos = youtube_client.list_uploads(channel_id, max_results=50, stop_before=cutoff)

        # Only page 1 was ever requested — pagination stopped as soon as
        # it saw an item predating the cutoff, never following
        # nextPageToken into page 2.
        assert requested_page_tokens == [None]
        assert {v.video_id for v in videos} == {"v_new1", "v_new2"}
        assert "v_old1" not in {v.video_id for v in videos}

    def test_no_cutoff_pages_all_the_way_through(self, monkeypatch, tmp_path) -> None:
        channel_id = "UCsomechannel2"
        monkeypatch.setattr(youtube_client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            youtube_client, "_get_uploads_playlist_id", lambda channel_id: "PLuploads"
        )

        page_1 = {
            "items": [_playlist_item("v1", "2026-06-01T00:00:00Z")],
            "nextPageToken": "page2token",
        }
        page_2 = {"items": [_playlist_item("v2", "2020-01-01T00:00:00Z")]}

        requested_page_tokens: list[str | None] = []

        def fake_get(url: str, params: dict) -> _FakeJsonResponse:
            requested_page_tokens.append(params.get("pageToken"))
            if params.get("pageToken") is None:
                return _FakeJsonResponse(page_1)
            return _FakeJsonResponse(page_2)

        monkeypatch.setattr(youtube_client, "_get", fake_get)

        videos = youtube_client.list_uploads(channel_id, max_results=50, stop_before=None)

        assert requested_page_tokens == [None, "page2token"]
        assert {v.video_id for v in videos} == {"v1", "v2"}
