"""Name-resolution safety probe.

Rebuilds the empirical check that validated the resolver in Phase 2: take
every real player, mangle their name the way YouTube auto-captions do, and
confirm the resolver either finds the RIGHT player or refuses — but never
silently returns the WRONG one. A wrong match is the only truly dangerous
outcome: it puts a player in the squad that nobody recommended.

Also probes garbage strings (must be UNMATCHED) and first-name-only
references with a correct team+position hint (the "Bruno" case).

Run:  uv run python <this file>
"""

from __future__ import annotations

import sys
from collections import Counter

from fpl_oracle.fpl.players import MatchStatus, PlayerDB, Position

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


def main() -> int:
    db = PlayerDB.load()
    players = db.all_players()

    stats = Counter()
    wrong: list[tuple[str, str, str]] = []

    # A mangling that happens to spell ANOTHER real player's name is not a
    # resolver failure — dropping the last letter of "McAteer" spells the
    # real "McAtee". Excluded so the probe measures the resolver, not the
    # coincidence.
    real_names = {n.lower() for pl in players for n in (pl.web_name, pl.full_name)}

    for p in players:
        pos = _POS[p.position]
        for variant in manglings(p.web_name):
            if variant.lower() in real_names and variant.lower() != p.web_name.lower():
                continue
            r = db.resolve(variant, team_inferred=p.team_short, position_inferred=pos)
            if r.status is MatchStatus.MATCHED:
                if r.player is not None and r.player.player_id == p.player_id:
                    stats["right"] += 1
                else:
                    stats["WRONG"] += 1
                    got = r.player.web_name if r.player else "?"
                    wrong.append((variant, p.web_name, got))
            else:
                stats[r.status.value] += 1

    # Garbage must never match.
    garbage_matched = []
    for g in GARBAGE:
        r = db.resolve(g, team_inferred=None, position_inferred=None)
        if r.status is MatchStatus.MATCHED:
            garbage_matched.append((g, r.player.web_name if r.player else "?"))

    # First-name-only, with a CORRECT team+position hint. The composite key
    # should make these resolvable; they are the "Bruno" case.
    first_name_cases = []
    for p in players:
        first = p.first_name.strip()
        if not first or " " in first or first.lower() in p.web_name.lower():
            continue
        # Only fair to test when that first name is unique for the club+position.
        rivals = [
            q
            for q in players
            if q.team_id == p.team_id
            and q.position == p.position
            and q.first_name.strip().lower() == first.lower()
        ]
        if len(rivals) != 1:
            continue
        r = db.resolve(first, team_inferred=p.team_short, position_inferred=_POS[p.position])
        ok = (
            r.status is MatchStatus.MATCHED
            and r.player is not None
            and r.player.player_id == p.player_id
        )
        badly = r.status is MatchStatus.MATCHED and not ok
        first_name_cases.append((first, p.web_name, r.status.value, ok, badly))

    total = sum(stats.values())
    fn_ok = sum(1 for *_, ok, _ in first_name_cases if ok)
    fn_wrong = sum(1 for *_, badly in first_name_cases if badly)

    print(f"players: {len(players)}   mangled cases: {total}")
    print(f"  right      : {stats['right']:5}  ({100 * stats['right'] / total:.1f}%)")
    print(f"  UNMATCHED  : {stats['UNMATCHED']:5}")
    print(f"  AMBIGUOUS  : {stats['AMBIGUOUS']:5}")
    print(f"  *** WRONG  : {stats['WRONG']:5} ***   <-- must be 0")
    for variant, expected, got in wrong[:15]:
        print(f"        {variant!r} -> {got!r} (expected {expected!r})")

    print(f"\ngarbage strings: {len(GARBAGE)}  matched: {len(garbage_matched)}   <-- must be 0")
    for g, got in garbage_matched:
        print(f"        {g!r} -> {got!r}")

    print(f"\nfirst-name-only (unique for club+position): {len(first_name_cases)} cases")
    print(f"  resolved correctly : {fn_ok}")
    print(f"  WRONGLY matched    : {fn_wrong}   <-- must be 0")

    return 0 if (stats["WRONG"] == 0 and not garbage_matched and fn_wrong == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
