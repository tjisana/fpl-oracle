"""Schema-contract tests for extract/schemas.py.

The extraction LLM emits plain JSON against these models, so the tests
exercise dict-shaped input the way structured output will deliver it.
"""

import pytest
from pydantic import ValidationError

from fpl_oracle.extract.schemas import Pick, PickAction, Provenance, VideoExtraction


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
