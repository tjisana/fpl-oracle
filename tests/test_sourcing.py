"""Tests for ingest/sourcing.py — the gemini-vs-captions transcript
backend switch. `load_transcript`, `fetch_transcript`, and
`fetch_transcript_via_gemini` are all faked; no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from youtube_transcript_api import RequestBlocked

from fpl_oracle.ingest import sourcing
from fpl_oracle.ingest.sourcing import DEFAULT_TRANSCRIPT_SOURCE, fetch_transcript_from
from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment


def _transcript(video_id: str, source: str = "youtube-captions") -> Transcript:
    return Transcript(
        video_id=video_id,
        language="en",
        is_generated=True,
        segments=[TranscriptSegment(text="hi", start=0.0, duration=1.0)],
        source=source,
    )


class TestDefaultSource:
    def test_default_is_gemini(self) -> None:
        assert DEFAULT_TRANSCRIPT_SOURCE == "gemini"


class TestCacheHit:
    def test_cache_hit_returns_cached_without_calling_either_fetcher(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        cached = _transcript("v1", source="gemini:gemini-3.5-flash")
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: cached)

        def boom_captions(*a, **k):
            raise AssertionError("fetch_transcript must not be called on a cache hit")

        def boom_gemini(*a, **k):
            raise AssertionError("fetch_transcript_via_gemini must not be called on a cache hit")

        monkeypatch.setattr(sourcing, "fetch_transcript", boom_captions)
        monkeypatch.setattr(sourcing, "fetch_transcript_via_gemini", boom_gemini)

        result = fetch_transcript_from("v1", "captions", dir=tmp_path)
        assert result == cached

        result = fetch_transcript_from("v1", "gemini", dir=tmp_path)
        assert result == cached

    def test_cache_hit_is_returned_regardless_of_which_backend_produced_it(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A transcript cached by one backend is fine to serve for a request
        naming the other backend — a cached transcript is a cached
        transcript."""
        cached = _transcript("v1", source="youtube-captions")
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: cached)
        monkeypatch.setattr(
            sourcing,
            "fetch_transcript_via_gemini",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("network hit on cache hit")),
        )

        result = fetch_transcript_from("v1", "gemini", dir=tmp_path)
        assert result is cached


class TestCaptionsPath:
    def test_miss_delegates_to_fetch_transcript(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: None)
        fetched = _transcript("v1")
        seen: dict = {}

        def fake_fetch_transcript(video_id, dir):
            seen["video_id"] = video_id
            seen["dir"] = dir
            return fetched

        monkeypatch.setattr(sourcing, "fetch_transcript", fake_fetch_transcript)

        result = fetch_transcript_from("v1", "captions", dir=tmp_path)

        assert result == fetched
        assert seen == {"video_id": "v1", "dir": tmp_path}

    def test_request_blocked_propagates(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: None)

        def blocked(video_id, dir):
            raise RequestBlocked(video_id=video_id)

        monkeypatch.setattr(sourcing, "fetch_transcript", blocked)

        with pytest.raises(RequestBlocked):
            fetch_transcript_from("v1", "captions", dir=tmp_path)


class TestGeminiPath:
    def test_miss_delegates_to_fetch_transcript_via_gemini_with_duration(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: None)
        fetched = _transcript("v1", source="gemini:gemini-3.5-flash")
        seen: dict = {}

        def fake_fetch_gemini(video_id, duration_seconds=None):
            seen["video_id"] = video_id
            seen["duration_seconds"] = duration_seconds
            return fetched

        monkeypatch.setattr(sourcing, "fetch_transcript_via_gemini", fake_fetch_gemini)

        result = fetch_transcript_from("v1", "gemini", dir=tmp_path, duration_seconds=317.0)

        assert result == fetched
        assert seen == {"video_id": "v1", "duration_seconds": 317.0}

    def test_default_source_with_no_cache_reaches_gemini(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: None)
        called: list[str] = []
        monkeypatch.setattr(
            sourcing,
            "fetch_transcript_via_gemini",
            lambda video_id, duration_seconds=None: (
                called.append(video_id) or _transcript(video_id)
            ),
        )

        fetch_transcript_from("v1", dir=tmp_path)

        assert called == ["v1"]

    def test_gemini_none_is_returned_not_raised(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sourcing, "load_transcript", lambda video_id, dir: None)
        monkeypatch.setattr(sourcing, "fetch_transcript_via_gemini", lambda *a, **k: None)

        assert fetch_transcript_from("v1", "gemini", dir=tmp_path) is None
