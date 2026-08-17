"""Tests for fpl_oracle.ingest.transcripts and the video-listing parsing
helper in fpl_oracle.ingest.youtube_client."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from fpl_oracle.ingest import youtube_client
from fpl_oracle.ingest.transcripts import (
    Transcript,
    TranscriptSegment,
    fetch_transcript,
    load_transcript,
    save_transcript,
)
from fpl_oracle.ingest.youtube_client import VideoInfo, _parse_playlist_items

# A video actually ingested by run_ingest (Let's Talk FPL, GW1 forwards
# video) — known to have an auto-generated English transcript as of this
# session, so it's a reasonable stand-in for "a known-stable video id".
_STABLE_VIDEO_ID = "pSKkaplg2V8"

_CANNED_PLAYLIST_ITEMS_RESPONSE = {
    "items": [
        {
            "snippet": {
                "publishedAt": "2026-08-15T18:00:00Z",
                "title": "My GW2 Team Reveal",
                "description": "Going through my squad for gameweek 2...",
                "resourceId": {"videoId": "vid_newest"},
            },
            "contentDetails": {
                "videoId": "vid_newest",
                "videoPublishedAt": "2026-08-15T18:00:00Z",
            },
        },
        {
            "snippet": {
                "publishedAt": "2026-08-08T18:00:00Z",
                "title": "My GW1 Team Reveal",
                "description": "Final team before deadline...",
                "resourceId": {"videoId": "vid_oldest"},
            },
            "contentDetails": {
                "videoId": "vid_oldest",
                "videoPublishedAt": "2026-08-08T18:00:00Z",
            },
        },
    ]
}


class TestTranscriptFullText:
    def test_joins_segment_texts_with_spaces(self) -> None:
        t = Transcript(
            video_id="abc123",
            language="en",
            is_generated=False,
            segments=[
                TranscriptSegment(text="hello", start=0.0, duration=1.0),
                TranscriptSegment(text="world", start=1.0, duration=1.0),
            ],
        )
        assert t.full_text == "hello world"

    def test_empty_segments_gives_empty_string(self) -> None:
        t = Transcript(video_id="abc123", language="en", is_generated=False, segments=[])
        assert t.full_text == ""


class TestSaveLoadRoundTrip:
    def test_round_trip_preserves_data(self, tmp_path: Path) -> None:
        t = Transcript(
            video_id="xyz789",
            language="en",
            is_generated=True,
            segments=[
                TranscriptSegment(text="kick off", start=0.0, duration=2.5),
                TranscriptSegment(text="in five minutes", start=2.5, duration=3.0),
            ],
        )
        path = save_transcript(t, dir=tmp_path)
        assert path == tmp_path / "xyz789.json"
        assert path.exists()

        loaded = load_transcript("xyz789", dir=tmp_path)
        assert loaded == t

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_transcript("does-not-exist", dir=tmp_path) is None

    def test_fetch_transcript_uses_on_disk_cache(self, tmp_path: Path) -> None:
        # If a transcript is already saved on disk, fetch_transcript should
        # return it without touching the network at all.
        t = Transcript(
            video_id="cached123",
            language="en",
            is_generated=False,
            segments=[TranscriptSegment(text="cached", start=0.0, duration=1.0)],
        )
        save_transcript(t, dir=tmp_path)

        result = fetch_transcript("cached123", dir=tmp_path)
        assert result == t


class TestParsePlaylistItems:
    def test_parses_videos_with_channel_id(self) -> None:
        videos = _parse_playlist_items(_CANNED_PLAYLIST_ITEMS_RESPONSE, channel_id="UCabc")
        assert len(videos) == 2
        assert all(isinstance(v, VideoInfo) for v in videos)

        newest = videos[0]
        assert newest.video_id == "vid_newest"
        assert newest.channel_id == "UCabc"
        assert newest.title == "My GW2 Team Reveal"
        assert newest.description.startswith("Going through my squad")
        assert newest.published_at.year == 2026
        assert newest.published_at.month == 8
        assert newest.published_at.day == 15

    def test_empty_items_gives_empty_list(self) -> None:
        assert _parse_playlist_items({"items": []}, channel_id="UCabc") == []

    def test_falls_back_to_snippet_when_content_details_incomplete(self) -> None:
        # Some items in an uploads playlist (private/members-only videos,
        # in practice) come back with a contentDetails block that's missing
        # videoId/videoPublishedAt entirely — the parser should fall back
        # to snippet.resourceId.videoId / snippet.publishedAt instead of
        # raising a KeyError.
        data = {
            "items": [
                {
                    "snippet": {
                        "publishedAt": "2026-08-01T09:30:00Z",
                        "title": "Members-only stream replay",
                        "description": "",
                        "resourceId": {"videoId": "vid_snippet_fallback"},
                    },
                    "contentDetails": {},
                }
            ]
        }
        videos = _parse_playlist_items(data, channel_id="UCabc")
        assert len(videos) == 1
        video = videos[0]
        assert video.video_id == "vid_snippet_fallback"
        assert video.published_at.year == 2026
        assert video.published_at.month == 8
        assert video.published_at.day == 1


class _FakeResponse:
    """Minimal stand-in for an httpx.Response, just enough for the
    read-through cache callers in youtube_client."""

    def __init__(self, json_data: dict) -> None:
        self._json_data = json_data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._json_data


_CANNED_UPLOADS_PLAYLIST_RESPONSE = {
    "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "PLxyz"}}}]
}


class TestListRecentVideosCacheFreshness:
    """The uploads-listing cache (unlike the rest of youtube_client's
    caches) goes stale after 6 hours — these exercise that override
    directly against `list_recent_videos`, with CACHE_DIR redirected to
    tmp_path and httpx.get monkeypatched so nothing touches the network."""

    _CHANNEL_ID = "UCabc"

    def _seed_uploads_playlist_cache(self, tmp_path: Path) -> None:
        # The uploads-playlist-ID lookup is cached indefinitely, so seed it
        # unconditionally to keep these tests focused on the uploads-list
        # freshness rule specifically.
        path = youtube_client._cache_path("uploads-playlist", self._CHANNEL_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_CANNED_UPLOADS_PLAYLIST_RESPONSE))

    def test_stale_cache_triggers_refetch(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(youtube_client, "CACHE_DIR", tmp_path)
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        self._seed_uploads_playlist_cache(tmp_path)

        uploads_cache = youtube_client._cache_path("uploads", self._CHANNEL_ID)
        uploads_cache.write_text(json.dumps(_CANNED_PLAYLIST_ITEMS_RESPONSE))
        stale_mtime = time.time() - 7 * 60 * 60  # 7h old, past the 6h freshness window
        os.utime(uploads_cache, (stale_mtime, stale_mtime))

        calls: list[str] = []

        def fake_get(url: str, **kwargs) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse(_CANNED_PLAYLIST_ITEMS_RESPONSE)

        monkeypatch.setattr(youtube_client.httpx, "get", fake_get)

        videos = youtube_client.list_recent_videos(self._CHANNEL_ID)

        assert len(calls) == 1
        assert "playlistItems" in calls[0]
        assert len(videos) == 2

    def test_fresh_cache_skips_network(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(youtube_client, "CACHE_DIR", tmp_path)
        self._seed_uploads_playlist_cache(tmp_path)

        uploads_cache = youtube_client._cache_path("uploads", self._CHANNEL_ID)
        uploads_cache.write_text(json.dumps(_CANNED_PLAYLIST_ITEMS_RESPONSE))
        # Leave mtime as "now" — well within the 6h freshness window.

        def fail_get(*args, **kwargs):
            raise AssertionError("httpx.get should not be called when the cache is fresh")

        monkeypatch.setattr(youtube_client.httpx, "get", fail_get)

        videos = youtube_client.list_recent_videos(self._CHANNEL_ID)

        assert len(videos) == 2


@pytest.mark.network
class TestFetchTranscriptNetwork:
    def test_fetch_known_stable_video_returns_segments(self, tmp_path: Path) -> None:
        transcript = fetch_transcript(_STABLE_VIDEO_ID, dir=tmp_path)
        assert transcript is not None
        assert transcript.video_id == _STABLE_VIDEO_ID
        assert len(transcript.segments) > 0
        assert transcript.full_text != ""
