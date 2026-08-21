"""Name-resolution safety probe.

Rebuilds the empirical check that validated the resolver in Phase 2: take
every real player, mangle their name the way YouTube auto-captions do, and
confirm the resolver either finds the RIGHT player or refuses — but never
silently returns the WRONG one. A wrong match is the only truly dangerous
outcome: it puts a player in the squad that nobody recommended.

Also probes garbage strings (must be UNMATCHED) and first-name-only
references with a correct team+position hint (the "Bruno" case).

BASELINE (below): a small, committed set of known-accepted failures. Each
entry is a structural mangling collision — a mangled name that happens to
spell a DIFFERENT real teammate's actual name closely enough to fool the
resolver — not a resolver defect, and not fixable without either rejecting
genuinely-correct matches elsewhere or knowing which of two real players a
three-letter mangling "really" meant. The probe passes (exit 0) as long as
the failures it finds are a SUBSET of this baseline; it fails loudly on
anything NEW, which is the actual regression signal this script exists to
give. Re-derive the baseline (re-run this script, copy the printed
failures) whenever the roster changes meaningfully enough to shift it —
this happens, e.g. a summer transfer window reshuffling who plays alongside
whom — and re-justify each entry rather than rubber-stamping the diff.

Accepted baseline entries, as of the 599-player live roster:

- "JFletcher" -> Fletcher (expected J.Fletcher). Man Utd start two
  midfielders named Fletcher: Jack ("J.Fletcher") and Tyler ("Fletcher").
  Dropping the "." from "J.Fletcher" produces "JFletcher", which is close
  enough to teammate Tyler Fletcher's own web_name "Fletcher" — same club,
  same position — that the fuzzy tiers prefer him. This is the residual
  risk the module docstring for `_first_name_reference`'s sibling,
  `_tier1_trustworthy`, already names explicitly ("14 such pairs exist in
  the live roster"; Fletcher/J.Fletcher is one).
- "Xaka" -> Sadiki (expected Xhaka). Dropping the "h" from "Xhaka" leaves a
  string phonetically closer to teammate Noah Sadiki than intended — both
  are Sunderland midfielders, so team+position corroborate the wrong
  player just as confidently as the right one would have been
  corroborated. Same root cause as above (Tier 2's phonetic tolerance is
  loose enough to conflate two same-club, same-position teammates), just
  hitting via the phonetic fallback instead of the fuzzy-score tiers.

("Ryan" mangled from "Rayan" (id 67, Bournemouth MID) used to appear here
too, resolving to real Bournemouth teammate Ryan Christie's first name —
but that is exactly a mangling that happens to spell another real
player's name, which the mangling-collision exclusion below now catches
(fixed defect: it previously checked only web_name/full_name, not first
names, and compared un-normalized). It no longer reaches the probe at
all, so it isn't a baseline entry — nothing here needed a resolver
change.)

Run:  uv run python <this file>
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import NamedTuple

from fpl_oracle.fpl.client import get_bootstrap_static
from fpl_oracle.fpl.players import MatchStatus, PlayerDB, Position, _normalize

_POS = {Position.GK: "GK", Position.DEF: "DEF", Position.MID: "MID", Position.FWD: "FWD"}


def manglings(name: str) -> list[str]:
    """Plausible auto-caption corruptions of a name."""
    out: list[str] = [name, name.lower(), name.upper()]
    if len(name) > 3:
        out.append(name[:-1])  # dropped last letter
        out.append(name[0] + name[2:])  # dropped 2nd letter
        out.append(name[:2] + name[1] + name[2:])  # doubled letter
        mid = len(name) // 2
        out.append(name[:mid] + " " + name[mid:])  # split into two words
    for a, b in (("ph", "f"), ("k", "c"), ("y", "i"), ("ij", "ei"), ("aa", "a"), ("é", "e")):
        if a in name.lower():
            out.append(name.lower().replace(a, b))
    return list(dict.fromkeys(out))


GARBAGE = [
    "the manager",
    "next week",
    "a really good option",
    "thomas of coventry",
    "my captain pick",
    "gameweek one",
    "the differential",
    "some guy",
    "bench boost",
    "triple captain",
    "this fella",
    "your vice",
]


class Failure(NamedTuple):
    """One resolver failure the probe found: the raw string it fed in,
    the player it should have matched (or "UNMATCHED"/"AMBIGUOUS" for the
    garbage-strings and first-name checks, which must never MATCH at
    all), and the wrong player it actually got. Hashable, so a run's
    failures can be compared against `BASELINE` as sets."""

    category: str  # "mangled" | "garbage" | "first_name"
    raw: str
    expected: str
    got: str


# See the module docstring above for why each of these is accepted rather
# than fixed. Keep this list SMALL — every entry here is a live blind spot
# for the pipeline, tolerated only because it's structural (a mangling
# that coincidentally spells a teammate's real name) rather than a
# resolver defect. A new entry appearing here should prompt asking "is
# this really structural, or did the resolver regress?" before adding it.
BASELINE: frozenset[Failure] = frozenset(
    {
        Failure("mangled", "JFletcher", "J.Fletcher", "Fletcher"),
        Failure("mangled", "Xaka", "Xhaka", "Sadiki"),
    }
)


def main() -> int:
    # Fetch once and build both the PlayerDB and the team full-name map
    # from the SAME payload — PlayerDB doesn't expose team full names
    # itself, and re-fetching separately would risk the two disagreeing
    # if bootstrap-static changes between calls.
    data = get_bootstrap_static()
    db = PlayerDB.from_bootstrap(data)
    team_full_names = {t["id"]: t["name"] for t in data["teams"]}
    players = db.all_players()

    stats = Counter()
    wrong: list[Failure] = []

    # A mangling that happens to spell ANOTHER real player's name is not a
    # resolver failure — dropping the last letter of "McAteer" spells the
    # real "McAtee". Keyed on the SAME normalization the resolver uses
    # (`_normalize`: lowercase, accents folded, letters only) so this
    # agrees with what the resolver actually treats as "the same name" —
    # comparing un-normalized strings missed accented collisions, and
    # first names are now a matching key too (`_first_name_reference`),
    # so they must be in this set as well, not just web_name/full_name.
    real_name_keys = {
        _normalize(n) for pl in players for n in (pl.web_name, pl.full_name, pl.first_name)
    }

    for p in players:
        pos = _POS[p.position]
        team_hint = team_full_names[p.team_id]
        for variant in manglings(p.web_name):
            variant_key = _normalize(variant)
            if variant_key in real_name_keys and variant_key != _normalize(p.web_name):
                continue
            r = db.resolve(variant, team_inferred=team_hint, position_inferred=pos)
            if r.status is MatchStatus.MATCHED:
                if r.player is not None and r.player.player_id == p.player_id:
                    stats["right"] += 1
                else:
                    stats["WRONG"] += 1
                    got = r.player.web_name if r.player else "?"
                    wrong.append(Failure("mangled", variant, p.web_name, got))
            else:
                stats[r.status.value] += 1

    # Garbage must never match.
    garbage_matched: list[Failure] = []
    for g in GARBAGE:
        r = db.resolve(g, team_inferred=None, position_inferred=None)
        if r.status is MatchStatus.MATCHED:
            got = r.player.web_name if r.player else "?"
            garbage_matched.append(Failure("garbage", g, "UNMATCHED", got))

    # First-name-only, with a CORRECT team+position hint. The composite key
    # should make these resolvable; they are the "Bruno" case.
    first_name_cases: list[tuple[str, str, str, bool, bool]] = []
    first_name_wrong: list[Failure] = []
    for p in players:
        first_raw = p.first_name.strip()
        if not first_raw or " " in first_raw:
            continue
        first_key = _normalize(first_raw)
        if not first_key or first_key in _normalize(p.web_name):
            continue
        # Only fair to test when that first name is unique for the
        # club+position — under the SAME normalization the resolver uses
        # for its own key (`_normalize`), not a bare `.lower()`. Comparing
        # un-normalized meant an accented first name (e.g. "Jérémy") and
        # its ASCII spelling ("Jeremy") looked like two DIFFERENT first
        # names to this filter while being the SAME key to the resolver —
        # so a genuine rival was invisible here exactly as it was invisible
        # to the resolver pre-1abf491, and "unique for club+position"
        # meant two different things in the two places that need to agree.
        rivals = [
            q
            for q in players
            if q.team_id == p.team_id
            and q.position == p.position
            and _normalize(q.first_name) == first_key
        ]
        if len(rivals) != 1:
            continue
        # Probe the DEACCENTED ASCII spelling ("Jeremy"), not the API's
        # accented one ("Jérémy"). Auto-captions emit the ASCII form
        # essentially always; testing the accented spelling meant this
        # probe was structurally incapable of catching the Doku/Monga
        # failure that motivated 1abf491 — the exact spelling it was
        # sent never had the collision the resolver needed to handle.
        first_ascii = first_key.capitalize()
        r = db.resolve(
            first_ascii,
            team_inferred=team_full_names[p.team_id],
            position_inferred=_POS[p.position],
        )
        ok = (
            r.status is MatchStatus.MATCHED
            and r.player is not None
            and r.player.player_id == p.player_id
        )
        badly = r.status is MatchStatus.MATCHED and not ok
        if badly:
            got = r.player.web_name if r.player else "?"
            first_name_wrong.append(Failure("first_name", first_ascii, p.web_name, got))
        first_name_cases.append((first_ascii, p.web_name, r.status.value, ok, badly))

    total = sum(stats.values())
    fn_ok = sum(1 for *_, ok, _ in first_name_cases if ok)
    fn_wrong = len(first_name_wrong)

    print(f"players: {len(players)}   mangled cases: {total}")
    print(f"  right      : {stats['right']:5}  ({100 * stats['right'] / total:.1f}%)")
    print(f"  UNMATCHED  : {stats['UNMATCHED']:5}")
    print(f"  AMBIGUOUS  : {stats['AMBIGUOUS']:5}")
    print(f"  *** WRONG  : {stats['WRONG']:5} ***")
    for f in wrong[:15]:
        print(f"        {f.raw!r} -> {f.got!r} (expected {f.expected!r})")

    print(f"\ngarbage strings: {len(GARBAGE)}  matched: {len(garbage_matched)}")
    for f in garbage_matched:
        print(f"        {f.raw!r} -> {f.got!r}")

    print(f"\nfirst-name-only (unique for club+position): {len(first_name_cases)} cases")
    print(f"  resolved correctly : {fn_ok}")
    print(f"  WRONGLY matched    : {fn_wrong}")
    for f in first_name_wrong:
        print(f"        {f.raw!r} -> {f.got!r} (expected {f.expected!r})")

    # The actual pass/fail signal: every failure found must already be in
    # the committed baseline. A NEW failure — not just a nonzero count —
    # is what should break the build; the baseline entries are known,
    # accepted, and re-justified in the module docstring above.
    current_failures = frozenset(wrong) | frozenset(garbage_matched) | frozenset(first_name_wrong)
    new_failures = current_failures - BASELINE
    fixed_since_baseline = BASELINE - current_failures

    if fixed_since_baseline:
        print(
            f"\n{len(fixed_since_baseline)} baseline entr"
            f"{'y is' if len(fixed_since_baseline) == 1 else 'ies are'} no longer failing "
            "— consider removing from BASELINE:"
        )
        for f in sorted(fixed_since_baseline):
            print(f"        [{f.category}] {f.raw!r} -> {f.got!r} (expected {f.expected!r})")

    if new_failures:
        print(f"\n*** {len(new_failures)} NEW failure(s) not in the committed baseline: ***")
        for f in sorted(new_failures):
            print(f"        [{f.category}] {f.raw!r} -> {f.got!r} (expected {f.expected!r})")
        print("\nA new failure here means the resolver regressed, or the baseline genuinely")
        print("needs a new, justified entry (see the module docstring) — it does not mean")
        print("silently widening BASELINE is the right fix.")
        return 1

    print("\nAll failures are within the committed baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
