"""Tests for fpl_oracle.fpl.prices: parsing bootstrap-static's
`price_change_*` / `cost_change_*` / `transfers_*_event` fields into a
per-player `PriceSignal`, and ranking players by imminence of a move.

Network-free, hand-built element fixtures (never touches `data/` or the
live API) covering: a rising player, a falling player, an all-zero
preseason payload, an older payload missing the fields entirely, a
locked price, a calibrating price, and the ranking function's ordering.
Live-API assertions are marked `@pytest.mark.network`.
"""

from __future__ import annotations

import pytest

from fpl_oracle.fpl import prices
from fpl_oracle.fpl.prices import (
    PriceDirection,
    PriceSignal,
    RawPriceFields,
    build_price_signals,
    rank_by_imminence,
)


def _element(player_id: int, **overrides: object) -> dict:
    """A minimal bootstrap-static element with sane price-field
    defaults, overridden per test. `id`/`now_cost` are the only fields a
    real payload always carries; everything else is optional so tests
    can freely omit fields to simulate an older payload."""
    base = {
        "id": player_id,
        "now_cost": 55,
        "price_change_projections": [],
        "price_change_hourly_rate": "0",
        "price_change_percent": "0",
        "price_change_locked_until": None,
        "price_change_calibrating": False,
        "cost_change_event": 0,
        "cost_change_event_fall": 0,
        "cost_change_start": 0,
        "cost_change_start_fall": 0,
        "transfers_in_event": 0,
        "transfers_out_event": 0,
        "selected_by_percent": "10.0",
    }
    base.update(overrides)
    return base


class TestRisingPlayer:
    def test_rising_player_picks_highest_likelihood_projection(self) -> None:
        element = _element(
            1,
            price_change_projections=[
                {"offset": 0, "projected_percent": "0", "likelihood": 5},
                {"offset": 1, "projected_percent": "1", "likelihood": 72},
                {"offset": 2, "projected_percent": "1", "likelihood": 20},
            ],
            transfers_in_event=500_000,
            transfers_out_event=10_000,
        )
        signal = build_price_signals({"elements": [element]})[1]

        assert signal.direction is PriceDirection.RISING
        assert signal.likelihood == 72.0
        assert signal.day_offset == 1
        assert signal.net_transfers_event == 490_000
        assert signal.locked is False
        assert signal.calibrating is False


class TestFallingPlayer:
    def test_falling_player(self) -> None:
        element = _element(
            2,
            price_change_projections=[
                {"offset": 0, "projected_percent": "-1", "likelihood": 88},
            ],
            transfers_in_event=1_000,
            transfers_out_event=250_000,
        )
        signal = build_price_signals({"elements": [element]})[2]

        assert signal.direction is PriceDirection.FALLING
        assert signal.likelihood == 88.0
        assert signal.day_offset == 0
        assert signal.net_transfers_event == -249_000


class TestPreseasonAllZero:
    def test_all_zero_projections_yield_stable_not_crash(self) -> None:
        element = _element(
            3,
            price_change_projections=[
                {"offset": 0, "projected_percent": "0", "likelihood": 0},
                {"offset": 1, "projected_percent": "0", "likelihood": 0},
                {"offset": 2, "projected_percent": "0", "likelihood": 0},
            ],
        )
        signal = build_price_signals({"elements": [element]})[3]

        assert signal.direction is PriceDirection.STABLE
        assert signal.locked is False
        assert signal.calibrating is False
        assert signal.net_transfers_event == 0


class TestMissingFieldsEntirely:
    def test_missing_price_fields_do_not_crash(self) -> None:
        # An older-shaped element: only the fields every payload has ever
        # carried, nothing price-change-related at all.
        element = {"id": 4, "now_cost": 45}
        signal = build_price_signals({"elements": [element]})[4]

        assert signal.direction is PriceDirection.STABLE
        assert signal.likelihood is None
        assert signal.day_offset is None
        assert signal.net_transfers_event == 0
        assert signal.locked is False
        assert signal.calibrating is False
        assert signal.now_cost == 45

    def test_missing_projections_falls_back_to_price_change_percent(self) -> None:
        # No projections array, but the single current-period percent is
        # present (an older payload shape, per the task's field list).
        element = {"id": 5, "now_cost": 60, "price_change_percent": "-1"}
        signal = build_price_signals({"elements": [element]})[5]

        assert signal.direction is PriceDirection.FALLING
        assert signal.likelihood is None
        assert signal.day_offset is None

    def test_null_projections_and_null_optional_fields(self) -> None:
        element = _element(
            6,
            price_change_projections=None,
            price_change_percent=None,
            price_change_hourly_rate=None,
            transfers_in_event=None,
            transfers_out_event=None,
            price_change_calibrating=None,
        )
        signal = build_price_signals({"elements": [element]})[6]

        assert signal.direction is PriceDirection.STABLE
        assert signal.net_transfers_event == 0
        assert signal.calibrating is False


class TestLockedPrice:
    def test_locked_price_is_unknown_even_with_a_strong_projection(self) -> None:
        element = _element(
            7,
            price_change_locked_until="2026-08-22T01:30:00Z",
            price_change_projections=[
                {"offset": 0, "projected_percent": "1", "likelihood": 95},
            ],
        )
        signal = build_price_signals({"elements": [element]})[7]

        assert signal.direction is PriceDirection.UNKNOWN
        assert signal.likelihood is None
        assert signal.day_offset is None
        assert signal.locked is True

    def test_zero_or_empty_locked_until_is_not_locked(self) -> None:
        for locked_value in (0, "0", "", None):
            element = _element(8, price_change_locked_until=locked_value)
            signal = build_price_signals({"elements": [element]})[8]
            assert signal.locked is False, f"locked_value={locked_value!r} should not lock"


class TestCalibratingPrice:
    def test_calibrating_price_is_unknown(self) -> None:
        element = _element(
            9,
            price_change_calibrating=True,
            price_change_projections=[
                {"offset": 0, "projected_percent": "-1", "likelihood": 99},
            ],
        )
        signal = build_price_signals({"elements": [element]})[9]

        assert signal.direction is PriceDirection.UNKNOWN
        assert signal.calibrating is True
        assert signal.likelihood is None


class TestRankByImminence:
    def _signal(
        self,
        player_id: int,
        direction: PriceDirection,
        likelihood: float | None,
        day_offset: int | None,
    ) -> PriceSignal:
        return PriceSignal(
            player_id=player_id,
            now_cost=50,
            direction=direction,
            likelihood=likelihood,
            day_offset=day_offset,
            net_transfers_event=0,
            locked=False,
            calibrating=False,
        )

    def test_orders_by_likelihood_desc_then_offset_asc(self) -> None:
        signals = [
            self._signal(1, PriceDirection.FALLING, 40.0, 0),
            self._signal(2, PriceDirection.RISING, 90.0, 2),
            self._signal(3, PriceDirection.RISING, 90.0, 0),
            self._signal(4, PriceDirection.RISING, 60.0, 0),
            self._signal(5, PriceDirection.STABLE, 100.0, 0),
        ]

        ranked = rank_by_imminence(signals, PriceDirection.RISING)

        assert [s.player_id for s in ranked] == [3, 2, 4]

    def test_no_likelihood_signal_sorts_last(self) -> None:
        signals = [
            self._signal(1, PriceDirection.FALLING, None, None),
            self._signal(2, PriceDirection.FALLING, 1.0, 2),
        ]

        ranked = rank_by_imminence(signals, PriceDirection.FALLING)

        assert [s.player_id for s in ranked] == [2, 1]

    def test_rejects_stable_and_unknown(self) -> None:
        with pytest.raises(ValueError, match="RISING or FALLING"):
            rank_by_imminence([], PriceDirection.STABLE)
        with pytest.raises(ValueError, match="RISING or FALLING"):
            rank_by_imminence([], PriceDirection.UNKNOWN)


class TestRawPriceFieldsParsing:
    def test_from_element_coerces_string_numbers(self) -> None:
        element = _element(
            10,
            cost_change_event="1",
            cost_change_event_fall="0",
            selected_by_percent="42.5",
        )
        raw = RawPriceFields.from_element(element)

        assert raw.cost_change_event == 1
        assert raw.selected_by_percent == 42.5

    def test_from_element_tolerates_garbage_projection_entries(self) -> None:
        element = _element(
            11,
            price_change_projections=[
                {"offset": 0, "projected_percent": "1", "likelihood": 50},
                "not-a-dict",
                {"offset": "not-an-int", "projected_percent": "1", "likelihood": 50},
            ],
        )
        raw = RawPriceFields.from_element(element)

        assert len(raw.price_change_projections) == 1
        assert raw.price_change_projections[0].offset == 0


@pytest.mark.network
class TestLivePriceSignals:
    def test_live_bootstrap_builds_signals_without_crashing(self) -> None:
        signals = prices.load_price_signals()
        assert len(signals) > 400
        # Season hasn't started as of writing — every signal should be a
        # cleanly-parsed verdict, never an exception getting here.
        for signal in signals.values():
            assert signal.direction in set(PriceDirection)
