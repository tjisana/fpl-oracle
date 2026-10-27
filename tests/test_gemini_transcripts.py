"""Gemini-sourced transcripts.

The load-bearing behaviour here is not "can it transcribe" — it's that a
transcript whose timings cannot be trusted says so, loudly, instead of
shipping plausible-looking wrong deep links into the evidence trail."""

from __future__ import annotations

from itertools import pairwise

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


class TestLongVideosAreRefusedUpFront:
    """A sanity ceiling (3 h by default) — long videos are otherwise read
    in windows, see TestChunkedReads. Nobody's team reveal is a stream."""

    def test_over_the_limit_returns_none_without_building_a_client(self, monkeypatch) -> None:
        monkeypatch.delenv(gt.MAX_VIDEO_SECONDS_ENV_VAR, raising=False)
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)

        def boom():
            raise AssertionError("no request should be made for an over-limit video")

        monkeypatch.setattr(gt, "_client", boom)
        assert gt.fetch_transcript_via_gemini("v", duration_seconds=4 * 3600) is None

    def test_at_or_under_the_limit_proceeds(self, monkeypatch) -> None:
        monkeypatch.delenv(gt.MAX_VIDEO_SECONDS_ENV_VAR, raising=False)
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        calls: list[int] = []

        def counting_client():
            calls.append(1)
            raise RuntimeError("stop here")

        monkeypatch.setattr(gt, "_client", counting_client)
        assert gt.fetch_transcript_via_gemini("v", duration_seconds=3 * 3600) is None
        assert calls, "a video at the ceiling must still be attempted"

    def test_unknown_duration_is_not_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        calls: list[int] = []

        def counting_client():
            calls.append(1)
            raise RuntimeError("stop here")

        monkeypatch.setattr(gt, "_client", counting_client)
        gt.fetch_transcript_via_gemini("v", duration_seconds=None)
        assert calls

    def test_env_var_raises_the_limit_for_paid_tier_keys(self, monkeypatch) -> None:
        monkeypatch.setenv(gt.MAX_VIDEO_SECONDS_ENV_VAR, str(5 * 3600))
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        calls: list[int] = []

        def counting_client():
            calls.append(1)
            raise RuntimeError("stop here")

        monkeypatch.setattr(gt, "_client", counting_client)
        gt.fetch_transcript_via_gemini("v", duration_seconds=4 * 3600, sleep=lambda s: None)
        assert calls

    def test_garbage_env_var_falls_back_to_the_default(self, monkeypatch) -> None:
        monkeypatch.setenv(gt.MAX_VIDEO_SECONDS_ENV_VAR, "lots")
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        assert gt.max_video_seconds() == gt.DEFAULT_MAX_VIDEO_SECONDS


class TestWallClockDeadline:
    """The per-read socket timeout did not fire on 2026-09-11 (connection
    kept alive for 6.5 h); the deadline must be wall-clock."""

    def test_returns_the_result_when_the_call_finishes_in_time(self) -> None:
        assert gt._call_with_deadline(lambda: 42, seconds=5) == 42

    def test_raises_timeout_when_the_call_outlives_the_deadline(self) -> None:
        import threading

        release = threading.Event()

        def slow() -> int:
            release.wait(10)
            return 1

        try:
            with pytest.raises(TimeoutError, match="wall-clock"):
                gt._call_with_deadline(slow, seconds=0.2)
        finally:
            release.set()

    def test_reraises_the_callables_own_error(self) -> None:
        def boom() -> int:
            raise ValueError("bad response")

        with pytest.raises(ValueError, match="bad response"):
            gt._call_with_deadline(boom, seconds=5)

    def test_a_timed_out_attempt_is_retried_then_gives_up_cleanly(self, monkeypatch) -> None:
        """Through the public function: a hang costs attempts, not the run."""
        monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
        monkeypatch.setenv(gt.GEMINI_API_KEY_ENV_VAR, "test-key")
        monkeypatch.setattr(gt, "REQUEST_TIMEOUT_MS", 100)
        attempts: list[int] = []

        class _Models:
            def generate_content(self, **kwargs):
                import threading

                attempts.append(1)
                threading.Event().wait(5)  # never returns in time

        class _Client:
            models = _Models()

        monkeypatch.setattr(gt, "_client", lambda: _Client())
        assert gt.fetch_transcript_via_gemini("v", duration_seconds=60) is None
        assert len(attempts) == gt.MAX_ATTEMPTS


def _fake_client_factory(monkeypatch, responder):
    """Install a fake Gemini client whose generate_content(**kwargs) calls
    `responder(kwargs)` and returns an object with `.parsed`."""
    calls: list[dict] = []

    class _Resp:
        def __init__(self, parsed) -> None:
            self.parsed = parsed

    class _Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return _Resp(responder(kwargs))

    class _Client:
        models = _Models()

    monkeypatch.setattr(gt, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv(gt.GEMINI_API_KEY_ENV_VAR, "test-key")
    monkeypatch.setattr(gt, "_client", lambda: _Client())
    return calls


def _window_of(kwargs) -> tuple[str, str] | None:
    part = kwargs["contents"].parts[0]
    vm = part.video_metadata
    return (vm.start_offset, vm.end_offset) if vm is not None else None


def _required_window(kwargs) -> tuple[str, str]:
    window = _window_of(kwargs)
    assert window is not None, "expected a windowed request"
    return window


class TestChunkWindows:
    def test_short_video_is_one_window(self) -> None:
        assert gt.chunk_windows(20 * 60) == [(0.0, 1200.0)]

    def test_exactly_one_chunk_is_one_window(self) -> None:
        assert gt.chunk_windows(gt.CHUNK_SECONDS) == [(0.0, float(gt.CHUNK_SECONDS))]

    def test_87_minute_show_is_three_consecutive_windows(self) -> None:
        assert gt.chunk_windows(87 * 60) == [(0.0, 1800.0), (1800.0, 3600.0), (3600.0, 5220.0)]

    def test_windows_are_gapless_and_cover_the_video(self) -> None:
        windows = gt.chunk_windows(12345.0)
        assert windows[0][0] == 0.0
        assert windows[-1][1] == 12345.0
        assert all(a[1] == b[0] for a, b in pairwise(windows))
        assert all(end - start <= gt.CHUNK_SECONDS for start, end in windows)


class TestChunkedReads:
    """The free tier's cap is 250k input tokens per MINUTE (~40 min of
    video per request). A long video is read window by window, one quota
    period apart, and stitched in order."""

    def test_short_video_is_a_single_request_without_offsets(self, monkeypatch) -> None:
        calls = _fake_client_factory(monkeypatch, lambda kw: _wire((0, "hello"), (5, "world")))
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=20 * 60, sleep=lambda s: None)
        assert t is not None
        assert len(calls) == 1
        assert _window_of(calls[0]) is None
        assert t.source == f"gemini:{gt.MODEL}"

    def test_long_video_is_read_in_windows_with_offsets_and_part_labels(self, monkeypatch) -> None:
        def responder(kw):
            start, _end = _required_window(kw)
            return _wire((0, f"text from {start}"))

        calls = _fake_client_factory(monkeypatch, responder)
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=87 * 60, sleep=lambda s: None)

        assert t is not None
        assert [_window_of(c) for c in calls] == [
            ("0s", "1800s"),
            ("1800s", "3600s"),
            ("3600s", "5220s"),
        ]
        prompts = [c["contents"].parts[1].text for c in calls]
        assert "part 1 of 3" in prompts[0] and "part 3 of 3" in prompts[2]
        assert "minutes 30 to 60" in prompts[1]

    def test_windows_are_stitched_in_order_and_marked_untimed_chunked(self, monkeypatch) -> None:
        def responder(kw):
            start, _end = _required_window(kw)
            # Gemini's per-window timings are meaningless; make them
            # DEscending across windows to prove we never re-sort on them.
            return _wire((9000 - float(start[:-1]), f"w{start}"))

        _fake_client_factory(monkeypatch, responder)
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=87 * 60, sleep=lambda s: None)
        assert t is not None
        assert [seg.text for seg in t.segments] == ["w0s", "w1800s", "w3600s"]
        assert t.source == f"gemini:{gt.MODEL}:untimed:chunked"
        assert all(seg.start == 0.0 and seg.duration == 0.0 for seg in t.segments)
        assert t.full_text == "w0s w1800s w3600s"

    def test_requests_are_spaced_one_quota_period_apart(self, monkeypatch) -> None:
        """Three windows, each request 'taking' 10 s: the second and third
        must wait the remaining 50 s so that request STARTS are 60 s apart."""
        now = [0.0]
        slept: list[float] = []

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        def responder(kw):
            now[0] += 10.0  # the request itself takes 10 s
            return _wire((0, "x"))

        _fake_client_factory(monkeypatch, responder)
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=87 * 60, sleep=sleep, clock=clock)
        assert t is not None
        assert slept == [gt.CHUNK_SPACING_SECONDS - 10.0, gt.CHUNK_SPACING_SECONDS - 10.0]

    def test_no_wait_when_a_request_already_took_a_full_quota_period(self, monkeypatch) -> None:
        now = [0.0]
        slept: list[float] = []

        def responder(kw):
            now[0] += 90.0
            return _wire((0, "x"))

        _fake_client_factory(monkeypatch, responder)
        gt.fetch_transcript_via_gemini(
            "v", duration_seconds=61 * 60, sleep=slept.append, clock=lambda: now[0]
        )
        assert slept == []

    def test_a_window_that_keeps_failing_loses_the_whole_video(self, monkeypatch) -> None:
        """All-or-nothing: a hole in the middle could be the team reveal."""
        attempts: list[str] = []

        def responder(kw):
            start, _ = _required_window(kw)
            attempts.append(start)
            if start == "1800s":
                raise RuntimeError("503 high demand")
            return _wire((0, "ok"))

        _fake_client_factory(monkeypatch, responder)
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=87 * 60, sleep=lambda s: None)
        assert t is None
        assert attempts.count("1800s") == gt.MAX_ATTEMPTS
        assert "3600s" not in attempts, "must stop at the failed window, not read on"

    def test_a_transient_window_failure_is_retried_and_the_video_survives(
        self, monkeypatch
    ) -> None:
        seen: list[str] = []

        def responder(kw):
            start, _ = _required_window(kw)
            seen.append(start)
            if start == "1800s" and seen.count("1800s") == 1:
                raise RuntimeError("503 once")
            return _wire((0, f"w{start}"))

        _fake_client_factory(monkeypatch, responder)
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=87 * 60, sleep=lambda s: None)
        assert t is not None
        assert [seg.text for seg in t.segments] == ["w0s", "w1800s", "w3600s"]

    def test_unknown_duration_is_a_single_unwindowed_request(self, monkeypatch) -> None:
        calls = _fake_client_factory(monkeypatch, lambda kw: _wire((0, "x")))
        t = gt.fetch_transcript_via_gemini("v", duration_seconds=None, sleep=lambda s: None)
        assert t is not None
        assert len(calls) == 1 and _window_of(calls[0]) is None
        assert t.source.endswith(":untimed")


class TestHarvestSeam:
    """`read_video_via_gemini`: the raising, countable, model-selectable
    entry point the unattended harvester uses. Defaults must not change
    what `fetch_transcript_via_gemini` does (covered above)."""

    def test_before_request_fires_once_per_api_attempt_including_retries(self, monkeypatch) -> None:
        n = {"calls": 0}

        def responder(kw):
            n["calls"] += 1
            if n["calls"] == 1:
                raise RuntimeError("503")
            return _wire((0, "ok"))

        _fake_client_factory(monkeypatch, responder)
        fired: list[int] = []
        t = gt.read_video_via_gemini(
            "v", 60, before_request=lambda: fired.append(1), sleep=lambda s: None
        )
        assert t.segments
        assert len(fired) == 2 == n["calls"]

    def test_model_is_passed_through_and_recorded_in_the_source(self, monkeypatch) -> None:
        calls = _fake_client_factory(monkeypatch, lambda kw: _wire((0, "x"), (10, "y")))
        t = gt.read_video_via_gemini("v", 60, model="gemini-test-model")
        assert calls[0]["model"] == "gemini-test-model"
        assert t.source.startswith("gemini:gemini-test-model")

    def test_default_model_is_unchanged(self, monkeypatch) -> None:
        calls = _fake_client_factory(monkeypatch, lambda kw: _wire((0, "x")))
        gt.fetch_transcript_via_gemini("v", 60)
        assert calls[0]["model"] == gt.MODEL

    def test_retry_delay_none_stops_after_the_first_failure_and_raises(self, monkeypatch) -> None:
        n = {"calls": 0}

        def responder(kw):
            n["calls"] += 1
            raise RuntimeError("boom")

        _fake_client_factory(monkeypatch, responder)
        with pytest.raises(RuntimeError, match="boom"):
            gt.read_video_via_gemini(
                "v", 60, max_attempts=2, retry_delay=lambda e, a: None, sleep=lambda s: None
            )
        assert n["calls"] == 1

    def test_retry_delay_value_sleeps_then_retries(self, monkeypatch) -> None:
        n = {"calls": 0}

        def responder(kw):
            n["calls"] += 1
            if n["calls"] == 1:
                raise RuntimeError("429")
            return _wire((0, "ok"))

        _fake_client_factory(monkeypatch, responder)
        slept: list[float] = []
        t = gt.read_video_via_gemini(
            "v", 60, max_attempts=2, retry_delay=lambda e, a: 65.0, sleep=slept.append
        )
        assert t.segments
        assert slept == [65.0]

    def test_before_request_error_aborts_without_a_call(self, monkeypatch) -> None:
        calls = _fake_client_factory(monkeypatch, lambda kw: _wire((0, "x")))

        def gate() -> None:
            raise LookupError("budget")

        with pytest.raises(LookupError):
            gt.read_video_via_gemini("v", 60, before_request=gate)
        assert calls == []

    def test_too_long_raises_its_own_error_without_a_request(self, monkeypatch) -> None:
        calls = _fake_client_factory(monkeypatch, lambda kw: _wire((0, "x")))
        monkeypatch.delenv(gt.MAX_VIDEO_SECONDS_ENV_VAR, raising=False)
        with pytest.raises(gt.VideoTooLongError):
            gt.read_video_via_gemini("v", 99 * 3600)
        assert calls == []

    def test_before_request_does_not_fire_when_the_client_cannot_be_built(
        self, monkeypatch
    ) -> None:
        def no_key():
            raise RuntimeError("no key")

        monkeypatch.setattr(gt, "_client", no_key)
        fired: list[int] = []
        with pytest.raises(RuntimeError, match="no key"):
            gt.read_video_via_gemini(
                "v", 60, before_request=lambda: fired.append(1), sleep=lambda s: None
            )
        assert fired == []
