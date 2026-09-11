"""Tests for extract/run_extract.py — no network, no LLM."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from fpl_oracle.extract import run_extract
from fpl_oracle.extract.run_extract import (
    GENERIC_IN_SEASON_PATTERNS,
    ExtractionRunSummary,
    ResolvedExtraction,
    VideoSelection,
    _resolve_gameweek_and_window,
    gameweek_title_patterns,
    is_gameweek_video,
    render_match_quality,
    resolve_extraction,
    resolve_target_gameweek,
)
from fpl_oracle.extract.schemas import Pick, VideoExtraction
from fpl_oracle.fpl import client as fpl_client_module
from fpl_oracle.fpl.players import MatchStatus, PlayerDB
from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment
from fpl_oracle.ingest.youtube_client import VideoInfo
from fpl_oracle.roster.models import Creator, Tier
from tests.test_players import _bootstrap_payload


@pytest.fixture
def db() -> PlayerDB:
    return PlayerDB.from_bootstrap(_bootstrap_payload())


class TestGameweekTitlePatterns:
    def test_gw2(self):
        assert gameweek_title_patterns(2) == ("gw2", "gw 2", "gameweek 2", "game week 2")

    def test_gw14(self):
        assert gameweek_title_patterns(14) == ("gw14", "gw 14", "gameweek 14", "game week 14")


class TestIsGameweekVideoExplicit:
    @pytest.mark.parametrize(
        "title",
        [
            "MY GW2 TEAM REVEAL",
            "FPL Gameweek 2 team selection!",
            "GW2 TEAM + GW20 PLANS",
            "FINAL TEAM GW 2",
            "Game Week 2 Squad",
        ],
    )
    def test_explicit_target_matches_unconditionally(self, title):
        assert is_gameweek_video(title, 2)

    def test_explicit_match_beats_missing_publish_window(self):
        # no published_at/published_after given at all — explicit match
        # doesn't need them.
        assert is_gameweek_video("GW2 TEAM REVEAL", 2)

    @pytest.mark.parametrize(
        "title",
        [
            "GW12 PREVIEW",
            "Gameweek 15 pod",
            "SEASON REVIEW 2025/26",
            "TOP 10 DEFENDERS THIS SEASON",
            "FPL DRAFT LEAGUE: MY DRAFT",
        ],
    )
    def test_negatives(self, title):
        assert not is_gameweek_video(title, 2)


class TestFalseStemGuard:
    def test_gw2_does_not_false_match_gw20_through_gw29(self):
        for n in range(20, 30):
            assert not is_gameweek_video(f"GW{n} PREVIEW", 2), f"gw{n} falsely matched gw2"

    def test_gw2_survives_alongside_a_genuine_gw20_mention(self):
        # "GW2 TEAM + GW20 PLANS" must still match on its genuine "gw2",
        # which survives the false-stem stripping ("gw20" does not).
        assert is_gameweek_video("GW2 TEAM + GW20 PLANS", 2)
        assert is_gameweek_video("MY GW2 SQUAD | THOUGHTS ON GW25", 2)

    def test_double_digit_target_has_no_stem_collision(self):
        # gw >= 10: no three-digit gameweek exists, so nothing to guard.
        assert is_gameweek_video("GW14 TEAM REVEAL", 14)
        assert not is_gameweek_video("GW4 TEAM REVEAL", 14)


class TestIsGameweekVideoWrongExplicitGameweek:
    def test_rejected_even_though_it_carries_a_generic_pattern(self):
        assert not is_gameweek_video("Transfer Talk GW14", 2)
        assert not is_gameweek_video("GW3 Final Thoughts", 2)

    def test_rejected_regardless_of_publish_window(self):
        previous_deadline = datetime(2026, 8, 21, tzinfo=UTC)
        published = datetime(2026, 8, 27, tzinfo=UTC)  # comfortably inside the window
        assert not is_gameweek_video(
            "GW3 Team News", 2, published_at=published, published_after=previous_deadline
        )


class TestIsGameweekVideoGeneric:
    PREV_DEADLINE = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)

    def test_all_generic_patterns_are_recognised(self):
        for pattern in GENERIC_IN_SEASON_PATTERNS:
            published = datetime(2026, 8, 22, tzinfo=UTC)
            assert is_gameweek_video(
                pattern.title(), 2, published_at=published, published_after=self.PREV_DEADLINE
            )

    def test_generic_inside_window_accepted(self):
        published = datetime(2026, 8, 22, tzinfo=UTC)
        assert is_gameweek_video(
            "Final Thoughts", 2, published_at=published, published_after=self.PREV_DEADLINE
        )

    def test_generic_at_exact_boundary_accepted(self):
        assert is_gameweek_video(
            "My Team", 2, published_at=self.PREV_DEADLINE, published_after=self.PREV_DEADLINE
        )

    def test_generic_outside_window_rejected(self):
        published = datetime(2026, 8, 20, tzinfo=UTC)  # before the previous deadline
        assert not is_gameweek_video(
            "Team News", 2, published_at=published, published_after=self.PREV_DEADLINE
        )

    def test_generic_with_no_published_at_and_a_window_rejected(self):
        assert not is_gameweek_video(
            "Wildcard", 2, published_at=None, published_after=self.PREV_DEADLINE
        )

    def test_generic_with_no_window_accepted_unconditionally(self):
        # gameweek 1: no previous deadline, so no lower bound at all.
        assert is_gameweek_video("My Team", 1, published_at=None, published_after=None)

    def test_no_pattern_at_all_rejected(self):
        assert not is_gameweek_video("Top 10 Defenders This Season", 2)


class TestResolveGameweekAndWindow:
    def _events(self) -> list[dict]:
        return [
            {"id": 1, "is_next": False, "deadline_time": "2026-08-15T10:00:00Z"},
            {"id": 2, "is_next": True, "deadline_time": "2026-08-28T17:30:00Z"},
            {"id": 3, "is_next": False, "deadline_time": "2026-09-04T17:30:00Z"},
        ]

    def test_defaults_to_is_next_event(self):
        gw, published_after = _resolve_gameweek_and_window(self._events(), None)
        assert gw == 2
        # published_after is the PREVIOUS event's (gw1's) deadline.
        assert published_after == datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    def test_explicit_gameweek_overrides_is_next(self):
        gw, published_after = _resolve_gameweek_and_window(self._events(), 3)
        assert gw == 3
        assert published_after == datetime(2026, 8, 28, 17, 30, tzinfo=UTC)

    def test_gameweek_1_has_no_window(self):
        gw, published_after = _resolve_gameweek_and_window(self._events(), 1)
        assert gw == 1
        assert published_after is None

    def test_missing_previous_event_has_no_window(self):
        events = [{"id": 5, "is_next": True, "deadline_time": "2026-10-01T17:30:00Z"}]
        gw, published_after = _resolve_gameweek_and_window(events, None)
        assert gw == 5
        assert published_after is None

    def test_no_is_next_event_raises(self):
        events = [{"id": 1, "is_next": False, "deadline_time": "2026-08-15T10:00:00Z"}]
        with pytest.raises(ValueError, match="is_next"):
            _resolve_gameweek_and_window(events, None)


def _pick(**overrides) -> Pick:
    payload = {
        "player_name_raw": "Sacca",
        "team_inferred": "Arsenal",
        "position_inferred": "MID",
        "action": "squad_include",
        "conviction": 5,
        "time_horizon": 1,
        "reasoning": "Nailed.",
        "provenance": "personal",
    }
    payload.update(overrides)
    return Pick.model_validate(payload)


def _extraction(picks, gameweek: int | None = 1) -> VideoExtraction:
    return VideoExtraction(
        creator_id="lets-talk-fpl",
        video_id="vidX",
        video_title="MY GW1 TEAM",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        gameweek=gameweek,
        picks=picks,
    )


class TestResolveExtraction:
    def test_matched_pick_gets_player_id_stamped(self, db):
        r = resolve_extraction(_extraction([_pick()]), db)
        (res,) = r.resolutions
        assert res.status is MatchStatus.MATCHED
        assert res.matched_web_name == "Saka"
        stamped = r.extraction.picks[0]
        assert stamped.player_id is not None
        assert db.get(stamped.player_id).web_name == "Saka"

    def test_unmatched_pick_keeps_none_and_carries_candidates(self, db):
        r = resolve_extraction(
            _extraction([_pick(player_name_raw="Ronaldinho", team_inferred=None)]), db
        )
        (res,) = r.resolutions
        assert res.status is not MatchStatus.MATCHED
        assert r.extraction.picks[0].player_id is None
        assert res.matched_web_name is None
        assert res.candidates  # flagged for review, never dropped silently

    def test_resolutions_parallel_picks(self, db):
        r = resolve_extraction(
            _extraction([_pick(), _pick(player_name_raw="Hall and", team_inferred=None)]), db
        )
        assert [res.pick_index for res in r.resolutions] == [0, 1]
        assert len(r.extraction.picks) == 2


def test_render_match_quality_lists_only_unresolved(db):
    r = resolve_extraction(
        _extraction([_pick(), _pick(player_name_raw="Ronaldinho", team_inferred=None)]), db
    )
    report = render_match_quality([r], gameweek=2)
    assert "1/2 picks matched (50%)" in report
    assert "GW2" in report  # the report header keys off the target gameweek, not a hardcoded GW1
    assert "'Ronaldinho'" in report
    assert "'Sacca'" not in report  # matched picks aren't noise in the report
    assert "lets-talk-fpl" in report


# ---------------------------------------------------------------------------
# Gap 2 — the WRONG GAMEWEEK? flag: the last human-visible safety net for a
# stale extraction being reused. render_match_quality's `gw_flag` logic was
# previously only exercised incidentally (the header says "GW2"); these pin
# the flag itself for a mismatch, a match, and the "unknown, not wrong" case.
# ---------------------------------------------------------------------------


class TestRenderMatchQualityFreeHitCount:
    """The per-creator match-quality line surfaces free-hit picks — the
    human review surface tomorrow morning — but only when there are any,
    to keep the common case (no free-hit content) noise-free."""

    def test_free_hit_count_shown_when_positive(self, db):
        r = resolve_extraction(
            _extraction(
                [_pick(squad_context="free_hit"), _pick(squad_context="own_team")], gameweek=2
            ),
            db,
        )
        report = render_match_quality([r], gameweek=2)
        assert "free-hit picks: 1" in report

    def test_free_hit_count_omitted_when_zero(self, db):
        r = resolve_extraction(_extraction([_pick(squad_context="own_team")], gameweek=2), db)
        report = render_match_quality([r], gameweek=2)
        assert "free-hit picks" not in report

    def test_free_hit_count_includes_unresolved_picks(self, db):
        # K counts picks with squad_context FREE_HIT regardless of whether
        # the name resolved — a free-hit draft with an unmatched player is
        # still a free-hit draft.
        r = resolve_extraction(
            _extraction(
                [_pick(squad_context="free_hit", player_name_raw="Ronaldinho", team_inferred=None)],
                gameweek=2,
            ),
            db,
        )
        assert r.resolutions[0].status is not MatchStatus.MATCHED
        report = render_match_quality([r], gameweek=2)
        assert "free-hit picks: 1" in report


class TestRenderMatchQualityWrongGameweekFlag:
    def test_mismatched_gameweek_is_flagged(self, db):
        # _extraction() defaults to gameweek=1; the report is rendered for GW2.
        r = resolve_extraction(_extraction([_pick()]), db)
        report = render_match_quality([r], gameweek=2)
        assert "WRONG GAMEWEEK" in report

    def test_matching_gameweek_is_not_flagged(self, db):
        r = resolve_extraction(_extraction([_pick()], gameweek=2), db)
        report = render_match_quality([r], gameweek=2)
        assert "WRONG GAMEWEEK" not in report

    def test_unknown_gameweek_is_not_flagged(self, db):
        # Absence of evidence isn't evidence of the wrong gameweek.
        r = resolve_extraction(_extraction([_pick()], gameweek=None), db)
        report = render_match_quality([r], gameweek=2)
        assert "WRONG GAMEWEEK" not in report


# ---------------------------------------------------------------------------
# Gap 3 — resolve_target_gameweek: the outer function wrapping
# _resolve_gameweek_and_window with the real (here, faked) FPL bootstrap
# fetch. `get_bootstrap_static` is imported inside the function body, so
# patching `fpl_client_module.get_bootstrap_static` is the correct seam —
# the local `from ... import` re-reads the module attribute at call time.
# ---------------------------------------------------------------------------


class TestResolveTargetGameweek:
    def _events(self) -> list[dict]:
        return [
            {"id": 1, "is_next": False, "deadline_time": "2026-08-15T10:00:00Z"},
            {"id": 2, "is_next": True, "deadline_time": "2026-08-28T17:30:00Z"},
            {"id": 3, "is_next": False, "deadline_time": "2026-09-04T17:30:00Z"},
        ]

    def test_explicit_gameweek_wins_over_api_default(self, monkeypatch):
        monkeypatch.setattr(
            fpl_client_module,
            "get_bootstrap_static",
            lambda force_refresh=False: {"events": self._events()},
        )
        gw, published_after = resolve_target_gameweek(gameweek=3)
        assert gw == 3
        assert published_after == datetime(2026, 8, 28, 17, 30, tzinfo=UTC)

    def test_no_argument_resolves_to_is_next_event(self, monkeypatch):
        monkeypatch.setattr(
            fpl_client_module,
            "get_bootstrap_static",
            lambda force_refresh=False: {"events": self._events()},
        )
        gw, published_after = resolve_target_gameweek()
        assert gw == 2
        # published_after comes from the PREVIOUS event's (gw1's) deadline.
        assert published_after == datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    def test_gameweek_1_yields_no_published_after(self, monkeypatch):
        events = [{"id": 1, "is_next": True, "deadline_time": None}]
        monkeypatch.setattr(
            fpl_client_module,
            "get_bootstrap_static",
            lambda force_refresh=False: {"events": events},
        )
        gw, published_after = resolve_target_gameweek()
        assert gw == 1
        assert published_after is None

    def test_no_event_marked_is_next_raises(self, monkeypatch):
        # End of season: nothing left to target.
        events = [{"id": 38, "is_next": False, "deadline_time": "2027-05-20T14:00:00Z"}]
        monkeypatch.setattr(
            fpl_client_module,
            "get_bootstrap_static",
            lambda force_refresh=False: {"events": events},
        )
        with pytest.raises(ValueError, match="is_next"):
            resolve_target_gameweek()

    def test_previous_event_missing_deadline_time_yields_no_window(self, monkeypatch):
        events = [
            {"id": 1, "is_next": False},  # no deadline_time key at all
            {"id": 2, "is_next": True, "deadline_time": "2026-08-28T17:30:00Z"},
        ]
        monkeypatch.setattr(
            fpl_client_module,
            "get_bootstrap_static",
            lambda force_refresh=False: {"events": events},
        )
        gw, published_after = resolve_target_gameweek()
        assert gw == 2
        assert published_after is None

    def test_force_refresh_reaches_the_bootstrap_fetch(self, monkeypatch):
        captured: dict[str, object] = {}

        def fake_get_bootstrap_static(force_refresh: bool = False) -> dict:
            captured["force_refresh"] = force_refresh
            return {"events": self._events()}

        monkeypatch.setattr(fpl_client_module, "get_bootstrap_static", fake_get_bootstrap_static)
        resolve_target_gameweek(force_refresh=True)
        assert captured["force_refresh"] is True


# ---------------------------------------------------------------------------
# CLI wiring: main() must thread --gameweek through to resolve_target_gameweek
# untouched (None when not passed, so it falls through to the API default).
# ---------------------------------------------------------------------------


class TestMainGameweekWiring:
    def test_explicit_gameweek_flag_reaches_resolve(self, monkeypatch, tmp_path):
        captured: dict[str, object] = {}

        def fake_resolve(gameweek=None, force_refresh=False):
            captured["gameweek"] = gameweek
            return (5, None)

        monkeypatch.setattr(run_extract, "resolve_target_gameweek", fake_resolve)
        monkeypatch.setattr(
            run_extract,
            "run_extraction",
            lambda *a, **k: ExtractionRunSummary(
                report_path=tmp_path / "mq.md", attempted=1, succeeded=1, failures=[], skipped=[]
            ),
        )
        run_extract.main(["--gameweek", "5"])
        assert captured["gameweek"] == 5

    def test_no_gameweek_flag_passes_none(self, monkeypatch, tmp_path):
        captured: dict[str, object] = {}

        def fake_resolve(gameweek=None, force_refresh=False):
            captured["gameweek"] = gameweek
            return (2, None)

        monkeypatch.setattr(run_extract, "resolve_target_gameweek", fake_resolve)
        monkeypatch.setattr(
            run_extract,
            "run_extraction",
            lambda *a, **k: ExtractionRunSummary(
                report_path=tmp_path / "mq.md", attempted=1, succeeded=1, failures=[], skipped=[]
            ),
        )
        run_extract.main([])
        assert captured["gameweek"] is None


# ---------------------------------------------------------------------------
# Gap 1 — run_extraction's skip-if-recent guard. This is the guard that
# stops a mid-run retry from re-billing every creator: cold run pays,
# a warm run (extraction already on disk) does NOT call Claude again unless
# --force, a corrupt cache file is treated as a miss (never aborts the run),
# and writes are atomic. video selection, fetch_transcript_from, extract_picks,
# and PlayerDB are all faked — no network, no Anthropic calls. These pass
# source="captions" explicitly so a faked fetch_transcript_from is the only
# transcript-layer call; the gemini path's extra duration lookup is covered
# separately in TestResolveTranscriptSourceDefault below.
# ---------------------------------------------------------------------------


def _video_info(video_id: str, title: str = "GW2 TEAM REVEAL", **overrides) -> VideoInfo:
    payload = {
        "video_id": video_id,
        "channel_id": "UC-fake",
        "title": title,
        "published_at": datetime(2026, 8, 22, tzinfo=UTC),
        "description": "",
    }
    payload.update(overrides)
    return VideoInfo.model_validate(payload)


def _creator_fixture(creator_id: str) -> Creator:
    return Creator(creator_id=creator_id, name=creator_id, youtube_hint=creator_id, tier=Tier.CORE)


def _transcript_fixture(video_id: str) -> Transcript:
    return Transcript(
        video_id=video_id,
        language="en",
        is_generated=True,
        segments=[TranscriptSegment(text="my team is Saka and co", start=0.0, duration=2.0)],
    )


def _resolved_extraction(
    creator_id: str, video_id: str, gameweek: int | None = 2
) -> VideoExtraction:
    return VideoExtraction(
        creator_id=creator_id,
        video_id=video_id,
        video_title="GW2 TEAM REVEAL",
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        gameweek=gameweek,
        picks=[_pick()],
    )


def _fake_select_videos(selections: list[VideoSelection]):
    def fake(gameweek, published_after, creator_ids=None, force_refresh=False):
        return selections

    return fake


class TestRunExtractionSkipIfRecent:
    def test_cold_run_calls_claude_and_reports_no_skips(self, monkeypatch, tmp_path, db) -> None:
        creator = _creator_fixture("andy")
        video = _video_info("vid1")
        monkeypatch.setattr(
            run_extract,
            "select_videos",
            _fake_select_videos([VideoSelection(creator=creator, video=video, error=None)]),
        )
        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)
        monkeypatch.setattr(
            run_extract,
            "fetch_transcript_from",
            lambda video_id, source, *, duration_seconds=None: _transcript_fixture(video_id),
        )
        calls: list[str] = []

        def fake_extract_picks(*, creator_id, video_id, **kwargs):
            calls.append(creator_id)
            return _resolved_extraction(creator_id, video_id)

        monkeypatch.setattr(run_extract, "extract_picks", fake_extract_picks)
        monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: db))

        summary = run_extract.run_extraction(
            gameweek=2, published_after=None, output_dir=tmp_path, source="captions"
        )

        assert calls == ["andy"]
        assert summary.skipped == []
        assert summary.skip_count == 0
        assert summary.succeeded == 1
        assert (tmp_path / "vid1.json").exists()

    def test_warm_run_skips_claude_call_and_is_counted_as_skipped(
        self, monkeypatch, tmp_path, db
    ) -> None:
        creator = _creator_fixture("andy")
        video = _video_info("vid1")
        monkeypatch.setattr(
            run_extract,
            "select_videos",
            _fake_select_videos([VideoSelection(creator=creator, video=video, error=None)]),
        )
        existing = ResolvedExtraction(
            extraction=_resolved_extraction("andy", "vid1"), resolutions=[]
        )
        (tmp_path / "vid1.json").write_text(existing.model_dump_json())

        def boom_extract(*a, **k):
            raise AssertionError("extract_picks must NOT be called on a warm skip")

        def boom_transcript(*a, **k):
            raise AssertionError("fetch_transcript_from must NOT be called on a warm skip")

        monkeypatch.setattr(run_extract, "extract_picks", boom_extract)
        monkeypatch.setattr(run_extract, "fetch_transcript_from", boom_transcript)
        monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: db))

        summary = run_extract.run_extraction(
            gameweek=2, published_after=None, output_dir=tmp_path, source="captions"
        )

        assert summary.skipped == ["andy"]
        assert summary.skip_count == 1
        # A deliberate, documented choice (see ExtractionRunSummary's docstring):
        # a skip still counts toward `succeeded` — a stored extraction did result.
        assert summary.succeeded == 1

    def test_force_flag_re_extracts_despite_existing_file(self, monkeypatch, tmp_path, db) -> None:
        creator = _creator_fixture("andy")
        video = _video_info("vid1")
        monkeypatch.setattr(
            run_extract,
            "select_videos",
            _fake_select_videos([VideoSelection(creator=creator, video=video, error=None)]),
        )
        existing = ResolvedExtraction(
            extraction=_resolved_extraction("andy", "vid1"), resolutions=[]
        )
        (tmp_path / "vid1.json").write_text(existing.model_dump_json())
        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)
        monkeypatch.setattr(
            run_extract,
            "fetch_transcript_from",
            lambda video_id, source, *, duration_seconds=None: _transcript_fixture(video_id),
        )
        calls: list[str] = []

        def fake_extract_picks(*, creator_id, video_id, **kwargs):
            calls.append(creator_id)
            return _resolved_extraction(creator_id, video_id)

        monkeypatch.setattr(run_extract, "extract_picks", fake_extract_picks)
        monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: db))

        summary = run_extract.run_extraction(
            gameweek=2, published_after=None, output_dir=tmp_path, force=True, source="captions"
        )

        assert calls == ["andy"]
        assert summary.skipped == []

    def test_corrupt_extraction_file_is_reextracted_and_run_continues(
        self, monkeypatch, tmp_path, db
    ) -> None:
        creator1 = _creator_fixture("andy")
        video1 = _video_info("vid1")
        creator2 = _creator_fixture("bob")
        video2 = _video_info("vid2")
        monkeypatch.setattr(
            run_extract,
            "select_videos",
            _fake_select_videos(
                [
                    VideoSelection(creator=creator1, video=video1, error=None),
                    VideoSelection(creator=creator2, video=video2, error=None),
                ]
            ),
        )
        # Truncated/corrupt — as a Ctrl-C or sleeping laptop mid-write would leave it.
        (tmp_path / "vid1.json").write_text('{"extraction": {"video_id": "vid1", trunc')
        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)
        monkeypatch.setattr(
            run_extract,
            "fetch_transcript_from",
            lambda video_id, source, *, duration_seconds=None: _transcript_fixture(video_id),
        )
        calls: list[str] = []

        def fake_extract_picks(*, creator_id, video_id, **kwargs):
            calls.append(creator_id)
            return _resolved_extraction(creator_id, video_id)

        monkeypatch.setattr(run_extract, "extract_picks", fake_extract_picks)
        monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: db))

        summary = run_extract.run_extraction(
            gameweek=2, published_after=None, output_dir=tmp_path, source="captions"
        )

        # The corrupt file did not abort the run: both creators were reached.
        assert calls == ["andy", "bob"]
        assert summary.skipped == []
        assert summary.succeeded == 2
        assert summary.failed == 0

    def test_atomic_write_leaves_no_tmp_file_behind(self, monkeypatch, tmp_path, db) -> None:
        creator = _creator_fixture("andy")
        video = _video_info("vid1")
        monkeypatch.setattr(
            run_extract,
            "select_videos",
            _fake_select_videos([VideoSelection(creator=creator, video=video, error=None)]),
        )
        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)
        monkeypatch.setattr(
            run_extract,
            "fetch_transcript_from",
            lambda video_id, source, *, duration_seconds=None: _transcript_fixture(video_id),
        )
        monkeypatch.setattr(
            run_extract,
            "extract_picks",
            lambda *, creator_id, video_id, **kwargs: _resolved_extraction(creator_id, video_id),
        )
        monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: db))

        run_extract.run_extraction(
            gameweek=2, published_after=None, output_dir=tmp_path, source="captions"
        )

        assert not list(tmp_path.glob("*.json.tmp"))
        assert (tmp_path / "vid1.json").exists()


# ---------------------------------------------------------------------------
# Gemini is the default transcript source (see ingest/sourcing.py). These pin
# that default reaching _resolve_transcript and, from there,
# fetch_transcript_from — and that run_extraction looks durations up for the
# gemini path only.
# ---------------------------------------------------------------------------


class TestResolveTranscriptSourceDefault:
    def test_resolve_transcript_defaults_to_gemini(self, monkeypatch) -> None:
        video = _video_info("vid1")
        seen: dict = {}

        def fake_fetch(video_id, source, *, duration_seconds=None):
            seen["source"] = source
            seen["duration_seconds"] = duration_seconds
            return _transcript_fixture(video_id)

        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(run_extract, "fetch_transcript_from", fake_fetch)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)

        video_out, transcript, blocked = run_extract._resolve_transcript([video], "andy")

        assert seen["source"] == "gemini"
        assert video_out is video
        assert transcript is not None
        assert blocked is False

    def test_resolve_transcript_threads_duration_for_the_matching_video(self, monkeypatch) -> None:
        video = _video_info("vid1")
        seen: dict = {}

        def fake_fetch(video_id, source, *, duration_seconds=None):
            seen["duration_seconds"] = duration_seconds
            return _transcript_fixture(video_id)

        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(run_extract, "fetch_transcript_from", fake_fetch)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)

        run_extract._resolve_transcript([video], "andy", "gemini", {"vid1": 512})

        assert seen["duration_seconds"] == 512

    def test_run_extraction_default_source_looks_up_durations_and_calls_gemini(
        self, monkeypatch, tmp_path, db
    ) -> None:
        creator = _creator_fixture("andy")
        video = _video_info("vid1")
        monkeypatch.setattr(
            run_extract,
            "select_videos",
            _fake_select_videos([VideoSelection(creator=creator, video=video, error=None)]),
        )
        monkeypatch.setattr(run_extract, "get_video_durations", lambda ids: {"vid1": 400})
        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        seen: dict = {}

        def fake_fetch(video_id, source, *, duration_seconds=None):
            seen["source"] = source
            seen["duration_seconds"] = duration_seconds
            return _transcript_fixture(video_id)

        monkeypatch.setattr(run_extract, "fetch_transcript_from", fake_fetch)
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: None)
        monkeypatch.setattr(
            run_extract,
            "extract_picks",
            lambda *, creator_id, video_id, **kwargs: _resolved_extraction(creator_id, video_id),
        )
        monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: db))

        summary = run_extract.run_extraction(gameweek=2, published_after=None, output_dir=tmp_path)

        assert seen["source"] == "gemini"
        assert seen["duration_seconds"] == 400
        assert summary.succeeded == 1

    def test_durations_lookup_transport_error_is_non_fatal(self, monkeypatch) -> None:
        """Same non-fatal contract as warm_cache._durations_for: a
        transport-level failure (connection refused, timeout) must not
        escape `_durations_for` and abort the whole extraction run."""

        def failing_durations(ids):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(run_extract, "get_video_durations", failing_durations)

        result = run_extract._durations_for([_video_info("vid1")], "andy")

        assert result == {}


# ---------------------------------------------------------------------------
# A network-fetched transcript (gemini in particular: 30-90s per video) must
# land on disk, not just be handed back to the caller — otherwise a retry of
# this creator, or tomorrow's warm_cache run, pays for the same fetch again.
# `fetch_transcript_from` only READS the cache; `_resolve_transcript` is
# responsible for writing a freshly-fetched one.
# ---------------------------------------------------------------------------


class TestResolveTranscriptSaves:
    def test_fetched_transcript_is_saved(self, monkeypatch) -> None:
        video = _video_info("vid1")
        fetched = _transcript_fixture("vid1")
        saved: list[Transcript] = []

        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: None)
        monkeypatch.setattr(
            run_extract, "fetch_transcript_from", lambda video_id, source, **k: fetched
        )
        monkeypatch.setattr(run_extract, "save_transcript", lambda t: saved.append(t))

        run_extract._resolve_transcript([video], "andy")

        assert saved == [fetched]

    def test_cached_transcript_is_not_re_saved(self, monkeypatch) -> None:
        video = _video_info("vid1")
        cached = _transcript_fixture("vid1")

        monkeypatch.setattr(run_extract, "load_transcript", lambda video_id: cached)

        def boom_fetch(*a, **k):
            raise AssertionError("fetch_transcript_from must not be called on a cache hit")

        def boom_save(*a, **k):
            raise AssertionError("save_transcript must not be called for an already-cached one")

        monkeypatch.setattr(run_extract, "fetch_transcript_from", boom_fetch)
        monkeypatch.setattr(run_extract, "save_transcript", boom_save)

        _video_out, transcript, _blocked = run_extract._resolve_transcript([video], "andy")

        assert transcript == cached
