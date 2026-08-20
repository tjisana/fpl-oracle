"""A practice draft: bots pick, and it stops dead when it is your turn.

A finished draft is no rehearsal — the thing worth practising is the two
seconds when the board becomes yours and you have to read it. So this drives a
live draft against the real league's team names, in a randomly generated snake
order, and then WAITS on your pick exactly as the real room does.

It emits the same payload shape as `draft/{league}/choices`, so the app cannot
tell the difference between this and the real thing. That is the point: what
you rehearse is what you will use.
"""

from __future__ import annotations

import random
import threading

from fpl_oracle.draft.board import ROSTER_SLOTS, Board, DraftPlayer
from fpl_oracle.fpl.players import Position

SQUAD_SIZE = sum(ROSTER_SLOTS.values())


class Simulator:
    """Runs a snake draft where every team but yours is a bot."""

    def __init__(
        self,
        entries: list[str],
        my_team: str,
        board: Board,
        *,
        seconds_per_pick: float = 2.5,
        seed: int | None = None,
        shuffle: bool = True,
    ) -> None:
        rng = random.Random(seed)
        self.order = list(entries)
        if shuffle:
            # The real draft order is randomly generated, so practising against
            # the league's listed order would rehearse the wrong thing.
            rng.shuffle(self.order)
        self.teams = len(self.order)
        self.my_team = my_team
        self.seconds_per_pick = seconds_per_pick
        self._lock = threading.RLock()
        self._stop = threading.Event()

        self.pool: list[DraftPlayer] = sorted(board.players, key=lambda p: -p.value)
        self.picks: list[dict] = []
        self.taken: set[int] = set()
        self.rosters: dict[str, dict[Position, int]] = {
            t: dict.fromkeys(ROSTER_SLOTS, 0) for t in self.order
        }

    # ---- snake ----
    def slot_for(self, pick_index: int) -> int:
        rd, off = divmod(pick_index, self.teams)
        return off if rd % 2 == 0 else self.teams - 1 - off

    @property
    def total_picks(self) -> int:
        return self.teams * SQUAD_SIZE

    def team_on_clock(self) -> str | None:
        if len(self.picks) >= self.total_picks:
            return None
        return self.order[self.slot_for(len(self.picks))]

    def is_my_turn(self) -> bool:
        return self.team_on_clock() == self.my_team

    # ---- picking ----
    def _best_for(self, team: str) -> DraftPlayer | None:
        """A bot takes the highest-value player it still has room for."""
        need = self.rosters[team]
        for p in self.pool:
            if p.player_id in self.taken:
                continue
            if need[p.position] >= ROSTER_SLOTS[p.position]:
                continue
            if p.status in {"u", "i", "s"}:
                continue
            return p
        return None

    def _record(self, team: str, player: DraftPlayer) -> None:
        index = len(self.picks) + 1
        self.picks.append(
            {
                "element": player.player_id,
                "entry_name": team,
                "index": index,
                "pick": (index - 1) % self.teams + 1,
                "round": (index - 1) // self.teams + 1,
                "was_auto": team != self.my_team,
            }
        )
        self.taken.add(player.player_id)
        self.rosters[team][player.position] += 1

    def step(self) -> bool:
        """Advance one bot pick. Returns False when it is your turn or we're done."""
        with self._lock:
            team = self.team_on_clock()
            if team is None or team == self.my_team:
                return False
            player = self._best_for(team)
            if player is None:
                return False
            self._record(team, player)
            return True

    def user_pick(self, element_id: int) -> tuple[bool, str]:
        with self._lock:
            if not self.is_my_turn():
                return False, f"not your turn — {self.team_on_clock()} is on the clock"
            if element_id in self.taken:
                return False, "already drafted"
            player = next((p for p in self.pool if p.player_id == element_id), None)
            if player is None:
                return False, "unknown player"
            need = self.rosters[self.my_team]
            if need[player.position] >= ROSTER_SLOTS[player.position]:
                return False, f"your {player.position} slots are full"
            self._record(self.my_team, player)
            return True, player.web_name

    # ---- the API shape the app already speaks ----
    def choices_payload(self) -> dict:
        with self._lock:
            return {"choices": list(self.picks), "idle": [], "element_status": []}

    def details_payload(self, league_name: str = "PRACTICE DRAFT") -> dict:
        return {
            "league": {"id": 0, "name": league_name, "draft_status": "live"},
            "league_entries": [{"entry_name": t, "entry_id": i} for i, t in enumerate(self.order)],
            "matches": [],
            "standings": [],
        }

    # ---- background bot loop ----
    def run(self) -> None:
        while not self._stop.is_set():
            if not self.step():
                # Your turn, or the draft is over — idle until that changes.
                self._stop.wait(0.4)
                continue
            self._stop.wait(self.seconds_per_pick)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
