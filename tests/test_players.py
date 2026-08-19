"""Tests for fpl_oracle.fpl.players: Position mapping, Player/PlayerDB
parsing, and the composite-key name resolver.

Fixture payload: a small hand-built bootstrap-static-shaped dict (never
touches `data/` or the network) covering the cases the fpl-domain skill
calls out as the #1 data-quality risk — exact matches, transcript
manglings, and the "bare 'Gabriel' matches three Arsenal players" example.
"""

from __future__ import annotations

import pytest

from fpl_oracle.fpl.players import MatchStatus, PlayerDB, Position

_TEAMS = [
    {"id": 1, "name": "Arsenal", "short_name": "ARS"},
    {"id": 2, "name": "Manchester City", "short_name": "MCI"},
    {"id": 3, "name": "Liverpool", "short_name": "LIV"},
    {"id": 4, "name": "Everton", "short_name": "EVE"},
]

_ELEMENTS = [
    {
        "id": 1,
        "web_name": "Saka",
        "first_name": "Bukayo",
        "second_name": "Saka",
        "team": 1,
        "element_type": 3,  # MID
        "now_cost": 100,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 2,
        "web_name": "Haaland",
        "first_name": "Erling",
        "second_name": "Haaland",
        "team": 2,
        "element_type": 4,  # FWD
        "now_cost": 150,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 3,
        "web_name": "Van Dijk",
        "first_name": "Virgil",
        "second_name": "van Dijk",
        "team": 3,
        "element_type": 2,  # DEF
        "now_cost": 65,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 4,
        "web_name": "Gabriel",
        "first_name": "Gabriel",
        "second_name": "Magalhaes",
        "team": 1,
        "element_type": 2,  # DEF
        "now_cost": 60,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 5,
        "web_name": "Martinelli",
        "first_name": "Gabriel",
        "second_name": "Martinelli",
        "team": 1,
        "element_type": 3,  # MID
        "now_cost": 70,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 6,
        "web_name": "G.Jesus",
        "first_name": "Gabriel",
        "second_name": "Jesus",
        "team": 1,
        "element_type": 4,  # FWD
        "now_cost": 75,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 7,
        "web_name": "Raya",
        "first_name": "David",
        "second_name": "Raya",
        "team": 1,
        "element_type": 1,  # GK
        "now_cost": 55,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 8,
        "web_name": "Cheapo",
        "first_name": "Some",
        "second_name": "Cheapo",
        "team": 4,
        "element_type": 2,  # DEF
        "now_cost": 40,
        "status": "a",
        "chance_of_playing_next_round": None,
    },
]


def _bootstrap_payload() -> dict:
    return {"teams": _TEAMS, "elements": _ELEMENTS}


@pytest.fixture
def db() -> PlayerDB:
    return PlayerDB.from_bootstrap(_bootstrap_payload())


class TestPositionFromElementType:
    def test_maps_known_types(self) -> None:
        assert Position.from_element_type(1) == Position.GK
        assert Position.from_element_type(2) == Position.DEF
        assert Position.from_element_type(3) == Position.MID
        assert Position.from_element_type(4) == Position.FWD

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown FPL element_type"):
            Position.from_element_type(5)


class TestPlayerPriceProperty:
    def test_price_m_converts_tenths_to_millions(self, db: PlayerDB) -> None:
        raya = db.get(7)
        assert raya is not None
        assert raya.now_cost == 55
        assert raya.price_m == 5.5

    def test_full_name_combines_first_and_second(self, db: PlayerDB) -> None:
        saka = db.get(1)
        assert saka is not None
        assert saka.full_name == "Bukayo Saka"


class TestPlayerDBFromBootstrap:
    def test_all_players_loaded(self, db: PlayerDB) -> None:
        assert len(db) == len(_ELEMENTS)

    def test_team_short_resolved_from_teams_list(self, db: PlayerDB) -> None:
        saka = db.get(1)
        assert saka is not None
        assert saka.team_short == "ARS"

    def test_null_chance_of_playing_stays_none(self, db: PlayerDB) -> None:
        # bootstrap-static can carry a null chance_of_playing_next_round
        # even for an available player — must not default to 100.
        cheapo = db.get(8)
        assert cheapo is not None
        assert cheapo.status == "a"
        assert cheapo.chance_of_playing_next_round is None


class TestResolveExactMatch:
    def test_exact_web_name_matches(self, db: PlayerDB) -> None:
        result = db.resolve("Saka")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 1


class TestResolveTranscriptManglings:
    """The fpl-domain skill's own canonical examples. Plain rapidfuzz
    token_set_ratio does not clear the 85 auto-accept threshold for any
    of these (measured: Sacca/Saka=66.7, Hall and/Haaland=45.5,
    Van Dyke/van Dijk=75.0) — they only resolve via the Tier 2 phonetic
    fallback, which requires team+position corroboration. That's the
    realistic path: the extractor infers team/position from context."""

    def test_sacca_resolves_to_saka_with_composite_key(self, db: PlayerDB) -> None:
        result = db.resolve("Sacca", team_inferred="Arsenal", position_inferred="MID")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 1

    def test_hall_and_resolves_to_haaland_with_composite_key(self, db: PlayerDB) -> None:
        result = db.resolve("Hall and", team_inferred="Man City", position_inferred="FWD")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 2

    def test_van_dyke_resolves_to_van_dijk_with_composite_key(self, db: PlayerDB) -> None:
        result = db.resolve("Van Dyke", team_inferred="Liverpool", position_inferred="DEF")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 3

    def test_sacca_without_composite_key_stays_unmatched(self, db: PlayerDB) -> None:
        # Pins the epistemics: a sub-85 fuzzy score is NEVER auto-accepted
        # alone, no matter how "obviously" it reads as a mangling to a
        # human. Without team/position corroboration there's nothing to
        # promote it out of Tier 2, so it must not silently match.
        result = db.resolve("Sacca")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None


class TestResolveBareGabrielAmbiguous:
    def test_bare_gabriel_is_ambiguous(self, db: PlayerDB) -> None:
        result = db.resolve("Gabriel")
        assert result.status == MatchStatus.AMBIGUOUS
        assert result.player is None
        candidate_ids = {c.player_id for c in result.candidates}
        assert {4, 5, 6}.issubset(candidate_ids)

    def test_gabriel_with_team_and_position_resolves_to_magalhaes(self, db: PlayerDB) -> None:
        result = db.resolve("Gabriel", team_inferred="ARS", position_inferred="DEF")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 4
        assert result.player.second_name == "Magalhaes"


class TestResolveGarbageAndThreshold:
    def test_garbage_name_is_unmatched_with_no_player(self, db: PlayerDB) -> None:
        result = db.resolve("Ronaldinho")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None

    def test_low_fuzzy_score_rejected_even_with_composite_key(self, db: PlayerDB) -> None:
        # Below the Tier 2 floor, composite-key corroboration alone can't
        # rescue a match — the name signal itself has to clear a sanity
        # bar first.
        result = db.resolve("Zzzqx", team_inferred="ARS", position_inferred="DEF")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None


@pytest.mark.network
class TestBootstrapLive:
    def test_live_bootstrap_parses_and_has_many_players(self) -> None:
        db = PlayerDB.load()
        assert len(db) > 400
