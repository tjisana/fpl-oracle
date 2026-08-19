"""FPL player database: bootstrap-static parsing, plus the composite-key
name resolver that turns a transcript-mangled player name into an FPL
player record — or refuses to, when it isn't confident. See the
`fpl-domain` skill ("Player name matching") for why this is the #1
data-quality risk in the pipeline.

Resolver epistemics (decided in Phase 2 clarification round 1, after
measuring that plain rapidfuzz `token_set_ratio` alone does not clear 85
for the skill's own canonical manglings — "Sacca"/Saka, "Hall and"/
Haaland, "Van Dyke"/van Dijk):

- Tier 1 — a fuzzy name score (rapidfuzz `token_set_ratio`, max of
  web_name vs full name) >= 85 is trusted ALONE, but only once it's clear
  of any tied candidates: either it's the sole top scorer, or the
  composite key (inferred team + inferred position) splits the tie. A
  bare tie at 85+ that the composite key can't split stays AMBIGUOUS
  (e.g. bare "Gabriel" with no team/position — three Arsenal players
  share that first name).
- Tier 2 — below 85, no single signal is trusted alone. A match is only
  accepted with TWO independent corroborations at once: a phonetic match
  (jellyfish metaphone, comparing the normalized raw name AND its last
  token against the candidate's web_name AND second_name, with a small
  edit-distance tolerance since name transliteration varies) AND the
  composite key agreeing on BOTH team and position. Below a 55 fuzzy
  floor, or missing either corroboration, nothing is auto-accepted.
- Never fabricate: an unresolved/undecided name always returns
  `player=None`, with the leading candidates attached for human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import jellyfish
from pydantic import BaseModel
from rapidfuzz import fuzz, utils

from fpl_oracle.fpl.client import get_bootstrap_static

# Tier 1: a fuzzy score at or above this is trusted alone (subject to the
# tie-splitting rule above).
_TIER1_ACCEPT_SCORE = 85.0
# Candidates within this many points of the top fuzzy score are treated as
# "tied" for tie-splitting / ambiguity purposes, in both tiers.
_TIE_MARGIN = 5.0
# Tier 2 (corroborated): below Tier 1, a fuzzy score below this floor is
# never plausible enough to accept even with full corroboration (phonetic
# + team + position, all three). Measured against the skill's own
# canonical manglings: token_set_ratio gives "Sacca"/Saka=66.7, "Van
# Dyke"/van Dijk=75.0, but "Hall and"/Haaland=45.5 — clarification round 1
# specified 55 here without re-checking that number, which would leave
# "Hall and" permanently unmatchable even with full corroboration. Lowered
# to 40; safe to set lower than the uncorroborated floor below because two
# independent signals (phonetic + composite key) are still required on top
# of it — this floor alone never accepts anything.
_TIER2_FLOOR_SCORE = 40.0
# Uncorroborated "plausible cluster" floor: used only when NEITHER tier
# accepted a match, to decide whether the leading candidates are worth
# flagging AMBIGUOUS for human review rather than flatly UNMATCHED. Kept
# at the original 55 (not lowered with the corroborated floor above) —
# with no phonetic/composite-key backing at all, a low bar here would flag
# pure noise as "ambiguous" (e.g. "Ronaldinho" happens to land two
# unrelated candidates ~40-42 apart from each other; that's noise, not a
# real cluster worth a human look).
_AMBIGUOUS_CLUSTER_FLOOR = 55.0
# Team-name fuzzy match (against short_name and full name): at/above this,
# the inferred team is treated as confirming the candidate.
_TEAM_CONFIRM_SCORE = 65.0
# Team-name fuzzy match: below this, the inferred team is treated as
# contradicting the candidate (a demotion signal, never used alone).
_TEAM_CONTRADICT_SCORE = 35.0
# How many leading candidates to surface for human review.
_REVIEW_CANDIDATE_COUNT = 5


class Position(StrEnum):
    GK = "GK"
    DEF = "DEF"
    MID = "MID"
    FWD = "FWD"

    @classmethod
    def from_element_type(cls, et: int) -> Position:
        mapping = {1: cls.GK, 2: cls.DEF, 3: cls.MID, 4: cls.FWD}
        if et not in mapping:
            raise ValueError(
                f"Unknown FPL element_type {et!r} — the game may have added a new "
                "position. Verify against the current bootstrap-static payload before "
                "extending this mapping; do not silently guess."
            )
        return mapping[et]


_POSITION_ALIASES: dict[str, Position] = {
    "gk": Position.GK,
    "gkp": Position.GK,
    "goalkeeper": Position.GK,
    "keeper": Position.GK,
    "def": Position.DEF,
    "defender": Position.DEF,
    "defence": Position.DEF,
    "defense": Position.DEF,
    "mid": Position.MID,
    "midfielder": Position.MID,
    "midfield": Position.MID,
    "fwd": Position.FWD,
    "forward": Position.FWD,
    "striker": Position.FWD,
    "attacker": Position.FWD,
}


def _parse_position(position_inferred: str | None) -> Position | None:
    """Parse a free-text inferred position into a `Position`, or None if
    it's missing/unrecognized. Never raises — an unparseable hint just
    means the composite key can't use it, not a data error."""
    if position_inferred is None:
        return None
    key = re.sub(r"[^a-z]", "", position_inferred.lower())
    return _POSITION_ALIASES.get(key)


class Player(BaseModel):
    player_id: int
    web_name: str
    first_name: str
    second_name: str
    team_id: int
    team_short: str
    position: Position
    now_cost: int  # tenths of a million, e.g. 55 == £5.5m
    status: str
    chance_of_playing_next_round: int | None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.second_name}"

    @property
    def price_m(self) -> float:
        return self.now_cost / 10


class MatchStatus(StrEnum):
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"


class MatchResult(BaseModel):
    player: Player | None
    score: float
    status: MatchStatus
    candidates: list[Player]
    name_raw: str


def _normalize(text: str) -> str:
    """Lowercase, letters-only — the shared normalization for both the
    raw transcript name and the candidate's web_name/second_name before
    phonetic comparison."""
    return re.sub(r"[^a-z]", "", text.lower())


def _fuzzy_score(name_raw: str, player: Player) -> float:
    """Max of rapidfuzz `token_set_ratio` against web_name and against
    the candidate's full name, per the fpl-domain skill's matching rule."""
    return max(
        fuzz.token_set_ratio(name_raw, player.web_name, processor=utils.default_process),
        fuzz.token_set_ratio(name_raw, player.full_name, processor=utils.default_process),
    )


def _metaphone_agree(code_a: str, code_b: str) -> bool:
    """Two metaphone codes 'agree' if they're identical or one edit apart
    — exact metaphone equality is too strict for real transliteration
    variance (e.g. "Saka" -> SK vs "Sacca" -> SKK), but this stays tight
    enough that it's only ever trusted alongside the composite-key gate,
    never alone."""
    if not code_a or not code_b:
        return False
    if code_a == code_b:
        return True
    return jellyfish.levenshtein_distance(code_a, code_b) <= 1


def _phonetic_agree(name_raw: str, player: Player) -> bool:
    """Compare the normalized, concatenated raw name AND its last token
    against the candidate's web_name and second_name (also normalized).
    Any of the four pairings agreeing counts as a phonetic match."""
    tokens = name_raw.split()
    norm_concat = _normalize(name_raw)
    norm_last = _normalize(tokens[-1]) if tokens else norm_concat

    mp_concat = jellyfish.metaphone(norm_concat)
    mp_last = jellyfish.metaphone(norm_last)
    mp_web = jellyfish.metaphone(_normalize(player.web_name))
    mp_second = jellyfish.metaphone(_normalize(player.second_name))

    return any(
        _metaphone_agree(raw_code, player_code)
        for raw_code in (mp_concat, mp_last)
        for player_code in (mp_web, mp_second)
    )


def _team_agreement(team_inferred: str | None, team_short: str, team_full: str) -> bool | None:
    """True if `team_inferred` fuzzily confirms this candidate's team,
    False if it contradicts it, None if there's no team hint or the text
    is too ambiguous to call either way. Never used as sole evidence —
    only ever combined with a name/phonetic signal."""
    if team_inferred is None:
        return None
    score = max(
        fuzz.token_set_ratio(team_inferred, team_short, processor=utils.default_process),
        fuzz.token_set_ratio(team_inferred, team_full, processor=utils.default_process),
    )
    if score >= _TEAM_CONFIRM_SCORE:
        return True
    if score < _TEAM_CONTRADICT_SCORE:
        return False
    return None


@dataclass
class _Candidate:
    player: Player
    fuzzy: float
    phonetic: bool
    team_agree: bool | None
    position_agree: bool | None


class PlayerDB:
    """All bootstrap-static players, keyed by id, plus the composite-key
    resolver. Construct via `from_bootstrap` (offline, testable) or
    `load` (fetches live via `fpl.client.get_bootstrap_static`)."""

    def __init__(self, players: dict[int, Player], team_full_names: dict[int, str]) -> None:
        self._players = players
        self._team_full_names = team_full_names

    @classmethod
    def from_bootstrap(cls, data: dict) -> PlayerDB:
        team_short_names = {t["id"]: t["short_name"] for t in data["teams"]}
        team_full_names = {t["id"]: t["name"] for t in data["teams"]}

        players: dict[int, Player] = {}
        for e in data["elements"]:
            team_id = e["team"]
            player = Player(
                player_id=e["id"],
                web_name=e["web_name"],
                first_name=e["first_name"],
                second_name=e["second_name"],
                team_id=team_id,
                team_short=team_short_names[team_id],
                position=Position.from_element_type(e["element_type"]),
                now_cost=e["now_cost"],
                status=e["status"],
                chance_of_playing_next_round=e.get("chance_of_playing_next_round"),
            )
            players[player.player_id] = player

        return cls(players=players, team_full_names=team_full_names)

    @classmethod
    def load(cls) -> PlayerDB:
        return cls.from_bootstrap(get_bootstrap_static())

    def get(self, player_id: int) -> Player | None:
        return self._players.get(player_id)

    def __len__(self) -> int:
        return len(self._players)

    def resolve(
        self,
        name_raw: str,
        team_inferred: str | None = None,
        position_inferred: str | None = None,
    ) -> MatchResult:
        """Resolve a transcript-extracted player name against the DB.
        See the module docstring for the two-tier acceptance rule. Never
        fabricates a match: `player` is None on anything but MATCHED."""
        parsed_position = _parse_position(position_inferred)

        scored: list[_Candidate] = []
        for player in self._players.values():
            fuzzy = _fuzzy_score(name_raw, player)
            team_agree = _team_agreement(
                team_inferred, player.team_short, self._team_full_names[player.team_id]
            )
            position_agree = None if parsed_position is None else player.position == parsed_position
            phonetic = _phonetic_agree(name_raw, player)
            scored.append(_Candidate(player, fuzzy, phonetic, team_agree, position_agree))

        if not scored:
            return MatchResult(
                player=None,
                score=0.0,
                status=MatchStatus.UNMATCHED,
                candidates=[],
                name_raw=name_raw,
            )

        scored.sort(key=lambda c: c.fuzzy, reverse=True)
        review_candidates = [c.player for c in scored[:_REVIEW_CANDIDATE_COUNT]]
        best = scored[0]

        # Tier 1: a strong fuzzy score, trusted alone unless tied with
        # other candidates that the composite key can't split.
        if best.fuzzy >= _TIER1_ACCEPT_SCORE:
            tied = [c for c in scored if best.fuzzy - c.fuzzy < _TIE_MARGIN]
            if len(tied) == 1:
                return MatchResult(
                    player=best.player,
                    score=best.fuzzy,
                    status=MatchStatus.MATCHED,
                    candidates=review_candidates,
                    name_raw=name_raw,
                )
            composite_winners = [c for c in tied if c.team_agree and c.position_agree]
            if len(composite_winners) == 1:
                winner = composite_winners[0]
                return MatchResult(
                    player=winner.player,
                    score=winner.fuzzy,
                    status=MatchStatus.MATCHED,
                    candidates=review_candidates,
                    name_raw=name_raw,
                )
            return MatchResult(
                player=None,
                score=best.fuzzy,
                status=MatchStatus.AMBIGUOUS,
                candidates=[c.player for c in tied],
                name_raw=name_raw,
            )

        # Tier 2: phonetic fallback. Below the trusted fuzzy threshold, a
        # match needs BOTH a phonetic agreement AND full composite-key
        # (team + position) agreement — independent corroboration, never
        # a bare score.
        tier2 = [
            c
            for c in scored
            if c.fuzzy >= _TIER2_FLOOR_SCORE and c.phonetic and c.team_agree and c.position_agree
        ]
        if len(tier2) == 1:
            winner = tier2[0]
            return MatchResult(
                player=winner.player,
                score=winner.fuzzy,
                status=MatchStatus.MATCHED,
                candidates=review_candidates,
                name_raw=name_raw,
            )
        if len(tier2) > 1:
            return MatchResult(
                player=None,
                score=tier2[0].fuzzy,
                status=MatchStatus.AMBIGUOUS,
                candidates=[c.player for c in tier2],
                name_raw=name_raw,
            )

        # Nothing cleared either tier. A plausible-but-unconfirmed cluster
        # (several candidates close together, none corroborated) is worth
        # a human look; a lone weak candidate is just unmatched. Uses the
        # stricter uncorroborated floor, not the Tier 2 corroboration floor.
        plausible = [c for c in scored if c.fuzzy >= _AMBIGUOUS_CLUSTER_FLOOR]
        if len(plausible) >= 2 and (plausible[0].fuzzy - plausible[1].fuzzy) < _TIE_MARGIN:
            return MatchResult(
                player=None,
                score=best.fuzzy,
                status=MatchStatus.AMBIGUOUS,
                candidates=[c.player for c in plausible[:_REVIEW_CANDIDATE_COUNT]],
                name_raw=name_raw,
            )
        return MatchResult(
            player=None,
            score=best.fuzzy,
            status=MatchStatus.UNMATCHED,
            candidates=review_candidates,
            name_raw=name_raw,
        )
