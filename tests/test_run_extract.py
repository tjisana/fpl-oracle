"""Tests for extract/run_extract.py — no network, no LLM."""

from datetime import UTC, datetime

import pytest

from fpl_oracle.extract.run_extract import (
    is_gw1_team_video,
    render_match_quality,
    resolve_extraction,
)
from fpl_oracle.extract.schemas import Pick, VideoExtraction
from fpl_oracle.fpl.players import MatchStatus, PlayerDB
from tests.test_players import _bootstrap_payload


@pytest.fixture
def db() -> PlayerDB:
    return PlayerDB.from_bootstrap(_bootstrap_payload())


class TestIsGw1TeamVideo:
    @pytest.mark.parametrize(
        "title",
        [
            "MY GW1 TEAM REVEAL",
            "FPL Gameweek 1 team selection!",
            "GW1 TEAM + GW17 PLANS",
            "FINAL TEAM GW 1",
            "WILDCARD ALREADY?!",
            # live misses from the FFH channel (persona reveals)
            "FPL Salah's FINAL FPL Team 🔒 | 4 x Top 1k! 💥 | Fantasy Premier League 2026/27",
            "BigManBakar's FINAL FPL Team 🔒 | 4th In The World!🌍| Fantasy Premier League 2026/27",
        ],
    )
    def test_positives(self, title):
        assert is_gw1_team_video(title)

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
        assert not is_gw1_team_video(title)

    def test_gw1x_title_can_still_match_on_non_numeric_pattern(self):
        assert is_gw1_team_video("GW14 WILDCARD TEAM REVEAL")
        assert not is_gw1_team_video("GW14 CAPTAIN PICKS")

    def test_genuine_gw1_survives_alongside_gw1x_mention(self):
        # review finding: the old guard discarded gw patterns entirely
        assert is_gw1_team_video("GW1 TEAM + GW17 PLANS")
        assert is_gw1_team_video("MY GW1 SQUAD | THOUGHTS ON GW15")


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
    report = render_match_quality([r])
    assert "1/2 picks matched (50%)" in report
    assert "'Ronaldinho'" in report
    assert "'Sacca'" not in report  # matched picks aren't noise in the report
    assert "lets-talk-fpl" in report
