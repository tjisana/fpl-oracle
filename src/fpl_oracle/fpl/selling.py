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

SELF-VALIDATION IS PART OF THE CONTRACT — but not of the selling
arithmetic itself. FPL's reported `entry_history.value` is the squad's
MARKET value at that gameweek's deadline: `sum(now_cost at the deadline)
+ bank`. It has nothing to do with selling prices (which reflect
purchase price, not current price), so `derive_squad_pricing` does not
"check the selling total" — it recomputes the market value independently
(from `now_cost`, rolled back to the relevant deadline via
`cost_change_event` when the squad is for the CURRENT event) and
reconciles THAT against FPL's figure. A match therefore validates squad
membership, the bank figure, and the price snapshot the derivation ran
on; it says nothing about whether the selling-price arithmetic above is
right, because that arithmetic follows deterministically from public
purchase facts (transfer log `element_in_cost`, or season-start price for
a GW1 starter) and needs no separate check.

This was verified against a live GW1 squad, where it happened to match:
derived market value £100.0m + £0.0m bank == the API's £100.0m. That
"match" was a coincidence of timing, not evidence the invariant was
right — no price had moved yet that season, so market value and the
naive `now_cost` sum were identical. GW2 exposed it on 2026-09-04: a live
squad (entry 6460595) reported `value` 1001 with bank 5, while summing
`now_cost` today gave 996 + 5 = 1001 only because two players' prices had
moved in OPPOSITE directions (+1, -1) after the GW2 deadline and happened
to cancel — `now_cost` alone is only ever right at the moment nothing has
moved since the deadline being checked. When the check fails, callers
must degrade to stating affordability as UNVERIFIED rather than
asserting it.
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
    market_value_at_deadline: int | None = Field(
        default=None,
        description=(
            "sum of now_cost across the owned squad, rolled back to the relevant "
            "deadline via cost_change_event when adjusted_for_event_changes is True — "
            "this, not the selling total, is what gets reconciled against FPL's reported "
            "squad value. None when the reconciliation could not be attempted at all "
            "(no reported value, or missing elements)."
        ),
    )
    adjusted_for_event_changes: bool = Field(
        default=False,
        description=(
            "True when now_cost was rolled back by cost_change_event before summing — "
            "only valid when the squad being reconciled is for bootstrap's CURRENT "
            "event, since cost_change_event is 'change since the current event's "
            "deadline'. False means either no rollback was needed/possible, or the "
            "squad's gameweek doesn't match the current event."
        ),
    )

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
    squad_gameweek: int | None = None,
    current_event: int | None = None,
    started_event: int | None = None,
) -> SquadPricing:
    """Derive purchase + selling price for every owned player, then
    reconcile FPL's reported squad value against the squad's MARKET value
    at the relevant deadline (see the module docstring for why market
    value, not the derived selling total, is what gets checked).

    `purchase_from_transfers` maps player_id -> `element_in_cost` for
    players transferred in this season; anyone absent is treated as an
    initial-squad player and priced from `cost_change_start`. That
    fallback is only valid for a manager who started at GW1 — a mid-season
    entrant never saw season-start prices — so `started_event` lets
    callers flag that case explicitly rather than relying on the
    reconciliation to happen to catch it.

    `squad_gameweek` is the gameweek the owned squad/bank/value being
    reconciled are for; `current_event` is the id of bootstrap-static's
    event with `is_current`. `cost_change_event` on each bootstrap
    element is the price change since the CURRENT event's deadline, so
    rolling `now_cost` back by it only recovers the deadline price when
    `squad_gameweek == current_event` — that is the only case
    `adjusted_for_event_changes` is True.
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

    if started_event is not None and started_event != 1:
        return SquadPricing(
            prices=prices,
            bank=bank,
            verified=False,
            note=(
                f"manager started at GW{started_event}, so season-start prices are not "
                "their purchase prices for the initial squad — UNVERIFIED"
            ),
        )

    adjust = (
        squad_gameweek is not None
        and current_event is not None
        and (squad_gameweek == current_event)
    )
    market_at_deadline = sum(
        e["now_cost"] - (e.get("cost_change_event", 0) if adjust else 0)
        for e in (elements_by_id[pid] for pid in owned_player_ids if pid in elements_by_id)
    )

    delta = reported_squad_value - (market_at_deadline + bank)
    if delta == 0:
        deadline_phrase = (
            "at the last deadline"
            if squad_gameweek is None
            else f"at the GW{squad_gameweek} deadline"
        )
        return SquadPricing(
            prices=prices,
            bank=bank,
            verified=True,
            market_value_at_deadline=market_at_deadline,
            adjusted_for_event_changes=adjust,
            note=(
                f"market value £{market_at_deadline / 10:.1f}m + bank £{bank / 10:.1f}m "
                f"reconciles exactly with FPL's reported £{reported_squad_value / 10:.1f}m "
                f"{deadline_phrase}; selling prices follow from purchase prices "
                "(transfer log / season start)"
            ),
        )

    note = (
        f"market value £{market_at_deadline / 10:.1f}m + bank £{bank / 10:.1f}m does NOT "
        f"match FPL's reported £{reported_squad_value / 10:.1f}m (off by £{delta / 10:+.1f}m) "
        "— the owned squad or bank may be stale, affordability is UNVERIFIED"
    )
    if not adjust and squad_gameweek is not None and current_event is not None:
        note += (
            f" (prices could not be rolled back to the GW{squad_gameweek} deadline: "
            f"bootstrap's current event is GW{current_event})"
        )
    return SquadPricing(
        prices=prices,
        bank=bank,
        verified=False,
        market_value_at_deadline=market_at_deadline,
        adjusted_for_event_changes=adjust,
        note=note,
    )
