"""Live draft-room assistant.

Run this in a terminal beside the official draft room and mark players off as
they go. It answers the only question that matters while the clock runs: given
what is gone and what my squad still needs, who should I take now?

    uv run python -m fpl_oracle.draft.live --teams 10

State is written after every pick, so a crashed terminal costs nothing —
relaunch and it resumes mid-draft.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from fpl_oracle.draft import client
from fpl_oracle.draft.board import (
    Board,
    DraftPlayer,
    build_board,
    squad_needs,
    value_over_next_available,
)
from fpl_oracle.draft.expert_rankings import ALL_BOARDS
from fpl_oracle.fpl.players import Position

STATE_PATH = Path("data/draft/state.json")

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN, MAGENTA = (
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[36m",
    "\033[35m",
)


class DraftState(BaseModel):
    """Who has been taken, and by whom. `owner` is "me" or "other"."""

    taken: dict[int, str] = Field(default_factory=dict)
    order: list[int] = Field(default_factory=list)  # for undo

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json())

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> DraftState:
        if not path.exists():
            return cls()
        return cls.model_validate_json(path.read_text())


def available(board: Board, state: DraftState) -> list[DraftPlayer]:
    return [p for p in board.players if p.player_id not in state.taken]


def my_squad(board: Board, state: DraftState) -> list[DraftPlayer]:
    return [p for p in board.players if state.taken.get(p.player_id) == "me"]


def resolve(query: str, players: list[DraftPlayer]) -> DraftPlayer | None:
    """Fuzzy-match a typed name — draft clocks do not wait for correct spelling."""
    q = query.strip().casefold()
    if not q:
        return None
    exact = [p for p in players if p.web_name.casefold() == q]
    if len(exact) == 1:
        return exact[0]
    choices: dict[str, DraftPlayer] = {}
    for p in players:
        choices[f"{p.web_name}|{p.player_id}"] = p
        choices[f"{p.full_name}|{p.player_id}"] = p
    hit = process.extractOne(q, list(choices), scorer=fuzz.WRatio, score_cutoff=60)
    return choices[hit[0]] if hit else None


def recommend(
    board: Board, state: DraftState, limit: int = 5
) -> list[tuple[DraftPlayer, float, float, str]]:
    """Rank the board for MY next pick.

    Value is the substance; the drop to the next man at the position is the
    urgency premium. A position I have already filled is worth nothing to me
    however good the player, so it is excluded outright.
    """
    avail = available(board, state)
    need = squad_needs(my_squad(board, state))
    out: list[tuple[DraftPlayer, float, float, str]] = []
    for p in avail:
        if need.get(p.position, 0) <= 0 or p.status in {"u", "i", "s"}:
            continue
        vona = value_over_next_available(p, avail)
        startable_left = len([q for q in avail if q.position == p.position and q.value > 0])
        score = p.value + 0.5 * max(vona, 0.0)
        notes = []
        if vona >= 20:
            notes.append(f"{RED}cliff after him{RESET}")
        if startable_left <= max(need[p.position], 3):
            notes.append(f"{YELLOW}only {startable_left} startable {p.position} left{RESET}")
        if p.status == "d":
            notes.append(f"{YELLOW}fitness doubt{RESET}")
        if p.expert_ranks:
            notes.append(DIM + ", ".join(f"{c} #{r}" for c, r in p.expert_ranks.items()) + RESET)
        out.append((p, score, vona, "  ".join(notes)))
    out.sort(key=lambda t: -t[1])
    return out[:limit]


def show_recommendation(board: Board, state: DraftState) -> None:
    recs = recommend(board, state)
    if not recs:
        print(f"\n  {DIM}squad full — nothing left to recommend{RESET}")
        return
    print(
        f"\n{BOLD}{GREEN}TAKE NEXT{RESET}  {DIM}(value + urgency from the drop behind him){RESET}"
    )
    for i, (p, _score, vona, notes) in enumerate(recs, 1):
        mark = f"{BOLD}►{RESET}" if i == 1 else " "
        print(
            f" {mark} {i}. {BOLD}{p.web_name:<18}{RESET} {p.position:<4} {p.team_short:<4} "
            f"value {p.value:+6.1f}   next {p.position} is {vona:+.0f} worse  {notes}"
        )


def show_board(
    board: Board, state: DraftState, limit: int = 15, pos: Position | None = None
) -> None:
    avail = available(board, state)
    need = squad_needs(my_squad(board, state))
    rows = [p for p in avail if pos is None or p.position == pos]
    if not rows:
        print(f"  {DIM}nothing available{RESET}")
        return
    print(
        f"\n{BOLD}{'#':>3} {'PLAYER':<20} {'POS':<4} {'TM':<4} "
        f"{'PROJ':>5} {'VALUE':>6} {'VONA':>6} {'T':>2}{RESET}"
    )
    for i, p in enumerate(rows[:limit], 1):
        vona = value_over_next_available(p, avail)
        full = need.get(p.position, 0) <= 0
        colour = DIM if full else (GREEN if vona >= 15 else "")
        cliff = f"{RED}◄ CLIFF{RESET}" if vona >= 20 and not full else ""
        flag = f" {RED}{p.available_flag}{RESET}" if p.available_flag else ""
        slot = f" {DIM}(slot full){RESET}" if full else ""
        print(
            f"{colour}{i:>3} {p.web_name:<20} {p.position:<4} {p.team_short:<4} "
            f"{p.projected_points:>5.0f} {p.value:>6.1f} {vona:>6.1f} {p.tier:>2}{RESET}"
            f" {cliff}{flag}{slot}"
        )


def show_scarcity(board: Board, state: DraftState) -> None:
    avail = available(board, state)
    need = squad_needs(my_squad(board, state))
    print(f"\n{BOLD}SCARCITY — startable players left{RESET}")
    for pos in (Position.FWD, Position.MID, Position.DEF, Position.GK):
        above = [p for p in avail if p.position == pos and p.value > 0]
        colour = RED if len(above) <= 3 else (YELLOW if len(above) <= 8 else GREEN)
        best = f"best: {above[0].web_name} (+{above[0].value:.0f})" if above else "none left"
        print(
            f"  {pos:<4} {colour}{len(above):>3} left{RESET}  "
            f"baseline {board.baselines[pos]:>5.0f}   need {need[pos]}   {DIM}{best}{RESET}"
        )


def show_squad(board: Board, state: DraftState) -> None:
    mine = my_squad(board, state)
    need = squad_needs(mine)
    print(f"\n{BOLD}MY SQUAD ({len(mine)}/15){RESET}")
    for pos in (Position.GK, Position.DEF, Position.MID, Position.FWD):
        group = [p for p in mine if p.position == pos]
        got = ", ".join(f"{p.web_name} ({p.team_short})" for p in group) or f"{DIM}—{RESET}"
        short = f"  {YELLOW}need {need[pos]} more{RESET}" if need[pos] > 0 else ""
        print(f"  {pos:<4} {got}{short}")
    if mine:
        by_club: dict[str, int] = {}
        for p in mine:
            by_club[p.team_short] = by_club.get(p.team_short, 0) + 1
        stacked = [f"{t}x{n}" for t, n in sorted(by_club.items(), key=lambda x: -x[1]) if n >= 3]
        if stacked:
            print(
                f"  {YELLOW}club stack: {', '.join(stacked)}{RESET} "
                f"{DIM}(legal in Draft, but concentrates fixture and rotation risk){RESET}"
            )
        print(f"  {DIM}total projected: {sum(p.projected_points for p in mine):.0f} pts{RESET}")


HELP = f"""
{BOLD}COMMANDS{RESET}
  {CYAN}<name>{RESET}          a rival drafted them        {DIM}e.g.  haaland{RESET}
  {CYAN}m <name>{RESET}        {BOLD}I{RESET} drafted them              {DIM}e.g.  m saka{RESET}
  {CYAN}p{RESET}               {BOLD}what should I take now{RESET}
  {CYAN}b [n]{RESET}           best available overall
  {CYAN}fwd mid def gk{RESET}  best available at a position
  {CYAN}s{RESET}               scarcity meter
  {CYAN}r{RESET}               my squad and remaining needs
  {CYAN}?<name>{RESET}         look a player up without drafting him
  {CYAN}u{RESET}               undo the last entry
  {CYAN}q{RESET}               quit (state is saved after every pick)
{DIM}VALUE = projected points minus the last STARTER at that position.
VONA  = the drop to the next man at his position; high means passing is costly.{RESET}
"""


def _lookup(board: Board, state: DraftState, query: str) -> None:
    p = resolve(query, board.players)
    if not p:
        print(f"  {RED}no match{RESET}")
        return
    owner = state.taken.get(p.player_id)
    where = f"{RED}taken ({owner}){RESET}" if owner else f"{GREEN}available{RESET}"
    experts = ", ".join(f"{c} #{r}" for c, r in p.expert_ranks.items()) or "none"
    print(
        f"  {BOLD}{p.web_name}{RESET} ({p.full_name}) {p.position} {p.team_short} — {where}\n"
        f"    FPL draft_rank {p.fpl_draft_rank}, creator boards: {experts}\n"
        f"    #{p.position_rank} {p.position} on the blended board   "
        f"projected {p.projected_points:.0f}   value {p.value:+.1f}   tier {p.tier}\n"
        f"    last season: {p.last_season_points} pts in {p.minutes} mins  "
        f"{p.available_flag} {p.news}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live FPL Draft assistant")
    ap.add_argument("--teams", type=int, default=10, help="managers in your league")
    ap.add_argument("--refresh", action="store_true", help="refetch injury news now")
    ap.add_argument("--reset", action="store_true", help="clear saved draft state")
    args = ap.parse_args(argv)

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    board = build_board(client.fetch_bootstrap(refresh=args.refresh), args.teams, ALL_BOARDS)
    state = DraftState.load()

    print(
        f"{BOLD}{MAGENTA}FPL DRAFT ASSISTANT{RESET} — {args.teams}-team league, "
        f"{args.teams * 15} players will be drafted"
    )
    print(
        f"{DIM}baselines (last starter): "
        f"{', '.join(f'{k} {v:.0f}' for k, v in board.baselines.items())}{RESET}"
    )
    if board.unresolved_expert_names:
        for creator, names in board.unresolved_expert_names.items():
            print(f"{YELLOW}warn:{RESET} {creator} names not matched: {', '.join(names)}")
    if state.taken:
        print(f"{YELLOW}resumed: {len(state.taken)} players already off the board{RESET}")
    print(HELP)
    show_board(board, state)
    show_scarcity(board, state)

    positions: dict[str, Position] = {p.value.casefold(): p for p in Position}

    while True:
        try:
            raw = input(f"\n{BOLD}draft>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            state.save()
            print("\nsaved. good luck.")
            return 0
        if not raw:
            continue
        low = raw.casefold()

        if low in {"q", "quit", "exit"}:
            state.save()
            print("saved. good luck.")
            return 0
        if low in {"h", "help"}:
            print(HELP)
            continue
        if low in {"b", "board"} or low.startswith("b "):
            parts = low.split()
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
            show_board(board, state, limit=n)
            continue
        if low in positions:
            show_board(board, state, limit=12, pos=positions[low])
            continue
        if low in {"s", "scarcity"}:
            show_scarcity(board, state)
            continue
        if low in {"p", "pick"}:
            show_recommendation(board, state)
            continue
        if low in {"r", "roster", "squad"}:
            show_squad(board, state)
            continue
        if low in {"u", "undo"}:
            if not state.order:
                print("  nothing to undo")
                continue
            pid = state.order.pop()
            state.taken.pop(pid, None)
            state.save()
            p = board.by_id(pid)
            print(f"  {GREEN}undone:{RESET} {p.web_name if p else pid} is back on the board")
            continue
        if raw.startswith("?"):
            _lookup(board, state, raw[1:])
            continue

        mine = low.startswith("m ")
        player = resolve(raw[2:] if mine else raw, board.players)
        if not player:
            print(f"  {RED}no match for {raw!r}{RESET} — try more letters")
            continue
        if player.player_id in state.taken:
            print(
                f"  {YELLOW}{player.web_name} already marked ({state.taken[player.player_id]}){RESET}"
            )
            continue

        vona = value_over_next_available(player, available(board, state))
        state.taken[player.player_id] = "me" if mine else "other"
        state.order.append(player.player_id)
        state.save()

        who = f"{GREEN}YOU drafted{RESET}" if mine else "off the board:"
        print(
            f"  {who} {BOLD}{player.web_name}{RESET} ({player.position} {player.team_short}) "
            f"value {player.value:+.1f}, vona {vona:+.1f}  [{len(state.taken)} gone]"
        )
        if mine:
            show_squad(board, state)
        show_scarcity(board, state)
        show_recommendation(board, state)


if __name__ == "__main__":
    sys.exit(main())
