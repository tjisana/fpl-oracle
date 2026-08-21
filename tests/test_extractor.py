"""Tests for extract/extractor.py — no network; the Anthropic client is faked.

The fakes model the REAL SDK contract: `messages.parse()` validates
eagerly and raises pydantic.ValidationError from the call itself when the
response text isn't valid JSON for the wire schema; `parsed_output` on a
returned response is a plain attribute that never raises.
"""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from fpl_oracle.extract import extractor
from fpl_oracle.extract.extractor import (
    ExtractionError,
    _WireExtraction,
    _WirePick,
    build_user_message,
    extract_picks,
)
from fpl_oracle.extract.schemas import Pick

WIRE_PICK = {
    "player_name_raw": "Sacca",
    "team_inferred": "Arsenal",
    "position_inferred": "MID",
    "action": "captain",
    "conviction": 5,
    "time_horizon": 1,
    "reasoning": "Nailed on set pieces.",
    "provenance": "personal",
    "urgency": None,
}


def _response(parsed, stop_reason="end_turn", text='{"gameweek": 1, "picks": []}'):
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed,
        content=[SimpleNamespace(type="text", text=text)],
    )


def _raise_validation_error():
    """Produce a real pydantic.ValidationError, as the SDK's eager parse does
    on non-JSON (truncated) output."""
    _WireExtraction.model_validate_json('{"gameweek": 1, "picks": [{"pla')


def _fake_client(monkeypatch, responses):
    """responses: list of response objects, or callables that raise."""
    calls = []
    it = iter(responses)

    def parse(**kwargs):
        calls.append(kwargs)
        item = next(it)
        if callable(item):
            item()
        return item

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    monkeypatch.setattr(extractor, "_get_client", lambda: client)
    return calls


def _extract(**overrides):
    kwargs: dict[str, Any] = {
        "creator_id": "lets-talk-fpl",
        "creator_name": "Andy",
        "video_id": "abc123",
        "video_title": "MY GW1 TEAM",
        "published_at": datetime(2026, 8, 10, tzinfo=UTC),
        "transcript_text": "i'm captaining sacca this week",
    }
    kwargs.update(overrides)
    return extract_picks(**kwargs)


def test_build_user_message_includes_channel_context_only_when_given():
    with_ctx = build_user_message("T", "Andy", "words", "shared channel show")
    without = build_user_message("T", "Andy", "words", None)
    assert "Channel context: shared channel show" in with_ctx
    assert "Channel context" not in without
    assert without.endswith("Transcript:\nwords")


def test_wire_schema_excludes_player_id():
    assert "player_id" not in _WirePick.model_fields
    # and the wire pick otherwise mirrors Pick's LLM-facing fields
    assert set(_WirePick.model_fields) == set(Pick.model_fields) - {"player_id"}


def test_pipeline_stamps_metadata_and_forces_player_id_none(monkeypatch):
    wire = _WireExtraction(gameweek=1, picks=[_WirePick.model_validate(WIRE_PICK)], chip_plans=[])
    calls = _fake_client(monkeypatch, [_response(wire)])

    result = _extract()

    assert result.creator_id == "lets-talk-fpl"
    assert result.video_id == "abc123"
    assert result.video_title == "MY GW1 TEAM"
    assert result.gameweek == 1
    assert result.picks[0].player_name_raw == "Sacca"
    assert result.picks[0].player_id is None
    assert calls[0]["output_format"] is _WireExtraction


def test_request_shape_is_pinned(monkeypatch):
    wire = _WireExtraction(
        gameweek=None, picks=[_WirePick.model_validate(WIRE_PICK)], chip_plans=[]
    )
    calls = _fake_client(monkeypatch, [_response(wire)])
    _extract()

    call = calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 16_000
    (system_block,) = call["system"]
    assert system_block["cache_control"] == {"type": "ephemeral"}
    assert "player_name_raw" in system_block["text"] or len(system_block["text"]) > 500


def test_parse_call_raising_reports_as_truncation(monkeypatch):
    _fake_client(monkeypatch, [_raise_validation_error])
    with pytest.raises(ExtractionError, match="likely truncated"):
        _extract()


def test_refusal_and_truncation_stop_reasons_raise(monkeypatch):
    _fake_client(monkeypatch, [_response(None, stop_reason="refusal")])
    with pytest.raises(ExtractionError, match="refused"):
        _extract()

    _fake_client(monkeypatch, [_response(None, stop_reason="max_tokens")])
    with pytest.raises(ExtractionError, match="truncated"):
        _extract()


def test_no_parsed_output_raises(monkeypatch):
    _fake_client(monkeypatch, [_response(None)])
    with pytest.raises(ExtractionError, match="no parseable"):
        _extract()


def _invalid_wire():
    """Wire-valid (unconstrained) but strict-invalid: conviction out of the
    1-5 band. The SDK accepts this; our strict pass must reject and retry."""
    return _WireExtraction(
        gameweek=1, picks=[_WirePick.model_validate({**WIRE_PICK, "conviction": 9})], chip_plans=[]
    )


def test_strict_validation_failure_retries_with_feedback_then_succeeds(monkeypatch):
    good = _WireExtraction(gameweek=1, picks=[_WirePick.model_validate(WIRE_PICK)], chip_plans=[])
    calls = _fake_client(
        monkeypatch,
        [_response(_invalid_wire(), text='{"bad": "output"}'), _response(good)],
    )

    result = _extract()

    assert result.gameweek == 1
    assert len(calls) == 2
    retry_messages = calls[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert retry_messages[-2]["content"] == '{"bad": "output"}'
    assert retry_messages[-1]["role"] == "user"
    assert "failed schema validation" in retry_messages[-1]["content"]


def test_strict_validation_exhausts_retries(monkeypatch):
    _fake_client(monkeypatch, [_response(_invalid_wire()) for _ in range(3)])
    with pytest.raises(ExtractionError, match="schema-invalid"):
        _extract()


def test_wire_model_accepts_what_strict_rejects():
    # sanity: the retry path is reachable — unconstrained wire, constrained strict
    wire = _WirePick.model_validate({**WIRE_PICK, "conviction": 9, "reasoning": "x" * 400})
    with pytest.raises(ValidationError):
        Pick.model_validate({**wire.model_dump(), "player_id": None})


def test_zero_picks_logs_warning(monkeypatch, caplog):
    wire = _WireExtraction(gameweek=1, picks=[], chip_plans=[])
    _fake_client(monkeypatch, [_response(wire)])
    with caplog.at_level(logging.WARNING):
        result = _extract()
    assert result.picks == []
    assert any("zero picks" in r.message for r in caplog.records)
