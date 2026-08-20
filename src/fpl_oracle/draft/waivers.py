"""Waiver-wire recommender for in-season FPL Draft.

Projections come from `draft/board.py`, which maps FPL's preseason
`draft_rank` onto last season's positional points curve — a preseason
estimate that does NOT update with in-season form or minutes, so a waiver
target who has been starting and scoring recently will be undervalued here.
This is a known, deliberate limitation, not a bug (see PLAN.md's "Draft
mode" section).

On draft day the right baseline is "the last starter across the league",
because the alternative to drafting a player is fielding a replacement-level
starter — that's what `board.py` does, correctly. In-season, that comparison
is wrong: you already have a specific 15-man squad. The only question that
matters for a waiver claim is: does adding this free agent AND dropping one
of my players improve my best legal starting XI's total projected points? A
free agent with a big draft-day "value" score is worthless to me if he'd sit
on my bench behind someone I already own. That is what this module answers.

    uv run python -m fpl_oracle.draft.waivers --league 38524 --entry 199528
"""

from __future__ import annotations

import argparse
import sys

from pydantic import BaseModel

from fpl_oracle.draft import client
from fpl_oracle.draft.board import DraftPlayer, build_board
from fpl_oracle.draft.expert_rankings import ALL_BOARDS
from fpl_oracle.fpl.players import Position

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN, MAGENTA = (
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[36m",
    "\033[35m",
)

# Legal FPL Draft starting-XI shape, verified live against the draft
# bootstrap's `settings.squad` on 2026-08-19: min_play_GKP=1, max_play_GKP=1,
# min_play_DEF=3, max_play_DEF=5, min_play_MID=2, max_play_MID=5,
# min_play_FWD=1, max_play_FWD=3, play=11. Note the settings dict keys use
# "GKP" for goalkeeper while DEF/MID/FWD match `Position`'s values directly
# — the same gotcha `board.py` already comments on for `select_*`.
# `best_xi()` takes only a squad, not the bootstrap dict, so these are
# hardcoded here rather than parsed live.
MIN_PLAY_GKP = 1
MAX_PLAY_GKP = 1
MIN_PLAY_DEF = 3
MAX_PLAY_DEF = 5
MIN_PLAY_MID = 2
MAX_PLAY_MID = 5
MIN_PLAY_FWD = 1
MAX_PLAY_FWD = 3
PLAY = 11

# Waiver priority is a spendable resource: a successful claim sends you to
# the bottom of the priority order, so a marginal XI gain often is not worth
# burning it. Surface the number rather than silently thresholding it away.
MIN_GAIN_WORTH_PRIORITY = 2.0

# Free-agent fitness statuses that make a player unclaimable outright (see
# DraftPlayer.available_flag). A "d" (doubt) free agent is still evaluated,
# just flagged.
_UNAVAILABLE_STATUSES = {"u", "i", "s"}


class XI(BaseModel):
    """The best legal starting XI from a 15-man squad, by projected points."""

    starters: list[DraftPlayer]  # exactly 11
    bench: list[DraftPlayer]  # the rest of the squad
    total_points: float  # sum of starters' projected_points
    formation: dict[Position, int]  # starter counts by position, e.g. {GK:1, DEF:4, MID:4, FWD:2}


class ClaimEvaluation(BaseModel):
    add: DraftPlayer
    drop: DraftPlayer
    gain: float  # best_xi(squad-drop+add).total_points - best_xi(squad).total_points
    changes_xi: bool  # True iff add actually enters the best XI (gain != 0, small float tolerance)
    availability_flag: str = ""  # add.available_flag, so it isn't buried in a table row
    worth_priority: bool = True  # False when gain is below the "not worth it" threshold
    note: str = ""  # e.g. "probably not worth your priority" when worth_priority is False


def best_xi(squad: list[DraftPlayer]) -> XI:
    """The best-scoring legal starting XI a 15-man squad can field.

    Enumerates every legal (DEF, MID, FWD) formation — exactly 1 GK; DEF in
    3..5; MID in 2..5; FWD in 1..3; all summing to 11 with the GK — and, for
    each one the squad can actually fill, sums the top-N players by
    `projected_points` at each position. Returns the XI for whichever
    feasible formation scores highest. Raises `ValueError` if the squad
    cannot fill ANY legal formation (e.g. fewer than 3 DEF) — that indicates
    a malformed squad, not something to paper over.
    """
    by_position: dict[Position, list[DraftPlayer]] = {p: [] for p in Position}
    for player in squad:
        by_position[player.position].append(player)
    for players in by_position.values():
        players.sort(key=lambda p: -p.projected_points)

    best_total: float | None = None
    best_formation: tuple[int, int, int] | None = None
    for def_n in range(MIN_PLAY_DEF, MAX_PLAY_DEF + 1):
        for mid_n in range(MIN_PLAY_MID, MAX_PLAY_MID + 1):
            for fwd_n in range(MIN_PLAY_FWD, MAX_PLAY_FWD + 1):
                if MIN_PLAY_GKP + def_n + mid_n + fwd_n != PLAY:
                    continue
                if (
                    len(by_position[Position.GK]) < MIN_PLAY_GKP
                    or len(by_position[Position.DEF]) < def_n
                    or len(by_position[Position.MID]) < mid_n
                    or len(by_position[Position.FWD]) < fwd_n
                ):
                    continue
                total = (
                    sum(p.projected_points for p in by_position[Position.GK][:MIN_PLAY_GKP])
                    + sum(p.projected_points for p in by_position[Position.DEF][:def_n])
                    + sum(p.projected_points for p in by_position[Position.MID][:mid_n])
                    + sum(p.projected_points for p in by_position[Position.FWD][:fwd_n])
                )
                if best_total is None or total > best_total:
                    best_total = total
                    best_formation = (def_n, mid_n, fwd_n)

    if best_formation is None or best_total is None:
        raise ValueError(
            "no legal starting XI formation (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD) is "
            f"feasible for this squad — squad has "
            f"{len(by_position[Position.GK])} GK, {len(by_position[Position.DEF])} DEF, "
            f"{len(by_position[Position.MID])} MID, {len(by_position[Position.FWD])} FWD"
        )

    def_n, mid_n, fwd_n = best_formation
    starters = (
        by_position[Position.GK][:MIN_PLAY_GKP]
        + by_position[Position.DEF][:def_n]
        + by_position[Position.MID][:mid_n]
        + by_position[Position.FWD][:fwd_n]
    )
    starter_ids = {p.player_id for p in starters}
    bench = [p for p in squad if p.player_id not in starter_ids]
    formation = {
        Position.GK: MIN_PLAY_GKP,
        Position.DEF: def_n,
        Position.MID: mid_n,
        Position.FWD: fwd_n,
    }
    return XI(starters=starters, bench=bench, total_points=best_total, formation=formation)


def evaluate_claim(
    squad: list[DraftPlayer], add: DraftPlayer, drop: DraftPlayer
) -> ClaimEvaluation:
    """Score one waiver claim: does swapping `drop` for `add` improve my best XI?

    A generic pure function — it just removes `drop`, appends `add`, and
    diffs `best_xi()` before and after. It does not check legality (e.g.
    same-position); `recommend_claims` is the layer that restricts pairing
    to legal same-position swaps, since FPL Draft's squad composition is
    fixed.
    """
    new_squad = [p for p in squad if p.player_id != drop.player_id] + [add]
    gain = best_xi(new_squad).total_points - best_xi(squad).total_points
    # "Does it change the XI" should be true for any nonzero swing, not just
    # a positive one — a caller could evaluate a bad swap on purpose.
    changes_xi = abs(gain) > 1e-9
    worth_priority = gain >= MIN_GAIN_WORTH_PRIORITY
    note = "probably not worth your priority" if gain > 0 and not worth_priority else ""
    return ClaimEvaluation(
        add=add,
        drop=drop,
        gain=gain,
        changes_xi=changes_xi,
        availability_flag=add.available_flag,
        worth_priority=worth_priority,
        note=note,
    )


def recommend_claims(
    squad: list[DraftPlayer], free_agents: list[DraftPlayer], limit: int = 15
) -> list[ClaimEvaluation]:
    """Rank every legal, positive-gain waiver claim available to this squad.

    FPL Draft enforces a FIXED squad composition (2 GK, 5 DEF, 5 MID, 3 FWD)
    at all times, so a legal real-world waiver claim is always a
    SAME-POSITION swap — you cannot legally drop a GK and add a DEF, since
    that would break the fixed composition. `evaluate_claim` itself stays
    generic (any add/drop pair, no legality check); this is where that
    real-world constraint is applied, by only pairing each free agent with
    squad players at his own position.
    """
    evaluations: list[ClaimEvaluation] = []
    for agent in free_agents:
        if agent.status in _UNAVAILABLE_STATUSES:
            continue
        for drop in (p for p in squad if p.position == agent.position):
            evaluation = evaluate_claim(squad, agent, drop)
            if evaluation.gain > 0:
                evaluations.append(evaluation)
    evaluations.sort(key=lambda e: -e.gain)
    return evaluations[:limit]


def squad_from_element_status(
    element_status: list[dict], players_by_id: dict[int, DraftPlayer], entry_id: int
) -> list[DraftPlayer]:
    """Every player `element_status` currently attributes to this entry_id.

    Skips any `element` id missing from `players_by_id` (defensive — should
    not happen, but a missing bootstrap entry must not crash the
    recommender) and anyone `in_accepted_trade` (mid-transfer, not
    legitimately part of the squad right now).
    """
    squad: list[DraftPlayer] = []
    for entry in element_status:
        if entry.get("in_accepted_trade"):
            continue
        if entry.get("owner") != entry_id:
            continue
        player = players_by_id.get(entry["element"])
        if player is None:
            continue
        squad.append(player)
    return squad


def free_agents_from_element_status(
    element_status: list[dict], players_by_id: dict[int, DraftPlayer]
) -> list[DraftPlayer]:
    """Free agents claimable right now: owner is None, draft status 'a', not `in_accepted_trade`.

    Skips any `element` id missing from `players_by_id`, same as
    `squad_from_element_status`.
    """
    agents: list[DraftPlayer] = []
    for entry in element_status:
        if entry.get("in_accepted_trade"):
            continue
        if entry.get("owner") is not None:
            continue
        if entry.get("status") != "a":
            continue
        player = players_by_id.get(entry["element"])
        if player is None:
            continue
        agents.append(player)
    return agents


def _fmt_player(p: DraftPlayer) -> str:
    return f"{p.web_name} ({p.position} {p.team_short})"


def _print_claims(claims: list[ClaimEvaluation]) -> None:
    if not claims:
        print(f"\n  {DIM}no positive-gain waiver claims found right now{RESET}")
        return
    print(f"\n{BOLD}{'ADD':<28} {'DROP':<28} {'GAIN':>6}  {'XI?':<4} {'NOTE'}{RESET}")
    for c in claims:
        gain_colour = GREEN if c.gain >= MIN_GAIN_WORTH_PRIORITY else YELLOW
        xi = "yes" if c.changes_xi else "no"
        note_bits = []
        if c.availability_flag:
            note_bits.append(f"{YELLOW}{c.availability_flag}{RESET}")
        if c.note:
            note_bits.append(f"{DIM}{c.note}{RESET}")
        note = "  ".join(note_bits)
        print(
            f"{_fmt_player(c.add):<28} {_fmt_player(c.drop):<28} "
            f"{gain_colour}{c.gain:>+6.1f}{RESET}  {xi:<4} {note}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FPL Draft waiver-wire recommender")
    ap.add_argument("--league", type=int, required=True, help="draft league id")
    ap.add_argument("--entry", type=int, required=True, help="your entry_id in the league")
    ap.add_argument("--limit", type=int, default=15, help="max claims to show")
    ap.add_argument("--refresh", action="store_true", help="refetch bootstrap injury news now")
    args = ap.parse_args(argv)

    league_details = client.fetch_league_entries(args.league)
    league_entries = league_details.get("league_entries", [])

    bootstrap = client.fetch_bootstrap(refresh=args.refresh)
    board = build_board(bootstrap, teams=len(league_entries), expert_boards=ALL_BOARDS)

    element_status_data = client.fetch_element_status(args.league)
    element_status = element_status_data.get("element_status", [])

    players_by_id = {p.player_id: p for p in board.players}
    squad = squad_from_element_status(element_status, players_by_id, args.entry)
    free_agents = free_agents_from_element_status(element_status, players_by_id)

    entry_name = next(
        (e.get("entry_name") for e in league_entries if e.get("entry_id") == args.entry),
        f"entry {args.entry}",
    )

    if not squad:
        print(f"{RED}no players found for entry_id {args.entry} in league {args.league}{RESET}")
        print(
            f"{DIM}check --entry is the entry_id (not the league_entries.id) for your team{RESET}"
        )
        return 1

    print(
        f"{BOLD}{MAGENTA}WAIVER RECOMMENDER{RESET} — {entry_name}  "
        f"{DIM}({len(squad)} in squad, {len(free_agents)} free agents){RESET}"
    )

    claims = recommend_claims(squad, free_agents, limit=args.limit)
    _print_claims(claims)
    return 0


if __name__ == "__main__":
    sys.exit(main())
