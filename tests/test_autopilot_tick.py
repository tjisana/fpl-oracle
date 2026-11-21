"""Autopilot tick end to end: fake clock, fake uploads, fake Gemini and
caption readers, tmp_path state and transcript dirs. No network."""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from youtube_transcript_api import IpBlocked

from fpl_oracle.ingest import harvest as h
from fpl_oracle.ingest import status as st
from fpl_oracle.ingest.autopilot import (
    AutopilotConfig,
    TickResult,
    apply_harvest_state,
    print_tick,
    tick,
    usage_total,
)
from fpl_oracle.ingest.autopilot_budget import BudgetPolicy
from fpl_oracle.ingest.ledger import GameweekDeadline, load_ledger
from fpl_oracle.ingest.transcripts import save_transcript
from fpl_oracle.roster.models import Creator
from tests.test_autopilot_units import FakeUploads, creator, vid
from tests.test_ingest_harvest import (
    MODEL,
    ApiError,
    FakeCaptions,
    FakeGemini,
    _transcript,
    err429_minute,
    err503,
)

D = datetime(2026, 11, 28, 17, 30, tzinfo=UTC)
DEADLINES = [
    GameweekDeadline(gameweek=11, deadline=D - timedelta(days=7)),
    GameweekDeadline(gameweek=12, deadline=D),
    GameweekDeadline(gameweek=13, deadline=D + timedelta(days=4)),
]
PROTECTED_NOW = D - timedelta(hours=20)  # Pacific day overlaps the final window
OPEN_NOW = D - timedelta(hours=90)  # well before it


def hrs(x: float) -> timedelta:
    return timedelta(hours=x)


def weighted(cid: str, channel: str, weight: float) -> Creator:
    return creator(cid, channel, weight=weight)


def run_tick(
    tmp_path: Path,
    now: datetime,
    uploads: FakeUploads,
    registry: list[Creator],
    *,
    gemini: FakeGemini | None = None,
    captions: FakeCaptions | None = None,
    captions_enabled: bool = True,
    dry_run: bool = False,
    limit: int = 18,
    reserve: int = 10,
    gemini_usable: bool = True,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> TickResult:
    config = AutopilotConfig(
        harvest=h.HarvestConfig(
            gemini_daily_limits={MODEL: limit},
            state_dir=tmp_path / "state",
            transcript_dir=tmp_path / "transcripts",
        ),
        policy=BudgetPolicy(reserve_requests=reserve),
    )
    return tick(
        clock=clock or (lambda: now),
        deadlines=DEADLINES,
        registry=registry,
        uploads=uploads,
        config=config,
        captions_enabled=captions_enabled,
        dry_run=dry_run,
        gemini_reader=gemini or FakeGemini(),
        caption_reader=captions or FakeCaptions(),
        gemini_available=lambda: gemini_usable,
        sleep=sleeper or (lambda _s: None),
    )


def simple_world(
    n: int,
    title: str = "GW12 Team Selection",
    published: datetime | None = None,
    duration: int = 900,
) -> tuple[list[Creator], FakeUploads]:
    """n creators on n channels, one video each, weights descending."""
    pub = published or (D - hrs(30))
    registry = [weighted(f"c{i}", f"UC{i}", 0.9 - i * 0.05) for i in range(n)]
    listings = {f"UC{i}": [vid(f"v{i}", f"UC{i}", title, pub)] for i in range(n)}
    durations = {f"v{i}": duration for i in range(n)}
    return registry, FakeUploads(listings, durations)


class TestTickHappyPath:
    def test_discovers_spends_in_weight_order_and_writes_the_ledger(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(3)
        gemini = FakeGemini()
        res = run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini)
        assert (res.outcome, res.gameweek, res.new_videos) == ("ran", 12, 3)
        assert [vid_ for vid_, _ in gemini.calls] == ["v0", "v1", "v2"]  # weight order
        assert res.requests_this_run == 3
        ledger = load_ledger(tmp_path / "state")
        assert {r.status for r in ledger.rows.values()} == {"ok"}
        assert ledger.rows["v0"].source == f"gemini:{MODEL}"
        assert ledger.last_scan_at == D - hrs(40)
        # the Phase 1 per-gameweek harvest state is fed too
        state = h.load_state(12, tmp_path / "state")
        assert {v.status for v in state.videos.values()} == {"ok"}
        assert h.load_usage(tmp_path / "state").used(MODEL, h.quota_day(D - hrs(40))) == 3

    def test_idempotent_second_tick_spends_nothing(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(2)
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry)
        gemini = FakeGemini()
        res = run_tick(tmp_path, now + hrs(4), uploads, registry, gemini=gemini)  # 3 h band: due
        assert res.outcome == "ran"
        assert gemini.calls == []
        assert res.requests_this_run == 0

    def test_not_due_does_nothing(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        now = D - hrs(100)
        run_tick(tmp_path, now, uploads, registry)
        listed_before = len(uploads.listed)
        res = run_tick(tmp_path, now + hrs(2), uploads, registry)
        assert res.outcome == "not_due"
        assert len(uploads.listed) == listed_before

    def test_cached_transcript_counts_as_done_without_gemini(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        save_transcript(_transcript("v0", "youtube-captions"), dir=tmp_path / "transcripts")
        gemini = FakeGemini()
        run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini)
        row = load_ledger(tmp_path / "state").rows["v0"]
        assert (row.status, row.source) == ("ok", "youtube-captions")
        assert gemini.calls == []

    def test_review_videos_are_never_spent_on(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1, title="GW11 Review")
        gemini = FakeGemini()
        run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini)
        assert gemini.calls == []
        assert load_ledger(tmp_path / "state").rows["v0"].status == "skipped"

    def test_season_over_is_a_noop(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        res = run_tick(tmp_path, D + timedelta(days=30), uploads, registry)
        assert res.outcome == "no_gameweek"
        assert uploads.listed == []

    def test_all_channels_failing_does_not_count_as_a_scan(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(2)
        uploads.failing = {"UC0", "UC1"}
        res = run_tick(tmp_path, D - hrs(40), uploads, registry)
        assert res.channels_failed == 2
        assert load_ledger(tmp_path / "state").last_scan_at is None


class TestLockContention:
    def test_tick_that_finds_the_lock_held_does_nothing(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        gemini = FakeGemini()
        with h.run_lock(tmp_path / "state"):
            res = run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini)
        assert res.outcome == "locked"
        assert uploads.listed == [] and gemini.calls == []
        assert not (tmp_path / "state" / "ledger.json").exists()

    def test_lock_is_released_afterwards(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        run_tick(tmp_path, D - hrs(40), uploads, registry)
        with h.run_lock(tmp_path / "state"):  # would raise if tick leaked the lock
            pass


class TestDryRun:
    def test_plans_but_writes_and_spends_nothing(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(3)
        gemini = FakeGemini()
        res = run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini, dry_run=True)
        assert res.outcome == "dry_run"
        assert [p.video_id for p in res.planned] == ["v0", "v1", "v2"]
        assert gemini.calls == []
        assert not (tmp_path / "state").exists()


class TestReserveInTick:
    def _plans(self, tmp_path: Path, now: datetime) -> tuple[TickResult, FakeGemini]:
        # 8 "plan" videos from heavy creators, 1 selection from a light one.
        pub = D - hrs(60)  # outside the 36 h window
        registry = [weighted(f"c{i}", f"UC{i}", 0.9 - i * 0.01) for i in range(8)]
        registry.append(weighted("light", "UCL", 0.3))
        listings = {f"UC{i}": [vid(f"p{i}", f"UC{i}", "GW12 captain picks", pub)] for i in range(8)}
        listings["UCL"] = [vid("sel", "UCL", "GW12 Team Selection", pub)]
        durations = {f"p{i}": 600 for i in range(8)} | {"sel": 600}
        gemini = FakeGemini()
        res = run_tick(
            tmp_path,
            now,
            FakeUploads(listings, durations),
            registry,
            gemini=gemini,
            limit=10,
            reserve=4,
        )
        return res, gemini

    def test_protected_day_holds_back_early_non_final_videos(self, tmp_path: Path) -> None:
        res, gemini = self._plans(tmp_path, PROTECTED_NOW)
        assert res.protected_day
        spent = [v for v, _ in gemini.calls]
        # cap for non-final = 10 - 4 = 6: six plans, then the selection (cap 10)
        assert spent == ["p0", "p1", "p2", "p3", "p4", "p5", "sel"]
        rows = load_ledger(tmp_path / "state").rows
        assert rows["p6"].status == "new" and rows["p7"].status == "new"
        assert rows["sel"].status == "ok"

    def test_unprotected_day_spends_freely(self, tmp_path: Path) -> None:
        res, gemini = self._plans(tmp_path, OPEN_NOW)
        assert not res.protected_day
        assert len(gemini.calls) == 9
        rows = load_ledger(tmp_path / "state").rows
        assert {r.status for r in rows.values()} == {"ok"}


class TestNoCaptionsMode:
    def test_gemini_error_stays_pending_and_never_touches_captions(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(2)
        gemini = FakeGemini({"v0": [ApiError(400, "bad request", "INVALID_ARGUMENT")]})
        captions = FakeCaptions()
        run_tick(
            tmp_path,
            D - hrs(40),
            uploads,
            registry,
            gemini=gemini,
            captions=captions,
            captions_enabled=False,
        )
        rows = load_ledger(tmp_path / "state").rows
        assert rows["v0"].status == "pending"  # S1: retried on Gemini the next quota day
        assert rows["v1"].status == "ok"
        assert captions.calls == []  # never touched

    def test_too_long_for_gemini_becomes_needs_captions(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1, duration=5 * 3600)
        gemini = FakeGemini()
        captions = FakeCaptions()
        run_tick(
            tmp_path,
            D - hrs(40),
            uploads,
            registry,
            gemini=gemini,
            captions=captions,
            captions_enabled=False,
        )
        assert load_ledger(tmp_path / "state").rows["v0"].status == "needs_captions"
        assert gemini.calls == [] and captions.calls == []

    def test_transient_503_stays_pending_with_a_retry_time(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        gemini = FakeGemini({"v0": [err503()]})
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        row = load_ledger(tmp_path / "state").rows["v0"]
        assert row.status == "pending"
        assert row.retry_after == now + timedelta(minutes=30)

    def test_needs_captions_rows_are_not_retried_without_captions(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1, duration=5 * 3600)
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, captions_enabled=False)
        captions = FakeCaptions()
        res = run_tick(
            tmp_path, now + hrs(4), uploads, registry, captions=captions, captions_enabled=False
        )
        assert res.outcomes == []
        assert captions.calls == []

    def test_a_captions_run_picks_up_needs_captions_rows(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1, duration=5 * 3600)
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, captions_enabled=False)
        captions = FakeCaptions()
        run_tick(tmp_path, now + hrs(4), uploads, registry, captions=captions)
        row = load_ledger(tmp_path / "state").rows["v0"]
        assert captions.calls == ["v0"]
        assert (row.status, row.source) == ("ok", "youtube-captions")

    def test_daily_limit_leaves_videos_pending_not_needs_captions(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(3)
        gemini = FakeGemini({"v0": [ApiError(429, "GenerateRequestsPerDay", "RESOURCE_EXHAUSTED")]})
        run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini, captions_enabled=False)
        rows = load_ledger(tmp_path / "state").rows
        assert {r.status for r in rows.values()} == {"pending"}


class TestCaptionsMode:
    def test_gemini_error_falls_back_to_captions(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        gemini = FakeGemini({"v0": [ApiError(400, "bad request", "INVALID_ARGUMENT")]})
        run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini)
        row = load_ledger(tmp_path / "state").rows["v0"]
        assert (row.status, row.source) == ("ok", "youtube-captions")

    def test_backstop_overrides_the_reserve_through_captions_only(self, tmp_path: Path) -> None:
        # protected day, 5 h to the deadline (inside the 6 h backstop); an
        # early plan video is over the reserve cap, so Gemini is not used,
        # but the backstop still covers it with captions.
        pub = D - hrs(60)
        registry = [weighted("c0", "UC0", 0.8)]
        uploads = FakeUploads({"UC0": [vid("p0", "UC0", "GW12 captain picks", pub)]}, {"p0": 600})
        gemini, captions = FakeGemini(), FakeCaptions()
        # used_today pre-seeded at the reserve cap
        state_dir = tmp_path / "state"
        usage = h.GeminiUsageFile()
        now = D - hrs(5)
        for _ in range(6):
            usage.add_request(MODEL, h.quota_day(now))
        h.save_usage(usage, state_dir)
        run_tick(
            tmp_path, now, uploads, registry, gemini=gemini, captions=captions, limit=10, reserve=4
        )
        assert gemini.calls == []
        assert captions.calls == ["p0"]
        # recorded as a budget hold, not as "Gemini unavailable"
        assert h.load_state(12, state_dir).videos["p0"].fell_back_reason == "budget"


class TestApplyHarvestState:
    def test_ok(self) -> None:
        from tests.test_autopilot_units import row

        r = row("a", status="pending")
        vs = h.VideoState(video_id="a", creator_id="c1", status="ok", source="gemini:x")
        apply_harvest_state(r, vs, captions_enabled=False, now=D)
        assert (r.status, r.source) == ("ok", "gemini:x")

    def test_stale_retry_after_is_cleared(self) -> None:
        from tests.test_autopilot_units import row

        r = row("a", status="pending")
        vs = h.VideoState(
            video_id="a", creator_id="c1", retry_after=D - hrs(1), fell_back_reason="too_long"
        )
        apply_harvest_state(r, vs, captions_enabled=False, now=D)
        assert (r.status, r.retry_after) == ("needs_captions", None)


ERR401 = ApiError(401, "API key not valid", "API_KEY_INVALID")


def used_total(tmp_path: Path) -> int:
    return usage_total(h.load_usage(tmp_path / "state"))


class TestKeyLevelFailure:
    def test_401_stops_all_gemini_spending_for_the_tick(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(3)
        gemini = FakeGemini({"v0": [ERR401]})
        captions = FakeCaptions()
        res = run_tick(
            tmp_path,
            D - hrs(40),
            uploads,
            registry,
            gemini=gemini,
            captions=captions,
            captions_enabled=False,
        )
        assert gemini.calls == [("v0", MODEL)]
        assert used_total(tmp_path) == 1  # only the request that actually failed
        assert res.requests_this_run == 1
        rows = load_ledger(tmp_path / "state").rows
        assert rows["v0"].status == "pending"  # not needs_captions
        assert rows["v1"].status == "new" and rows["v2"].status == "new"  # untouched
        assert captions.calls == []

    def test_401_in_captions_mode_sends_the_rest_to_captions_without_gemini(
        self, tmp_path: Path
    ) -> None:
        registry, uploads = simple_world(3)
        gemini = FakeGemini({"v0": [ERR401]})
        captions = FakeCaptions()
        run_tick(tmp_path, D - hrs(40), uploads, registry, gemini=gemini, captions=captions)
        assert gemini.calls == [("v0", MODEL)]
        assert used_total(tmp_path) == 1
        assert captions.calls == ["v0", "v1", "v2"]

    def test_missing_key_without_captions_counts_nothing_and_stops(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(3)
        gemini = FakeGemini()
        res = run_tick(
            tmp_path,
            D - hrs(40),
            uploads,
            registry,
            gemini=gemini,
            captions_enabled=False,
            gemini_usable=False,
        )
        assert gemini.calls == [] and used_total(tmp_path) == 0
        assert len(res.outcomes) == 1  # the loop stopped after the first video
        rows = load_ledger(tmp_path / "state").rows
        assert rows["v0"].status == "pending"
        assert rows["v1"].status == "new" and rows["v2"].status == "new"


class TestGeminiErrorRetriedNextDay:
    def test_error_row_is_workable_again_on_a_later_quota_day(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(1)
        gemini = FakeGemini({"v0": [ApiError(400, "bad request", "INVALID_ARGUMENT")]})
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        assert load_ledger(tmp_path / "state").rows["v0"].status == "pending"
        assert len(gemini.calls) == 1
        # same quota day: not retried on Gemini (Phase 1 semantics)
        run_tick(tmp_path, now + hrs(4), uploads, registry, gemini=gemini, captions_enabled=False)
        assert len(gemini.calls) == 1
        # next quota day: Gemini is tried again and succeeds
        run_tick(tmp_path, now + hrs(26), uploads, registry, gemini=gemini, captions_enabled=False)
        assert len(gemini.calls) == 2
        assert load_ledger(tmp_path / "state").rows["v0"].status == "ok"


class TestCaptionsCircuitBreaker:
    def test_ip_block_stops_caption_attempts_for_the_rest_of_the_tick(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(4)
        captions = FakeCaptions({"v0": IpBlocked("v0")})
        res = run_tick(
            tmp_path,
            D - hrs(40),
            uploads,
            registry,
            captions=captions,
            gemini_usable=False,  # everything goes to captions
        )
        assert captions.calls == ["v0"]
        rows = load_ledger(tmp_path / "state").rows
        assert {r.status for r in rows.values()} == {"pending"}
        assert len(res.outcomes) == 4


class TestReserveAllowanceAndUnknownDuration:
    def test_unknown_duration_is_left_alone_on_a_protected_day(self, tmp_path: Path) -> None:
        registry = [weighted("c0", "UC0", 0.8)]
        pub = D - hrs(60)
        uploads = FakeUploads({"UC0": [vid("p0", "UC0", "GW12 captain picks", pub)]}, {})
        gemini, captions = FakeGemini(), FakeCaptions()
        run_tick(
            tmp_path,
            PROTECTED_NOW,
            uploads,
            registry,
            gemini=gemini,
            captions=captions,
            limit=10,
            reserve=4,
        )
        assert gemini.calls == [] and captions.calls == []
        row = load_ledger(tmp_path / "state").rows["p0"]
        assert (row.status, row.duration_s) == ("new", None)
        # a later scan that learns the duration lets it be picked up
        uploads.duration_map["p0"] = 600
        run_tick(
            tmp_path,
            PROTECTED_NOW + hrs(4),
            uploads,
            registry,
            gemini=gemini,
            limit=10,
            reserve=4,
        )
        assert gemini.calls == [("p0", MODEL)]

    def test_a_429_retry_cannot_overshoot_the_reserve_cap(self, tmp_path: Path) -> None:
        pub = D - hrs(60)
        registry = [weighted("c0", "UC0", 0.8)]
        uploads = FakeUploads({"UC0": [vid("p0", "UC0", "GW12 captain picks", pub)]}, {"p0": 600})
        usage = h.GeminiUsageFile()
        for _ in range(5):  # cap = 10 - 4 = 6, so exactly one request is allowed
            usage.add_request(MODEL, h.quota_day(PROTECTED_NOW))
        h.save_usage(usage, tmp_path / "state")
        gemini = FakeGemini({"p0": [err429_minute()]})
        captions = FakeCaptions()
        run_tick(
            tmp_path,
            PROTECTED_NOW,
            uploads,
            registry,
            gemini=gemini,
            captions=captions,
            limit=10,
            reserve=4,
        )
        assert used_total(tmp_path) == 6  # NOT 7: the in-run retry was refused
        assert load_ledger(tmp_path / "state").rows["p0"].status == "pending"
        assert captions.calls == []  # a cap is not a reason to fall back


class TestLowCoverage:
    def _world(self, duration: int) -> tuple[list[Creator], FakeUploads]:
        return simple_world(2, duration=duration)

    def test_flag_set_inside_the_backstop_window_when_nothing_is_covered(
        self, tmp_path: Path
    ) -> None:
        registry, uploads = self._world(5 * 3600)  # too long for Gemini -> needs_captions
        res = run_tick(tmp_path, D - hrs(3), uploads, registry, captions_enabled=False, reserve=0)
        assert res.low_coverage and res.weight_covered_fraction == 0
        readiness = st.ReadinessReport.model_validate_json(
            (tmp_path / "state" / "readiness.json").read_text()
        )
        assert readiness.low_coverage
        buf = io.StringIO()
        print_tick(res, Console(file=buf, width=200, color_system=None))
        assert "LOW COVERAGE" in buf.getvalue()

    def test_not_flagged_outside_the_window(self, tmp_path: Path) -> None:
        registry, uploads = self._world(5 * 3600)
        res = run_tick(tmp_path, D - hrs(40), uploads, registry, captions_enabled=False)
        assert not res.low_coverage

    def test_not_flagged_when_covered_or_in_captions_mode(self, tmp_path: Path) -> None:
        registry, uploads = self._world(900)
        res = run_tick(tmp_path, D - hrs(3), uploads, registry, captions_enabled=False)
        assert not res.low_coverage and res.weight_covered_fraction == 1.0
        registry2, uploads2 = self._world(5 * 3600)
        res2 = run_tick(tmp_path / "x", D - hrs(3), uploads2, registry2, captions_enabled=True)
        assert not res2.low_coverage


class TestScanVersusSpend:
    def test_a_503_at_the_daily_scan_is_retried_by_the_next_hourly_tick(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 11, 24, 8, 10, tzinfo=UTC)  # far band, the 08:10 scan
        registry, uploads = simple_world(1, published=now - hrs(3))
        gemini = FakeGemini({"v0": [err503()]})
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        assert load_ledger(tmp_path / "state").rows["v0"].status == "pending"
        listed = len(uploads.listed)
        res = run_tick(
            tmp_path, now + hrs(1), uploads, registry, gemini=gemini, captions_enabled=False
        )
        assert res.outcome == "ran" and not res.scan_due
        assert len(uploads.listed) == listed  # no new scan
        assert load_ledger(tmp_path / "state").rows["v0"].status == "ok"

    def test_tick_with_nothing_to_retry_stays_not_due(self, tmp_path: Path) -> None:
        now = datetime(2026, 11, 24, 8, 10, tzinfo=UTC)
        registry, uploads = simple_world(1, published=now - hrs(3))
        run_tick(tmp_path, now, uploads, registry)
        res = run_tick(tmp_path, now + hrs(1), uploads, registry)
        assert res.outcome == "not_due"

    def test_retry_timer_still_blocks_an_early_tick(self, tmp_path: Path) -> None:
        now = datetime(2026, 11, 24, 8, 10, tzinfo=UTC)
        registry, uploads = simple_world(1, published=now - hrs(3))
        gemini = FakeGemini({"v0": [err503()]})
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        res = run_tick(
            tmp_path, now + timedelta(minutes=10), uploads, registry, captions_enabled=False
        )
        assert res.outcome == "not_due"


class TestRequestsAcrossMidnight:
    def test_usage_total_sums_all_days(self) -> None:
        usage = h.GeminiUsageFile()
        usage.add_request(MODEL, "2026-11-26")
        usage.add_request(MODEL, "2026-11-27")
        usage.add_request("other", "2026-11-27")
        assert usage_total(usage) == 3

    def test_tick_crossing_pacific_midnight_reports_the_true_spend(self, tmp_path: Path) -> None:
        start = datetime(2026, 11, 27, 7, 59, tzinfo=UTC)  # 23:59 Pacific
        state = {"t": start}

        def clock() -> datetime:
            state["t"] = state["t"] + timedelta(seconds=5)
            return state["t"]

        registry, uploads = simple_world(3, published=start - hrs(3))
        res = run_tick(tmp_path, start, uploads, registry, clock=clock)
        assert res.requests_this_run == 3
        usage = h.load_usage(tmp_path / "state")
        assert len(usage.models[MODEL].requests) == 2  # it really did span two days


class TestDryRunWhenNotDue:
    def test_shows_what_would_be_planned_and_writes_nothing(self, tmp_path: Path) -> None:
        now = datetime(2026, 11, 24, 8, 10, tzinfo=UTC)
        registry, uploads = simple_world(1, published=now - hrs(3))
        gemini = FakeGemini({"v0": [err503()]})
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        ledger_file = tmp_path / "state" / "ledger.json"
        before = ledger_file.read_text()
        res = run_tick(
            tmp_path,
            now + timedelta(minutes=45),
            uploads,
            registry,
            dry_run=True,
            captions_enabled=False,
        )
        # 45 min: scan not due and retry expired, still shown as a dry-run plan
        assert res.outcome == "dry_run" and not res.scan_due
        assert [p.video_id for p in res.planned] == ["v0"]
        assert ledger_file.read_text() == before
        buf = io.StringIO()
        print_tick(res, Console(file=buf, width=200, color_system=None))
        assert "WOULD" in buf.getvalue()


class TestCaptionPacingAcrossVideos:
    def test_the_gap_between_caption_scrapes_applies_across_videos(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(3)
        captions = FakeCaptions()
        sleeps: list[float] = []
        run_tick(
            tmp_path,
            D - hrs(40),
            uploads,
            registry,
            captions=captions,
            gemini_usable=False,
            sleeper=sleeps.append,
        )
        assert captions.calls == ["v0", "v1", "v2"]
        assert sleeps == [h.CAPTION_PACING_SECONDS] * 2


class TestKeyFailureMarker:
    def test_a_bad_key_is_not_retried_for_the_rest_of_the_quota_day(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(2)
        gemini = FakeGemini({"v0": [ERR401]})
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        assert used_total(tmp_path) == 1
        usage = h.load_usage(tmp_path / "state")
        assert usage.key_failed(MODEL, h.quota_day(now))
        # tick 2, same quota day: zero requests, rows stay pending
        res = run_tick(
            tmp_path, now + hrs(4), uploads, registry, gemini=gemini, captions_enabled=False
        )
        assert used_total(tmp_path) == 1 and len(gemini.calls) == 1
        assert res.requests_this_run == 0
        assert load_ledger(tmp_path / "state").rows["v0"].status == "pending"

    def test_next_quota_day_tries_again_and_a_success_clears_the_marker(
        self, tmp_path: Path
    ) -> None:
        registry, uploads = simple_world(1)
        gemini = FakeGemini({"v0": [ERR401]})
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, gemini=gemini, captions_enabled=False)
        # a day later, a nul-error world: the request goes out again and works
        run_tick(tmp_path, now + hrs(26), uploads, registry, gemini=gemini, captions_enabled=False)
        assert len(gemini.calls) == 2
        usage = h.load_usage(tmp_path / "state")
        assert not usage.models[MODEL].key_failed_days
        assert load_ledger(tmp_path / "state").rows["v0"].status == "ok"

    def test_old_usage_files_load_without_the_field(self) -> None:
        usage = h.GeminiUsageFile.model_validate_json(
            '{"models": {"m": {"requests": {"2026-11-01": 2}, "exhausted_days": []}}}'
        )
        assert usage.models["m"].key_failed_days == []


class TestLostTranscriptIsRecovered:
    def test_ok_row_without_its_cache_file_goes_back_to_pending_and_refetches(
        self, tmp_path: Path
    ) -> None:
        registry, uploads = simple_world(1)
        gemini = FakeGemini()
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry, gemini=gemini)
        (tmp_path / "transcripts" / "v0.json").unlink()
        run_tick(tmp_path, now + hrs(4), uploads, registry, gemini=gemini)
        assert len(gemini.calls) == 2
        assert load_ledger(tmp_path / "state").rows["v0"].status == "ok"

    def test_lost_transcript_is_not_reported_ok_when_it_cannot_be_refetched(
        self, tmp_path: Path
    ) -> None:
        registry, uploads = simple_world(1)
        now = D - hrs(40)
        run_tick(tmp_path, now, uploads, registry)
        (tmp_path / "transcripts" / "v0.json").unlink()
        run_tick(
            tmp_path, now + hrs(4), uploads, registry, captions_enabled=False, gemini_usable=False
        )
        assert load_ledger(tmp_path / "state").rows["v0"].status == "pending"


class TestDryRunSeesTheCache:
    def test_plan_excludes_cached_videos_and_nothing_is_written(self, tmp_path: Path) -> None:
        registry, uploads = simple_world(2)
        save_transcript(_transcript("v0", "gemini:x"), dir=tmp_path / "transcripts")
        res = run_tick(tmp_path, D - hrs(40), uploads, registry, dry_run=True)
        assert [p.video_id for p in res.planned] == ["v1"]
        assert not (tmp_path / "state").exists()


class TestNowFlag:
    def test_now_without_dry_run_is_refused(self) -> None:
        import pytest

        from fpl_oracle.ingest.autopilot import main

        with pytest.raises(SystemExit) as exc:
            main(["tick", "--now", "2030-01-01T00:00:00"])
        assert exc.value.code == 2
