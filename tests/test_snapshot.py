"""Tests for fpl_oracle.fpl.snapshot: the daily price snapshot and the
transfer-log purchase-price derivation. Network-free throughout —
`fpl.client.httpx.get` is monkeypatched, following tests/test_client.py's
style; `fpl.client.get_transfers` fixtures follow tests/test_entry.py's
`_transfer_record`-style shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fpl_oracle.fpl import client, snapshot


class _FakeResponse:
    def __init__(self, json_data: dict | list) -> None:
        self._json_data = json_data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict | list:
        return self._json_data


_CANNED_BOOTSTRAP = {
    "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
    "elements": [
        {"id": 1, "web_name": "Saka", "now_cost": 100},
        {"id": 2, "web_name": "Raya", "now_cost": 55},
    ],
}


class TestWriteSnapshot:
    def test_writes_ids_and_now_cost_with_utc_timestamp(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(_CANNED_BOOTSTRAP))

        snapshot_dir = tmp_path / "snapshots"
        now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

        path = snapshot.write_snapshot(directory=snapshot_dir, now=now)

        assert path == snapshot_dir / "2026-08-27.json"
        data = json.loads(path.read_text())
        assert data["snapshot_date"] == "2026-08-27"
        prices = {p["player_id"]: p["now_cost"] for p in data["prices"]}
        assert prices == {1: 100, 2: 55}

    def test_idempotent_for_the_same_utc_date(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path / "cache")
        calls: list[str] = []

        def fake_get(url: str, **kw) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse(_CANNED_BOOTSTRAP)

        monkeypatch.setattr(client.httpx, "get", fake_get)

        snapshot_dir = tmp_path / "snapshots"
        now = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)

        first = snapshot.write_snapshot(directory=snapshot_dir, now=now)
        first_contents = first.read_text()

        # A second run later the SAME UTC date must not overwrite the
        # file, move taken_at forward, or refetch bootstrap-static.
        later_same_day = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)
        second = snapshot.write_snapshot(directory=snapshot_dir, now=later_same_day)

        assert second == first
        assert second.read_text() == first_contents
        # bootstrap-static was only ever fetched once, for the first call
        # (fpl.client's own 6h-fresh cache would also prevent a second
        # live hit, but write_snapshot's idempotency check short-circuits
        # before it even asks).
        assert len(calls) == 1

    def test_different_utc_dates_write_different_files(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(_CANNED_BOOTSTRAP))

        snapshot_dir = tmp_path / "snapshots"
        day1 = snapshot.write_snapshot(
            directory=snapshot_dir, now=datetime(2026, 8, 27, tzinfo=UTC)
        )
        day2 = snapshot.write_snapshot(
            directory=snapshot_dir, now=datetime(2026, 8, 28, tzinfo=UTC)
        )

        assert day1 != day2
        assert day1.exists()
        assert day2.exists()


def _transfer(element_in: int, element_in_cost: int, event: int) -> dict:
    return {
        "element_in": element_in,
        "element_in_cost": element_in_cost,
        "element_out": 999,
        "element_out_cost": 50,
        "entry": 42,
        "event": event,
        "time": "2026-08-25T12:00:00Z",
    }


def _url_aware(transfers: list):
    """httpx.get stand-in that answers BOTH endpoints derive_purchase_prices
    now touches: the transfer log, and entry history (needed to identify
    Free Hit gameweeks, whose transfers are reverted and must be ignored)."""

    def fake_get(url: str, **kwargs) -> _FakeResponse:
        if "history" in url:
            return _FakeResponse({"past": [], "current": [], "chips": []})
        return _FakeResponse(transfers)

    return fake_get


class TestDerivePurchasePrices:
    def test_transferred_in_player_price_recovered_from_transfer_log(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            client.httpx,
            "get",
            lambda url, **kw: _FakeResponse(
                {"past": [], "current": [], "chips": []}
                if "history" in url
                else [_transfer(element_in=5, element_in_cost=75, event=3)]
            ),
        )

        result = snapshot.derive_purchase_prices(42)

        assert result.known == {5: 75}

    def test_never_transferred_player_is_absent_not_guessed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # An empty transfer log — every currently-owned player was part
        # of the initial squad. `known` must be empty, never fabricated
        # from now_cost or anything else.
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(client.httpx, "get", _url_aware([]))

        result = snapshot.derive_purchase_prices(42)

        assert result.known == {}
        assert "never transferred in" in result.unknown_note.lower()

    def test_repurchased_player_uses_the_latest_transfer_in(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Player 5 was bought at 70, sold, then bought again at 80 —
        # oldest-first per the API. Only the LATEST purchase describes
        # what's currently held.
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        transfers = [
            _transfer(element_in=5, element_in_cost=70, event=2),
            _transfer(element_in=5, element_in_cost=80, event=6),
        ]
        monkeypatch.setattr(client.httpx, "get", _url_aware(transfers))

        result = snapshot.derive_purchase_prices(42)

        assert result.known == {5: 80}


def _rec(*, element_in: int, element_in_cost: int, event: int) -> client.TransferRecord:
    """A real TransferRecord — the module-seam tests below patch
    `snapshot.get_transfers`, so these must be model instances, not dicts."""
    return client.TransferRecord(
        element_in=element_in,
        element_in_cost=element_in_cost,
        element_out=0,
        element_out_cost=0,
        entry=1,
        event=event,
        time="",
    )


class TestFreeHitTransfersAreExcluded:
    """A Free Hit squad is reverted after its gameweek, so its transfers
    appear in the log but describe nothing the manager owns. Counting them
    asserts a purchase price for a player handed straight back — and,
    because latest-transfer-in wins, OVERWRITES a real earlier purchase."""

    def _wire(self, monkeypatch, transfers, chips):
        monkeypatch.setattr(snapshot, "get_transfers", lambda entry_id: transfers)
        monkeypatch.setattr(
            snapshot,
            "get_entry_history",
            lambda entry_id: SimpleNamespace(chips=chips),
        )

    def test_free_hit_transfer_is_ignored(self, monkeypatch) -> None:
        self._wire(
            monkeypatch,
            [_rec(element_in=1, element_in_cost=100, event=10)],
            [SimpleNamespace(name="freehit", event=10)],
        )
        assert snapshot.derive_purchase_prices(1).known == {}

    def test_free_hit_does_not_overwrite_a_real_purchase(self, monkeypatch) -> None:
        """The dangerous case: a genuine GW3 buy, then the same player
        brought in again on a GW10 Free Hit. The real price must survive."""
        self._wire(
            monkeypatch,
            [
                _rec(element_in=7, element_in_cost=95, event=3),
                _rec(element_in=7, element_in_cost=150, event=10),
            ],
            [SimpleNamespace(name="freehit", event=10)],
        )
        assert snapshot.derive_purchase_prices(1).known == {7: 95}

    def test_wildcard_transfers_are_kept(self, monkeypatch) -> None:
        """Wildcard transfers are permanent — only Free Hit reverts."""
        self._wire(
            monkeypatch,
            [_rec(element_in=7, element_in_cost=95, event=3)],
            [SimpleNamespace(name="wildcard", event=3)],
        )
        assert snapshot.derive_purchase_prices(1).known == {7: 95}

    def test_missing_history_is_flagged_not_guessed(self, monkeypatch) -> None:
        monkeypatch.setattr(
            snapshot,
            "get_transfers",
            lambda entry_id: [_rec(element_in=1, element_in_cost=100, event=2)],
        )
        monkeypatch.setattr(snapshot, "get_entry_history", lambda entry_id: None)

        result = snapshot.derive_purchase_prices(1)

        assert result.known == {1: 100}
        assert "Free Hit" in result.unknown_note
