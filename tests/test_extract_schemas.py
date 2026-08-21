"""Schema-contract tests for extract/schemas.py.

The extraction LLM emits plain JSON against these models, so the tests
exercise dict-shaped input the way structured output will deliver it.
"""

import pytest
from pydantic import ValidationError

from fpl_oracle.extract.schemas import (
    Chip,
    ChipPlan,
    Pick,
    PickAction,
    Provenance,
    Urgency,
    VideoExtraction,
)


def _pick_payload(**overrides) -> dict:
    payload = {
        "player_name_raw": "Sacca",
        "team_inferred": "Arsenal",
        "position_inferred": "MID",
        "action": "squad_include",
        "conviction": 5,
        "time_horizon": 1,
        "reasoning": "Nailed on penalties and set pieces.",
        "provenance": "personal",
    }
    payload.update(overrides)
    return payload


def test_pick_round_trip_from_llm_shaped_dict():
    pick = Pick.model_validate(_pick_payload())
    assert pick.player_name_raw == "Sacca"  # verbatim, not corrected
    assert pick.action is PickAction.SQUAD_INCLUDE
    assert pick.provenance is Provenance.PERSONAL
    assert pick.player_id is None  # unresolved until the fpl/ resolver runs
    assert Pick.model_validate_json(pick.model_dump_json()) == pick


def test_composite_key_fields_allow_none_but_must_be_present():
    pick = Pick.model_validate(_pick_payload(team_inferred=None, position_inferred=None))
    assert pick.team_inferred is None
    with pytest.raises(ValidationError):
        Pick.model_validate({k: v for k, v in _pick_payload().items() if k != "team_inferred"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"conviction": 0},
        {"conviction": 6},
        {"time_horizon": 0},
        {"time_horizon": 39},
        {"action": "buy"},
        {"position_inferred": "STRIKER"},
        {"player_name_raw": ""},
        {"reasoning": "x" * 301},
    ],
)
def test_invalid_pick_payloads_rejected(overrides):
    with pytest.raises(ValidationError):
        Pick.model_validate(_pick_payload(**overrides))


def test_provenance_is_required_not_defaulted():
    payload = _pick_payload()
    del payload["provenance"]
    with pytest.raises(ValidationError):
        Pick.model_validate(payload)


def test_video_extraction_round_trip():
    extraction = VideoExtraction.model_validate(
        {
            "creator_id": "lets-talk-fpl",
            "video_id": "abc123DEF45",
            "video_title": "MY GW1 TEAM REVEAL",
            "published_at": "2026-08-10T09:00:00Z",
            "gameweek": 1,
            "picks": [_pick_payload(), _pick_payload(action="captain", provenance="group")],
        }
    )
    assert extraction.gameweek == 1
    assert [p.action for p in extraction.picks] == [PickAction.SQUAD_INCLUDE, PickAction.CAPTAIN]
    assert VideoExtraction.model_validate_json(extraction.model_dump_json()) == extraction

    assert extraction.model_copy(update={"gameweek": None}).gameweek is None
    with pytest.raises(ValidationError):
        VideoExtraction.model_validate(extraction.model_dump(mode="json") | {"gameweek": 99})


class TestInSeasonExtensions:
    """Chip plans and urgency, added for in-season content (2026-08-20).
    GW1 videos are squad reveals; in-season videos are transfers, chip
    timing, and price-driven urgency."""

    def test_chip_plan_is_separate_from_picks_and_needs_no_player(self) -> None:
        # A chip is not an opinion about a footballer. Forcing it into Pick
        # would push a player-less row through a resolver whose whole job is
        # to refuse rows without a real player.
        plan = ChipPlan(
            chip=Chip.WILDCARD,
            target_gameweek=8,
            conviction=5,
            reasoning="Fixtures turn after the international break.",
            provenance=Provenance.PERSONAL,
        )
        assert plan.chip is Chip.WILDCARD
        assert "player" not in ChipPlan.model_fields

    def test_chip_plan_gameweek_is_optional(self) -> None:
        # "Wildcard at some point soon" names no gameweek; the model must be
        # able to say so rather than inventing a number.
        plan = ChipPlan(
            chip=Chip.FREE_HIT,
            conviction=2,
            reasoning="Thinking about it for a blank.",
            provenance=Provenance.PERSONAL,
        )
        assert plan.target_gameweek is None

    def test_chip_plan_rejects_out_of_range_gameweek(self) -> None:
        with pytest.raises(ValidationError):
            ChipPlan(
                chip=Chip.BENCH_BOOST,
                target_gameweek=39,
                conviction=3,
                reasoning="x",
                provenance=Provenance.PERSONAL,
            )

    def test_extraction_without_chip_plans_still_parses(self) -> None:
        """Every extraction file written before chip_plans existed must keep
        parsing — data/extractions/ is append-only and never migrated."""
        raw = (
            '{"creator_id":"a","video_id":"b","video_title":"t",'
            '"published_at":"2026-08-01T00:00:00Z","gameweek":1,"picks":[]}'
        )
        extraction = VideoExtraction.model_validate_json(raw)
        assert extraction.chip_plans == []

    def test_urgency_defaults_to_none(self) -> None:
        # Most picks carry no timing signal; forcing one would be noise.
        pick = Pick(
            player_name_raw="Saka",
            team_inferred="Arsenal",
            position_inferred="MID",
            player_id=None,
            action=PickAction.TRANSFER_IN,
            conviction=4,
            time_horizon=6,
            reasoning="Good run of fixtures.",
            provenance=Provenance.PERSONAL,
        )
        assert pick.urgency is None

    def test_urgency_is_independent_of_time_horizon(self) -> None:
        # A long-horizon hold can still be urgent to BUY tonight — the two
        # fields answer different questions.
        pick = Pick(
            player_name_raw="Saka",
            team_inferred="Arsenal",
            position_inferred="MID",
            player_id=None,
            action=PickAction.TRANSFER_IN,
            conviction=5,
            time_horizon=10,
            reasoning="Get him before the rise tonight.",
            provenance=Provenance.PERSONAL,
            urgency=Urgency.BEFORE_PRICE_CHANGE,
        )
        assert pick.urgency is Urgency.BEFORE_PRICE_CHANGE
        assert pick.time_horizon == 10
