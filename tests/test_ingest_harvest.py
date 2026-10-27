"""Tests for ingest/harvest.py — the unattended transcript harvester.

Fully offline: fake Gemini and caption readers, an injected clock and
sleep, and tmp_path for the transcript cache and state directory.
"""

from __future__ import annotations

import io
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree.ElementTree import ParseError

import httpx
import pytest
import requests
from rich.console import Console
from youtube_transcript_api import IpBlocked

from fpl_oracle.ingest import harvest as h
from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment, save_transcript

MODEL = "gemini-3.5-flash"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)  # 05:00 Pacific, 22 Oct
FAR_DEADLINE = T0 + timedelta(days=3)


class ApiError(Exception):
    """Stand-in for google.genai's APIError (duck-typed by the classifier)."""

    def __init__(self, code: int, message: str = "", status: str = "") -> None:
        super().__init__(f"{code} {status} {message}")
        self.code = code
        self.status = status


def err503() -> ApiError:
    return ApiError(503, "high demand", "UNAVAILABLE")


def err429_minute() -> ApiError:
    return ApiError(
        429, "GenerateContentInputTokensPerModelPerMinute-FreeTier", "RESOURCE_EXHAUSTED"
    )


def err429_daily() -> ApiError:
    return ApiError(429, "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "RESOURCE_EXHAUSTED")


def _transcript(video_id: str, source: str) -> Transcript:
    return Transcript(
        video_id=video_id,
        language="en",
        is_generated=True,
        segments=[TranscriptSegment(text="hello", start=0.0, duration=1.0)],
        source=source,
    )


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


class FakeGemini:
    """Scripted Gemini reader. `scripts[video_id]` is a list of per-REQUEST
    outcomes ("ok" or an exception to raise), consumed in order; missing or
    exhausted scripts mean "ok". It mimics the real seam: calls
    before_request before every request, one request per 30-minute window,
    and one retry only if retry_delay allows."""

    def __init__(self, scripts: dict[str, list[object]] | None = None) -> None:
        self.scripts = scripts or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        video_id: str,
        duration_seconds: float | None,
        *,
        model: str,
        before_request: Callable[[], None],
        retry_delay: Callable[[Exception, int], float | None],
        sleep: Callable[[float], None],
    ) -> Transcript:
        self.calls.append((video_id, model))
        script = self.scripts.setdefault(video_id, [])
        windows = max(1, math.ceil(duration_seconds / 1800)) if duration_seconds else 1
        for _ in range(windows):
            for attempt in (1, 2):
                before_request()
                outcome = script.pop(0) if script else "ok"
                if outcome == "ok":
                    break
                assert isinstance(outcome, Exception)
                delay = retry_delay(outcome, attempt)
                if delay is None or attempt == 2:
                    raise outcome
                sleep(delay)
        return _transcript(video_id, f"gemini:{model}")


class FakeCaptions:
    def __init__(self, outcomes: dict[str, object] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    def __call__(self, video_id: str) -> Transcript | None:
        self.calls.append(video_id)
        outcome = self.outcomes.get(video_id, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return None
        return _transcript(video_id, "youtube-captions")


def target(
    creator_id: str, weight: float, duration: int | None = 600, video_id: str | None = None
) -> h.CreatorTarget:
    vid = video_id or f"vid-{creator_id}"
    return h.CreatorTarget(
        creator_id=creator_id,
        name=creator_id,
        weight=weight,
        video=h.VideoRef(video_id=vid, title=f"title {creator_id}", duration_seconds=duration),
    )


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # request_cost reads the ceiling via max_video_seconds() (which loads .env);
    # the key check would too. Tests never read the environment.
    monkeypatch.setattr(h, "max_video_seconds", lambda: 3 * 3600)
    monkeypatch.setattr(h, "gemini_api_key_present", lambda: True)


@pytest.fixture
def config(tmp_path: Path) -> h.HarvestConfig:
    return h.HarvestConfig(state_dir=tmp_path / "state", transcript_dir=tmp_path / "transcripts")


def run(
    targets: list[h.CreatorTarget],
    config: h.HarvestConfig,
    *,
    clock: Clock | None = None,
    gemini: FakeGemini | None = None,
    captions: FakeCaptions | None = None,
    deadline: datetime | None = FAR_DEADLINE,
    gameweek: int = 9,
    gemini_available: Callable[[], bool] | None = None,
) -> tuple[h.HarvestResult, Clock, FakeGemini, FakeCaptions]:
    clock = clock or Clock()
    gemini = gemini or FakeGemini()
    captions = captions or FakeCaptions()
    result = h.run_harvest(
        gameweek,
        targets,
        deadline,
        config,
        now=clock.now,
        sleep=clock.sleep,
        gemini_reader=gemini,
        caption_reader=captions,
        gemini_available=gemini_available,
    )
    return result, clock, gemini, captions


def used(config: h.HarvestConfig, day: str | None = None, model: str = MODEL) -> int:
    return h.load_usage(config.state_dir).used(model, day or h.quota_day(T0))


def state_of(config: h.HarvestConfig) -> h.HarvestState:
    return h.load_state(9, config.state_dir)


# ---- pure helpers ----------------------------------------------------------


class TestCostAndQuotaDay:
    @pytest.mark.parametrize(
        ("seconds", "cost"),
        [(None, 1), (600, 1), (1800, 1), (1801, 2), (3600, 2), (5220, 3), (5400, 3), (5401, 4)],
    )
    def test_cost_is_ceil_of_30_minute_windows(self, seconds: int | None, cost: int) -> None:
        assert h.request_cost(seconds) == cost

    def test_over_ceiling_costs_nothing_because_it_is_never_sent(self) -> None:
        assert h.request_cost(4 * 3600) is None

    def test_quota_day_rolls_over_at_midnight_pacific_not_utc(self) -> None:
        # 06:59 UTC on 22 Oct = 23:59 PDT on 21 Oct; 07:01 UTC = 00:01 PDT on 22 Oct
        assert h.quota_day(datetime(2026, 10, 22, 6, 59, tzinfo=UTC)) == "2026-10-21"
        assert h.quota_day(datetime(2026, 10, 22, 7, 1, tzinfo=UTC)) == "2026-10-22"

    def test_quota_day_follows_the_dst_change(self) -> None:
        # PST (UTC-8) after 1 Nov: 07:30 UTC is still the previous day
        assert h.quota_day(datetime(2026, 11, 2, 7, 30, tzinfo=UTC)) == "2026-11-01"
        assert h.quota_day(datetime(2026, 11, 2, 8, 30, tzinfo=UTC)) == "2026-11-02"


class TestClassify:
    def test_503_and_timeouts_are_transient(self) -> None:
        assert h.classify_gemini_error(err503()) is h.GeminiFailure.TRANSIENT
        assert h.classify_gemini_error(TimeoutError("x")) is h.GeminiFailure.TRANSIENT

    def test_429_per_minute_vs_per_day(self) -> None:
        assert h.classify_gemini_error(err429_minute()) is h.GeminiFailure.RATE_MINUTE
        assert h.classify_gemini_error(err429_daily()) is h.GeminiFailure.RATE_DAILY

    def test_anything_else_is_other(self) -> None:
        assert h.classify_gemini_error(ValueError("bad json")) is h.GeminiFailure.OTHER


class TestDeadline:
    def test_deadline_from_events(self) -> None:
        events = [{"id": 8, "deadline_time": "2026-10-17T10:00:00Z"}, {"id": 9}]
        assert h.deadline_for_gameweek(events, 8) == datetime(2026, 10, 17, 10, tzinfo=UTC)
        assert h.deadline_for_gameweek(events, 9) is None
        assert h.deadline_for_gameweek(events, 10) is None


# ---- behaviours 3, 11: skip what is done, provenance -----------------------


class TestIdempotentRerun:
    def test_rerun_touches_nothing_and_rewrites_nothing(self, config: h.HarvestConfig) -> None:
        targets = [target("a", 0.9), target("b", 0.5)]
        run(targets, config)
        path = h.state_path(9, config.state_dir)
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns

        gemini, captions = FakeGemini(), FakeCaptions()
        result, clock, _, _ = run(targets, config, gemini=gemini, captions=captions)
        assert gemini.calls == [] and captions.calls == []
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime
        assert clock.sleeps == []
        assert result.no_transcript == []

    def test_a_transcript_already_in_the_cache_is_adopted_not_refetched(
        self, config: h.HarvestConfig
    ) -> None:
        save_transcript(_transcript("vid-a", "youtube-captions"), dir=config.transcript_dir)
        gemini, captions = FakeGemini(), FakeCaptions()
        result, _, _, _ = run([target("a", 0.9)], config, gemini=gemini, captions=captions)
        assert gemini.calls == [] and captions.calls == []
        assert state_of(config).videos["vid-a"].status == "ok"
        assert state_of(config).videos["vid-a"].source == "youtube-captions"
        assert result.rows[0].status == "ok"
        assert not h.usage_path(config.state_dir).exists()  # nothing spent


class TestProvenance:
    def test_gemini_and_caption_sources_survive_into_state_and_cache(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 1}})
        run([target("a", 0.9), target("b", 0.5)], cfg)  # b falls back (budget)
        state = state_of(cfg)
        assert state.videos["vid-a"].source == f"gemini:{MODEL}"
        assert state.videos["vid-b"].source == "youtube-captions"
        from fpl_oracle.ingest.transcripts import load_transcript

        saved = load_transcript("vid-a", dir=cfg.transcript_dir)
        assert saved is not None and saved.source == f"gemini:{MODEL}"


# ---- behaviours 4, 5: budget and priority ----------------------------------


class TestBudgetAndPriority:
    def test_highest_weight_goes_first(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini()
        run([target("low", 0.2), target("top", 0.9), target("mid", 0.5)], config, gemini=gemini)
        assert [v for v, _ in gemini.calls] == ["vid-top", "vid-mid", "vid-low"]

    def test_multi_window_videos_cost_one_request_per_window(self, config: h.HarvestConfig) -> None:
        targets = [target("long", 0.9, duration=87 * 60), target("short", 0.5, duration=600)]
        result, _, _, _ = run(targets, config)
        state = state_of(config)
        assert state.videos["vid-long"].gemini_requests_used == 3
        assert state.videos["vid-short"].gemini_requests_used == 1
        assert used(config) == 4
        assert result.gemini_budget_left == {MODEL: 14}
        assert result.gemini_requests_this_run == 4

    def test_a_video_the_remaining_budget_cannot_cover_uses_captions_immediately(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 4}})
        targets = [
            target("a", 0.9, duration=87 * 60),  # 3
            target("b", 0.8, duration=87 * 60),  # needs 3, only 1 left -> captions
            target("c", 0.7, duration=600),  # 1, still fits
        ]
        gemini, captions = FakeGemini(), FakeCaptions()
        run(targets, cfg, gemini=gemini, captions=captions)
        assert [v for v, _ in gemini.calls] == ["vid-a", "vid-c"]
        assert captions.calls == ["vid-b"]
        vs = state_of(cfg).videos["vid-b"]
        assert vs.fell_back_reason == "budget" and vs.source == "youtube-captions"

    def test_budget_is_never_exceeded_even_when_retries_burn_requests(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 2}})
        gemini = FakeGemini({"vid-a": [err429_minute(), "ok"]})  # 2 requests for 1 window
        captions = FakeCaptions()
        run(
            [target("a", 0.9, duration=600), target("b", 0.5, duration=600)],
            cfg,
            gemini=gemini,
            captions=captions,
        )
        assert used(cfg) == 2
        assert captions.calls == ["vid-b"]  # budget gone -> captions

    def test_counters_are_per_quota_day(self, config: h.HarvestConfig) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 1}})
        run([target("a", 0.9)], cfg)
        # next Pacific day: budget is fresh again
        later = Clock(T0 + timedelta(days=1))
        gemini = FakeGemini()
        run([target("a", 0.9), target("b", 0.5)], cfg, clock=later, gemini=gemini)
        assert [v for v, _ in gemini.calls] == ["vid-b"]
        assert used(cfg, "2026-10-22") == 1
        assert used(cfg, "2026-10-23") == 1

    def test_over_ceiling_video_goes_to_captions_as_too_long(self, config: h.HarvestConfig) -> None:
        gemini, captions = FakeGemini(), FakeCaptions()
        run([target("a", 0.9, duration=4 * 3600)], config, gemini=gemini, captions=captions)
        assert gemini.calls == []
        assert state_of(config).videos["vid-a"].fell_back_reason == "too_long"


class TestSecondModel:
    def test_second_model_is_a_config_change_only(self, config: h.HarvestConfig) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 1, "gemini-other": 5}})
        gemini = FakeGemini()
        run([target("a", 0.9), target("b", 0.5), target("c", 0.4)], cfg, gemini=gemini)
        assert gemini.calls == [
            ("vid-a", MODEL),
            ("vid-b", "gemini-other"),
            ("vid-c", "gemini-other"),
        ]
        assert state_of(cfg).videos["vid-b"].source == "gemini:gemini-other"

    def test_daily_429_on_the_first_model_tries_the_next_before_captions(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 18, "gemini-other": 5}})
        gemini = FakeGemini({"vid-a": [err429_daily()]})
        captions = FakeCaptions()
        run([target("a", 0.9), target("b", 0.5)], cfg, gemini=gemini, captions=captions)
        assert gemini.calls == [
            ("vid-a", MODEL),
            ("vid-a", "gemini-other"),
            ("vid-b", "gemini-other"),
        ]
        assert captions.calls == []


# ---- behaviour 6: failure handling -----------------------------------------


class TestFailures:
    def test_503_sets_retry_after_and_is_not_retried_in_run(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini({"vid-a": [err503()]})
        captions = FakeCaptions()
        result, clock, _, _ = run([target("a", 0.9)], config, gemini=gemini, captions=captions)
        assert gemini.calls == [("vid-a", MODEL)]  # one invocation, one request
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "pending"
        assert vs.attempts == 1 and vs.gemini_requests_used == 1  # the failure counted
        assert vs.retry_after == T0 + timedelta(minutes=30)
        assert captions.calls == []  # far from the deadline: wait, don't fall back
        assert clock.sleeps == []
        assert result.no_transcript == ["a"]

    def test_backoff_escalates_30m_2h_6h_and_waits_out_the_timer(
        self, config: h.HarvestConfig
    ) -> None:
        clock = Clock()
        gemini = FakeGemini({"vid-a": [err503(), err503(), err503(), err503()]})
        targets = [target("a", 0.9)]
        run(targets, config, clock=clock, gemini=gemini)
        # before the timer expires: not touched
        clock.t = T0 + timedelta(minutes=10)
        run(targets, config, clock=clock, gemini=gemini)
        assert len(gemini.calls) == 1
        expected = [timedelta(hours=2), timedelta(hours=6), timedelta(hours=6)]
        for delta in expected:
            retry_after = state_of(config).videos["vid-a"].retry_after
            assert retry_after is not None
            clock.t = retry_after + timedelta(seconds=1)
            run(targets, config, clock=clock, gemini=gemini)
            assert state_of(config).videos["vid-a"].retry_after == clock.t + delta

    def test_timeout_is_treated_like_503(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini({"vid-a": [TimeoutError("deadline")]})
        run([target("a", 0.9)], config, gemini=gemini)
        assert state_of(config).videos["vid-a"].retry_after == T0 + timedelta(minutes=30)

    def test_per_minute_429_sleeps_65s_once_then_continues(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini({"vid-a": [err429_minute(), "ok"]})
        _, clock, _, captions = run([target("a", 0.9)], config, gemini=gemini)
        assert clock.sleeps == [65.0]
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "ok" and vs.gemini_requests_used == 2
        assert captions.calls == []

    def test_per_minute_429_that_repeats_is_not_looped(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini({"vid-a": [err429_minute(), err429_minute()]})
        _, clock, _, _ = run([target("a", 0.9)], config, gemini=gemini)
        assert clock.sleeps == [65.0]
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "pending" and vs.gemini_requests_used == 2
        assert vs.retry_after is not None

    def test_daily_429_switches_the_rest_of_the_day_to_captions(
        self, config: h.HarvestConfig
    ) -> None:
        gemini = FakeGemini({"vid-a": [err429_daily()]})
        captions = FakeCaptions()
        targets = [target("a", 0.9), target("b", 0.5), target("c", 0.3)]
        run(targets, config, gemini=gemini, captions=captions)
        assert gemini.calls == [("vid-a", MODEL)]  # nobody else tried Gemini
        assert captions.calls == ["vid-a", "vid-b", "vid-c"]
        state = state_of(config)
        assert h.load_usage(config.state_dir).is_exhausted(MODEL, h.quota_day(T0))
        assert all(
            state.videos[v].fell_back_reason == "gemini_daily_limit"
            for v in ("vid-a", "vid-b", "vid-c")
        )

    def test_exhausted_flag_persists_to_the_next_run_the_same_day(
        self, config: h.HarvestConfig
    ) -> None:
        run(
            [target("a", 0.9)],
            config,
            gemini=FakeGemini({"vid-a": [err429_daily()]}),
            captions=FakeCaptions({"vid-a": None}),
        )
        gemini = FakeGemini()
        run([target("a", 0.9), target("b", 0.5)], config, gemini=gemini)
        assert gemini.calls == []


# ---- behaviour 7: deadline backstop ----------------------------------------


class TestDeadlineBackstop:
    def test_inside_the_window_captions_override_retry_timers(
        self, config: h.HarvestConfig
    ) -> None:
        deadline = T0 + timedelta(hours=3)
        gemini = FakeGemini({"vid-a": [err503()]})
        captions = FakeCaptions()
        run([target("a", 0.9)], config, gemini=gemini, captions=captions, deadline=deadline)
        # 503 -> retry timer set, but the deadline is 3h away (< 6h): backstop fires
        assert captions.calls == ["vid-a"]
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "ok" and vs.fell_back_reason == "deadline_backstop"

    def test_backstop_overrides_a_future_retry_timer_from_an_earlier_run(
        self, config: h.HarvestConfig
    ) -> None:
        clock = Clock()
        run(
            [target("a", 0.9)],
            config,
            clock=clock,
            gemini=FakeGemini({"vid-a": [err503()]}),
            deadline=T0 + timedelta(days=2),
        )
        clock.t = T0 + timedelta(minutes=5)  # timer (30 min) still running
        gemini, captions = FakeGemini(), FakeCaptions()
        run(
            [target("a", 0.9)],
            config,
            clock=clock,
            gemini=gemini,
            captions=captions,
            deadline=clock.t + timedelta(hours=5),
        )
        assert gemini.calls == [] and captions.calls == ["vid-a"]

    def test_outside_the_window_there_is_no_captions_fallback(
        self, config: h.HarvestConfig
    ) -> None:
        captions = FakeCaptions()
        run(
            [target("a", 0.9)],
            config,
            gemini=FakeGemini({"vid-a": [err503()]}),
            captions=captions,
            deadline=T0 + timedelta(hours=7),
        )
        assert captions.calls == []

    def test_backstop_hours_is_configurable(self, config: h.HarvestConfig) -> None:
        cfg = config.model_copy(update={"backstop_hours": 12})
        captions = FakeCaptions()
        run(
            [target("a", 0.9)],
            cfg,
            gemini=FakeGemini({"vid-a": [err503()]}),
            captions=captions,
            deadline=T0 + timedelta(hours=10),
        )
        assert captions.calls == ["vid-a"]

    def test_backstop_ignores_budget(self, config: h.HarvestConfig) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 0}})
        captions = FakeCaptions()
        run([target("a", 0.9)], cfg, captions=captions, deadline=T0 + timedelta(hours=1))
        assert captions.calls == ["vid-a"]


# ---- behaviour 8: captions -------------------------------------------------


class TestCaptions:
    def _starved(self, config: h.HarvestConfig) -> h.HarvestConfig:
        return config.model_copy(update={"gemini_daily_limits": {MODEL: 0}})

    def test_ip_blocked_stops_all_caption_attempts_and_leaves_videos_pending(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = self._starved(config)
        captions = FakeCaptions({"vid-a": IpBlocked("vid-a")})
        result, _, _, _ = run(
            [target("a", 0.9), target("b", 0.5), target("c", 0.3)], cfg, captions=captions
        )
        assert captions.calls == ["vid-a"]  # exactly one attempt, then stop
        state = state_of(cfg)
        assert {state.videos[v].status for v in ("vid-a", "vid-b", "vid-c")} == {"pending"}
        assert result.no_transcript == ["a", "b", "c"]

    def test_the_backstop_pass_also_respects_the_block(self, config: h.HarvestConfig) -> None:
        captions = FakeCaptions({"vid-a": IpBlocked("vid-a")})
        run(
            [target("a", 0.9), target("b", 0.5)],
            self._starved(config),
            captions=captions,
            deadline=T0 + timedelta(hours=1),
        )
        assert captions.calls == ["vid-a"]

    def test_caption_requests_are_paced(self, config: h.HarvestConfig) -> None:
        _, clock, _, captions = run(
            [target("a", 0.9), target("b", 0.5), target("c", 0.3)],
            self._starved(config),
        )
        assert captions.calls == ["vid-a", "vid-b", "vid-c"]
        assert clock.sleeps == [h.CAPTION_PACING_SECONDS] * 2

    def test_no_captions_marks_failed_and_is_retried_next_run(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = self._starved(config)
        run([target("a", 0.9)], cfg, captions=FakeCaptions({"vid-a": None}))
        assert state_of(cfg).videos["vid-a"].status == "failed"
        captions = FakeCaptions()
        run([target("a", 0.9)], cfg, captions=captions)
        assert captions.calls == ["vid-a"]
        assert state_of(cfg).videos["vid-a"].status == "ok"


# ---- behaviour 9: state ----------------------------------------------------


class TestState:
    def test_round_trip(self, config: h.HarvestConfig) -> None:
        state = h.HarvestState(gameweek=9, updated_at=T0)
        state.videos["v"] = h.VideoState(
            video_id="v",
            creator_id="c",
            status="failed",
            source="youtube-captions",
            attempts=2,
            gemini_requests_used=3,
            last_error="x",
            retry_after=T0,
            fell_back_reason="budget",
        )
        h.save_state(state, config.state_dir)
        assert h.load_state(9, config.state_dir) == state

    def test_usage_file_round_trip(self, config: h.HarvestConfig) -> None:
        usage = h.GeminiUsageFile()
        usage.add_request(MODEL, "2026-10-22")
        usage.add_request(MODEL, "2026-10-22")
        usage.mark_exhausted(MODEL, "2026-10-22")
        h.save_usage(usage, config.state_dir)
        assert h.load_usage(config.state_dir) == usage
        assert usage.used(MODEL, "2026-10-22") == 2 and usage.used(MODEL, "2026-10-23") == 0

    def test_missing_file_is_a_fresh_state(self, config: h.HarvestConfig) -> None:
        assert h.load_state(9, config.state_dir) == h.HarvestState(gameweek=9)

    def test_crash_mid_write_leaves_the_previous_state_intact(
        self, config: h.HarvestConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = h.HarvestState(gameweek=9)
        old.videos["v"] = h.VideoState(video_id="v", creator_id="c", attempts=5)
        h.save_state(old, config.state_dir)

        new = old.model_copy(deep=True)
        new.videos["v"].attempts = 6

        def crash(src: object, dst: object) -> None:
            raise OSError("power cut between write and rename")

        monkeypatch.setattr(os, "replace", crash)
        with pytest.raises(OSError, match="power cut"):
            h.save_state(new, config.state_dir)
        monkeypatch.undo()

        # the half-finished temp file is ignored; the old state still loads
        assert h.load_state(9, config.state_dir) == old
        assert list(config.state_dir.glob("*.tmp")) == []  # cleaned up

    def test_unreadable_state_refuses_to_reset_the_counters(self, config: h.HarvestConfig) -> None:
        path = h.state_path(9, config.state_dir)
        path.parent.mkdir(parents=True)
        path.write_text("{ not json")
        with pytest.raises(RuntimeError, match="unreadable"):
            h.load_state(9, config.state_dir)

    def test_every_request_is_persisted_before_the_next_step(self, config: h.HarvestConfig) -> None:
        """A crash after request N must not forget request N."""

        class Crash(Exception): ...

        def reader(video_id, duration, *, model, before_request, retry_delay, sleep):
            before_request()
            raise Crash

        # The failure is classified OTHER and the run moves on; what matters
        # is that the counter reached disk before the failing call.
        h.run_harvest(
            9,
            [target("a", 0.9)],
            FAR_DEADLINE,
            config,
            now=Clock().now,
            sleep=lambda s: None,
            gemini_reader=reader,
            caption_reader=FakeCaptions(),
        )
        assert used(config) == 1


# ---- behaviour 10: report --------------------------------------------------


def render(result: h.HarvestResult) -> str:
    buf = io.StringIO()
    h.print_report(result, Console(file=buf, width=200, force_terminal=False))
    return buf.getvalue()


class TestReport:
    def test_table_lists_creator_video_source_status_and_fallback_reason(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 1}})
        result, *_ = run([target("a", 0.9), target("b", 0.5)], cfg)
        text = render(result)
        assert "vid-a" in text and f"gemini:{MODEL}" in text
        assert "youtube-captions" in text and "captions fallback: budget" in text

    def test_creators_without_a_transcript_are_listed_with_hours_to_deadline(
        self, config: h.HarvestConfig
    ) -> None:
        missing = h.CreatorTarget(creator_id="ghost", name="ghost", weight=0.4, video=None)
        result, *_ = run([target("a", 0.9), missing], config, deadline=T0 + timedelta(hours=30))
        text = render(result)
        assert result.no_transcript == ["ghost"]
        assert "ghost" in text and "30.0 h to deadline" in text

    def test_warns_loudly_when_under_half_the_weight_within_12h(
        self, config: h.HarvestConfig
    ) -> None:
        targets = [target("a", 0.3), target("b", 0.7)]
        gemini = FakeGemini({"vid-b": [err503()]})
        # deadline 8h away: inside the 12h warning window, outside the 6h backstop
        result, *_ = run(targets, config, gemini=gemini, deadline=T0 + timedelta(hours=8))
        assert result.weight_covered_fraction == pytest.approx(0.3)
        assert result.warn_low_coverage is True
        assert "WARNING" in render(result)

    def test_no_warning_at_half_or_more(self, config: h.HarvestConfig) -> None:
        targets = [target("a", 0.5), target("b", 0.5)]
        gemini = FakeGemini({"vid-b": [err503()]})
        result, *_ = run(targets, config, gemini=gemini, deadline=T0 + timedelta(hours=8))
        assert result.weight_covered_fraction == pytest.approx(0.5)
        assert result.warn_low_coverage is False

    def test_no_warning_when_the_deadline_is_far(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini({"vid-a": [err503()]})
        result, *_ = run(
            [target("a", 0.9)], config, gemini=gemini, deadline=T0 + timedelta(hours=40)
        )
        assert result.warn_low_coverage is False


# ---- acceptance: dry run and a GW8-like day --------------------------------


class TestDryRun:
    def test_dry_run_spends_nothing_and_writes_nothing(self, config: h.HarvestConfig) -> None:
        targets = [
            target("a", 0.9, duration=87 * 60),
            target("b", 0.5, duration=600),
            target("c", 0.3, duration=600),
        ]
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 4}})
        result = h.plan_harvest(9, targets, FAR_DEADLINE, cfg, now=Clock().now)
        assert result.dry_run is True
        by_creator = {r.creator_id: r for r in result.rows}
        assert by_creator["a"].source == f"gemini:{MODEL} (3 requests)"
        assert by_creator["b"].source == f"gemini:{MODEL} (1 request)"
        assert by_creator["c"].source == "captions (budget)"
        assert result.gemini_budget_left == {MODEL: 0}
        assert result.gemini_requests_this_run == 0
        assert not cfg.state_dir.exists()
        assert not cfg.transcript_dir.exists()
        assert "DRY RUN" in render(result)

    def test_dry_run_reports_remaining_budget_from_saved_state(
        self, config: h.HarvestConfig
    ) -> None:
        run([target("a", 0.9)], config)  # spends 1
        result = h.plan_harvest(
            9, [target("a", 0.9), target("b", 0.5)], FAR_DEADLINE, config, now=Clock().now
        )
        assert result.gemini_budget_left == {MODEL: 16}  # 18 - 1 spent - 1 planned
        assert used(config) == 1  # unchanged


class TestGw8LikeDay:
    def test_ten_same_day_videos_two_multi_window_all_transcribed_within_budget(
        self, config: h.HarvestConfig
    ) -> None:
        durations = [600, 900, 1200, 1500, 1700, 400, 800, 1000, 87 * 60, 90 * 60]
        targets = [target(f"c{i}", 0.9 - i * 0.05, duration=d) for i, d in enumerate(durations)]
        # a tight day: 9 requests, but the videos need 8 short + 3 + 3 = 14
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 9}})
        gemini = FakeGemini({"vid-c2": [err503()]})  # plus a 503 for flavour
        captions = FakeCaptions()
        result, *_ = run(
            targets, cfg, gemini=gemini, captions=captions, deadline=T0 + timedelta(hours=2)
        )  # inside the backstop
        state = state_of(cfg)
        assert all(vs.status == "ok" for vs in state.videos.values())
        assert len(state.videos) == 10
        assert used(cfg) <= 9
        assert result.no_transcript == []
        sources = {vs.source for vs in state.videos.values()}
        assert sources == {f"gemini:{MODEL}", "youtube-captions"}


# ---- review fixes -----------------------------------------------------------


def starved(config: h.HarvestConfig) -> h.HarvestConfig:
    return config.model_copy(update={"gemini_daily_limits": {MODEL: 0}})


class TestCaptionTransportErrors:
    @pytest.mark.parametrize(
        "error",
        [requests.ConnectionError("reset"), OSError("net down"), httpx.ConnectError("x")],
    )
    def test_a_transport_error_stops_captions_but_the_run_finishes(
        self, config: h.HarvestConfig, error: Exception
    ) -> None:
        cfg = starved(config)
        captions = FakeCaptions({"vid-b": error})
        result, *_ = run(
            [target("a", 0.9), target("b", 0.5), target("c", 0.3)], cfg, captions=captions
        )
        assert captions.calls == ["vid-a", "vid-b"]  # c never attempted
        state = state_of(cfg)
        assert state.videos["vid-a"].status == "ok"
        assert state.videos["vid-b"].status == "pending"
        assert state.videos["vid-c"].status == "pending"
        assert type(error).__name__ in (state.videos["vid-b"].last_error or "")
        assert result.no_transcript == ["b", "c"]
        assert "vid-b" in render(result)


class TestGeminiNonCapacityErrors:
    @pytest.mark.parametrize(
        "error",
        [
            ValueError("Gemini returned zero usable segments"),
            ApiError(400, "bad", "INVALID_ARGUMENT"),
        ],
    )
    def test_other_errors_go_straight_to_captions_without_a_retry_timer(
        self, config: h.HarvestConfig, error: Exception
    ) -> None:
        gemini = FakeGemini({"vid-a": [error]})
        captions = FakeCaptions()
        run([target("a", 0.9)], config, gemini=gemini, captions=captions)
        vs = state_of(config).videos["vid-a"]
        assert captions.calls == ["vid-a"]
        assert vs.status == "ok" and vs.retry_after is None and vs.transient_failures == 0
        assert vs.fell_back_reason == "gemini_error"
        assert used(config) == 1  # the attempt did reach the API: counted

    def test_missing_key_routes_everything_to_captions_and_spends_nothing(
        self, config: h.HarvestConfig
    ) -> None:
        gemini, captions = FakeGemini(), FakeCaptions()
        run(
            [target("a", 0.9), target("b", 0.5)],
            config,
            gemini=gemini,
            captions=captions,
            gemini_available=lambda: False,
        )
        assert gemini.calls == []
        assert captions.calls == ["vid-a", "vid-b"]
        assert {v.fell_back_reason for v in state_of(config).videos.values()} == {
            "gemini_unavailable"
        }
        assert used(config) == 0

    def test_dry_run_reports_unavailable_gemini(self, config: h.HarvestConfig) -> None:
        result = h.plan_harvest(
            9,
            [target("a", 0.9)],
            FAR_DEADLINE,
            config,
            now=Clock().now,
            gemini_available=lambda: False,
        )
        assert result.rows[0].source == "captions (gemini_unavailable)"

    def test_client_failure_through_the_real_seam_spends_zero_budget(
        self, config: h.HarvestConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_oracle.ingest import gemini_transcripts as gt

        def no_key() -> None:
            raise RuntimeError("GEMINI_API_KEY is not set")

        monkeypatch.setattr(gt, "_client", no_key)
        monkeypatch.setattr(gt, "max_video_seconds", lambda: 3 * 3600)
        captions = FakeCaptions()
        h.run_harvest(
            9,
            [target("a", 0.9)],
            FAR_DEADLINE,
            config,
            now=Clock().now,
            sleep=lambda s: None,
            caption_reader=captions,
        )
        assert used(config) == 0
        assert state_of(config).videos["vid-a"].gemini_requests_used == 0
        assert state_of(config).videos["vid-a"].fell_back_reason == "gemini_error"
        assert captions.calls == ["vid-a"]


class TestSharedQuota:
    def test_two_gameweeks_on_the_same_pacific_day_share_one_budget(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 1}})
        run([target("a", 0.9)], cfg, gameweek=9)
        gemini, captions = FakeGemini(), FakeCaptions()
        run([target("b", 0.9)], cfg, gameweek=10, gemini=gemini, captions=captions)
        assert gemini.calls == []  # GW10 sees GW9's spend
        assert captions.calls == ["vid-b"]
        assert used(cfg) == 1
        assert sorted(p.name for p in cfg.state_dir.glob("*.json")) == [
            "10.json",
            "9.json",
            "gemini_usage.json",
        ]

    def test_gameweek_state_holds_no_counters(self, config: h.HarvestConfig) -> None:
        run([target("a", 0.9)], config)
        assert "gemini_usage" not in h.state_path(9, config.state_dir).read_text()


class TestRunLock:
    def test_a_held_lock_fails_fast_and_touches_nothing(self, config: h.HarvestConfig) -> None:
        gemini = FakeGemini()
        with h.run_lock(config.state_dir), pytest.raises(h.HarvestLockedError, match="another run"):
            run([target("a", 0.9)], config, gemini=gemini)
        assert gemini.calls == []
        assert not h.state_path(9, config.state_dir).exists()

    def test_the_lock_is_released_after_a_run(self, config: h.HarvestConfig) -> None:
        run([target("a", 0.9)], config)
        run([target("a", 0.9)], config)  # would raise if still held

    def test_unique_temp_files_do_not_collide(self, config: h.HarvestConfig) -> None:
        state = h.HarvestState(gameweek=9)
        h.save_state(state, config.state_dir)
        h.save_state(state, config.state_dir)
        assert list(config.state_dir.glob("*.tmp")) == []


class TestBackoffCounter:
    def test_first_503_after_other_failures_gets_the_30_minute_step(
        self, config: h.HarvestConfig
    ) -> None:
        # Day 1: daily 429, then captions find nothing -> failed (attempts = 2).
        run(
            [target("a", 0.9)],
            config,
            gemini=FakeGemini({"vid-a": [err429_daily()]}),
            captions=FakeCaptions({"vid-a": None}),
        )
        assert state_of(config).videos["vid-a"].status == "failed"
        # Day 2: the first 503 is still the FIRST transient failure.
        clock = Clock(T0 + timedelta(days=1))
        run([target("a", 0.9)], config, clock=clock, gemini=FakeGemini({"vid-a": [err503()]}))
        vs = state_of(config).videos["vid-a"]
        assert vs.transient_failures == 1
        assert vs.retry_after == clock.t + timedelta(minutes=30)

    def test_repeated_per_minute_429_uses_the_first_step_and_does_not_count(
        self, config: h.HarvestConfig
    ) -> None:
        gemini = FakeGemini({"vid-a": [err429_minute(), err429_minute()]})
        _, clock, _, _ = run([target("a", 0.9)], config, gemini=gemini)
        vs = state_of(config).videos["vid-a"]
        assert vs.transient_failures == 0
        assert vs.retry_after == clock.t + timedelta(minutes=30)


class TestStaleFieldsAreCleared:
    def test_gemini_success_clears_fell_back_reason(self, config: h.HarvestConfig) -> None:
        run(
            [target("a", 0.9)],
            config,
            gemini=FakeGemini({"vid-a": [err429_daily()]}),
            captions=FakeCaptions({"vid-a": None}),
        )
        assert state_of(config).videos["vid-a"].fell_back_reason == "gemini_daily_limit"
        run([target("a", 0.9)], config, clock=Clock(T0 + timedelta(days=1)))
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "ok" and vs.fell_back_reason is None

    def test_reset_to_pending_clears_reason_and_retry_timer(self, config: h.HarvestConfig) -> None:
        stale = h.HarvestState(gameweek=9)
        stale.videos["vid-a"] = h.VideoState(
            video_id="vid-a",
            creator_id="a",
            status="ok",  # but nothing in the transcript cache
            source="youtube-captions",
            retry_after=T0 + timedelta(hours=5),
            fell_back_reason="budget",
        )
        h.save_state(stale, config.state_dir)
        gemini = FakeGemini()
        run([target("a", 0.9)], config, gemini=gemini)
        assert gemini.calls == [("vid-a", MODEL)]  # not held back by the stale timer
        vs = state_of(config).videos["vid-a"]
        assert vs.fell_back_reason is None and vs.retry_after is None


class TestMidVideoBudgetExhaustion:
    def test_retries_that_eat_the_budget_abort_the_video_to_captions(
        self, config: h.HarvestConfig
    ) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 2}})
        # 2 windows (cost 2); window 1 needs a retry, so window 2 has no budget left
        gemini = FakeGemini({"vid-a": [err429_minute(), "ok"]})
        captions = FakeCaptions()
        run([target("a", 0.9, duration=3600)], cfg, gemini=gemini, captions=captions)
        vs = state_of(cfg).videos["vid-a"]
        assert used(cfg) == 2 and vs.gemini_requests_used == 2
        assert captions.calls == ["vid-a"]
        assert vs.fell_back_reason == "budget" and vs.status == "ok"


class FakeGoogleClient:
    """Stands in for google.genai.Client behind `gemini_transcripts._client`."""

    def __init__(self, script: list[object]) -> None:
        self.script = script
        self.models_called: list[str] = []
        client = self

        class _Resp:
            def __init__(self, parsed: object) -> None:
                self.parsed = parsed

        class _Models:
            def generate_content(self, **kwargs: object) -> _Resp:
                client.models_called.append(str(kwargs["model"]))
                outcome = client.script.pop(0) if client.script else "ok"
                if isinstance(outcome, Exception):
                    raise outcome
                from fpl_oracle.ingest import gemini_transcripts as gt

                return _Resp(
                    gt._WireTranscript(
                        language="en", segments=[gt._WireSegment(start_seconds=0, text="hi")]
                    )
                )

        self.models = _Models()


class TestRealSeamEndToEnd:
    def test_budget_error_from_the_gate_reaches_the_next_model_branch(
        self, config: h.HarvestConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_oracle.ingest import gemini_transcripts as gt

        fake = FakeGoogleClient([err429_minute()])  # first request 429s per-minute
        monkeypatch.setattr(gt, "_client", lambda: fake)
        monkeypatch.setattr(gt, "max_video_seconds", lambda: 3 * 3600)
        cfg = config.model_copy(update={"gemini_daily_limits": {MODEL: 2, "gemini-other": 5}})
        clock = Clock()
        h.run_harvest(
            9,
            [target("a", 0.9, duration=3600)],  # 2 windows
            FAR_DEADLINE,
            cfg,
            now=clock.now,
            sleep=clock.sleep,
            caption_reader=FakeCaptions(),
        )
        # MODEL: window 1 429 + retry (2 requests, budget gone); window 2's gate
        # raises BudgetExhaustedError through the real seam -> next model reads both.
        assert fake.models_called == [MODEL, MODEL, "gemini-other", "gemini-other"]
        assert 65.0 in clock.sleeps
        assert used(cfg) == 2 and used(cfg, model="gemini-other") == 2
        vs = state_of(cfg).videos["vid-a"]
        assert vs.status == "ok" and (vs.source or "").startswith("gemini:gemini-other")


class TestClassifyShapes:
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadTimeout("t"),
            httpx.ConnectError("c"),
            httpx.ReadError("r"),
            httpx.RemoteProtocolError("p"),
        ],
    )
    def test_httpx_errors_are_transient(self, error: Exception) -> None:
        assert h.classify_gemini_error(error) is h.GeminiFailure.TRANSIENT

    def test_real_quota_ids_decide_daily_versus_minute(self) -> None:
        daily = ApiError(
            429,
            "Quota exceeded: quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "RESOURCE_EXHAUSTED",
        )
        minute = ApiError(
            429,
            "Quota exceeded: quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "RESOURCE_EXHAUSTED",
        )
        assert h.classify_gemini_error(daily) is h.GeminiFailure.RATE_DAILY
        assert h.classify_gemini_error(minute) is h.GeminiFailure.RATE_MINUTE

    def test_a_400_is_other(self) -> None:
        assert h.classify_gemini_error(ApiError(400, "x", "INVALID_ARGUMENT")) is (
            h.GeminiFailure.OTHER
        )


class TestSharedVideoId:
    def test_two_creators_on_one_video_cost_one_fetch(self, config: h.HarvestConfig) -> None:
        targets = [
            target("a", 0.9, video_id="vid-shared"),
            target("b", 0.5, video_id="vid-shared"),
        ]
        gemini = FakeGemini()
        result, *_ = run(targets, config, gemini=gemini)
        assert gemini.calls == [("vid-shared", MODEL)]
        assert used(config) == 1
        assert result.no_transcript == []
        assert [r.status for r in result.rows] == ["ok", "ok"]
        assert result.weight_covered_fraction == pytest.approx(1.0)


class TestMainOffline:
    @pytest.fixture
    def fakes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        from fpl_oracle.extract import run_extract as re_
        from fpl_oracle.fpl import client as fpl_client
        from fpl_oracle.ingest import youtube_client as yc
        from fpl_oracle.ingest.youtube_client import VideoInfo
        from fpl_oracle.roster.models import Creator, Tier

        monkeypatch.chdir(tmp_path)  # default data/ paths land in tmp
        monkeypatch.setenv("COLUMNS", "250")  # keep rich from wrapping table cells
        seen: dict[str, object] = {}

        def resolve(gameweek: int | None = None, **kw: object) -> tuple[int, None]:
            seen["requested_gameweek"] = gameweek
            return (gameweek or 9, None)

        def select(gw: int, published_after: object, **kw: object) -> list[re_.VideoSelection]:
            seen["select_kwargs"] = kw
            creator = Creator(
                creator_id="c1",
                name="c1",
                youtube_hint="c1",
                tier=Tier.CORE,
                channel_id="UC1",
                weight=0.8,
            )
            video = VideoInfo(
                video_id="v1", channel_id="UC1", title="GW9 team", published_at=T0, description=""
            )
            return [re_.VideoSelection(creator=creator, video=video)]

        monkeypatch.setattr(re_, "resolve_target_gameweek", resolve)
        monkeypatch.setattr(re_, "select_videos", select)
        monkeypatch.setattr(
            fpl_client,
            "get_bootstrap_static",
            lambda force_refresh=False: {
                "events": [{"id": 9, "deadline_time": "2026-10-24T17:30:00Z"}]
            },
        )
        monkeypatch.setattr(yc, "get_video_durations", lambda ids: dict.fromkeys(ids, 3600))
        return seen

    def test_dry_run_with_the_default_gameweek_spends_and_writes_nothing(
        self, fakes: dict[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert h.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert fakes["requested_gameweek"] is None  # default = next gameweek
        assert fakes["select_kwargs"] == {"force_refresh": True}
        assert "GW9" in out and "DRY RUN" in out
        assert f"gemini:{MODEL} (2 requests)" in out
        assert not Path("data").exists()

    def test_cached_listings_flag_disables_the_forced_refresh(
        self, fakes: dict[str, object]
    ) -> None:
        h.main(["--dry-run", "--cached-listings"])
        assert fakes["select_kwargs"] == {"force_refresh": False}

    def test_held_lock_exits_nonzero_with_a_clear_message(
        self, fakes: dict[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with h.run_lock(h.DEFAULT_STATE_DIR):
            assert h.main(["--gameweek", "9"]) == 2
        assert "another run" in capsys.readouterr().err


# ---- second review fixes ----------------------------------------------------


class TestGeminiErrorDay:
    def test_a_bad_video_is_not_retried_on_gemini_the_same_pacific_day(
        self, config: h.HarvestConfig
    ) -> None:
        bad = ValueError("Gemini returned zero usable segments")
        run(
            [target("a", 0.9)],
            config,
            gemini=FakeGemini({"vid-a": [bad]}),
            captions=FakeCaptions({"vid-a": None}),
        )
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "failed" and vs.gemini_error_day == h.quota_day(T0)
        # later, same Pacific day: no Gemini request, but captions are tried again
        gemini, captions = FakeGemini(), FakeCaptions({"vid-a": None})
        run(
            [target("a", 0.9)],
            config,
            clock=Clock(T0 + timedelta(hours=3)),
            gemini=gemini,
            captions=captions,
        )
        assert gemini.calls == [] and captions.calls == ["vid-a"]
        assert used(config) == 1
        # next Pacific day: Gemini gets another chance
        gemini = FakeGemini()
        run([target("a", 0.9)], config, clock=Clock(T0 + timedelta(days=1)), gemini=gemini)
        assert gemini.calls == [("vid-a", MODEL)]

    @pytest.mark.parametrize(
        "error",
        [
            ApiError(400, "API key not valid. API_KEY_INVALID", "INVALID_ARGUMENT"),
            ApiError(401, "nope", ""),
            ApiError(403, "SERVICE_DISABLED: Generative Language API", "PERMISSION_DENIED"),
            ApiError(403, "Generative Language API has not been used in project 1", ""),
        ],
    )
    def test_a_key_level_error_disables_gemini_for_the_rest_of_the_run(
        self, config: h.HarvestConfig, error: Exception
    ) -> None:
        gemini = FakeGemini({"vid-a": [error]})
        captions = FakeCaptions({"vid-a": None})
        run(
            [target("a", 0.9), target("b", 0.5), target("c", 0.3)],
            config,
            gemini=gemini,
            captions=captions,
        )
        assert gemini.calls == [("vid-a", MODEL)]  # only the first video tried
        state = state_of(config)
        assert state.videos["vid-a"].fell_back_reason == "gemini_error"
        assert state.videos["vid-b"].fell_back_reason == "gemini_unavailable"
        assert state.videos["vid-c"].fell_back_reason == "gemini_unavailable"
        assert state.videos["vid-a"].gemini_error_day == h.quota_day(T0)
        # the triggering video is not retried on Gemini later the same Pacific day
        gemini2 = FakeGemini()
        run(
            [target("a", 0.9)],
            config,
            clock=Clock(T0 + timedelta(hours=2)),
            gemini=gemini2,
            captions=FakeCaptions({"vid-a": None}),
        )
        assert gemini2.calls == []
        # ...but tomorrow it is
        gemini3 = FakeGemini()
        run([target("a", 0.9)], config, clock=Clock(T0 + timedelta(days=1)), gemini=gemini3)
        assert gemini3.calls == [("vid-a", MODEL)]

    @pytest.mark.parametrize(
        "error", [ApiError(403, "video is private", "PERMISSION_DENIED"), ApiError(403, "x", "")]
    )
    def test_a_bare_403_is_a_per_video_error_and_gemini_stays_on(
        self, config: h.HarvestConfig, error: Exception
    ) -> None:
        gemini = FakeGemini({"vid-a": [error]})
        captions = FakeCaptions()
        run(
            [target("a", 0.9), target("b", 0.5), target("c", 0.3)],
            config,
            gemini=gemini,
            captions=captions,
        )
        assert gemini.calls == [("vid-a", MODEL), ("vid-b", MODEL), ("vid-c", MODEL)]
        assert captions.calls == ["vid-a"]
        state = state_of(config)
        assert state.videos["vid-a"].fell_back_reason == "gemini_error"
        assert state.videos["vid-a"].gemini_error_day == h.quota_day(T0)
        assert state.videos["vid-b"].source == f"gemini:{MODEL}"

    def test_error_day_is_the_day_the_request_was_sent(self, config: h.HarvestConfig) -> None:
        clock = Clock(datetime(2026, 10, 22, 6, 59, 58, tzinfo=UTC))  # 23:59:58 PDT, 21 Oct

        def reader(video_id, duration, /, *, model, before_request, retry_delay, sleep):
            before_request()
            clock.t += timedelta(seconds=10)  # past midnight Pacific
            raise ValueError("bad response")

        h.run_harvest(
            9,
            [target("a", 0.9)],
            FAR_DEADLINE,
            config,
            now=clock.now,
            sleep=clock.sleep,
            gemini_reader=reader,
            caption_reader=FakeCaptions({"vid-a": None}),
        )
        assert state_of(config).videos["vid-a"].gemini_error_day == "2026-10-21"


class TestUnexpectedCaptionErrors:
    def test_the_traceback_is_logged_so_our_own_bugs_stay_diagnosable(
        self, config: h.HarvestConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        captions = FakeCaptions({"vid-a": TypeError("our bug")})
        with caplog.at_level("WARNING", logger=h.logger.name):
            run([target("a", 0.9)], starved(config), captions=captions)
        records = [r for r in caplog.records if "failed unexpectedly" in r.getMessage()]
        assert len(records) == 1 and records[0].exc_info is not None
        assert state_of(config).videos["vid-a"].last_error == (
            "captions error: TypeError; left pending"
        )

    @pytest.mark.parametrize("error", [ParseError("no element found"), RuntimeError("weird")])
    def test_an_unexpected_error_skips_that_video_and_continues(
        self, config: h.HarvestConfig, error: Exception
    ) -> None:
        captions = FakeCaptions({"vid-b": error})
        result, *_ = run(
            [target("a", 0.9), target("b", 0.5), target("c", 0.3)],
            starved(config),
            captions=captions,
        )
        assert captions.calls == ["vid-a", "vid-b", "vid-c"]  # c still attempted
        state = state_of(config)
        assert state.videos["vid-b"].status == "pending"
        assert type(error).__name__ in (state.videos["vid-b"].last_error or "")
        assert state.videos["vid-a"].status == "ok" and state.videos["vid-c"].status == "ok"
        assert result.no_transcript == ["b"]


class TestExhaustedDayIsTheSentDay:
    def test_a_daily_429_after_midnight_marks_the_day_the_request_was_sent(
        self, config: h.HarvestConfig
    ) -> None:
        clock = Clock(datetime(2026, 10, 22, 6, 59, 58, tzinfo=UTC))  # 23:59:58 PDT, 21 Oct

        def reader(video_id, duration, /, *, model, before_request, retry_delay, sleep):
            before_request()
            clock.t += timedelta(seconds=10)  # now 00:00:08 PDT on the 22nd
            raise err429_daily()

        h.run_harvest(
            9,
            [target("a", 0.9)],
            FAR_DEADLINE,
            config,
            now=clock.now,
            sleep=clock.sleep,
            gemini_reader=reader,
            caption_reader=FakeCaptions(),
        )
        usage = h.load_usage(config.state_dir)
        assert usage.is_exhausted(MODEL, "2026-10-21")
        assert not usage.is_exhausted(MODEL, "2026-10-22")
        assert usage.used(MODEL, "2026-10-21") == 1


class TestFieldCleanup:
    def test_adopting_a_gemini_transcript_from_cache_clears_a_stale_reason(
        self, config: h.HarvestConfig
    ) -> None:
        stale = h.HarvestState(gameweek=9)
        stale.videos["vid-a"] = h.VideoState(
            video_id="vid-a", creator_id="a", status="pending", fell_back_reason="budget"
        )
        h.save_state(stale, config.state_dir)
        save_transcript(_transcript("vid-a", f"gemini:{MODEL}"), dir=config.transcript_dir)
        run([target("a", 0.9)], config)
        vs = state_of(config).videos["vid-a"]
        assert vs.status == "ok" and vs.fell_back_reason is None

    def test_adopting_a_captions_transcript_keeps_the_reason(self, config: h.HarvestConfig) -> None:
        stale = h.HarvestState(gameweek=9)
        stale.videos["vid-a"] = h.VideoState(
            video_id="vid-a", creator_id="a", status="pending", fell_back_reason="budget"
        )
        h.save_state(stale, config.state_dir)
        save_transcript(_transcript("vid-a", "youtube-captions"), dir=config.transcript_dir)
        run([target("a", 0.9)], config)
        assert state_of(config).videos["vid-a"].fell_back_reason == "budget"

    def test_reset_to_pending_clears_last_error_and_transient_failures(
        self, config: h.HarvestConfig
    ) -> None:
        stale = h.HarvestState(gameweek=9)
        stale.videos["vid-a"] = h.VideoState(
            video_id="vid-a",
            creator_id="a",
            status="failed",
            last_error="no English captions available",
            transient_failures=2,
            gemini_error_day="2026-10-22",  # NOT cleared: it gates Gemini for the day
        )
        h.save_state(stale, config.state_dir)
        run([target("a", 0.9)], config, captions=FakeCaptions({"vid-a": None}))
        vs = state_of(config).videos["vid-a"]
        assert vs.transient_failures == 0
        assert vs.gemini_error_day == "2026-10-22"
        assert vs.last_error == "no English captions available"  # re-set by this run's attempt


class TestBackstopKeepsFirstReason:
    def test_backstop_does_not_overwrite_an_existing_reason(self, config: h.HarvestConfig) -> None:
        # Budget gone -> captions (reason budget) hit a block -> pending; the
        # backstop pass then runs but must keep "budget".
        captions = FakeCaptions({"vid-a": requests.ConnectionError("down")})
        run(
            [target("a", 0.9)],
            starved(config),
            captions=captions,
            deadline=T0 + timedelta(hours=1),
        )
        assert state_of(config).videos["vid-a"].fell_back_reason == "budget"

    def test_backstop_sets_its_reason_when_none_exists(self, config: h.HarvestConfig) -> None:
        run(
            [target("a", 0.9)],
            config,
            gemini=FakeGemini({"vid-a": [err503()]}),
            deadline=T0 + timedelta(hours=1),
        )
        assert state_of(config).videos["vid-a"].fell_back_reason == "deadline_backstop"


class TestEmptyLimits:
    def test_no_configured_models_means_gemini_unavailable(self, config: h.HarvestConfig) -> None:
        cfg = config.model_copy(update={"gemini_daily_limits": {}})
        captions = FakeCaptions()
        run([target("a", 0.9)], cfg, captions=captions)
        assert state_of(cfg).videos["vid-a"].fell_back_reason == "gemini_unavailable"
