"""Tests for ingest/warm_cache.py — pure logic and wiring only, all network
functions (list_recent_videos, get_video_durations, fetch_transcript_from,
save_transcript, load_transcript) faked. No live network. Follows the
fakes-injected pattern in tests/test_attribution.py.

Most of these tests exercise the caption-scrape-specific behaviour (sleep
throttling, RequestBlocked propagation, the caption-flavoured breaker
message) that predates the gemini/captions split, so they pass
`source="captions"` explicitly to keep testing exactly that path. Tests
specific to the gemini-default behaviour live in their own classes below."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from youtube_transcript_api import RequestBlocked

from fpl_oracle.ingest import warm_cache
from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment
from fpl_oracle.ingest.warm_cache import (
    CreatorWarmSummary,
    default_since,
    eligible_creators_filtered,
    filter_since,
    parse_since,
    run_warm_cache,
    videos_to_warm,
    warm_creator,
)
from fpl_oracle.ingest.youtube_client import VideoInfo, YouTubeApiError
from fpl_oracle.roster.models import Creator, Tier

_CHANNEL_ID = "UCabc"


def _video(video_id: str, title: str = "some title", *, published_at: datetime) -> VideoInfo:
    return VideoInfo(
        video_id=video_id,
        channel_id=_CHANNEL_ID,
        title=title,
        published_at=published_at,
        description="",
    )


def _creator(creator_id: str, channel_id: str | None = None, **kwargs) -> Creator:
    # Each fake creator gets its own channel by default (sole owner, so
    # `eligible_creators` includes it) — pass an explicit `channel_id` to
    # test shared-channel attribution instead.
    return Creator(
        creator_id=creator_id,
        name=creator_id,
        youtube_hint=creator_id,
        tier=Tier.SECONDARY,
        channel_id=channel_id or f"UC-{creator_id}",
        **kwargs,
    )


def _transcript(video_id: str) -> Transcript:
    return Transcript(
        video_id=video_id,
        language="en",
        is_generated=True,
        segments=[TranscriptSegment(text="hi", start=0.0, duration=1.0)],
    )


class TestParseSince:
    def test_parses_date_to_utc_midnight(self) -> None:
        assert parse_since("2026-08-22") == datetime(2026, 8, 22, tzinfo=UTC)


class TestDefaultSince:
    def test_is_seven_days_ago_at_utc_midnight(self) -> None:
        expected_day = (datetime.now(UTC) - timedelta(days=warm_cache.DEFAULT_SINCE_DAYS)).date()
        expected = datetime(expected_day.year, expected_day.month, expected_day.day, tzinfo=UTC)
        assert default_since() == expected
        assert default_since().tzinfo is UTC


class TestFilterSince:
    def test_keeps_videos_on_or_after_cutoff(self) -> None:
        cutoff = datetime(2026, 8, 22, tzinfo=UTC)
        old = _video("old", published_at=datetime(2026, 8, 20, tzinfo=UTC))
        boundary = _video("boundary", published_at=cutoff)
        new = _video("new", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        assert filter_since([old, boundary, new], cutoff) == [boundary, new]


class TestEligibleCreatorsFiltered:
    def test_unknown_creator_id_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(warm_cache, "REGISTRY", [_creator("a"), _creator("b")])
        with pytest.raises(ValueError, match="nope"):
            eligible_creators_filtered(["a", "nope"])

    def test_filters_to_known_ids(self, monkeypatch) -> None:
        monkeypatch.setattr(warm_cache, "REGISTRY", [_creator("a"), _creator("b")])
        result = eligible_creators_filtered(["b"])
        assert [c.creator_id for c in result] == ["b"]

    def test_none_returns_all_eligible(self, monkeypatch) -> None:
        monkeypatch.setattr(warm_cache, "REGISTRY", [_creator("a"), _creator("b")])
        result = eligible_creators_filtered(None)
        assert {c.creator_id for c in result} == {"a", "b"}


class TestVideosToWarm:
    def test_applies_since_filter_and_cap_newest_first(self, monkeypatch) -> None:
        creator = _creator("solo")
        videos = [
            _video("v_newest", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            _video("v_mid", published_at=datetime(2026, 8, 23, tzinfo=UTC)),
            _video("v_old", published_at=datetime(2026, 8, 10, tzinfo=UTC)),
        ]
        monkeypatch.setattr(
            warm_cache, "list_recent_videos", lambda channel_id, max_results: videos
        )
        monkeypatch.setattr(warm_cache, "get_video_durations", lambda ids: {})

        result = videos_to_warm(
            creator, [creator], since=datetime(2026, 8, 22, tzinfo=UTC), max_per_creator=1
        )

        assert [v.video_id for v in result] == ["v_newest"]

    def test_respects_shared_channel_attribution(self, monkeypatch) -> None:
        salah = _creator("fpl-salah", title_filter="FPL Salah")
        matthew = _creator("fpl-matthew", title_filter="FPL Matthew")
        videos = [
            _video("v1", "FPL Salah's team", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            _video("v2", "FPL Matthew's team", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
        ]
        monkeypatch.setattr(
            warm_cache, "list_recent_videos", lambda channel_id, max_results: videos
        )
        monkeypatch.setattr(warm_cache, "get_video_durations", lambda ids: {})

        result = videos_to_warm(
            salah, [salah, matthew], since=datetime(2026, 8, 1, tzinfo=UTC), max_per_creator=5
        )

        assert [v.video_id for v in result] == ["v1"]

    def test_shorts_filtered_out(self, monkeypatch) -> None:
        creator = _creator("solo")
        long_video = _video("v_long", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        short_video = _video("v_short", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(
            warm_cache,
            "list_recent_videos",
            lambda channel_id, max_results: [long_video, short_video],
        )
        monkeypatch.setattr(
            warm_cache, "get_video_durations", lambda ids: {"v_long": 300, "v_short": 45}
        )

        result = videos_to_warm(
            creator, [creator], since=datetime(2026, 8, 1, tzinfo=UTC), max_per_creator=5
        )

        assert [v.video_id for v in result] == ["v_long"]

    def test_duration_lookup_failure_is_non_fatal(self, monkeypatch) -> None:
        creator = _creator("solo")
        video = _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(
            warm_cache, "list_recent_videos", lambda channel_id, max_results: [video]
        )

        def failing_durations(ids):
            raise YouTubeApiError("quota exceeded")

        monkeypatch.setattr(warm_cache, "get_video_durations", failing_durations)

        result = videos_to_warm(
            creator, [creator], since=datetime(2026, 8, 1, tzinfo=UTC), max_per_creator=5
        )

        assert [v.video_id for v in result] == ["v1"]

    def test_duration_lookup_transport_error_is_non_fatal(self, monkeypatch) -> None:
        """`YouTubeApiError` (a non-2xx response) is not the only way a
        duration lookup can fail — a connection error or timeout raises
        `httpx.ConnectError`/`httpx.HTTPError`, which does NOT subclass
        `YouTubeApiError`. Either must leave the run running, not abort it —
        this is the new default (gemini) path's equivalent of the
        quota-blip case above."""
        creator = _creator("solo")
        video = _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(
            warm_cache, "list_recent_videos", lambda channel_id, max_results: [video]
        )

        def failing_durations(ids):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(warm_cache, "get_video_durations", failing_durations)

        result = videos_to_warm(
            creator, [creator], since=datetime(2026, 8, 1, tzinfo=UTC), max_per_creator=5
        )

        assert [v.video_id for v in result] == ["v1"]


class TestWarmCreator:
    def test_dry_run_makes_zero_transcript_calls(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("solo")
        cached_video = _video("cached", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        new_video = _video("new", published_at=datetime(2026, 8, 24, tzinfo=UTC))
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: [cached_video, new_video])

        def fake_load_transcript(video_id, dir):
            return _transcript(video_id) if video_id == "cached" else None

        monkeypatch.setattr(warm_cache, "load_transcript", fake_load_transcript)

        def boom(*a, **k):
            raise AssertionError("fetch_transcript_from must not be called during --dry-run")

        monkeypatch.setattr(warm_cache, "fetch_transcript_from", boom)
        monkeypatch.setattr(warm_cache, "save_transcript", boom)

        summary = warm_creator(
            creator,
            [creator],
            since=datetime(2026, 8, 1, tzinfo=UTC),
            max_per_creator=5,
            dir=tmp_path,
            dry_run=True,
            source="captions",
        )

        assert summary.considered == 2
        assert summary.already_cached == 1
        assert summary.newly_fetched == 0
        assert summary.failed == 0
        cached_outcome = next(v for v in summary.videos if v.video_id == "cached")
        new_outcome = next(v for v in summary.videos if v.video_id == "new")
        assert cached_outcome.already_cached is True
        assert new_outcome.already_cached is False

    def test_live_run_counts_already_cached_vs_newly_fetched_vs_failed(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        creator = _creator("solo")
        cached_video = _video("cached", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        fetchable_video = _video("fetchable", published_at=datetime(2026, 8, 24, tzinfo=UTC))
        no_transcript_video = _video(
            "no-transcript", published_at=datetime(2026, 8, 23, tzinfo=UTC)
        )
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [cached_video, fetchable_video, no_transcript_video],
        )

        def fake_load_transcript(video_id, dir):
            return _transcript(video_id) if video_id == "cached" else None

        monkeypatch.setattr(warm_cache, "load_transcript", fake_load_transcript)

        def fake_fetch_transcript(video_id, source, *, dir, duration_seconds=None):
            if video_id == "fetchable":
                return _transcript(video_id)
            if video_id == "no-transcript":
                return None
            raise AssertionError(f"unexpected fetch for {video_id} (already cached)")

        monkeypatch.setattr(warm_cache, "fetch_transcript_from", fake_fetch_transcript)

        saved: list[str] = []
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: saved.append(t.video_id))

        summary = warm_creator(
            creator,
            [creator],
            since=datetime(2026, 8, 1, tzinfo=UTC),
            max_per_creator=5,
            dir=tmp_path,
            dry_run=False,
            source="captions",
        )

        assert summary.considered == 3
        assert summary.already_cached == 1
        assert summary.newly_fetched == 1
        assert summary.failed == 1
        assert saved == ["fetchable"]  # only the newly-fetched one is saved

    def test_video_listing_failure_is_recorded_not_raised(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        creator = _creator("solo")

        def failing_videos_to_warm(*a, **k):
            raise YouTubeApiError("boom")

        monkeypatch.setattr(warm_cache, "videos_to_warm", failing_videos_to_warm)

        summary = warm_creator(
            creator,
            [creator],
            since=datetime(2026, 8, 1, tzinfo=UTC),
            max_per_creator=5,
            dir=tmp_path,
            dry_run=False,
        )

        assert isinstance(summary, CreatorWarmSummary)
        assert summary.considered == 0
        assert summary.error is not None
        assert "boom" in summary.error

    def test_request_blocked_propagates_out_of_warm_creator(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        creator = _creator("solo")
        video = _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: [video])
        monkeypatch.setattr(warm_cache, "load_transcript", lambda video_id, dir: None)

        def blocked(*a, **k):
            raise RequestBlocked(video_id="v1")

        monkeypatch.setattr(warm_cache, "fetch_transcript_from", blocked)

        with pytest.raises(warm_cache.WarmingInterrupted) as excinfo:
            warm_creator(
                creator,
                [creator],
                since=datetime(2026, 8, 1, tzinfo=UTC),
                max_per_creator=5,
                dir=tmp_path,
                dry_run=False,
                source="captions",
                delay_seconds=0.0,
            )
        assert "RequestBlocked" in excinfo.value.reason


class TestRunWarmCache:
    def test_per_creator_failure_is_non_fatal_and_run_continues(self, monkeypatch) -> None:
        creators = [_creator("a"), _creator("b")]
        monkeypatch.setattr(warm_cache, "REGISTRY", creators)

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run, **kw):
            if creator.creator_id == "a":
                return CreatorWarmSummary(
                    creator_id="a",
                    creator_name="a",
                    considered=0,
                    already_cached=0,
                    newly_fetched=0,
                    failed=0,
                    error="video listing failed: boom",
                )
            return CreatorWarmSummary(
                creator_id="b",
                creator_name="b",
                considered=1,
                already_cached=0,
                newly_fetched=1,
                failed=0,
            )

        monkeypatch.setattr(warm_cache, "warm_creator", fake_warm_creator)

        result = run_warm_cache(since=datetime(2026, 8, 1, tzinfo=UTC))

        assert [c.creator_id for c in result.creators] == ["a", "b"]
        assert result.blocked is False
        assert result.creators[0].yielded_nothing is True
        assert result.creators[1].yielded_nothing is False

    def test_stops_on_request_blocked_and_reports_remaining_untouched(self, monkeypatch) -> None:
        creators = [_creator("a"), _creator("b"), _creator("c")]
        monkeypatch.setattr(warm_cache, "REGISTRY", creators)
        called: list[str] = []

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run, **kw):
            called.append(creator.creator_id)
            if creator.creator_id == "a":
                return CreatorWarmSummary(
                    creator_id="a",
                    creator_name="a",
                    considered=1,
                    already_cached=0,
                    newly_fetched=1,
                    failed=0,
                )
            raise warm_cache.WarmingInterrupted(f"{creator.creator_id}: YouTubeRequestFailed", [])

        monkeypatch.setattr(warm_cache, "warm_creator", fake_warm_creator)

        result = run_warm_cache(since=datetime(2026, 8, 1, tzinfo=UTC))

        assert called == ["a", "b"]  # c never attempted
        assert [c.creator_id for c in result.creators] == ["a"]
        assert result.blocked is True
        assert result.blocked_reason is not None
        assert "b" in result.blocked_reason

    def test_dry_run_flag_threaded_through_to_warm_creator(self, monkeypatch) -> None:
        creators = [_creator("a")]
        monkeypatch.setattr(warm_cache, "REGISTRY", creators)
        seen_dry_run: list[bool] = []

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run, **kw):
            seen_dry_run.append(dry_run)
            return CreatorWarmSummary(
                creator_id=creator.creator_id,
                creator_name=creator.name,
                considered=0,
                already_cached=0,
                newly_fetched=0,
                failed=0,
            )

        monkeypatch.setattr(warm_cache, "warm_creator", fake_warm_creator)

        run_warm_cache(since=datetime(2026, 8, 1, tzinfo=UTC), dry_run=True)

        assert seen_dry_run == [True]


class TestThrottle:
    """The first live run pulled 34 transcripts in 59s and was IP-blocked.
    The throttle is the fix; these pin that it applies to network work and
    only to network work."""

    def test_network_fetches_sleep_between_them(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        videos = [
            _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            _video("v2", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            _video("v3", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
        ]
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: videos)
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: _transcript(vid),
        )
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: None)
        slept: list[float] = []

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            source="captions",
            delay_seconds=2.0,
            sleep=slept.append,
        )

        assert len(slept) == 3, "every network fetch must be throttled"
        # jitter is +/-40%, so each delay stays inside [1.2, 2.8]
        assert all(1.2 <= d <= 2.8 for d in slept), slept

    def test_cached_hits_never_sleep(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [_video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))],
        )
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: _transcript(vid))
        slept: list[float] = []

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            source="captions",
            delay_seconds=2.0,
            sleep=slept.append,
        )

        assert slept == [], "a fully cached creator must not sleep at all"

    def test_dry_run_never_sleeps(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [_video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))],
        )
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        slept: list[float] = []

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            True,
            source="captions",
            delay_seconds=2.0,
            sleep=slept.append,
        )

        assert slept == []


class TestCircuitBreaker:
    """`fetch_transcript_from` returns None (not an exception) for anti-bot
    states like PoTokenRequired and YouTubeDataUnparsable, which are
    CouldNotRetrieveTranscript subclasses outside its re-raise set (captions
    path) — or simply because Gemini gave up after its retries (gemini
    path). Without a breaker a blocked run marks EVERY video 'no transcript
    available'/'gemini could not read the video', exits 0, and looks
    finished."""

    def test_trips_after_consecutive_failures_with_no_success(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        creator = _creator("a")
        n = warm_cache.CONSECUTIVE_FAILURE_LIMIT + 3
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [
                _video(f"v{i}", published_at=datetime(2026, 8, 25, tzinfo=UTC)) for i in range(n)
            ],
        )
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: None,
        )

        with pytest.raises(warm_cache.WarmingInterrupted) as excinfo:
            warm_creator(
                creator,
                [creator],
                datetime(2026, 8, 1, tzinfo=UTC),
                5,
                tmp_path,
                False,
                source="captions",
                delay_seconds=0.0,
                breaker=warm_cache._FailureStreak(),
            )

        assert "in a row" in excinfo.value.reason
        assert "caption-less videos" in excinfo.value.reason
        # the outcomes recorded before tripping are carried out, not lost
        assert len(excinfo.value.outcomes) == warm_cache.CONSECUTIVE_FAILURE_LIMIT

    def test_a_success_resets_the_streak(self) -> None:
        breaker = warm_cache._FailureStreak(limit=3)
        assert breaker.record_failure() is False
        assert breaker.record_failure() is False
        breaker.record_success()
        assert breaker.record_failure() is False, "success must reset the streak"

    def test_genuinely_caption_less_videos_do_not_trip_it(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A creator with a couple of caption-less uploads is normal and
        must not be mistaken for a block."""
        creator = _creator("a")
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [
                _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
                _video("v2", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            ],
        )
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: None,
        )

        summary = warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            source="captions",
            delay_seconds=0.0,
            breaker=warm_cache._FailureStreak(),
        )

        assert summary.failed == 2


class TestPartialProgressIsPreserved:
    def test_in_flight_creator_keeps_the_transcripts_it_did_fetch(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Real integration of run_warm_cache + warm_creator, not two
        halves faked separately. Creator 'a' fetches two transcripts, then
        the third is blocked; both must survive into the summary."""
        creators = [_creator("a"), _creator("b")]
        monkeypatch.setattr(warm_cache, "REGISTRY", creators)
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [
                _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
                _video("v2", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
                _video("v3", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            ],
        )
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: None)

        def fetch(vid, source, *, dir, duration_seconds=None):
            if vid == "v3":
                raise RequestBlocked(video_id=vid)
            return _transcript(vid)

        monkeypatch.setattr(warm_cache, "fetch_transcript_from", fetch)

        result = run_warm_cache(
            since=datetime(2026, 8, 1, tzinfo=UTC),
            source="captions",
            delay_seconds=0.0,
            sleep=lambda _: None,
        )

        assert result.blocked is True
        assert [c.creator_id for c in result.creators] == ["a"]
        assert result.creators[0].newly_fetched == 2, "work already done must not vanish"
        assert result.total_creators == 2
        assert result.never_attempted == 1

    def test_unexpected_exception_still_yields_a_summary(self, monkeypatch) -> None:
        """A connection reset must not escape as a traceback and destroy
        the report of everything fetched before it."""
        creators = [_creator("a"), _creator("b")]
        monkeypatch.setattr(warm_cache, "REGISTRY", creators)

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run, **kw):
            if creator.creator_id == "a":
                return CreatorWarmSummary(
                    creator_id="a",
                    creator_name="a",
                    considered=1,
                    already_cached=0,
                    newly_fetched=1,
                    failed=0,
                )
            raise ConnectionResetError("connection reset by peer")

        monkeypatch.setattr(warm_cache, "warm_creator", fake_warm_creator)

        result = run_warm_cache(since=datetime(2026, 8, 1, tzinfo=UTC))

        assert result.blocked is True
        assert "ConnectionResetError" in (result.blocked_reason or "")
        assert result.newly_fetched == 1, "creator a's work is still reported"


class TestDirIsHonoured:
    """Every fake in this suite previously accepted `dir` and ignored it,
    so a regression to the default directory would have passed the whole
    suite — while writing transcripts where extraction cannot see them."""

    def test_dir_reaches_every_transcript_call(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        target = tmp_path / "somewhere-else"
        seen: dict[str, list[Path]] = {"load": [], "fetch": [], "save": []}
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [_video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))],
        )
        monkeypatch.setattr(
            warm_cache, "load_transcript", lambda vid, dir: seen["load"].append(dir) or None
        )
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: (
                seen["fetch"].append(dir) or _transcript(vid)
            ),
        )
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: seen["save"].append(dir))

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            target,
            False,
            source="captions",
            delay_seconds=0.0,
        )

        assert seen["load"] == [target]
        assert seen["fetch"] == [target]
        assert seen["save"] == [target]


class TestMain:
    def test_dir_and_delay_reach_run_warm_cache(self, monkeypatch, tmp_path: Path) -> None:
        seen: dict = {}

        def fake_run(**kwargs):
            seen.update(kwargs)
            return warm_cache.WarmCacheRunSummary(creators=[], dry_run=False, total_creators=0)

        monkeypatch.setattr(warm_cache, "run_warm_cache", fake_run)
        rc = warm_cache.main(["--dir", str(tmp_path), "--delay", "3.5", "--since", "2026-08-22"])

        assert rc == 0
        assert seen["dir"] == tmp_path
        assert seen["delay_seconds"] == 3.5
        assert seen["since"] == datetime(2026, 8, 22, tzinfo=UTC)

    def test_blocked_run_exits_nonzero(self, monkeypatch) -> None:
        """The exit code is how a scheduled run signals it needs attention."""
        monkeypatch.setattr(
            warm_cache,
            "run_warm_cache",
            lambda **k: warm_cache.WarmCacheRunSummary(
                creators=[],
                dry_run=False,
                blocked=True,
                blocked_reason="IpBlocked",
                total_creators=5,
            ),
        )
        assert warm_cache.main([]) == 2

    def test_dry_run_forces_zero_delay(self, monkeypatch) -> None:
        seen: dict = {}
        monkeypatch.setattr(
            warm_cache,
            "run_warm_cache",
            lambda **k: (
                seen.update(k)
                or warm_cache.WarmCacheRunSummary(creators=[], dry_run=True, total_creators=0)
            ),
        )
        warm_cache.main(["--dry-run", "--delay", "5"])
        assert seen["delay_seconds"] == 0.0, "a dry run makes no network calls, so never waits"

    def test_source_flag_reaches_run_warm_cache(self, monkeypatch) -> None:
        seen: dict = {}

        def fake_run(**kwargs):
            seen.update(kwargs)
            return warm_cache.WarmCacheRunSummary(creators=[], dry_run=False, total_creators=0)

        monkeypatch.setattr(warm_cache, "run_warm_cache", fake_run)
        warm_cache.main([])
        assert seen["source"] == "gemini", "gemini is the default source"

        warm_cache.main(["--source", "captions"])
        assert seen["source"] == "captions"


class TestGeminiIsDefault:
    """Gemini is the default transcript source (see sourcing.py's module
    docstring for why): never IP-blocked, so `warm_creator`/`run_warm_cache`
    must use it without the caller asking, must not apply the
    captions-only throttle, and must fail/report distinctly from a caption
    scrape miss."""

    def test_default_source_reaches_fetch_transcript_from(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        creator = _creator("a")
        video = _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: [video])
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: None)
        monkeypatch.setattr(warm_cache, "get_video_durations", lambda ids: {"v1": 300})
        seen_sources: list[str] = []

        def fake_fetch(vid, source, *, dir, duration_seconds=None):
            seen_sources.append(source)
            return _transcript(vid)

        monkeypatch.setattr(warm_cache, "fetch_transcript_from", fake_fetch)

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            delay_seconds=0.0,
        )

        assert seen_sources == ["gemini"]

    def test_gemini_never_sleeps_even_with_delay(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        videos = [
            _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
            _video("v2", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
        ]
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: videos)
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: None)
        monkeypatch.setattr(warm_cache, "get_video_durations", lambda ids: {"v1": 300, "v2": 300})
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: _transcript(vid),
        )
        slept: list[float] = []

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            delay_seconds=2.0,
            sleep=slept.append,
        )

        assert slept == [], "gemini has its own per-video latency; the scrape throttle is moot"

    def test_gemini_none_is_recorded_with_gemini_specific_reason(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        creator = _creator("a")
        video = _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: [video])
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(warm_cache, "get_video_durations", lambda ids: {"v1": 300})
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: None,
        )

        summary = warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            delay_seconds=0.0,
        )

        assert summary.failed == 1
        (outcome,) = summary.videos
        assert outcome.failure_reason == "gemini could not read the video"

    def test_breaker_trip_message_is_gemini_flavoured(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        n = warm_cache.CONSECUTIVE_FAILURE_LIMIT
        monkeypatch.setattr(
            warm_cache,
            "videos_to_warm",
            lambda *a, **k: [
                _video(f"v{i}", published_at=datetime(2026, 8, 25, tzinfo=UTC)) for i in range(n)
            ],
        )
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(
            warm_cache, "get_video_durations", lambda ids: {vid: 300 for vid in ids}
        )
        monkeypatch.setattr(
            warm_cache,
            "fetch_transcript_from",
            lambda vid, source, *, dir, duration_seconds=None: None,
        )

        with pytest.raises(warm_cache.WarmingInterrupted) as excinfo:
            warm_creator(
                creator,
                [creator],
                datetime(2026, 8, 1, tzinfo=UTC),
                5,
                tmp_path,
                False,
                delay_seconds=0.0,
                breaker=warm_cache._FailureStreak(),
            )

        assert "Gemini API is down" in excinfo.value.reason
        assert "8-hours-of-video-per-day" in excinfo.value.reason

    def test_durations_are_passed_through_to_the_fetch(self, monkeypatch, tmp_path: Path) -> None:
        creator = _creator("a")
        video = _video("v1", published_at=datetime(2026, 8, 25, tzinfo=UTC))
        monkeypatch.setattr(warm_cache, "videos_to_warm", lambda *a, **k: [video])
        monkeypatch.setattr(warm_cache, "load_transcript", lambda vid, dir: None)
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: None)
        monkeypatch.setattr(warm_cache, "get_video_durations", lambda ids: {"v1": 483})
        seen_durations: list[float | None] = []

        def fake_fetch(vid, source, *, dir, duration_seconds=None):
            seen_durations.append(duration_seconds)
            return _transcript(vid)

        monkeypatch.setattr(warm_cache, "fetch_transcript_from", fake_fetch)

        warm_creator(
            creator,
            [creator],
            datetime(2026, 8, 1, tzinfo=UTC),
            5,
            tmp_path,
            False,
            delay_seconds=0.0,
        )

        assert seen_durations == [483]
