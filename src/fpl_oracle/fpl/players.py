"""FPL player database: bootstrap-static parsing, plus the composite-key
name resolver that turns a transcript-mangled player name into an FPL
player record — or refuses to, when it isn't confident. See the
`fpl-domain` skill ("Player name matching") for why this is the #1
data-quality risk in the pipeline.

Resolver epistemics (clarification round 1 set the two-tier design;
a fresh-context review against the real 592-player roster then found
Tier 1 itself was silently over-trusting — round 2 hardens it):

- Tier 1 — a fuzzy name score (rapidfuzz `token_set_ratio`, max of
  web_name vs full name) >= 85 is trusted, but ONLY when:
    (a) it isn't tied with other candidates the composite key can't
        split (e.g. bare "Gabriel" with no team/position — three
        Arsenal players share that first name — stays AMBIGUOUS), AND
    (b) the winning candidate doesn't explicitly CONTRADICT a supplied
        team/position hint (team_agree is False, or position disagrees)
        — a contradicted winner is never accepted, not even alone, AND
    (c) the winning score didn't come from a token-SUBSET match unless
        corroborated. `token_set_ratio` returns 100 whenever one name's
        tokens are a subset of the other's — "Hall and" vs a player
        web-named "Hall" scores 100 this way, as does bare "Mohamed"
        against anyone's "Mohamed <surname>" full name. A subset match
        (raw's tokens != the matched string's tokens) is trusted only
        with a phonetic agreement or a near-exact token_sort_ratio,
        never on the raw score alone. Note this also structurally
        satisfies "no tie-split composite-key accept below 85 without
        phonetics" — token_set_ratio >= token_sort_ratio always holds,
        so a fuzzy score under 90 can never clear the token_sort
        alternative, leaving phonetic agreement as the only way in.
  A candidate that fails (b) or (c) doesn't fall back to some other
  Tier-1 candidate — it falls through to Tier 2 for that whole call.
- Tier 2 — below 85, no single signal is trusted alone. A match is only
  accepted with TWO independent corroborations at once: a phonetic match
  (jellyfish metaphone, comparing the normalized raw name AND its last
  token against the candidate's web_name AND second_name, with a small
  edit-distance tolerance since name transliteration varies) AND the
  composite key agreeing on BOTH team and position. Below a 40 fuzzy
  floor, or missing either corroboration, nothing is auto-accepted.
- AMBIGUOUS is reserved for genuine tie evidence only: an unsplittable
  Tier-1 tie, or more than one corroborated Tier-2 candidate. There is
  no "uncorroborated plausible cluster" AMBIGUOUS path — at 592 players,
  garbage names routinely land two unrelated candidates within a few
  points of each other, so a score-only cluster check just relabels
  garbage as AMBIGUOUS instead of UNMATCHED without adding information
  (UNMATCHED already carries the top-5 candidates for review).
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

# Tier 1: a fuzzy score at or above this is trusted, subject to the
# contradiction-veto / subset-hardening rules in the module docstring.
_TIER1_ACCEPT_SCORE = 85.0
# Candidates within this many points of the top fuzzy score are treated as
# "tied" for tie-splitting / ambiguity purposes, in both tiers.
_TIE_MARGIN = 5.0
# Tier 2 (corroborated): below Tier 1, a fuzzy score below this floor is
# never plausible enough to accept even with full corroboration (phonetic
# + team + position, all three). Measured against the skill's own
# canonical manglings: token_set_ratio gives "Sacca"/Saka=66.7, "Van
# Dyke"/van Dijk=54.5-75.0 depending on roster, "Hall and"/Haaland=45.5.
# Empirically validated against the real bootstrap roster (see PR review):
# a 55 floor would leave both "Hall and" and "Van Dyke" permanently
# unmatchable even with full corroboration. 40 stays safe because two
# independent signals (phonetic + composite key) are still required on
# top of it — this floor alone never accepts anything.
_TIER2_FLOOR_SCORE = 40.0
# Token-sort-ratio floor: the alternative to phonetic agreement for
# trusting a Tier-1 subset match (see docstring point (c)). Near-exact
# only — genuine full-name typos ("Erling Halaand") score ~93-96 here;
# subset-100 pathologies ("Hall and" vs "Hall", bare "Mohamed" vs
# "Mohamed Salah") score ~53-70, well below this.
_TOKEN_SORT_TRUST_SCORE = 90.0
# Team-name fuzzy match (against short_name and full name): at/above this,
# the inferred team is treated as confirming the candidate.
_TEAM_CONFIRM_SCORE = 65.0
# Team-name fuzzy match: below this, the inferred team is treated as
# contradicting the candidate — this is a real signal now (Tier-1
# contradiction veto), not just a demotion.
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


def _token_set(text: str) -> frozenset[str]:
    """Lowercase, letters-only tokens as a set — used to tell a genuine
    exact match from a `token_set_ratio` subset-100 pathology (see module
    docstring point (c))."""
    return frozenset(_normalize(tok) for tok in text.split())


def _fuzzy_score_detail(name_raw: str, player: Player) -> tuple[float, str]:
    """Max of rapidfuzz `token_set_ratio` against web_name and against
    the candidate's full name, per the fpl-domain skill's matching rule.
    Also returns *which* of those two strings produced the winning score,
    since Tier-1 subset-hardening needs to know what it's comparing
    against."""
    web_score = fuzz.token_set_ratio(name_raw, player.web_name, processor=utils.default_process)
    full_score = fuzz.token_set_ratio(name_raw, player.full_name, processor=utils.default_process)
    if web_score >= full_score:
        return web_score, player.web_name
    return full_score, player.full_name


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


def _phonetic_agree(name_raw: str, mp_web: str, mp_second: str) -> bool:
    """Compare the normalized, concatenated raw name AND its last token
    against the candidate's (precomputed) web_name and second_name
    metaphone codes. Any of the four pairings agreeing counts as a
    phonetic match."""
    tokens = name_raw.split()
    norm_concat = _normalize(name_raw)
    norm_last = _normalize(tokens[-1]) if tokens else norm_concat

    mp_concat = jellyfish.metaphone(norm_concat)
    mp_last = jellyfish.metaphone(norm_last)

    return any(
        _metaphone_agree(raw_code, player_code)
        for raw_code in (mp_concat, mp_last)
        for player_code in (mp_web, mp_second)
    )


# Common extractor-style club names/spellings that don't fuzzy-match well
# against the current bootstrap-static `short_name`/`name` fields — e.g. a
# transcript says "Tottenham" but the payload only carries short_name=TOT,
# name="Spurs" (measured: "Tottenham"->Spurs scores 50.0, "Manchester
# United"->"Man Utd" scores 58.3, both below the confirm bar). Keys are
# `_normalize`d (lowercase, letters-only) common phrasings; values are the
# canonical `short_name` from the current live bootstrap-static payload
# (sanity-checked against it directly — see PlayerDB.load() usage). Covers
# all 20 current-season clubs; most already fuzzy-match fine without an
# alias (e.g. "Arsenal"/"Liverpool"/"Chelsea" match their `name` field
# directly) but every club gets at least one entry for robustness.
_TEAM_ALIASES: dict[str, str] = {
    "arsenal": "ARS",
    "astonvilla": "AVL",
    "villa": "AVL",
    "bournemouth": "BOU",
    "afcbournemouth": "BOU",
    "brentford": "BRE",
    "brighton": "BHA",
    "brightonhovealbion": "BHA",
    "brightonandhovealbion": "BHA",
    "chelsea": "CHE",
    "coventry": "COV",
    "coventrycity": "COV",
    "crystalpalace": "CRY",
    "palace": "CRY",
    "everton": "EVE",
    "fulham": "FUL",
    "hull": "HUL",
    "hullcity": "HUL",
    "ipswich": "IPS",
    "ipswichtown": "IPS",
    "leeds": "LEE",
    "leedsunited": "LEE",
    "liverpool": "LIV",
    "mancity": "MCI",
    "manchestercity": "MCI",
    "manutd": "MUN",
    "manu": "MUN",
    "manunited": "MUN",
    "manchesterunited": "MUN",
    "manchesterutd": "MUN",
    "newcastle": "NEW",
    "newcastleunited": "NEW",
    "nottinghamforest": "NFO",
    "nottsforest": "NFO",
    "nottmforest": "NFO",
    "forest": "NFO",
    "tottenham": "TOT",
    "tottenhamhotspur": "TOT",
    "spurs": "TOT",
    "sunderland": "SUN",
}


def _team_agreement(team_inferred: str | None, team_short: str, team_full: str) -> bool | None:
    """True if `team_inferred` confirms this candidate's team, False if it
    contradicts it, None if there's no team hint or the text is too
    ambiguous to call either way. An alias-table hit is decisive either
    way (it identifies a specific intended club); otherwise falls back to
    fuzzy matching against both short_name and the full name."""
    if team_inferred is None:
        return None
    alias_short = _TEAM_ALIASES.get(_normalize(team_inferred))
    if alias_short is not None:
        return alias_short == team_short
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
    matched_target: str
    phonetic: bool
    team_agree: bool | None
    position_agree: bool | None
    exact_name: bool = False
    """Raw name's token set equals the candidate's web_name OR full name.

    Computed against BOTH, not just `matched_target`: the two can score
    the same 100 and `matched_target` keeps whichever won the max, so
    "Virgil van Dijk" (full name, exact) was being judged against
    web_name "Virgil" and treated as a suspicious token-subset."""


def _tier1_trustworthy(name_raw: str, candidate: _Candidate) -> bool:
    """Gate for accepting a Tier-1 (>=85) candidate outright.

    Rules, in order:

    1. A POSITION contradiction is always disqualifying — positions don't
       change mid-window, so a mismatch means the wrong player.
    2. A TEAM contradiction is disqualifying UNLESS the name is exact.
       Team is the one composite-key field that goes stale: the extracting
       LLM infers it from its own PL knowledge, which lags the summer
       transfer window, so "Semenyo + Bournemouth" (now Man City) and
       "Isak + Newcastle" (now Liverpool) are stale inference, not wrong
       players. An exact name match plus an agreeing position is stronger
       evidence than a team string the model guessed from memory.
    3. An EXACT name (raw's token set equals web_name or full name) is
       trusted. Anything else scored >=85 came from a token-subset match
       and needs a phonetic or near-exact token_sort_ratio backstop —
       otherwise "Hall and" matching a player web-named "Hall", or bare
       "Mohamed" matching anyone's "Mohamed <surname>" full name, would
       sail through on a bare 100 that has nothing to do with the
       candidate."""
    if candidate.position_agree is False:
        return False
    if candidate.team_agree is False and not candidate.exact_name:
        return False
    if candidate.exact_name:
        return True
    if candidate.phonetic:
        return True
    return (
        fuzz.token_sort_ratio(name_raw, candidate.matched_target, processor=utils.default_process)
        >= _TOKEN_SORT_TRUST_SCORE
    )


class PlayerDB:
    """All bootstrap-static players, keyed by id, plus the composite-key
    resolver. Construct via `from_bootstrap` (offline, testable) or
    `load` (fetches live via `fpl.client.get_bootstrap_static`)."""

    def __init__(
        self,
        players: dict[int, Player],
        team_full_names: dict[int, str],
        player_metaphones: dict[int, tuple[str, str]],
    ) -> None:
        self._players = players
        self._team_full_names = team_full_names
        # (metaphone(web_name), metaphone(second_name)) per player, computed
        # once here rather than on every resolve() call — the DB is built
        # once and resolved against many times per extraction run.
        self._player_metaphones = player_metaphones

    @classmethod
    def from_bootstrap(cls, data: dict) -> PlayerDB:
        team_short_names = {t["id"]: t["short_name"] for t in data["teams"]}
        team_full_names = {t["id"]: t["name"] for t in data["teams"]}

        players: dict[int, Player] = {}
        player_metaphones: dict[int, tuple[str, str]] = {}
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
            player_metaphones[player.player_id] = (
                jellyfish.metaphone(_normalize(player.web_name)),
                jellyfish.metaphone(_normalize(player.second_name)),
            )

        return cls(
            players=players, team_full_names=team_full_names, player_metaphones=player_metaphones
        )

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
            fuzzy, matched_target = _fuzzy_score_detail(name_raw, player)
            team_agree = _team_agreement(
                team_inferred, player.team_short, self._team_full_names[player.team_id]
            )
            position_agree = None if parsed_position is None else player.position == parsed_position
            mp_web, mp_second = self._player_metaphones[player.player_id]
            phonetic = _phonetic_agree(name_raw, mp_web, mp_second)
            raw_tokens = _token_set(name_raw)
            exact_name = raw_tokens == _token_set(player.web_name) or raw_tokens == _token_set(
                f"{player.first_name} {player.second_name}"
            )
            scored.append(
                _Candidate(
                    player,
                    fuzzy,
                    matched_target,
                    phonetic,
                    team_agree,
                    position_agree,
                    exact_name,
                )
            )

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

        # Tier 1: a strong fuzzy score. Trusted only once contradiction
        # veto + subset hardening clear it (see _tier1_trustworthy and the
        # module docstring) — a winner that fails either doesn't fall back
        # to some other Tier-1 candidate, it falls through to Tier 2 below.
        if best.fuzzy >= _TIER1_ACCEPT_SCORE:
            tied = [c for c in scored if best.fuzzy - c.fuzzy < _TIE_MARGIN]
            if len(tied) == 1:
                sole = tied[0]
                if _tier1_trustworthy(name_raw, sole):
                    return MatchResult(
                        player=sole.player,
                        score=sole.fuzzy,
                        status=MatchStatus.MATCHED,
                        candidates=review_candidates,
                        name_raw=name_raw,
                    )
                # Untrustworthy sole "winner" — fall through to Tier 2.
            else:
                composite_winners = [c for c in tied if c.team_agree and c.position_agree]
                if len(composite_winners) == 1:
                    winner = composite_winners[0]
                    if _tier1_trustworthy(name_raw, winner):
                        return MatchResult(
                            player=winner.player,
                            score=winner.fuzzy,
                            status=MatchStatus.MATCHED,
                            candidates=review_candidates,
                            name_raw=name_raw,
                        )
                    # Untrustworthy composite winner — fall through to Tier 2.
                else:
                    return MatchResult(
                        player=None,
                        score=best.fuzzy,
                        status=MatchStatus.AMBIGUOUS,
                        candidates=[c.player for c in tied],
                        name_raw=name_raw,
                    )

        # Tier 2: phonetic fallback. Below the trusted fuzzy threshold (or
        # falling through from a vetoed Tier-1 "winner"), a match needs
        # BOTH a phonetic agreement AND full composite-key (team +
        # position) agreement — independent corroboration, never a bare
        # score.
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

        # Nothing cleared either tier. No score-only "plausible cluster"
        # fallback — at hundreds of players, unrelated garbage routinely
        # lands two candidates within a few points of each other, so a
        # bare-score cluster check just mislabels noise as AMBIGUOUS.
        # UNMATCHED already carries the top candidates for human review.
        return MatchResult(
            player=None,
            score=best.fuzzy,
            status=MatchStatus.UNMATCHED,
            candidates=review_candidates,
            name_raw=name_raw,
        )
