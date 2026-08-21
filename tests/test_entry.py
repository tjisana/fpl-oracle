"""Tests for fpl_oracle.fpl.entry: my-squad assembly and free-transfer
derivation, plus the fpl.client fetch functions entry.py depends on
(get_picks, get_transfers). Network-free throughout — hand-built payload
fixtures shaped like the real API responses, following
tests/test_players.py's fixture style; httpx.get is monkeypatched rather
than hit live.
"""

from __future__ import annotations

import httpx
import pytest

from fpl_oracle.fpl import client, entry
from fpl_oracle.fpl.players import PlayerDB, Position

# ---------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------


def _bootstrap_payload() -> dict:
    return {
        "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
        "elements": [
            {
                "id": 1,
                "web_name": "Saka",
                "first_name": "Bukayo",
                "second_name": "Saka",
                "team": 1,
                "element_type": 3,  # MID
                "now_cost": 100,
                "status": "a",
                "chance_of_playing_next_round": 100,
            },
            {
                "id": 2,
                "web_name": "Raya",
                "first_name": "David",
                "second_name": "Raya",
                "team": 1,
                "element_type": 1,  # GK
                "now_cost": 55,
                "status": "a",
                "chance_of_playing_next_round": 100,
            },
        ],
    }


@pytest.fixture
def player_db() -> PlayerDB:
    return PlayerDB.from_bootstrap(_bootstrap_payload())


def _picks_payload(**overrides: object) -> dict:
    base: dict = {
        "active_chip": None,
        "entry_history": {
            "event": 3,
            "points": 60,
            "total_points": 180,
            "rank": 100,
            "overall_rank": 5000,
            "bank": 5,
            "value": 1005,
            "event_transfers": 1,
            "event_transfers_cost": 0,
            "points_on_bench": 8,
        },
        "picks": [
            {
                "element": 1,
                "position": 1,
                "multiplier": 2,
                "is_captain": True,
                "is_vice_captain": False,
            },
            {
                "element": 2,
                "position": 12,
                "multiplier": 0,
                "is_captain": False,
                "is_vice_captain": False,
            },
        ],
    }
    base.update(overrides)
    return base


class _FakeResponse:
    """Minimal httpx.Response stand-in that can actually raise a real
    httpx.HTTPStatusError on a bad status, unlike test_client.py's
    always-succeeds helper — get_picks's 4xx-vs-5xx branching needs a
    real exception with a real `.response.status_code` to catch."""

    def __init__(self, json_data: dict | list, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.com")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self) -> dict | list:
        return self._json_data


def _gw_history(
    event: int, event_transfers: int, event_transfers_cost: int = 0
) -> client.GameweekHistory:
    return client.GameweekHistory(
        event=event,
        points=50,
        total_points=50 * event,
        rank=1000,
        overall_rank=1000,
        bank=10,
        value=1000,
        event_transfers=event_transfers,
        event_transfers_cost=event_transfers_cost,
        points_on_bench=0,
    )


def _transfer_record(event: int) -> client.TransferRecord:
    return client.TransferRecord(
        element_in=1,
        element_in_cost=50,
        element_out=2,
        element_out_cost=45,
        entry=1,
        event=event,
        time="2026-08-25T12:00:00Z",
    )


# ---------------------------------------------------------------------
# Parsing a picks payload into MySquad
# ---------------------------------------------------------------------


class TestBuildMySquad:
    def test_parses_picks_payload(self, player_db: PlayerDB) -> None:
        picks = client.PicksResponse.model_validate(_picks_payload())

        squad = entry.build_my_squad(
            entry_id=42,
            gameweek=3,
            team_name="Test FC",
            picks=picks,
            player_db=player_db,
            free_transfers=1,
            free_transfers_note="test",
        )

        assert squad.entry_id == 42
        assert squad.gameweek == 3
        assert squad.team_name == "Test FC"
        assert len(squad.players) == 2
        assert squad.bank == 5
        assert squad.bank_m == 0.5
        assert squad.team_value == 1005
        assert squad.team_value_m == 100.5
        assert squad.free_transfers == 1

    def test_captain_and_starting_flags_derived_from_multiplier(self, player_db: PlayerDB) -> None:
        picks = client.PicksResponse.model_validate(_picks_payload())

        squad = entry.build_my_squad(
            entry_id=1,
            gameweek=3,
            team_name="X",
            picks=picks,
            player_db=player_db,
            free_transfers=None,
            free_transfers_note="n/a",
        )

        saka = next(p for p in squad.players if p.player_id == 1)
        raya = next(p for p in squad.players if p.player_id == 2)

        assert saka.is_captain is True
        assert saka.is_starting is True  # multiplier=2
        assert saka.multiplier == 2
        assert saka.position == Position.MID

        assert raya.is_starting is False  # benched, multiplier=0
        assert raya.is_captain is False

    def test_unknown_player_id_skipped_not_crashed(self, player_db: PlayerDB) -> None:
        picks = client.PicksResponse.model_validate(
            _picks_payload(
                picks=[
                    {
                        "element": 999,  # not in the fixture bootstrap
                        "position": 1,
                        "multiplier": 1,
                        "is_captain": False,
                        "is_vice_captain": False,
                    }
                ]
            )
        )

        squad = entry.build_my_squad(
            entry_id=1,
            gameweek=3,
            team_name="X",
            picks=picks,
            player_db=player_db,
            free_transfers=None,
            free_transfers_note="n/a",
        )

        assert squad.players == []


# ---------------------------------------------------------------------
# Selling price vs current price — never fabricated from now_cost
# ---------------------------------------------------------------------


class TestSellingPriceNeverRecomputedFromNowCost:
    """The task's core correctness requirement: a selling price is NOT a
    current price, and this module must never paper over the public
    API's lack of per-pick pricing by substituting `now_cost` for it.
    """

    def test_owned_player_keeps_purchase_and_selling_distinct(self) -> None:
        # If a future authenticated fetch populates these (see entry.py's
        # module docstring — the public picks endpoint doesn't), the
        # model must represent them independently, never derive one from
        # the other.
        owned = entry.OwnedPlayer(
            player_id=1,
            web_name="Saka",
            position=Position.MID,
            is_starting=True,
            is_captain=False,
            is_vice_captain=False,
            multiplier=1,
            purchase_price=100,  # bought at £10.0m
            selling_price=105,  # now sells at £10.5m (risen since purchase)
        )

        assert owned.purchase_price_m == 10.0
        assert owned.selling_price_m == 10.5
        assert owned.selling_price != owned.purchase_price

    def test_build_my_squad_never_fabricates_price_from_now_cost(self, player_db: PlayerDB) -> None:
        # Saka's now_cost in the fixture is 100 (£10.0m). The public
        # picks payload carries no pricing fields at all, so
        # build_my_squad must leave both None rather than quietly filling
        # them in from now_cost.
        picks = client.PicksResponse.model_validate(_picks_payload())

        squad = entry.build_my_squad(
            entry_id=1,
            gameweek=3,
            team_name="X",
            picks=picks,
            player_db=player_db,
            free_transfers=None,
            free_transfers_note="n/a",
        )

        saka = next(p for p in squad.players if p.player_id == 1)
        assert saka.purchase_price is None
        assert saka.selling_price is None


# ---------------------------------------------------------------------
# client.get_picks — pre-deadline "not available yet" path
# ---------------------------------------------------------------------


class TestGetPicksPreDeadline:
    def test_404_returns_none_without_crashing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            client.httpx, "get", lambda url, **kw: _FakeResponse({"detail": "Not found."}, 404)
        )

        assert client.get_picks(42, 1) is None

    def test_404_is_never_cached_to_disk(self, tmp_path, monkeypatch) -> None:
        # A pre-deadline 404 is temporal, not permanent — unlike get_entry
        # (a bad id is a bad id forever), the SAME gameweek becomes
        # available the moment its deadline passes. Caching the negative
        # result the way _get_json does would permanently poison every
        # later check, including the deadline-morning rerun this data
        # exists to serve.
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        calls: list[str] = []

        def fake_get(url: str, **kw: object) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse({"detail": "Not found."}, 404)

        monkeypatch.setattr(client.httpx, "get", fake_get)

        assert client.get_picks(42, 1) is None
        assert client.get_picks(42, 1) is None

        assert len(calls) == 2  # every call re-hit the network
        assert not client._picks_cache_path(42, 1).exists()

    def test_successful_fetch_is_cached_on_disk(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        calls: list[str] = []

        def fake_get(url: str, **kw: object) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse(_picks_payload())

        monkeypatch.setattr(client.httpx, "get", fake_get)

        first = client.get_picks(42, 3)
        second = client.get_picks(42, 3)

        assert len(calls) == 1  # second call served from the disk cache
        assert first == second
        assert first is not None
        assert first.entry_history.event == 3

    def test_server_error_propagates_not_swallowed(self, tmp_path, monkeypatch) -> None:
        # A 5xx is a real failure, not "not available yet" — must not be
        # silently treated the same as a pre-deadline 404.
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse({}, 500))

        with pytest.raises(httpx.HTTPStatusError):
            client.get_picks(42, 1)


class TestFetchMySquadPreDeadline:
    def test_returns_none_when_picks_not_public_yet(self, player_db: PlayerDB, monkeypatch) -> None:
        monkeypatch.setattr(client, "get_picks", lambda entry_id, gw: None)

        result = entry.fetch_my_squad(entry_id=42, gameweek=1, player_db=player_db)

        assert result is None


# ---------------------------------------------------------------------
# client.get_transfers
# ---------------------------------------------------------------------


class TestGetTransfers:
    def test_parses_raw_list_into_transfer_records(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(client, "CACHE_DIR", tmp_path)
        raw = [
            {
                "element_in": 1,
                "element_in_cost": 50,
                "element_out": 2,
                "element_out_cost": 45,
                "entry": 42,
                "event": 2,
                "time": "2026-08-25T12:00:00Z",
            }
        ]
        monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _FakeResponse(raw))

        transfers = client.get_transfers(42)

        assert len(transfers) == 1
        assert transfers[0].event == 2
        assert transfers[0].element_in == 1


# ---------------------------------------------------------------------
# FPL_ENTRY_ID configuration
# ---------------------------------------------------------------------


class TestGetEntryId:
    def test_missing_env_var_raises_clear_error(self, monkeypatch) -> None:
        monkeypatch.delenv(entry.ENTRY_ID_ENV_VAR, raising=False)
        # Never let a real .env on disk leak a real id into this test.
        monkeypatch.setattr(entry, "load_dotenv", lambda: None)

        with pytest.raises(entry.EntryIdNotConfiguredError, match="FPL_ENTRY_ID"):
            entry.get_entry_id()

    def test_present_parses_to_int(self, monkeypatch) -> None:
        monkeypatch.setattr(entry, "load_dotenv", lambda: None)
        monkeypatch.setenv(entry.ENTRY_ID_ENV_VAR, "123456")

        assert entry.get_entry_id() == 123456

    def test_non_integer_raises_value_error(self, monkeypatch) -> None:
        monkeypatch.setattr(entry, "load_dotenv", lambda: None)
        monkeypatch.setenv(entry.ENTRY_ID_ENV_VAR, "not-a-number")

        with pytest.raises(ValueError, match="not a valid integer"):
            entry.get_entry_id()


# ---------------------------------------------------------------------
# Free-transfer derivation
# ---------------------------------------------------------------------


class TestDeriveFreeTransfers:
    def test_target_at_or_before_started_event_returns_none(self) -> None:
        result = entry.derive_free_transfers(
            target_gameweek=1, started_event=1, history=[], chips=[], transfers=[]
        )

        assert result.free_transfers is None
        assert "gameweek 1" in result.note

    def test_first_gameweek_after_start_grants_one(self) -> None:
        result = entry.derive_free_transfers(
            target_gameweek=2,
            started_event=1,
            history=[_gw_history(1, event_transfers=0)],
            chips=[],
            transfers=[],
        )

        assert result.free_transfers == 1

    def test_used_transfer_draws_down_then_reaccrues(self) -> None:
        # GW2: 1 transfer made (transfers log and history agree), no
        # chip -> spends the 1 FT granted for GW2, leaving 0, then +1
        # accrual for GW3 -> 1 available heading into GW3.
        result = entry.derive_free_transfers(
            target_gameweek=3,
            started_event=1,
            history=[_gw_history(1, 0), _gw_history(2, event_transfers=1)],
            chips=[],
            transfers=[_transfer_record(2)],
        )

        assert result.free_transfers == 1

    def test_unused_transfers_bank_up_to_cap(self) -> None:
        history = [_gw_history(1, 0)] + [_gw_history(gw, 0) for gw in range(2, 10)]

        result = entry.derive_free_transfers(
            target_gameweek=10, started_event=1, history=history, chips=[], transfers=[]
        )

        assert result.free_transfers == entry.FREE_TRANSFER_CAP == 5

    def test_wildcard_week_does_not_draw_down_the_bank(self) -> None:
        # GW2: wildcard played, 8 transfers made at 0 cost — must NOT
        # reduce the free-transfer bank the way an unchipped week would.
        result = entry.derive_free_transfers(
            target_gameweek=3,
            started_event=1,
            history=[_gw_history(1, 0), _gw_history(2, event_transfers=8, event_transfers_cost=0)],
            chips=[client.ChipPlay(name="wildcard", event=2)],
            transfers=[_transfer_record(2) for _ in range(8)],
        )

        # ft stays at 1 through GW2 (untouched by the wildcard), then +1
        # accrual for GW3 -> 2.
        assert result.free_transfers == 2

    def test_missing_gameweek_history_returns_none(self) -> None:
        result = entry.derive_free_transfers(
            target_gameweek=4,
            started_event=1,
            history=[_gw_history(1, 0), _gw_history(2, 0)],  # GW3 missing
            chips=[],
            transfers=[],
        )

        assert result.free_transfers is None
        assert "missing" in result.note.lower()

    def test_transfers_log_and_history_disagreement_returns_none(self) -> None:
        # History says 2 transfers were made in GW2; the transfers log
        # only has 1 record for that event. Never guess which is right.
        result = entry.derive_free_transfers(
            target_gameweek=3,
            started_event=1,
            history=[_gw_history(1, 0), _gw_history(2, event_transfers=2)],
            chips=[],
            transfers=[_transfer_record(2)],
        )

        assert result.free_transfers is None
        assert "disagree" in result.note.lower()


class TestFetchFreeTransfers:
    def test_entry_not_found_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(client, "get_entry", lambda entry_id: None)

        result = entry.fetch_free_transfers(entry_id=999, target_gameweek=2)

        assert result.free_transfers is None
        assert "not found" in result.note.lower()
