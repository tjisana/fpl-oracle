"""Autopilot M1 unit tests: cadence, ledger, classifier, budget policy,
discovery. Fully offline, fake clocks and fake uploads clients."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from fpl_oracle.ingest import ledger as L
from fpl_oracle.ingest.autopilot_budget import (
    BudgetPolicy,
    daily_cap,
    demoted_video_ids,
    is_protected_day,
    may_spend,
    order_candidates,
    plan_spend,
    quota_day_bounds,
)
from fpl_oracle.ingest.cadence import is_due, latest_daily_anchor, next_gameweek
from fpl_oracle.ingest.classify import RegexClassifier, VideoKind, priority_multiplier
from fpl_oracle.ingest.discovery import SHORT_MARK, discover
from fpl_oracle.ingest.youtube_client import VideoInfo, YouTubeApiError
from fpl_oracle.roster.models import Creator, Tier

# GW12 deadline: Sat 28 Nov 2026 17:30 UTC (09:30 Pacific). GW11 a week earlier.
D = datetime(2026, 11, 28, 17, 30, tzinfo=UTC)
D11 = D - timedelta(days=7)
DEADLINES = [
    L.GameweekDeadline(gameweek=11, deadline=D11),
    L.GameweekDeadline(gameweek=12, deadline=D),
    L.GameweekDeadline(gameweek=13, deadline=D + timedelta(days=4)),
]


def hrs(h: float) -> timedelta:
    return timedelta(hours=h)


# --------------------------------------------------------------------------
# Cadence
# --------------------------------------------------------------------------


class TestCadence:
    def test_never_scanned_is_always_due(self) -> None:
        for h in (200, 100, 50, 3, -5):
            assert is_due(D - hrs(h), D, None)

    def test_daily_anchor(self) -> None:
        assert latest_daily_anchor(datetime(2026, 11, 24, 8, 9, tzinfo=UTC)) == datetime(
            2026, 11, 23, 8, 10, tzinfo=UTC
        )
        assert latest_daily_anchor(datetime(2026, 11, 24, 8, 10, tzinfo=UTC)) == datetime(
            2026, 11, 24, 8, 10, tzinfo=UTC
        )

    def test_far_band_scans_once_a_day_after_the_0810_anchor(self) -> None:
        now = D - hrs(100)  # 24 Nov 13:30 UTC, anchor 08:10 the same day
        assert not is_due(now, D, datetime(2026, 11, 24, 8, 11, tzinfo=UTC))
        assert is_due(now, D, datetime(2026, 11, 23, 8, 11, tzinfo=UTC))

    def test_far_band_anchor_boundary(self) -> None:
        last = datetime(2026, 11, 23, 8, 11, tzinfo=UTC)
        assert not is_due(datetime(2026, 11, 24, 8, 9, tzinfo=UTC), D, last)
        assert is_due(datetime(2026, 11, 24, 8, 10, tzinfo=UTC), D, last)

    def test_far_band_ignores_short_intervals(self) -> None:
        now = D - hrs(72) - timedelta(minutes=1)  # still the daily band
        assert not is_due(now, D, now - hrs(5))

    def test_72h_is_the_start_of_the_three_hour_band(self) -> None:
        now = D - hrs(72)  # 25 Nov 17:30
        assert is_due(now, D, now - hrs(3))
        assert is_due(now, D, now - timedelta(hours=2, minutes=55))  # slack boundary
        assert not is_due(now, D, now - timedelta(hours=2, minutes=54))
        assert not is_due(now, D, now - hrs(1))

    def test_mid_band_runs_the_daily_scan_too(self) -> None:
        now = datetime(2026, 11, 26, 8, 10, tzinfo=UTC)  # ~57 h to go
        assert is_due(now, D, datetime(2026, 11, 25, 8, 12, tzinfo=UTC))
        # an hour after a scan at 08:10 the 3 h rule is not met, anchor is done
        assert not is_due(now + hrs(1), D, now)

    def test_6h_is_the_start_of_the_hourly_band(self) -> None:
        now = D - hrs(6)  # 11:30
        assert is_due(now, D, now - hrs(1))
        assert is_due(now, D, now - timedelta(minutes=55))  # slack boundary
        assert not is_due(now, D, now - timedelta(minutes=54))

    def test_just_over_6h_is_still_the_three_hour_band(self) -> None:
        now = D - hrs(6) - timedelta(minutes=1)
        assert not is_due(now, D, now - hrs(1))
        assert is_due(now, D, now - hrs(3))

    def test_deadline_instant_is_hourly_and_after_it_daily(self) -> None:
        assert is_due(D, D, D - hrs(1))
        after = D + timedelta(minutes=1)
        assert not is_due(after, D, D - hrs(1))  # < 0: daily only, today's scan done
        assert is_due(after, D, datetime(2026, 11, 27, 8, 0, tzinfo=UTC))  # anchor passed

    def test_custom_slack(self) -> None:
        now = D - hrs(10)
        last = now - hrs(3) + timedelta(minutes=2)
        assert not is_due(now, D, last, slack=timedelta(0))
        assert is_due(now, D, last, slack=timedelta(minutes=2))

    def test_next_gameweek(self) -> None:
        assert _next_gw(D - hrs(1)) == 12
        # at the deadline instant it is no longer "in the future"
        assert _next_gw(D) == 13
        assert _next_gw(D + timedelta(days=10)) is None
        assert _next_gw(D11 - hrs(1)) == 11

    def test_next_gameweek_is_order_independent(self) -> None:
        got = next_gameweek(list(reversed(DEADLINES)), D - hrs(1))
        assert got is not None and got.gameweek == 12


def _next_gw(now: datetime) -> int | None:
    got = next_gameweek(DEADLINES, now)
    return None if got is None else got.gameweek


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


def row(
    vid: str,
    creator: str = "c1",
    kind: VideoKind = "selection",
    published: datetime | None = None,
    status: L.LedgerStatus = "new",
    duration: int | None = 900,
    gameweek: int | None = 12,
    title: str | None = None,
) -> L.LedgerRow:
    return L.LedgerRow(
        video_id=vid,
        creator_id=creator,
        title=title or f"title {vid}",
        published_at=published or (D - hrs(30)),
        duration_s=duration,
        gameweek=gameweek,
        kind=kind,
        first_seen_at=D - hrs(29),
        status=status,
    )


class TestGameweekAssignment:
    def test_just_before_a_deadline_belongs_to_it(self) -> None:
        assert L.assign_gameweek(D - timedelta(seconds=1), DEADLINES) == 12

    def test_exactly_at_the_deadline_belongs_to_the_next(self) -> None:
        assert L.assign_gameweek(D, DEADLINES) == 13

    def test_just_after_belongs_to_the_next(self) -> None:
        assert L.assign_gameweek(D + timedelta(seconds=1), DEADLINES) == 13

    def test_after_the_last_deadline_is_none(self) -> None:
        assert L.assign_gameweek(D + timedelta(days=30), DEADLINES) is None

    def test_before_the_first_deadline(self) -> None:
        assert L.assign_gameweek(D11 - timedelta(days=30), list(reversed(DEADLINES))) == 11

    def test_deadlines_from_events(self) -> None:
        events = [
            {"id": 2, "deadline_time": "2026-11-28T17:30:00Z"},
            {"id": 1, "deadline_time": "2026-11-21T11:00:00Z"},
            {"id": 3, "deadline_time": None},
        ]
        got = L.deadlines_from_events(events)
        assert [d.gameweek for d in got] == [1, 2]
        assert got[1].deadline == datetime(2026, 11, 28, 17, 30, tzinfo=UTC)


class TestLedgerFile:
    def test_round_trip(self, tmp_path: Path) -> None:
        ledger = L.Ledger(last_scan_at=D, updated_at=D)
        ledger.rows["a"] = row("a")
        ledger.rows["b"] = row("b", status="ok")
        ledger.rows["b"].source = "gemini:gemini-3.5-flash"
        path = L.save_ledger(ledger, tmp_path)
        assert path == tmp_path / "ledger.json"
        assert L.load_ledger(tmp_path) == ledger

    def test_missing_file_is_an_empty_ledger(self, tmp_path: Path) -> None:
        assert L.load_ledger(tmp_path) == L.Ledger()

    def test_unreadable_file_raises(self, tmp_path: Path) -> None:
        (tmp_path / "ledger.json").write_text("{not json")
        with pytest.raises(RuntimeError, match="unreadable"):
            L.load_ledger(tmp_path)

    def test_write_is_atomic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        old = L.Ledger()
        old.rows["a"] = row("a")
        L.save_ledger(old, tmp_path)
        new = old.model_copy(deep=True)
        new.rows["b"] = row("b")

        def boom(src: object, dst: object) -> None:
            raise OSError("disk gone")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="disk gone"):
            L.save_ledger(new, tmp_path)
        monkeypatch.undo()
        assert L.load_ledger(tmp_path) == old  # old file intact
        assert [p.name for p in tmp_path.iterdir()] == ["ledger.json"]  # no temp litter

    def test_for_gameweek(self) -> None:
        ledger = L.Ledger()
        ledger.rows["a"] = row("a", gameweek=12)
        ledger.rows["b"] = row("b", gameweek=11)
        assert [r.video_id for r in ledger.for_gameweek(12)] == ["a"]


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("GW12 TEAM SELECTION | Final Thoughts", "final"),
        ("My FINAL DECISIONS for Gameweek 12", "final"),
        ("GW12 Team Locked In", "final"),
        ("GW12 Team Selection", "selection"),
        ("FPL GW12: Team Reveal", "selection"),
        ("GW11 Review + GW12 Team Selection", "selection"),
        ("GW11 Review and Rank Update", "review"),
        ("GW12 Recap", "review"),
        ("Reacting to GW12: Live Q&A", "live"),
        ("FPL Live Stream", "live"),
        ("The FPL Podcast Episode 44", "show"),
        ("FPL Pod - GW12 preview", "show"),
        ("Free Hit Draft", "plan"),
        ("Wildcard Template", "plan"),
        ("GW12 Captain Picks", "plan"),
        ("GW12 Transfer Plans", "transfers"),
        ("Best transfers this week", "transfers"),
        ("I tried a new training routine", "other"),
        ("Tottenham tactics explained", "other"),
    ],
)
def test_regex_classifier(title: str, kind: str) -> None:
    assert RegexClassifier().classify(title) == kind


def test_multipliers() -> None:
    assert {k: priority_multiplier(k) for k in ("selection", "final")} == {
        "selection": 1.0,
        "final": 1.0,
    }
    assert priority_multiplier("transfers") == priority_multiplier("plan") == 0.6
    assert priority_multiplier("other") == 0.3
    assert all(priority_multiplier(k) == 0 for k in ("review", "live", "show"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Budget policy
# --------------------------------------------------------------------------

POLICY = BudgetPolicy()  # 36 h, reserve 10


class TestQuotaDay:
    def test_bounds_are_pacific_midnights(self) -> None:
        start, end = quota_day_bounds(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))
        assert start == datetime(2026, 11, 26, 8, 0, tzinfo=UTC)  # PST: UTC-8
        assert end == datetime(2026, 11, 27, 8, 0, tzinfo=UTC)

    def test_bounds_in_daylight_time(self) -> None:
        start, _ = quota_day_bounds(datetime(2026, 7, 1, 15, 0, tzinfo=UTC))
        assert start == datetime(2026, 7, 1, 7, 0, tzinfo=UTC)  # PDT: UTC-7


class TestProtectedDay:
    # Window = 27 Nov 05:30 UTC .. 28 Nov 17:30 UTC.
    @pytest.mark.parametrize(
        ("now", "protected"),
        [
            (datetime(2026, 11, 25, 15, 0, tzinfo=UTC), False),  # Pacific 25th: ends 26th 08:00
            (datetime(2026, 11, 26, 7, 59, tzinfo=UTC), False),  # last minute of that day
            (datetime(2026, 11, 26, 8, 0, tzinfo=UTC), True),  # Pacific 26th ends 27th 08:00
            (datetime(2026, 11, 27, 20, 0, tzinfo=UTC), True),
            (datetime(2026, 11, 28, 17, 29, tzinfo=UTC), True),  # contains the deadline
            (datetime(2026, 11, 29, 8, 0, tzinfo=UTC), False),  # day starts after the deadline
        ],
    )
    def test_overlap(self, now: datetime, protected: bool) -> None:
        assert is_protected_day(now, D, POLICY) is protected

    def test_no_deadline_never_protected(self) -> None:
        assert not is_protected_day(D, None, POLICY)

    def test_window_is_a_parameter(self) -> None:
        now = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
        assert is_protected_day(now, D, BudgetPolicy(final_window_hours=72))


class TestReserveRule:
    PROTECTED = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
    OPEN = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)

    def test_protected_day_caps_early_non_final_videos(self) -> None:
        early_plan = row("p", kind="plan", published=D - hrs(100))
        assert daily_cap(early_plan, self.PROTECTED, D, 18, POLICY) == 8

    def test_protected_day_full_limit_for_final_class_or_window(self) -> None:
        early_selection = row("s", kind="selection", published=D - hrs(100))
        late_plan = row("p", kind="plan", published=D - hrs(10))
        assert daily_cap(early_selection, self.PROTECTED, D, 18, POLICY) == 18
        assert daily_cap(late_plan, self.PROTECTED, D, 18, POLICY) == 18

    def test_window_edge_is_inclusive(self) -> None:
        edge = row("e", kind="plan", published=D - hrs(36))
        just_before = row("f", kind="plan", published=D - hrs(36) - timedelta(seconds=1))
        assert daily_cap(edge, self.PROTECTED, D, 18, POLICY) == 18
        assert daily_cap(just_before, self.PROTECTED, D, 18, POLICY) == 8

    def test_unprotected_day_spends_freely(self) -> None:
        early_plan = row("p", kind="plan", published=D - hrs(100))
        assert daily_cap(early_plan, self.OPEN, D, 18, POLICY) == 18

    def test_may_spend_respects_the_cap(self) -> None:
        early_plan = row("p", kind="plan", published=D - hrs(100))
        final = row("s", kind="final", published=D - hrs(100))
        kw = {"now": self.PROTECTED, "deadline": D, "limit": 18, "policy": POLICY}
        assert may_spend(early_plan, 2, used_today=6, **kw)  # 8 <= 8
        assert not may_spend(early_plan, 2, used_today=7, **kw)
        assert may_spend(final, 2, used_today=7, **kw)
        assert may_spend(final, 2, used_today=16, **kw)  # 18 <= 18
        assert not may_spend(final, 2, used_today=17, **kw)

    def test_video_never_sent_to_gemini_spends_nothing(self) -> None:
        early_plan = row("p", kind="plan", published=D - hrs(100))
        assert may_spend(
            early_plan, None, used_today=99, now=self.PROTECTED, deadline=D, limit=18, policy=POLICY
        )

    def test_reserve_is_a_parameter(self) -> None:
        early_plan = row("p", kind="plan", published=D - hrs(100))
        assert daily_cap(early_plan, self.PROTECTED, D, 18, BudgetPolicy(reserve_requests=4)) == 14
        assert daily_cap(early_plan, self.PROTECTED, D, 5, POLICY) == 0  # never negative

    def test_plan_spend_admits_final_after_non_finals_hit_the_cap(self) -> None:
        plans = [
            row(f"p{i}", creator=f"c{i}", kind="plan", published=D - hrs(100)) for i in range(12)
        ]
        finals = [
            row(f"f{i}", creator=f"f{i}", kind="final", published=D - hrs(5)) for i in range(4)
        ]
        plan = plan_spend(
            [*plans, *finals], used_today=0, limit=18, now=self.PROTECTED, deadline=D, policy=POLICY
        )
        ids = [p.video_id for p in plan]
        assert ids == [f"p{i}" for i in range(8)] + [f"f{i}" for i in range(4)]
        assert sum(p.cost or 0 for p in plan) == 12

    def test_plan_spend_unprotected_day_takes_everything_up_to_the_limit(self) -> None:
        plans = [
            row(f"p{i}", creator=f"c{i}", kind="plan", published=D - hrs(100)) for i in range(12)
        ]
        plan = plan_spend(plans, used_today=0, limit=10, now=self.OPEN, deadline=D, policy=POLICY)
        assert len(plan) == 10

    def test_plan_spend_skips_the_unaffordable_and_keeps_going(self) -> None:
        big = row("big", kind="final", duration=3 * 1800)  # 3 requests
        small = row("small", kind="final", duration=600)
        plan = plan_spend(
            [big, small], used_today=16, limit=18, now=self.OPEN, deadline=D, policy=POLICY
        )
        assert [p.video_id for p in plan] == ["small"]


class TestPriority:
    WEIGHTS: ClassVar[dict[str, float]] = {"a": 0.9, "b": 0.5, "c": 0.9, "d": 0.5}

    def test_weight_times_multiplier_then_newest(self) -> None:
        rows = [
            row("a-plan", "a", "plan"),  # 0.54
            row("b-sel", "b", "selection"),  # 0.50
            row("c-other", "c", "other"),  # 0.27
            row("d-sel-new", "d", "selection", published=D - hrs(5)),  # 0.50, newer
        ]
        got = [r.video_id for r in order_candidates(rows, self.WEIGHTS)]
        assert got == ["a-plan", "d-sel-new", "b-sel", "c-other"]

    def test_unknown_creator_gets_the_default_weight(self) -> None:
        rows = [row("x", "ghost", "selection"), row("y", "b", "selection")]
        assert [r.video_id for r in order_candidates(rows, self.WEIGHTS)] == ["y", "x"]

    def test_older_video_is_demoted_behind_a_newer_selection(self) -> None:
        old_final = row("old", "a", "final", published=D - hrs(50))  # score 0.9
        newer_sel = row("new", "a", "selection", published=D - hrs(10))  # score 0.9
        other_creator = row("z", "b", "plan", published=D - hrs(40))  # score 0.3
        rows = [old_final, newer_sel, other_creator]
        assert demoted_video_ids(rows) == {"old"}
        got = [r.video_id for r in order_candidates(rows, self.WEIGHTS, demoted_video_ids(rows))]
        assert got == ["new", "z", "old"]  # demoted last, but still in the list

    def test_a_newer_non_selection_does_not_demote(self) -> None:
        rows = [
            row("old", "a", "selection", published=D - hrs(50)),
            row("new", "a", "plan", published=D - hrs(10)),
        ]
        assert demoted_video_ids(rows) == set()

    def test_skipped_newer_selection_does_not_demote(self) -> None:
        rows = [
            row("old", "a", "plan", published=D - hrs(50)),
            row("new", "a", "selection", published=D - hrs(10), status="skipped"),
        ]
        assert demoted_video_ids(rows) == set()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def vid(video_id: str, channel: str, title: str, published: datetime | None = None) -> VideoInfo:
    return VideoInfo(
        video_id=video_id,
        channel_id=channel,
        title=title,
        published_at=published or (D - hrs(30)),
        description="",
    )


def creator(
    cid: str,
    channel: str | None,
    *,
    weight: float | None = None,
    title_filter: str | None = None,
    channel_primary: bool = False,
) -> Creator:
    return Creator(
        creator_id=cid,
        name=cid.title(),
        youtube_hint=cid,
        tier=Tier.CORE,
        channel_id=channel,
        weight=weight,
        title_filter=title_filter,
        channel_primary=channel_primary,
    )


class FakeUploads:
    def __init__(
        self,
        listings: dict[str, list[VideoInfo]],
        durations: dict[str, int] | None = None,
        failing: set[str] | None = None,
        durations_fail: bool = False,
    ) -> None:
        self.listings = listings
        self.duration_map = durations or {}
        self.failing = failing or set()
        self.durations_fail = durations_fail
        self.listed: list[str] = []

    def recent_uploads(self, channel_id: str) -> list[VideoInfo]:
        self.listed.append(channel_id)
        if channel_id in self.failing:
            raise YouTubeApiError("quota")
        return self.listings.get(channel_id, [])

    def durations(self, video_ids: Sequence[str]) -> dict[str, int]:
        if self.durations_fail:
            raise httpx.ConnectError("offline")
        return {v: self.duration_map[v] for v in video_ids if v in self.duration_map}


class TestDiscovery:
    ROSTER = (
        creator("alice", "UC_A"),
        creator("bob", "UC_B"),
        creator("host", "UC_SHARED", channel_primary=True),
        creator("guest", "UC_SHARED", title_filter="Guest Show"),
    )

    def test_upserts_classifies_and_assigns_gameweeks(self) -> None:
        client = FakeUploads(
            {
                "UC_A": [
                    vid("a1", "UC_A", "GW12 Team Selection"),
                    vid("a0", "UC_A", "GW11 Final Thoughts", D11 - hrs(5)),
                ],
                "UC_B": [vid("b1", "UC_B", "GW12 Review Show")],
            },
            {"a1": 1200, "a0": 1200, "b1": 1200},
        )
        ledger = L.Ledger()
        res = discover(ledger, self.ROSTER, client, DEADLINES, D - hrs(20))
        assert res.new_videos == 3
        assert res.channels_failed == 0
        a1, a0, b1 = (ledger.rows[v] for v in ("a1", "a0", "b1"))
        assert (a1.kind, a1.gameweek, a1.status) == ("selection", 12, "new")
        assert (a0.kind, a0.gameweek) == ("final", 11)
        assert (b1.kind, b1.status) == ("review", "skipped")
        assert a1.creator_id == "alice"
        assert a1.duration_s == 1200
        assert a1.first_seen_at == D - hrs(20)

    def test_shorts_are_skipped(self) -> None:
        client = FakeUploads({"UC_A": [vid("s1", "UC_A", "GW12 Team Selection")]}, {"s1": 59})
        ledger = L.Ledger()
        discover(ledger, self.ROSTER, client, DEADLINES, D)
        assert ledger.rows["s1"].status == "skipped"
        assert ledger.rows["s1"].last_error == SHORT_MARK

    def test_unknown_duration_is_kept(self) -> None:
        client = FakeUploads(
            {"UC_A": [vid("u1", "UC_A", "GW12 Team Selection")]}, durations_fail=True
        )
        ledger = L.Ledger()
        res = discover(ledger, self.ROSTER, client, DEADLINES, D)
        assert ledger.rows["u1"].status == "new"
        assert ledger.rows["u1"].duration_s is None
        assert res.errors  # recorded, not raised

    def test_existing_rows_keep_their_status(self) -> None:
        client = FakeUploads({"UC_A": [vid("a1", "UC_A", "GW12 Team Selection")]}, {"a1": 1200})
        ledger = L.Ledger()
        discover(ledger, self.ROSTER, client, DEADLINES, D)
        ledger.rows["a1"].status = "ok"
        ledger.rows["a1"].source = "gemini"
        res = discover(ledger, self.ROSTER, client, DEADLINES, D + hrs(1))
        assert res.new_videos == 0
        assert ledger.rows["a1"].status == "ok"
        assert ledger.rows["a1"].first_seen_at == D

    def test_a_failing_channel_does_not_abort_the_scan(self) -> None:
        client = FakeUploads(
            {"UC_B": [vid("b1", "UC_B", "GW12 Team Selection")]}, {"b1": 900}, failing={"UC_A"}
        )
        ledger = L.Ledger()
        res = discover(ledger, self.ROSTER, client, DEADLINES, D)
        assert (res.channels_failed, res.channels_scanned) == (1, 2)
        assert list(ledger.rows) == ["b1"]

    def test_shared_channel_attribution_and_single_listing(self) -> None:
        client = FakeUploads(
            {
                "UC_SHARED": [
                    vid("g1", "UC_SHARED", "Guest Show: GW12 captain"),
                    vid("h1", "UC_SHARED", "GW12 Team Selection"),
                ]
            },
            {"g1": 900, "h1": 900},
        )
        ledger = L.Ledger()
        discover(ledger, self.ROSTER, client, DEADLINES, D)
        assert ledger.rows["g1"].creator_id == "guest"
        assert ledger.rows["h1"].creator_id == "host"
        assert client.listed.count("UC_SHARED") == 1

    def test_creator_without_a_channel_is_ignored(self) -> None:
        client = FakeUploads({})
        discover(L.Ledger(), (creator("nochan", None),), client, DEADLINES, D)
        assert client.listed == []

    def test_a_finished_premiere_is_revived(self) -> None:
        listing = {"UC_A": [vid("p1", "UC_A", "GW12 Team Selection")]}
        ledger = L.Ledger()
        discover(ledger, self.ROSTER, FakeUploads(listing, {"p1": 0}), DEADLINES, D)
        assert ledger.rows["p1"].status == "skipped"
        discover(ledger, self.ROSTER, FakeUploads(listing, {"p1": 1500}), DEADLINES, D)
        assert (ledger.rows["p1"].status, ledger.rows["p1"].duration_s) == ("new", 1500)
        assert ledger.rows["p1"].last_error is None


class _Resp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


def _durations_payload(video_id: str, iso: str) -> _Resp:
    return _Resp({"items": [{"id": video_id, "contentDetails": {"duration": iso}}]})


class CachingClient:
    """An UploadsClient whose durations go through the REAL cached
    `youtube_client.get_video_durations` (the FakeUploads above does not cache)."""

    def __init__(self, listing: list[VideoInfo]) -> None:
        self.listing = listing

    def recent_uploads(self, channel_id: str) -> list[VideoInfo]:
        return self.listing

    def durations(self, video_ids: Sequence[str]) -> dict[str, int]:
        from fpl_oracle.ingest import youtube_client as yc

        return yc.get_video_durations(list(video_ids))


class TestPremiereDurationIsNotCached:
    def test_zero_duration_is_never_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_oracle.ingest import youtube_client as yc

        monkeypatch.setattr(yc, "CACHE_DIR", tmp_path)
        answers = iter([_durations_payload("p1", "P0D"), _durations_payload("p1", "PT25M")])
        monkeypatch.setattr(yc, "_get", lambda url, params: next(answers))
        assert yc.get_video_durations(["p1"]) == {"p1": 0}
        assert yc.get_video_durations(["p1"]) == {"p1": 1500}  # asked the API again
        assert yc.get_video_durations(["p1"]) == {"p1": 1500}  # now cached, no 3rd call

    def test_an_old_cached_zero_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_oracle.ingest import youtube_client as yc

        monkeypatch.setattr(yc, "CACHE_DIR", tmp_path)
        yc._write_cache(yc._cache_path_preserving_case("duration", "p1"), {"duration_s": 0})
        monkeypatch.setattr(yc, "_get", lambda url, params: _durations_payload("p1", "PT10M"))
        assert yc.get_video_durations(["p1"]) == {"p1": 600}

    def test_a_premiere_is_revived_through_the_caching_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_oracle.ingest import youtube_client as yc

        monkeypatch.setattr(yc, "CACHE_DIR", tmp_path)
        answers = iter([_durations_payload("p1", "P0D"), _durations_payload("p1", "PT25M")])
        monkeypatch.setattr(yc, "_get", lambda url, params: next(answers))
        client = CachingClient([vid("p1", "UC_A", "GW12 Team Selection")])
        roster = (creator("alice", "UC_A"),)
        ledger = L.Ledger()
        discover(ledger, roster, client, DEADLINES, D)
        assert ledger.rows["p1"].status == "skipped"
        discover(ledger, roster, client, DEADLINES, D + hrs(1))
        assert (ledger.rows["p1"].status, ledger.rows["p1"].duration_s) == ("new", 1500)
