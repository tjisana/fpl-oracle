"""Tests for ingest/warm_cache.py — pure logic and wiring only, all network
functions (list_recent_videos, get_video_durations, fetch_transcript,
save_transcript, load_transcript) faked. No live network. Follows the
fakes-injected pattern in tests/test_attribution.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from youtube_transcript_api import RequestBlocked, YouTubeRequestFailed

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
            raise AssertionError("fetch_transcript must not be called during --dry-run")

        monkeypatch.setattr(warm_cache, "fetch_transcript", boom)
        monkeypatch.setattr(warm_cache, "save_transcript", boom)

        summary = warm_creator(
            creator,
            [creator],
            since=datetime(2026, 8, 1, tzinfo=UTC),
            max_per_creator=5,
            dir=tmp_path,
            dry_run=True,
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

        def fake_fetch_transcript(video_id, dir):
            if video_id == "fetchable":
                return _transcript(video_id)
            if video_id == "no-transcript":
                return None
            raise AssertionError(f"unexpected fetch for {video_id} (already cached)")

        monkeypatch.setattr(warm_cache, "fetch_transcript", fake_fetch_transcript)

        saved: list[str] = []
        monkeypatch.setattr(warm_cache, "save_transcript", lambda t, dir: saved.append(t.video_id))

        summary = warm_creator(
            creator,
            [creator],
            since=datetime(2026, 8, 1, tzinfo=UTC),
            max_per_creator=5,
            dir=tmp_path,
            dry_run=False,
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

        monkeypatch.setattr(warm_cache, "fetch_transcript", blocked)

        with pytest.raises(RequestBlocked):
            warm_creator(
                creator,
                [creator],
                since=datetime(2026, 8, 1, tzinfo=UTC),
                max_per_creator=5,
                dir=tmp_path,
                dry_run=False,
            )


class TestRunWarmCache:
    def test_per_creator_failure_is_non_fatal_and_run_continues(self, monkeypatch) -> None:
        creators = [_creator("a"), _creator("b")]
        monkeypatch.setattr(warm_cache, "REGISTRY", creators)

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run):
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

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run):
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
            raise YouTubeRequestFailed(
                video_id="x", http_error=requests.exceptions.HTTPError("blocked")
            )

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

        def fake_warm_creator(creator, registry, since, max_per_creator, dir, dry_run):
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
