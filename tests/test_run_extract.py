"""Tests for extract/run_extract.py — no network, no LLM."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_oracle.extract.run_extract import (
    GENERIC_IN_SEASON_PATTERNS,
    _resolve_gameweek_and_window,
    gameweek_title_patterns,
    is_gameweek_video,
    render_match_quality,
    resolve_extraction,
)
from fpl_oracle.extract.schemas import Pick, VideoExtraction
from fpl_oracle.fpl.players import MatchStatus, PlayerDB
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


def _extraction(picks) -> VideoExtraction:
    return VideoExtraction(
        creator_id="lets-talk-fpl",
        video_id="vidX",
        video_title="MY GW1 TEAM",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        gameweek=1,
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
