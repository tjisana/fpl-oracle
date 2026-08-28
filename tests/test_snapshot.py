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


class TestDerivePurchasePrices:
    def test_transferred_in_player_price_recovered_from_transfer_log(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            client.httpx,
            "get",
            lambda url, **kw: _FakeResponse([_transfer(element_in=5, element_in_cost=75, event=3)]),
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
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse([]))

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
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(transfers))

        result = snapshot.derive_purchase_prices(42)

        assert result.known == {5: 80}
