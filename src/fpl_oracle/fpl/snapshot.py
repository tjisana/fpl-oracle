"""Daily price snapshot + purchase-price derivation.

ROADMAP.md KNOWN ISSUES: `selling_price` is not available from any public
FPL endpoint — it lives on the authenticated `my-team/{id}/` endpoint, and
`fpl.entry` deliberately leaves it `None` rather than recomputing it from
`now_cost` (selling price is purchase price plus half of any rise since,
never just the current price — see `fpl.entry`'s module docstring). This
module is the path forward the roadmap describes, in two independent
pieces:

1. `write_snapshot()` — every player's `id` + `now_cost`, dumped to
   `data/snapshots/{YYYY-MM-DD}.json` (UTC date) once a day. This is the
   raw material a FUTURE purchase-price backfill for the INITIAL squad
   would need (see `derive_purchase_prices`'s docstring for why that
   backfill isn't implemented yet). Free, additive, writes only under
   the gitignored `data/` tree — safe to run daily starting immediately.
2. `derive_purchase_prices(entry_id)` — for a player TRANSFERRED IN at
   some point this season, `entry/{id}/transfers/` already carries the
   exact cost paid (`element_in_cost`) directly from the API — no
   snapshot needed, no computation beyond picking the latest transfer-in
   per player. This is the part of the roadmap note that's fully
   resolvable today.

Both pieces refuse to guess. Neither ever recomputes a price from
`now_cost` as a substitute for a real purchase/selling price — the exact
mistake `fpl.entry` was built to avoid (see its module docstring point 1).
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from fpl_oracle.fpl.client import get_bootstrap_static, get_transfers

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_DIR = Path("data/snapshots")


class PlayerPriceSnapshot(BaseModel):
    player_id: int
    now_cost: int


class PriceSnapshot(BaseModel):
    """One day's `data/snapshots/{date}.json`: every player's `now_cost`
    at `taken_at`. `snapshot_date` is the UTC calendar date this snapshot
    represents — stored alongside `taken_at` (a precise timestamp) so a
    later reader can filter/sort snapshots without re-deriving the date
    from either the filename or a timezone-sensitive parse of `taken_at`.
    """

    taken_at: datetime
    snapshot_date: date
    prices: list[PlayerPriceSnapshot]


def _snapshot_path(directory: Path, snapshot_date: date) -> Path:
    return directory / f"{snapshot_date.isoformat()}.json"


def write_snapshot(
    directory: Path = DEFAULT_SNAPSHOT_DIR,
    *,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> Path:
    """Dump every player's `id` + `now_cost` (from `bootstrap-static`) to
    `directory/{YYYY-MM-DD}.json`, keyed off the UTC date.

    IDEMPOTENT for a given UTC date: if a snapshot file for today already
    exists, it is left untouched and its path is returned as-is — a
    second run the same day (e.g. a daily cron firing twice, or a manual
    rerun) never overwrites the day's snapshot, moves its `taken_at`
    forward, or spends a redundant `bootstrap-static` fetch. Returns the
    path of the snapshot for `now`'s UTC date either way.
    """
    now = now or datetime.now(UTC)
    today = now.date()
    directory.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(directory, today)
    if path.exists():
        logger.info("snapshot: %s already exists — leaving it untouched", path)
        return path

    data = get_bootstrap_static(force_refresh=force_refresh)
    prices = [
        PlayerPriceSnapshot(player_id=e["id"], now_cost=e["now_cost"])
        for e in data.get("elements", [])
    ]
    snapshot = PriceSnapshot(taken_at=now, snapshot_date=today, prices=prices)
    path.write_text(snapshot.model_dump_json(indent=2))
    logger.info("snapshot: wrote %s (%d players)", path, len(prices))
    return path


class PurchasePrices(BaseModel):
    """Purchase price (tenths of a million, matching `now_cost`'s
    convention) for every player of `entry_id`'s that's derivable from
    the transfer log alone.

    `known` covers every player who appears as `element_in` in
    `entry/{id}/transfers/` at least once — the API records the exact
    cost paid at `element_in_cost`, so this is read directly, never
    computed or estimated. A player bought, sold, and bought again uses
    the LATEST such transfer (the earlier purchase was necessarily sold
    before the player could be re-bought, so only the most recent one
    describes what's currently held).

    Anyone NOT a key of `known` — a player held since the manager's
    INITIAL squad and never transferred since — is UNKNOWN: their
    purchase price isn't in the transfer log at all, because they were
    never transferred in. `unknown_note` explains why, in general terms
    (this function has no squad list to enumerate them by id — pair its
    result with `fpl.entry.fetch_my_squad`'s player ids to see exactly
    who that is for a given gameweek). Per the module docstring, the only
    way to resolve them is a price snapshot from on-or-before the
    manager's initial squad selection; `snapshot.py` only starts
    collecting snapshots from the day it's first run, so today (or any
    day before enough history accumulates) that backfill has nothing to
    work with. This function does NOT attempt it — deliberately: a
    snapshot taken well after squad selection could easily post-date a
    price rise/fall the manager's actual purchase never saw, and silently
    treating that snapshot's `now_cost` as the purchase price would be
    exactly the kind of guess this module exists to refuse. Never
    recomputed from `now_cost` as a substitute either, for the same
    reason `fpl.entry.OwnedPlayer` never does.
    """

    known: dict[int, int]
    unknown_note: str = Field(
        default=(
            "any owned player not in `known` was never transferred in this season — "
            "held since the initial squad. Resolving those needs a price snapshot from "
            "on-or-before initial squad selection, which snapshot.py does not yet have "
            "enough history to provide; never guessed or backfilled from now_cost."
        )
    )


def derive_purchase_prices(entry_id: int) -> PurchasePrices:
    """Replay `entry_id`'s full transfer log (`client.get_transfers`,
    oldest-first per the API) to recover the purchase price of every
    player transferred in this season. Pure over the fetched log — see
    `PurchasePrices`'s docstring for exactly what `known` covers and why
    initial-squad players are left `unknown`.
    """
    transfers = get_transfers(entry_id)
    known: dict[int, int] = {}
    for t in transfers:
        # Oldest-first input means a later `element_in` for the same
        # player id simply overwrites an earlier one here — exactly the
        # "latest transfer-in wins" rule the docstring describes.
        known[t.element_in] = t.element_in_cost
    return PurchasePrices(known=known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Write today's FPL price snapshot (id + now_cost for every player) to "
            "data/snapshots/{YYYY-MM-DD}.json. Idempotent — safe to run daily via cron."
        )
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_SNAPSHOT_DIR,
        help=f"snapshot directory (default: {DEFAULT_SNAPSHOT_DIR})",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="bypass the 6h bootstrap-static cache and refetch live before snapshotting",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    path = write_snapshot(directory=args.dir, force_refresh=args.force_refresh)
    print(f"snapshot: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
