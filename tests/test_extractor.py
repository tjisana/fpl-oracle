"""Tests for extract/extractor.py — no network; the Anthropic client is faked."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from fpl_oracle.extract import extractor
from fpl_oracle.extract.extractor import (
    ExtractionError,
    _LLMExtraction,
    build_user_message,
    extract_picks,
)
from fpl_oracle.extract.schemas import Pick

PICK = {
    "player_name_raw": "Sacca",
    "team_inferred": "Arsenal",
    "position_inferred": "MID",
    "action": "captain",
    "conviction": 5,
    "time_horizon": 1,
    "reasoning": "Nailed on set pieces.",
    "provenance": "personal",
}


def _response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, content=[])


def _fake_client(monkeypatch, responses):
    calls = []
    it = iter(responses)

    def parse(**kwargs):
        calls.append(kwargs)
        return next(it)

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


def test_pipeline_stamps_metadata_llm_supplies_picks_only(monkeypatch):
    llm_out = _LLMExtraction(gameweek=1, picks=[Pick.model_validate(PICK)])
    calls = _fake_client(monkeypatch, [_response(llm_out)])

    result = _extract()

    assert result.creator_id == "lets-talk-fpl"
    assert result.video_id == "abc123"
    assert result.gameweek == 1
    assert result.picks[0].player_name_raw == "Sacca"
    assert result.picks[0].player_id is None
    # the LLM was asked only for the pick-level schema
    assert calls[0]["output_format"] is _LLMExtraction


def test_refusal_and_truncation_raise(monkeypatch):
    _fake_client(monkeypatch, [_response(None, stop_reason="refusal")])
    with pytest.raises(ExtractionError, match="refused"):
        _extract()

    _fake_client(monkeypatch, [_response(None, stop_reason="max_tokens")])
    with pytest.raises(ExtractionError, match="truncated"):
        _extract()


def test_unparseable_output_raises(monkeypatch):
    _fake_client(monkeypatch, [_response(None)])
    with pytest.raises(ExtractionError, match="no parseable"):
        _extract()


class _ExplodingResponse:
    """parsed_output raises ValidationError on access, as the SDK does when
    client-side constraint validation fails."""

    stop_reason = "end_turn"

    def __init__(self):
        self.content = [SimpleNamespace(type="text", text='{"gameweek": 99, "picks": []}')]

    @property
    def parsed_output(self):
        _LLMExtraction.model_validate({"gameweek": 99, "picks": []})


def test_validation_error_retries_with_feedback_then_succeeds(monkeypatch):
    good = _LLMExtraction(gameweek=1, picks=[Pick.model_validate(PICK)])
    calls = _fake_client(monkeypatch, [_ExplodingResponse(), _response(good)])

    result = _extract()

    assert result.gameweek == 1
    assert len(calls) == 2
    retry_messages = calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "failed schema validation" in retry_messages[-1]["content"]


def test_validation_error_exhausts_retries(monkeypatch):
    _fake_client(monkeypatch, [_ExplodingResponse()] * 3)
    with pytest.raises(ExtractionError, match="schema-invalid"):
        _extract()
