"""Tests for roster/claim_extract.py — no network; the client is faked."""

from types import SimpleNamespace

import pytest

from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment
from fpl_oracle.roster import claim_extract
from fpl_oracle.roster.claim_extract import (
    ClaimExtractionError,
    _WireClaim,
    _WireClaims,
    extract_rank_claims,
    locate_quote_timestamp,
    render_timestamped_transcript,
)
from fpl_oracle.roster.claims import verify_candidates


def _transcript():
    return Transcript(
        video_id="vid123",
        language="en",
        is_generated=True,
        segments=[
            TranscriptSegment(text="welcome back everyone", start=0.0, duration=4.0),
            TranscriptSegment(text="so this season i finished", start=4.2, duration=3.1),
            TranscriptSegment(text="588th in the world", start=7.5, duration=2.8),
            TranscriptSegment(text="unbelievable scenes", start=10.4, duration=2.0),
        ],
    )


def _response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, content=[])


def _fake_client(monkeypatch, responses):
    calls = []
    it = iter(responses)

    def parse(**kwargs):
        calls.append(kwargs)
        item = next(it)
        if callable(item):
            item()
        return item

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    monkeypatch.setattr(claim_extract, "_get_client", lambda: client)
    return calls


def _wire_claim(**overrides):
    payload = {
        "quote": "this season i finished 588th in the world",
        "timestamp_hint_s": 999,  # deliberately wrong — must be overridden
        "claimed_season": "2025/26",
        "claimed_rank": 588,
        "claim_kind": "overall_rank",
        "notes": None,
    }
    payload.update(overrides)
    return _WireClaim.model_validate(payload)


def test_render_timestamped_transcript():
    rendered = render_timestamped_transcript(_transcript())
    assert rendered.splitlines()[1] == "[4s] so this season i finished"


def test_locate_quote_timestamp_cross_segment_and_casefold():
    t = _transcript()
    # spans segments 2-3, sentence-cased and whitespace-mangled
    assert locate_quote_timestamp("This season I  finished 588th", t) == 4
    assert locate_quote_timestamp("588th in the world", t) == 7
    assert locate_quote_timestamp("never said this", t) is None
    assert locate_quote_timestamp("   ", t) is None


def test_timestamp_derived_mechanically_not_trusted(monkeypatch):
    _fake_client(monkeypatch, [_response(_WireClaims(claims=[_wire_claim()]))])
    (candidate,) = extract_rank_claims(
        creator_id="lets-talk-fpl",
        video_id="vid123",
        video_title="SEASON REVIEW",
        transcript=_transcript(),
    )
    assert candidate.timestamp_s == 4  # derived from the quote, not the 999 hint
    assert candidate.creator_id == "lets-talk-fpl"
    assert candidate.claimed_rank == 588
    # and the quote verifies against the same transcript downstream
    (verified,) = verify_candidates([candidate], {"vid123": _transcript()})
    assert verified.quote_verified is True


def test_unlocatable_quote_falls_back_to_hint(monkeypatch):
    claim = _wire_claim(quote="i finished five hundredth", timestamp_hint_s=42)
    _fake_client(monkeypatch, [_response(_WireClaims(claims=[claim]))])
    (candidate,) = extract_rank_claims(
        creator_id="c",
        video_id="vid123",
        video_title="T",
        transcript=_transcript(),
    )
    assert candidate.timestamp_s == 42
    (verified,) = verify_candidates([candidate], {"vid123": _transcript()})
    assert verified.quote_verified is False


def test_empty_claims_is_valid(monkeypatch):
    _fake_client(monkeypatch, [_response(_WireClaims(claims=[]))])
    assert (
        extract_rank_claims(creator_id="c", video_id="v", video_title="T", transcript=_transcript())
        == []
    )


def test_parse_call_raising_reports_as_truncation(monkeypatch):
    def _raise():
        _WireClaims.model_validate_json('{"claims": [{"quo')

    _fake_client(monkeypatch, [_raise])
    with pytest.raises(ClaimExtractionError, match="likely truncated"):
        extract_rank_claims(creator_id="c", video_id="v", video_title="T", transcript=_transcript())


def test_refusal_raises(monkeypatch):
    _fake_client(monkeypatch, [_response(None, stop_reason="refusal")])
    with pytest.raises(ClaimExtractionError, match="refused"):
        extract_rank_claims(creator_id="c", video_id="v", video_title="T", transcript=_transcript())
