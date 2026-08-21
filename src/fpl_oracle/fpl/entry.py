"""My-squad awareness: what the owner actually owns, as of a given
gameweek, and how many free transfers they have to work with.

Everything in-season depends on this (ROADMAP.md, "My-squad awareness")
— the transfer recommender, price-change alerts, and the report's "my
squad" section all key off `MySquad`. Built on `fpl.client`'s raw
entry/picks/history/transfers fetches (all public, no auth) and
`fpl.players.PlayerDB` for player identity (position, name).

Two things this module is careful to get right, per the task spec:

1. SELLING PRICE IS NOT CURRENT PRICE. FPL sells a player at their
   purchase price plus half of any price rise since, rounded down (and at
   a loss with no such adjustment on a fall) — never at `now_cost`.
   Getting this wrong misstates the owner's available budget on every
   transfer decision. HOWEVER: the public `entry/{id}/event/{gw}/picks/`
   endpoint this module fetches from does NOT carry `purchase_price` or
   `selling_price` per pick — those fields exist only on the
   authenticated `my-team/{id}/` endpoint, which requires a logged-in FPL
   session and is out of scope for a "no auth" module (see this module's
   docstring note in the project report / PLAN.md for the full
   discrepancy against the original task spec). `OwnedPlayer` therefore
   carries these as `int | None`, always `None` for now — NEVER
   recomputed from `now_cost` as a stand-in, per the explicit instruction
   that doing so misstates budget. A follow-up that adds authenticated
   `my-team` access is the way to actually populate them.
2. FREE TRANSFERS ARE DERIVED, NOT GIVEN. No endpoint exposes "N free
   transfers available" directly. `derive_free_transfers` replays the
   season's `entry/history` (`current`, `event_transfers`) and
   `entry/transfers` records against the known accrual rule (+1/gameweek,
   capped at 5 per the fpl-domain skill's squad-rules section; a
   Wildcard/Free Hit gameweek neither costs nor draws down the bank) and
   cross-checks the two data sources against each other. Any gap or
   disagreement returns `None` with a reason attached rather than a
   guessed number the owner might act on.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel

from fpl_oracle.fpl import client
from fpl_oracle.fpl.players import PlayerDB, Position

ENTRY_ID_ENV_VAR = "FPL_ENTRY_ID"

# Bankable free-transfer cap — per the fpl-domain skill's squad-rules
# section ("1 free transfer per gameweek (bankable, cap has been 5)").
# The skill flags this as something to verify at season start; if FPL
# changes it again, update the skill first and this constant second.
FREE_TRANSFER_CAP = 5

# Chip names (as the API spells them, lowercase) that suspend
# free-transfer bookkeeping for the gameweek they're played: transfers
# made that week cost nothing, and — this is the part that's easy to get
# wrong — do NOT draw down the accumulated free-transfer bank either. The
# bank carries through unchanged into the following week's +1 accrual.
_FT_EXEMPT_CHIPS = frozenset({"wildcard", "freehit"})


class EntryIdNotConfiguredError(RuntimeError):
    """Raised by `get_entry_id` when FPL_ENTRY_ID isn't set. The id is
    public data, not a secret, but lives in `.env` for convenience
    alongside the project's other configuration."""

    def __init__(self) -> None:
        super().__init__(
            f"{ENTRY_ID_ENV_VAR} is not set. This is the owner's public FPL manager "
            "id, not a secret — find it by logging into fantasy.premierleague.com, "
            "opening 'Points' or 'Transfers', and reading the number in the URL: "
            "fantasy.premierleague.com/entry/<ID>/event/1. Set it in .env as "
            f"{ENTRY_ID_ENV_VAR}=<ID>, the same way the project's other config lives "
            "there."
        )


def get_entry_id() -> int:
    """Read the owner's FPL entry id from `FPL_ENTRY_ID` in the
    environment (`.env`, via python-dotenv — same convention as
    `extract/extractor.py`'s `load_dotenv()` use). Never hardcoded: this
    id is owner-specific, and the module must work for whoever's `.env`
    it's running against.
    """
    load_dotenv()
    raw = os.environ.get(ENTRY_ID_ENV_VAR)
    if raw is None or not raw.strip():
        raise EntryIdNotConfiguredError()
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ValueError(f"{ENTRY_ID_ENV_VAR}={raw!r} is not a valid integer entry id.") from e


class OwnedPlayer(BaseModel):
    """One of the manager's 15 owned players for a given gameweek."""

    player_id: int
    web_name: str
    position: Position
    is_starting: bool
    is_captain: bool
    is_vice_captain: bool
    multiplier: int
    # Tenths of a million, matching `now_cost`'s convention. Always None
    # today — see the module docstring, point 1: the public picks
    # endpoint doesn't carry these fields, and this module never
    # recomputes them from `now_cost` as a substitute.
    purchase_price: int | None
    selling_price: int | None

    @property
    def purchase_price_m(self) -> float | None:
        return None if self.purchase_price is None else self.purchase_price / 10

    @property
    def selling_price_m(self) -> float | None:
        return None if self.selling_price is None else self.selling_price / 10


class MySquad(BaseModel):
    """The manager's 15 picks for `gameweek`, plus budget and
    free-transfer state.

    `bank` and `team_value` are read from the picks payload's own
    `entry_history` sub-object (exact for THIS gameweek), not from
    `entry/{id}/`'s `last_deadline_bank/value` (which only ever reflects
    the manager's MOST RECENT deadline — the wrong source once a report
    is built for a past gameweek).

    `free_transfers` is the count available for `gameweek + 1` — the next
    transfer decision — not for `gameweek` itself, since the picks for
    `gameweek` are already locked in by the time they're public. `None`
    when it couldn't be reliably derived; see `derive_free_transfers`.
    """

    entry_id: int
    gameweek: int
    team_name: str
    players: list[OwnedPlayer]
    bank: int
    team_value: int
    free_transfers: int | None
    free_transfers_note: str

    @property
    def bank_m(self) -> float:
        return self.bank / 10

    @property
    def team_value_m(self) -> float:
        return self.team_value / 10


def build_my_squad(
    *,
    entry_id: int,
    gameweek: int,
    team_name: str,
    picks: client.PicksResponse,
    player_db: PlayerDB,
    free_transfers: int | None,
    free_transfers_note: str,
) -> MySquad:
    """Pure assembly of a `MySquad` from an already-fetched picks payload
    — no I/O, fully unit-testable (mirrors `PlayerDB.from_bootstrap` vs
    `PlayerDB.load`'s pure/network split in `fpl.players`)."""
    owned: list[OwnedPlayer] = []
    for pick in picks.picks:
        player = player_db.get(pick.element)
        if player is None:
            # A player id the picks payload references but the current
            # PlayerDB doesn't know — a stale/mismatched bootstrap-static
            # snapshot, not a crash condition. Skip rather than fabricate
            # an identity for it.
            continue
        owned.append(
            OwnedPlayer(
                player_id=player.player_id,
                web_name=player.web_name,
                position=player.position,
                is_starting=pick.multiplier > 0,
                is_captain=pick.is_captain,
                is_vice_captain=pick.is_vice_captain,
                multiplier=pick.multiplier,
                purchase_price=None,
                selling_price=None,
            )
        )

    return MySquad(
        entry_id=entry_id,
        gameweek=gameweek,
        team_name=team_name,
        players=owned,
        bank=picks.entry_history.bank,
        team_value=picks.entry_history.value,
        free_transfers=free_transfers,
        free_transfers_note=free_transfers_note,
    )


def fetch_my_squad(entry_id: int, gameweek: int, player_db: PlayerDB) -> MySquad | None:
    """Fetch and assemble `entry_id`'s squad for `gameweek`. Returns None
    — cleanly, never raising — when picks for that gameweek aren't public
    yet (pre-deadline); see `client.get_picks`.
    """
    picks = client.get_picks(entry_id, gameweek)
    if picks is None:
        return None

    entry_summary = client.get_entry(entry_id)
    team_name = entry_summary.team_name if entry_summary is not None else f"entry {entry_id}"

    ft_result = fetch_free_transfers(entry_id, target_gameweek=gameweek + 1)

    return build_my_squad(
        entry_id=entry_id,
        gameweek=gameweek,
        team_name=team_name,
        picks=picks,
        player_db=player_db,
        free_transfers=ft_result.free_transfers,
        free_transfers_note=ft_result.note,
    )


class FreeTransferResult(BaseModel):
    free_transfers: int | None
    note: str


def derive_free_transfers(
    *,
    target_gameweek: int,
    started_event: int,
    history: list[client.GameweekHistory],
    chips: list[client.ChipPlay],
    transfers: list[client.TransferRecord],
) -> FreeTransferResult:
    """Replay the accrual rule (see module docstring, point 2) across
    every completed gameweek from `started_event` up to
    `target_gameweek - 1`, returning the free transfers available for
    `target_gameweek`.

    Pure — no I/O — takes already-fetched, already-parsed history: this
    is the piece that's actually a claim about FPL's rules, so it's the
    piece that gets tested in isolation.

    Refuses to guess: returns `free_transfers=None` (with a `note`
    explaining why) whenever the inputs can't support a confident replay
    — a gap in the gameweek history, or the transfers log and history's
    own `event_transfers` count disagreeing for some gameweek.
    """
    if target_gameweek <= started_event:
        return FreeTransferResult(
            free_transfers=None,
            note=(
                f"gameweek {target_gameweek} is at or before the manager's first "
                f"gameweek ({started_event}) — free-transfer accounting hasn't started yet"
            ),
        )

    required_events = list(range(started_event, target_gameweek))
    by_event = {h.event: h for h in history}
    missing = [gw for gw in required_events if gw not in by_event]
    if missing:
        return FreeTransferResult(
            free_transfers=None,
            note=(
                f"missing gameweek history for GW{missing} — cannot reliably replay "
                "free-transfer accrual across a gap"
            ),
        )

    chip_by_event = {c.event: c.name.lower() for c in chips}
    transfers_by_event: dict[int, int] = {}
    for t in transfers:
        transfers_by_event[t.event] = transfers_by_event.get(t.event, 0) + 1

    # Starting stock: FPL grants 1 free transfer for the gameweek right
    # after the manager's first (gameweek 1 itself, or `started_event` for
    # a manager who joined mid-season, is initial squad selection, not a
    # transfer week).
    free_transfers = 1
    for gw in required_events:
        if gw == started_event:
            continue
        gw_history = by_event[gw]
        made = transfers_by_event.get(gw, 0)
        if made != gw_history.event_transfers:
            return FreeTransferResult(
                free_transfers=None,
                note=(
                    f"transfers log ({made}) and history event_transfers "
                    f"({gw_history.event_transfers}) disagree for GW{gw} — derivation "
                    "aborted rather than guessed"
                ),
            )
        chip = chip_by_event.get(gw)
        if chip not in _FT_EXEMPT_CHIPS:
            free_transfers = max(free_transfers - made, 0)
        free_transfers = min(free_transfers + 1, FREE_TRANSFER_CAP)

    return FreeTransferResult(
        free_transfers=free_transfers,
        note=(
            f"derived by replaying {len(required_events)} completed gameweek(s) of "
            "transfer history against the +1/gameweek, cap-5 accrual rule"
        ),
    )


def fetch_free_transfers(entry_id: int, target_gameweek: int) -> FreeTransferResult:
    """Thin network wrapper around `derive_free_transfers`: fetches the
    entry summary (for `started_event`), history (for `current` +
    `chips`), and the transfer log, then delegates."""
    entry_summary = client.get_entry(entry_id)
    if entry_summary is None:
        return FreeTransferResult(
            free_transfers=None, note=f"entry {entry_id} not found — cannot derive free transfers"
        )

    history = client.get_entry_history(entry_id)
    if history is None:
        return FreeTransferResult(
            free_transfers=None,
            note=f"entry {entry_id} has no history data — cannot derive free transfers",
        )

    transfers = client.get_transfers(entry_id)

    return derive_free_transfers(
        target_gameweek=target_gameweek,
        started_event=entry_summary.started_event,
        history=history.current,
        chips=history.chips,
        transfers=transfers,
    )
