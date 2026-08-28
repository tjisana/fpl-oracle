"""Derived purchase and selling prices for an owned squad.

WHY THIS IS NOT THE FORBIDDEN SHORTCUT. The project has an absolute rule
against treating `now_cost` as selling price: FPL sells a player at their
purchase price plus HALF of any rise since (rounded down), and at
`now_cost` on a fall. Substituting `now_cost` overstates budget on every
player who has risen, which corrupts every transfer decision that follows.
`fpl.entry.OwnedPlayer` therefore left both fields `None`, on the
understanding that the real numbers live only on the authenticated
`my-team/{id}/` endpoint.

That understanding was wrong, and this module exists because the inputs
turn out to be public:

* For a player TRANSFERRED IN, `entry/{id}/transfers/` records
  `element_in_cost` — the exact price paid. Latest transfer-in wins.
* For a player from the INITIAL squad, purchase price is the season-start
  price, because prices are frozen until the season begins. That is
  `now_cost - cost_change_start`, both on `bootstrap-static`'s elements.

Given purchase price, the selling rule is deterministic arithmetic. So
nothing here is estimated: it is the real rule applied to real public
facts, which is a different thing from the `now_cost` substitution the
rule forbids.

SELF-VALIDATION IS PART OF THE CONTRACT. `reconcile` checks the derived
total against the figure FPL itself reports for the squad
(`entry_history.value`). Verified against a live GW1 squad: derived
selling £100.0m + £0.0m bank == the API's £100.0m, while the naive
`now_cost` sum was £100.1m — the £0.1m the rule is there to catch. When
the check fails, callers must degrade to stating affordability as
UNVERIFIED rather than asserting it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


def selling_price(purchase_price: int, now_cost: int) -> int:
    """FPL's selling rule, in tenths of a million.

    A rise is shared with the manager at half, ROUNDED DOWN — so a 0.1
    rise returns nothing at all (1 // 2 == 0), which is why a naive
    `now_cost` reading is wrong even in the first week of a season. A
    fall is borne in full: the player sells at the lower current price.
    """
    if now_cost > purchase_price:
        return purchase_price + (now_cost - purchase_price) // 2
    return now_cost


def season_start_price(now_cost: int, cost_change_start: int) -> int:
    """What a player cost when the season opened. Prices don't move until
    the season starts, so this IS the purchase price for anyone in the
    manager's initial squad."""
    return now_cost - cost_change_start


class DerivedPrice(BaseModel):
    player_id: int
    purchase_price: int
    now_cost: int
    selling_price: int
    source: str = Field(
        description="'transfer-log' (element_in_cost) or 'season-start' (initial squad)"
    )


class SquadPricing(BaseModel):
    """Derived prices for a whole squad, plus whether they can be trusted.

    `verified` is the load-bearing field. False means the derivation did
    not reconcile against FPL's own squad value, and every affordability
    claim built on it must be presented as unverified rather than stated
    as fact — with £0.0 in the bank, a wrong selling price is the
    difference between a legal transfer and one the owner cannot make.
    """

    prices: dict[int, DerivedPrice]
    bank: int
    verified: bool
    note: str

    def selling(self, player_id: int) -> int | None:
        derived = self.prices.get(player_id)
        return None if derived is None else derived.selling_price

    def budget_for(self, player_id: int) -> int | None:
        """What the owner could spend replacing `player_id`: that player's
        selling price plus whatever is in the bank."""
        sell = self.selling(player_id)
        return None if sell is None else sell + self.bank


def derive_squad_pricing(
    *,
    owned_player_ids: list[int],
    elements_by_id: dict[int, dict],
    purchase_from_transfers: dict[int, int],
    bank: int,
    reported_squad_value: int | None,
) -> SquadPricing:
    """Derive purchase + selling price for every owned player, then check
    the total against `reported_squad_value` (FPL's own `entry_history.value`).

    `purchase_from_transfers` maps player_id -> `element_in_cost` for
    players transferred in this season; anyone absent is treated as an
    initial-squad player and priced from `cost_change_start`. That
    fallback is only valid for a manager who started at GW1 — a mid-season
    entrant never saw season-start prices — so callers must not pass one,
    and the reconciliation below is what catches it if they do.
    """
    prices: dict[int, DerivedPrice] = {}
    for player_id in owned_player_ids:
        element = elements_by_id.get(player_id)
        if element is None:
            continue
        now_cost = element["now_cost"]
        if player_id in purchase_from_transfers:
            purchase = purchase_from_transfers[player_id]
            source = "transfer-log"
        else:
            purchase = season_start_price(now_cost, element["cost_change_start"])
            source = "season-start"
        prices[player_id] = DerivedPrice(
            player_id=player_id,
            purchase_price=purchase,
            now_cost=now_cost,
            selling_price=selling_price(purchase, now_cost),
            source=source,
        )

    total_selling = sum(p.selling_price for p in prices.values())

    if reported_squad_value is None:
        return SquadPricing(
            prices=prices,
            bank=bank,
            verified=False,
            note=(
                "no reported squad value to reconcile against — selling prices are derived "
                "but UNVERIFIED; treat affordability as unconfirmed"
            ),
        )

    if len(prices) != len(owned_player_ids):
        return SquadPricing(
            prices=prices,
            bank=bank,
            verified=False,
            note=(
                f"only {len(prices)} of {len(owned_player_ids)} owned players are in the "
                "player database, so the squad total cannot be reconciled — UNVERIFIED"
            ),
        )

    delta = reported_squad_value - (total_selling + bank)
    if delta == 0:
        return SquadPricing(
            prices=prices,
            bank=bank,
            verified=True,
            note=(
                f"derived selling total £{total_selling / 10:.1f}m + bank "
                f"£{bank / 10:.1f}m reconciles exactly with FPL's reported squad value"
            ),
        )
    return SquadPricing(
        prices=prices,
        bank=bank,
        verified=False,
        note=(
            f"derived selling total £{total_selling / 10:.1f}m + bank £{bank / 10:.1f}m "
            f"does NOT match FPL's reported £{reported_squad_value / 10:.1f}m "
            f"(off by £{delta / 10:+.1f}m) — affordability is UNVERIFIED. Most likely a "
            "mid-season entry, or a transfer the log doesn't describe."
        ),
    )
