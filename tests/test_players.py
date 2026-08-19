"""Tests for fpl_oracle.fpl.players: Position mapping, Player/PlayerDB
parsing, and the composite-key name resolver.

Fixture payload: a small hand-built bootstrap-static-shaped dict (never
touches `data/` or the network) covering the cases the fpl-domain skill
and two rounds of clarification/review call out as the #1 data-quality
risk — exact matches, transcript manglings, the "bare 'Gabriel' matches
three Arsenal players" example, and the `token_set_ratio` subset-100
pathologies found by probing the real bootstrap roster ("Hall and"
matching a player web-named "Hall"; bare first names matching a full
name's first half).
"""

from __future__ import annotations

import pytest

from fpl_oracle.fpl.players import (
    MatchStatus,
    Player,
    PlayerDB,
    Position,
    _Candidate,
    _team_agreement,
    _tier1_trustworthy,
)

_TEAMS = [
    {"id": 1, "name": "Arsenal", "short_name": "ARS"},
    {"id": 2, "name": "Manchester City", "short_name": "MCI"},
    {"id": 3, "name": "Liverpool", "short_name": "LIV"},
    {"id": 4, "name": "Everton", "short_name": "EVE"},
    {"id": 5, "name": "Chelsea", "short_name": "CHE"},
    {"id": 6, "name": "Spurs", "short_name": "TOT"},
    {"id": 7, "name": "Man Utd", "short_name": "MUN"},
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
    {
        # Subset-100 trap: token_set_ratio("Hall and", "Hall") == 100,
        # even though this is a completely different player at a
        # different club/position than Haaland. Different team (Everton,
        # not Man City) AND different position (DEF, not FWD) from the
        # "Hall and" -> Haaland mangling this is meant to trap.
        "id": 9,
        "web_name": "Hall",
        "first_name": "Some",
        "second_name": "Hall",
        "team": 4,
        "element_type": 2,  # DEF
        "now_cost": 45,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        # For the [80,85) tie-split-requires-phonetics regression and the
        # bare-first-name ("Cole") subset pathology.
        "id": 10,
        "web_name": "Palmer",
        "first_name": "Cole",
        "second_name": "Palmer",
        "team": 5,
        "element_type": 3,  # MID
        "now_cost": 65,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        # First-name-only pathology fixture: two players sharing a first
        # name ("Mohamed") that a bare extracted name must not silently
        # resolve to either of.
        "id": 11,
        "web_name": "M.Salah",
        "first_name": "Mohamed",
        "second_name": "Salah",
        "team": 3,
        "element_type": 3,  # MID
        "now_cost": 130,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 12,
        "web_name": "Diaby",
        "first_name": "Mohamed",
        "second_name": "Diaby",
        "team": 4,
        "element_type": 3,  # MID
        "now_cost": 45,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 13,
        "web_name": "Son",
        "first_name": "Heung-min",
        "second_name": "Son",
        "team": 6,
        "element_type": 3,  # MID
        "now_cost": 95,
        "status": "a",
        "chance_of_playing_next_round": 100,
    },
    {
        "id": 14,
        "web_name": "Fernandes",
        "first_name": "Bruno",
        "second_name": "Fernandes",
        "team": 7,
        "element_type": 3,  # MID
        "now_cost": 85,
        "status": "a",
        "chance_of_playing_next_round": 100,
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
        # This is the subset-100 trap: token_set_ratio("Hall and", "Hall")
        # == 100, so without the Tier-1 contradiction veto + subset
        # hardening this would silently MATCH the decoy player web-named
        # "Hall" (id 9, Everton DEF) instead of Haaland — wrong player,
        # contradicting both the team (Man City) and position (FWD)
        # hints. The veto rejects the "Hall" decoy and falls through to
        # Tier 2, where Haaland resolves correctly via phonetic + team +
        # position corroboration.
        result = db.resolve("Hall and", team_inferred="Man City", position_inferred="FWD")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 2

    def test_bare_hall_and_is_unmatched_not_the_decoy(self, db: PlayerDB) -> None:
        # Without team/position hints, neither the "Hall" decoy (subset
        # match, no phonetic/token_sort backing) nor Haaland (fuzzy 45.5,
        # no composite-key corroboration available) clears either tier.
        result = db.resolve("Hall and")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None

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
        # UNMATCHED still carries candidates for human review — there is
        # no separate "uncorroborated ambiguous cluster" status anymore.
        assert len(result.candidates) > 0

    def test_another_garbage_name_is_unmatched(self, db: PlayerDB) -> None:
        # A second garbage name, since a single example isn't strong
        # evidence against the "score-only cluster" false-AMBIGUOUS
        # failure mode the real-roster probe found (8/20 garbage names
        # came back AMBIGUOUS under the old design).
        result = db.resolve("Beckham")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None

    def test_low_fuzzy_score_rejected_even_with_composite_key(self, db: PlayerDB) -> None:
        # Below the Tier 2 floor, composite-key corroboration alone can't
        # rescue a match — the name signal itself has to clear a sanity
        # bar first.
        result = db.resolve("Zzzqx", team_inferred="ARS", position_inferred="DEF")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None


class TestResolveFirstNameOnlyPathology:
    """ "Mohamed"/"Declan"/"Cole" hit a sole/tied top-100 via the
    full-name half of token_set_ratio's subset match — the SAME root
    cause as the "Hall and" trap, just via the full_name field instead
    of web_name. Must not silently resolve to any specific player."""

    def test_bare_cole_does_not_silently_match_palmer(self, db: PlayerDB) -> None:
        # Sole-winner variant: "Cole" hits fuzzy=100 against ONLY
        # Palmer's full name ("Cole Palmer") in this fixture — no other
        # candidate is anywhere close, so this exercises the sole-winner
        # Tier-1 hardening path directly (the exact shape of the bug: a
        # lone top-100 with nothing else in `tied` to trigger a
        # composite-key check at all, pre-fix).
        result = db.resolve("Cole")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None

    def test_bare_mohamed_does_not_silently_match_either_shared_name(self, db: PlayerDB) -> None:
        # Tied variant: two players (Salah, Diaby) share the first name
        # "Mohamed" and both hit fuzzy=100 via their full names.
        result = db.resolve("Mohamed")
        assert result.player is None
        assert result.status in (MatchStatus.AMBIGUOUS, MatchStatus.UNMATCHED)


class TestResolveTier1ContradictionVeto:
    def test_stale_team_hint_does_not_veto_an_exact_name(self, db: PlayerDB) -> None:
        # BEHAVIOUR CHANGE (2026-08-19), driven by the first live GW1 run:
        # a contradicting TEAM no longer vetoes a sole EXACT name match.
        # The extracting LLM infers team from its own PL knowledge, which
        # lags the summer transfer window, so it emitted "Semenyo +
        # Bournemouth" (Man City), "Isak + Newcastle" (Liverpool) and 18
        # more — every one a correct player killed by a stale hint. An
        # exact name plus an agreeing position outweighs a team string
        # guessed from memory. Palmer here stands in for that case.
        result = db.resolve("Palmer", team_inferred="Man City", position_inferred="MID")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 10

    def test_contradicting_position_still_vetoes_an_exact_name(self, db: PlayerDB) -> None:
        # The other half of the rule: positions do NOT change mid-window,
        # so a position contradiction stays disqualifying even for an
        # exact name. This is what keeps the veto meaningful.
        result = db.resolve("Palmer", team_inferred="Chelsea", position_inferred="GK")
        assert result.player is None
        assert result.status == MatchStatus.UNMATCHED

    def test_contradicting_team_still_vetoes_a_non_exact_name(self, db: PlayerDB) -> None:
        # The team veto's own regression pin. Deliberately NOT the "Hall
        # and" case: the Hall decoy is a DEF, so a FWD hint is vetoed by
        # the position rule before the team rule is ever consulted — a
        # review mutation proved that deleting the team veto entirely
        # left that test green. "Erling Halaand" is non-exact (a typo)
        # but clears the token_sort_ratio backstop, so ONLY the team
        # veto can stop it.
        wrong_club = db.resolve("Erling Halaand", team_inferred="Everton", position_inferred="FWD")
        assert wrong_club.player is None

        right_club = db.resolve("Erling Halaand", team_inferred="Man City", position_inferred="FWD")
        assert right_club.player is not None
        assert right_club.player.web_name == "Haaland"

    def test_exact_full_name_is_trusted_over_the_web_name_it_tied_with(self, db: PlayerDB) -> None:
        # The van Dijk bug, pinned. FPL stores him as web_name "Van Dijk"
        # with full name "Virgil van Dijk"; the raw string scores 100
        # against BOTH, and whichever wins the max becomes
        # `matched_target`. When that was the web_name, the exact FULL
        # name was misread as a suspicious token-subset and a certain
        # match was thrown away (live: "Virgil van Dijk" + Liverpool +
        # DEF -> UNMATCHED at score 100). Exactness must be judged
        # against both names, not just the one that won the tie.
        result = db.resolve("Virgil van Dijk", team_inferred="Liverpool", position_inferred="DEF")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 3

    def test_exact_name_override_requires_a_positively_agreeing_position(
        self, db: PlayerDB
    ) -> None:
        # Overriding a contradicting team is only allowed when the
        # position positively agrees — a missing position is not enough,
        # or an exact surname plus a wrong club would match on the name
        # alone.
        assert db.resolve("Palmer", team_inferred="Man City", position_inferred="MID").player
        assert db.resolve("Palmer", team_inferred="Man City", position_inferred=None).player is None

    def test_matching_hint_still_accepts_sole_winner(self, db: PlayerDB) -> None:
        # Sanity check on the veto: a CONFIRMING hint must not break the
        # ordinary sole-winner accept path.
        result = db.resolve("Palmer", team_inferred="Chelsea", position_inferred="MID")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 10


class TestTier1TrustworthySubset85Boundary:
    """Direct (white-box) pin on the [80,85) tie-split rule: composite-key
    (team+position) agreement alone must never accept a sub-85 subset
    match without phonetic corroboration. token_set_ratio is guaranteed
    >= token_sort_ratio, so nothing under 90 can ever clear the
    token_sort alternative — phonetic agreement is the only way through
    below that. Tested directly against `_tier1_trustworthy` since
    engineering a real two-candidate tie that lands a second member
    exactly in [80,85) is incidental to rapidfuzz's scoring internals,
    not to the rule being pinned."""

    def _candidate(self, *, fuzzy: float, matched_target: str, phonetic: bool) -> _Candidate:
        player = Player(
            player_id=999,
            web_name="Placeholder",
            first_name="Place",
            second_name="Holder",
            team_id=1,
            team_short="ARS",
            position=Position.MID,
            now_cost=50,
            status="a",
            chance_of_playing_next_round=100,
        )
        return _Candidate(
            player=player,
            fuzzy=fuzzy,
            matched_target=matched_target,
            phonetic=phonetic,
            team_agree=True,
            position_agree=True,
        )

    def test_sub_85_subset_match_without_phonetic_is_not_trustworthy(self) -> None:
        candidate = self._candidate(fuzzy=83.3, matched_target="Palmer", phonetic=False)
        assert _tier1_trustworthy("Pamder", candidate) is False

    def test_sub_85_subset_match_with_phonetic_is_trustworthy(self) -> None:
        candidate = self._candidate(fuzzy=83.3, matched_target="Palmer", phonetic=True)
        assert _tier1_trustworthy("Pamder", candidate) is True

    def test_exact_token_match_is_always_trustworthy_regardless_of_phonetic(self) -> None:
        candidate = self._candidate(fuzzy=100.0, matched_target="Saka", phonetic=False)
        assert _tier1_trustworthy("Saka", candidate) is True


class TestResolveTier2FloorNoPhoneticNoAccept:
    """Black-box companion to the white-box test above: the same
    "Pamder"/Palmer pair (fuzzy 83.3, below Tier 1, no phonetic
    agreement) must not match even end-to-end through resolve(), despite
    full team+position corroboration."""

    def test_pamder_does_not_match_palmer_without_phonetic_agreement(self, db: PlayerDB) -> None:
        result = db.resolve("Pamder", team_inferred="Chelsea", position_inferred="MID")
        assert result.status == MatchStatus.UNMATCHED
        assert result.player is None


class TestTeamAliases:
    """Extractor-style full club names that don't fuzzy-match the current
    bootstrap-static short_name/name fields well (measured against the
    live payload: "Tottenham"->Spurs=50.0, "Manchester United"->"Man
    Utd"=58.3, both below the confirm bar) must still corroborate via the
    alias table."""

    def test_tottenham_confirms_tot(self) -> None:
        assert _team_agreement("Tottenham", "TOT", "Spurs") is True

    def test_tottenham_hotspur_confirms_tot(self) -> None:
        assert _team_agreement("Tottenham Hotspur", "TOT", "Spurs") is True

    def test_manchester_united_confirms_mun(self) -> None:
        assert _team_agreement("Manchester United", "MUN", "Man Utd") is True

    def test_alias_contradicts_wrong_team(self) -> None:
        # An alias hit is decisive both ways — "Tottenham" must not
        # neutrally shrug at a Man City candidate, it should contradict.
        assert _team_agreement("Tottenham", "MCI", "Man City") is False

    def test_son_resolves_with_tottenham_alias_via_tier2(self, db: PlayerDB) -> None:
        # End-to-end: "Sun" vs "Son" scores 66.7 — below Tier 1, so this
        # only clears Tier 2, and only with the Tottenham alias
        # corroborating (without it, team_agree is None and Tier 2's
        # composite-key requirement fails).
        result = db.resolve("Sun", team_inferred="Tottenham", position_inferred="MID")
        assert result.status == MatchStatus.MATCHED
        assert result.player is not None
        assert result.player.player_id == 13


@pytest.mark.network
class TestBootstrapLive:
    def test_live_bootstrap_parses_and_has_many_players(self) -> None:
        db = PlayerDB.load()
        assert len(db) > 400
