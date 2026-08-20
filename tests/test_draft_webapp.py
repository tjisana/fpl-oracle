"""Tests for the self-contained draft web app generator.

The app is used live and unrepeatably, so the failure that matters is shipping
an html file with an unsubstituted placeholder or a broken payload — it would
be discovered on draft night with no time to fix it.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from fpl_oracle.draft.webapp import LEAGUE, Manager, render

PLACEHOLDERS = ("__PLAYERS__", "__MANAGERS__", "__BASELINES__")


@pytest.fixture
def payload() -> dict:
    elements = []
    pid = 1
    for element_type in (1, 2, 3, 4):
        for i in range(60):
            elements.append(
                {
                    "id": pid,
                    "web_name": f"P{pid}",
                    "first_name": "First",
                    "second_name": f"P{pid}",
                    "team": 1,
                    "element_type": element_type,
                    "draft_rank": pid,
                    "total_points": 300 - i * 4,
                    "minutes": 2000,
                    "status": "a",
                    "news": "",
                }
            )
            pid += 1
    return {"elements": elements, "teams": [{"id": 1, "short_name": "TST"}]}


@pytest.fixture
def html(monkeypatch, payload) -> str:
    monkeypatch.setattr("fpl_oracle.draft.webapp.client.fetch_bootstrap", lambda **_: payload)
    return render(teams=10)


def _embedded(html: str, name: str) -> Any:
    match = re.search(rf"const {name} = (.*?);\n", html, re.DOTALL)
    assert match, f"{name} not found in generated html"
    return json.loads(match.group(1))


def test_no_placeholder_survives(html: str) -> None:
    for token in PLACEHOLDERS:
        assert token not in html


def test_embedded_payloads_are_valid_json(html: str) -> None:
    players = _embedded(html, "PLAYERS")
    managers = _embedded(html, "MANAGERS")
    baselines = _embedded(html, "BASELINES")
    assert isinstance(players, list) and players
    assert len(managers) == 10
    assert set(baselines) == {"GK", "DEF", "MID", "FWD"}


def test_players_carry_every_field_the_app_reads(html: str) -> None:
    """The app is plain JS — a missing key surfaces as `undefined` on screen."""
    required = {"id", "name", "full", "pos", "team", "proj", "value", "tier", "status", "experts"}
    for player in _embedded(html, "PLAYERS"):
        assert required <= set(player)


def test_exactly_one_manager_is_flagged_as_me(html: str) -> None:
    managers = _embedded(html, "MANAGERS")
    assert [m["team"] for m in managers if m["me"]] == ["Third Kit"]


def test_league_matches_the_real_draft(html: str) -> None:
    assert len(LEAGUE) == 10
    assert len({m.team for m in LEAGUE}) == 10, "duplicate team name would break the snake order"


def test_board_is_trimmed_but_deep_enough_for_a_full_draft(html: str) -> None:
    """10 teams x 15 = 150 picks, so the pool must comfortably exceed that."""
    assert len(_embedded(html, "PLAYERS")) > 150


def test_custom_league_size_changes_the_baselines(monkeypatch, payload) -> None:
    monkeypatch.setattr("fpl_oracle.draft.webapp.client.fetch_bootstrap", lambda **_: payload)
    small = _embedded(render(teams=6), "BASELINES")
    large = _embedded(render(teams=12), "BASELINES")
    assert large["FWD"] < small["FWD"]


def test_custom_league_is_honoured(monkeypatch, payload) -> None:
    monkeypatch.setattr("fpl_oracle.draft.webapp.client.fetch_bootstrap", lambda **_: payload)
    league = [Manager(team="A", manager="a", me=True), Manager(team="B", manager="b")]
    managers = _embedded(render(teams=2, league=league), "MANAGERS")
    assert [m["team"] for m in managers] == ["A", "B"]
