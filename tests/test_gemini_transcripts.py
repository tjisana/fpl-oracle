"""Gemini-sourced transcripts.

The load-bearing behaviour here is not "can it transcribe" — it's that a
transcript whose timings cannot be trusted says so, loudly, instead of
shipping plausible-looking wrong deep links into the evidence trail."""

from __future__ import annotations

import pytest

from fpl_oracle.ingest import gemini_transcripts as gt
from fpl_oracle.ingest.transcripts import Transcript


def _wire(*pairs: tuple[float, str], language: str = "en") -> gt._WireTranscript:
    return gt._WireTranscript(
        language=language,
        segments=[gt._WireSegment(start_seconds=s, text=t) for s, t in pairs],
    )


class TestTimingValidation:
    """Measured on the first two real videos: a 317s video came back with
    segments spanning 461s, and a 1832s video came back spanning 2638s.
    The text was right; the timings were invented."""

    def test_timings_within_the_video_are_kept(self) -> None:
        t = gt._to_transcript("v", _wire((0.0, "a"), (10.0, "b"), (20.0, "c")), 300.0)
        assert [s.start for s in t.segments] == [0.0, 10.0, 20.0]
        assert t.source == f"gemini:{gt.MODEL}"

    def test_timings_overrunning_the_video_are_discarded_entirely(self) -> None:
        t = gt._to_transcript("v", _wire((0.0, "a"), (100.0, "b"), (461.0, "c")), 317.0)
        assert [s.start for s in t.segments] == [0.0, 0.0, 0.0]
        assert t.source.endswith(":untimed")

    def test_partial_drift_discards_ALL_timings_not_just_the_tail(self) -> None:
        """Drift that large means the whole timeline is unreliable. Keeping
        the early segments would imply the surviving ones are trustworthy."""
        t = gt._to_transcript("v", _wire((0.0, "a"), (5.0, "b"), (9999.0, "c")), 300.0)
        assert all(s.start == 0.0 for s in t.segments)

    def test_unknown_duration_means_untrusted(self) -> None:
        """With nothing to validate against, timings are discarded rather
        than shipped unverified."""
        t = gt._to_transcript("v", _wire((0.0, "a"), (10.0, "b")), None)
        assert all(s.start == 0.0 for s in t.segments)
        assert t.source.endswith(":untimed")

    def test_small_overrun_is_tolerated(self) -> None:
        t = gt._to_transcript("v", _wire((0.0, "a"), (302.0, "b")), 300.0)
        assert t.source == f"gemini:{gt.MODEL}"


class TestTranscriptShape:
    def test_durations_derive_from_the_next_segment_start(self) -> None:
        """Gemini reports where a segment begins, never how long it lasts.
        Deriving from the next start is exact; a fixed guess would put
        wrong offsets on the links the timings exist to power."""
        t = gt._to_transcript("v", _wire((0.0, "a"), (10.0, "b"), (25.0, "c")), 300.0)
        assert [s.duration for s in t.segments[:2]] == [10.0, 15.0]

    def test_segments_are_ordered_even_if_the_model_is_not(self) -> None:
        t = gt._to_transcript("v", _wire((20.0, "c"), (0.0, "a"), (10.0, "b")), 300.0)
        assert [s.text for s in t.segments] == ["a", "b", "c"]

    def test_empty_segments_are_dropped(self) -> None:
        t = gt._to_transcript("v", _wire((0.0, "a"), (5.0, "   "), (10.0, "b")), 300.0)
        assert [s.text for s in t.segments] == ["a", "b"]

    def test_full_text_survives_untimed_mode(self) -> None:
        """Extraction reads full_text, so losing timings must not lose
        content — that is the whole reason to keep the text."""
        t = gt._to_transcript("v", _wire((0.0, "hello"), (9999.0, "world")), 100.0)
        assert t.full_text == "hello world"


class TestProvenance:
    def test_scraped_transcripts_keep_the_default_source(self) -> None:
        """Every transcript already on disk predates the source field and
        must keep loading unchanged."""
        t = Transcript.model_validate_json(
            '{"video_id":"v","language":"en","is_generated":true,"segments":[]}'
        )
        assert t.source == "youtube-captions"

    def test_gemini_transcripts_are_distinguishable_from_scraped(self) -> None:
        t = gt._to_transcript("v", _wire((0.0, "a")), 300.0)
        assert t.source != "youtube-captions"
        assert "gemini" in t.source


class TestFetchIsNeverFatal:
    def test_missing_api_key_raises_a_clear_error(self, monkeypatch) -> None:
        monkeypatch.delenv(gt.GEMINI_API_KEY_ENV_VAR, raising=False)
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        with pytest.raises(RuntimeError, match=gt.GEMINI_API_KEY_ENV_VAR):
            gt._client()

    def test_client_has_a_hard_request_timeout(self, monkeypatch) -> None:
        """2026-09-11: a retry hung for 35+ minutes on deadline day because
        the SDK defaults to no timeout. The client must always set one."""
        monkeypatch.setenv(gt.GEMINI_API_KEY_ENV_VAR, "test-key")
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        captured: dict = {}

        class _FakeClient:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        from google import genai

        monkeypatch.setattr(genai, "Client", _FakeClient)
        gt._client()
        assert captured["http_options"].timeout == gt.REQUEST_TIMEOUT_MS
        assert gt.REQUEST_TIMEOUT_MS == 10 * 60 * 1000

    def test_repeated_failure_returns_none_rather_than_raising(self, monkeypatch) -> None:
        """Matches fetch_transcript's contract so the two are
        interchangeable: a per-video failure never kills the run."""

        def boom():
            raise RuntimeError("API down")

        monkeypatch.setattr(gt, "_client", boom)
        assert gt.fetch_transcript_via_gemini("v") is None
