"""Price-change signal extraction: turns the raw `price_change_*` /
`cost_change_*` / `transfers_*_event` fields on a `bootstrap-static`
element into a per-player `PriceSignal` — direction, confidence, and how
soon a move is projected.

Design note (parallel model, not an extension of `fpl.players.Player`):
these fields are parsed into their own `RawPriceFields` model here, kept
entirely separate from `Player`. `Player` is the identity/roster model
consumed pervasively by `solver/`, `consensus/`, `draft/`, and
`availability.py` — none of which have any use for market-timing
internals, and several of those modules' tests construct `Player(...)`
directly by keyword. Bundling volatile price-projection fields onto
`Player` would force every one of those consumers to carry dead weight
for a concern they don't have, blurring the module boundary CLAUDE.md
asks for ("Pydantic models at every module boundary"). A parallel model
parses the same raw bootstrap element dict `Player.from_bootstrap` does,
independently, and this module's public surface (`PriceSignal`,
`build_price_signals`, `rank_by_imminence`) is the only thing other code
needs to import.

Season-start caveat (verified live 2026-08-21, GW1 not yet underway):
every price-change field currently reads zero/empty/None because no
transfers have happened yet — `price_change_projections` entries carry
`likelihood: 0`, `cost_change_event` is `0`, etc. That is not "this
module doesn't work", it's "there is no signal yet" — every code path
below treats an all-zero or entirely-missing payload as a clean STABLE
result, never a crash, and the fixtures in `tests/test_prices.py` cover
that shape explicitly alongside hand-built rising/falling cases.

Confidence rule (the one that matters most): `price_change_locked_until`
and `price_change_calibrating` each mean "FPL itself isn't vouching for
a projection right now" — a recent change is still settling, or the
price is temporarily frozen. Trusting the projection anyway is exactly
the failure mode the caller (a squad owner deciding whether to take a
transfer hit tonight) can least afford, so both map straight to
UNKNOWN, before any projection is even inspected. Because the live
payload never showed either field populated today, the exact shape of
`price_change_locked_until` (timestamp? seconds remaining?) is
unconfirmed — see `_is_locked` for why this treats ANY non-empty,
non-zero value as "locked" rather than trying to parse or compare a
timestamp we haven't seen live: a false "locked" costs one UNKNOWN
verdict, a false "not locked" costs a bad transfer.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from fpl_oracle.fpl.client import get_bootstrap_static

# What a JSON-decoded bootstrap-static scalar field can actually be —
# used instead of bare `object` on the coercion helpers below so `float()`
# / `int()` type-check soundly (an arbitrary `object` minus `None` still
# isn't guaranteed to support `SupportsFloat`; this union is).
_JSONScalar = str | int | float | bool | None


class PriceDirection(StrEnum):
    RISING = "RISING"
    FALLING = "FALLING"
    STABLE = "STABLE"
    UNKNOWN = "UNKNOWN"


class PriceProjection(BaseModel):
    """One entry of `price_change_projections`: FPL's own forward-looking
    projection for a single day offset (0 = today/tonight's change window,
    1 = tomorrow night, ...)."""

    offset: int
    projected_percent: float
    likelihood: float

    @field_validator("projected_percent", "likelihood", mode="before")
    @classmethod
    def _coerce_float(cls, value: _JSONScalar) -> float:
        """The live payload showed these as strings (`'0'`); coerce
        defensively rather than trusting the type. An unparseable or
        missing value becomes 0.0 — "no signal" — never a crash."""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


class RawPriceFields(BaseModel):
    """Every price/transfer field `bootstrap-static` carries per element,
    parsed as-is (no direction/confidence judgment yet — that's
    `_signal_from_raw`). All fields beyond `player_id`/`now_cost` are
    optional: older payloads or a season that hasn't started yet may omit
    them entirely, and that must never be a parse error."""

    player_id: int
    now_cost: int
    price_change_projections: list[PriceProjection] = Field(default_factory=list)
    price_change_hourly_rate: float | None = None
    price_change_percent: float | None = None
    price_change_locked_until: int | float | str | None = None
    price_change_calibrating: bool | None = None
    cost_change_event: int | None = None
    cost_change_event_fall: int | None = None
    cost_change_start: int | None = None
    cost_change_start_fall: int | None = None
    transfers_in_event: int | None = None
    transfers_out_event: int | None = None
    selected_by_percent: float | None = None

    @classmethod
    def from_element(cls, element: dict) -> RawPriceFields:
        return cls(
            player_id=element["id"],
            now_cost=element.get("now_cost", 0),
            price_change_projections=_parse_projections(element.get("price_change_projections")),
            price_change_hourly_rate=_coerce_optional_float(
                element.get("price_change_hourly_rate")
            ),
            price_change_percent=_coerce_optional_float(element.get("price_change_percent")),
            price_change_locked_until=element.get("price_change_locked_until"),
            price_change_calibrating=_coerce_optional_bool(element.get("price_change_calibrating")),
            cost_change_event=_coerce_optional_int(element.get("cost_change_event")),
            cost_change_event_fall=_coerce_optional_int(element.get("cost_change_event_fall")),
            cost_change_start=_coerce_optional_int(element.get("cost_change_start")),
            cost_change_start_fall=_coerce_optional_int(element.get("cost_change_start_fall")),
            transfers_in_event=_coerce_optional_int(element.get("transfers_in_event")),
            transfers_out_event=_coerce_optional_int(element.get("transfers_out_event")),
            selected_by_percent=_coerce_optional_float(element.get("selected_by_percent")),
        )


def _parse_projections(raw: object) -> list[PriceProjection]:
    """Parse `price_change_projections` defensively: a missing key, a
    non-list value, or a malformed entry within the list all degrade to
    "skip it", never a crash — the field is entirely absent from older
    payloads and the season-not-started payload well may not have it
    settled either."""
    if not isinstance(raw, list):
        return []
    projections: list[PriceProjection] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            offset = int(item.get("offset", 0))
        except (TypeError, ValueError):
            continue
        projections.append(
            PriceProjection(
                offset=offset,
                projected_percent=item.get("projected_percent"),
                likelihood=item.get("likelihood"),
            )
        )
    return projections


def _coerce_optional_float(value: _JSONScalar) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: _JSONScalar) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_bool(value: _JSONScalar) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no", ""):
            return False
        return None
    return None


def _is_locked(locked_until: float | str | None) -> bool:
    """See the module docstring's "Confidence rule" section: any
    non-empty, non-zero value is treated as "currently locked" — a
    conservative reading chosen because the field's exact shape has never
    been observed live with a real value in it."""
    if locked_until is None:
        return False
    if isinstance(locked_until, int | float):
        return locked_until != 0
    if isinstance(locked_until, str):
        return locked_until.strip() not in ("", "0")
    return bool(locked_until)


class PriceSignal(BaseModel):
    """Per-player verdict: is this player's price about to move, in which
    direction, how confident is FPL's own projection, and is that
    projection even trustworthy right now."""

    player_id: int
    now_cost: int
    direction: PriceDirection
    likelihood: float | None
    day_offset: int | None
    net_transfers_event: int
    locked: bool
    calibrating: bool

    @property
    def price_m(self) -> float:
        return self.now_cost / 10


def _best_projection(projections: list[PriceProjection]) -> PriceProjection | None:
    """The projection FPL itself is most confident in: highest
    `likelihood`, ties broken toward the soonest offset (smaller offset
    = sooner). Returns None for an empty list, never raises."""
    if not projections:
        return None
    return max(projections, key=lambda p: (p.likelihood, -p.offset))


def _direction_from_percent(percent: float) -> PriceDirection:
    if percent > 0:
        return PriceDirection.RISING
    if percent < 0:
        return PriceDirection.FALLING
    return PriceDirection.STABLE


def _signal_from_raw(raw: RawPriceFields) -> PriceSignal:
    net_transfers = (raw.transfers_in_event or 0) - (raw.transfers_out_event or 0)
    locked = _is_locked(raw.price_change_locked_until)
    calibrating = bool(raw.price_change_calibrating)

    if locked or calibrating:
        # UNKNOWN regardless of what the projections say — see the
        # module docstring. A locked/calibrating price must never imply
        # confidence the projection doesn't currently carry.
        return PriceSignal(
            player_id=raw.player_id,
            now_cost=raw.now_cost,
            direction=PriceDirection.UNKNOWN,
            likelihood=None,
            day_offset=None,
            net_transfers_event=net_transfers,
            locked=locked,
            calibrating=calibrating,
        )

    best = _best_projection(raw.price_change_projections)
    if best is not None:
        direction = _direction_from_percent(best.projected_percent)
        likelihood: float | None = best.likelihood
        day_offset: int | None = best.offset
    elif raw.price_change_percent is not None:
        # No projections array at all (older payload shape) — fall back
        # to the single current-period percent as the only signal on
        # offer. No offset/likelihood attached to it: it isn't a forward
        # projection, it's "this is what's already moving".
        direction = _direction_from_percent(raw.price_change_percent)
        likelihood = None
        day_offset = None
    else:
        # Nothing to go on at all (missing fields, or a genuinely flat
        # preseason payload) — STABLE, not UNKNOWN: there's no reason to
        # distrust "no movement" when nothing suggests otherwise, unlike
        # the locked/calibrating case above which actively distrusts it.
        direction = PriceDirection.STABLE
        likelihood = None
        day_offset = None

    return PriceSignal(
        player_id=raw.player_id,
        now_cost=raw.now_cost,
        direction=direction,
        likelihood=likelihood,
        day_offset=day_offset,
        net_transfers_event=net_transfers,
        locked=locked,
        calibrating=calibrating,
    )


def build_price_signals(data: dict) -> dict[int, PriceSignal]:
    """Build one `PriceSignal` per element in a `bootstrap-static`-shaped
    payload, keyed by player id. Pure/offline — takes the raw dict
    (`fpl.client.get_bootstrap_static()`'s return shape), never fetches
    itself; see `load_price_signals` for the live-fetching wrapper."""
    signals: dict[int, PriceSignal] = {}
    for element in data.get("elements", []):
        raw = RawPriceFields.from_element(element)
        signals[raw.player_id] = _signal_from_raw(raw)
    return signals


def load_price_signals(force_refresh: bool = False) -> dict[int, PriceSignal]:
    """Fetch live `bootstrap-static` (via the shared 6h-fresh cache — see
    `fpl.client.get_bootstrap_static`) and build price signals from it.
    `force_refresh` passes straight through, same meaning as everywhere
    else it appears: bypass the freshness check and refetch live."""
    return build_price_signals(get_bootstrap_static(force_refresh=force_refresh))


def rank_by_imminence(
    signals: Iterable[PriceSignal], direction: PriceDirection
) -> list[PriceSignal]:
    """Rank players by how imminent a projected price move in `direction`
    is — the primitive behind "who in my squad is about to fall" and "who
    am I about to miss a rise on" (a caller filters `signals` down to
    their squad, or their watchlist, before calling this).

    Only RISING and FALLING are meaningful directions to rank by: STABLE
    carries no move to be imminent about, and UNKNOWN is precisely the
    set of players whose projection isn't trustworthy enough to rank at
    all (locked/calibrating) — sorting them would imply a confidence the
    signal explicitly doesn't have. Passing either raises rather than
    silently returning an empty or nonsensical ordering.

    Sort order: highest likelihood first (FPL's own confidence in the
    move), ties broken by soonest day_offset. A signal with no likelihood
    at all (the older-payload `price_change_percent`-only fallback, where
    `likelihood` and `day_offset` are both None) sorts last within its
    direction — it's a real signal but a strictly weaker one than any
    offset-and-likelihood-bearing projection.
    """
    if direction not in (PriceDirection.RISING, PriceDirection.FALLING):
        raise ValueError(
            f"rank_by_imminence only accepts RISING or FALLING, got {direction!r} — "
            "STABLE has no move to rank by imminence and UNKNOWN has no trustworthy "
            "signal to sort on."
        )
    matching = [s for s in signals if s.direction is direction]
    return sorted(
        matching,
        key=lambda s: (
            s.likelihood is None,
            -(s.likelihood or 0.0),
            s.day_offset if s.day_offset is not None else _NO_OFFSET_SORT_KEY,
        ),
    )


# Sort key for a signal with no day_offset (the price_change_percent-only
# fallback path) — sorts after any real offset (0, 1, 2, ...) within the
# same likelihood tier.
_NO_OFFSET_SORT_KEY = 1 << 30
