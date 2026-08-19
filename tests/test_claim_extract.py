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
        creator_name="Andy",
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
        creator_name="C",
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
        extract_rank_claims(
            creator_id="c",
            creator_name="C",
            video_id="v",
            video_title="T",
            transcript=_transcript(),
        )
        == []
    )


def test_parse_call_raising_reports_as_truncation(monkeypatch):
    def _raise():
        _WireClaims.model_validate_json('{"claims": [{"quo')

    _fake_client(monkeypatch, [_raise])
    with pytest.raises(ClaimExtractionError, match="likely truncated"):
        extract_rank_claims(
            creator_id="c",
            creator_name="C",
            video_id="v",
            video_title="T",
            transcript=_transcript(),
        )


def test_refusal_raises(monkeypatch):
    _fake_client(monkeypatch, [_response(None, stop_reason="refusal")])
    with pytest.raises(ClaimExtractionError, match="refused"):
        extract_rank_claims(
            creator_id="c",
            creator_name="C",
            video_id="v",
            video_title="T",
            transcript=_transcript(),
        )


def test_user_message_carries_creator_context_and_cached_system(monkeypatch):
    calls = _fake_client(monkeypatch, [_response(_WireClaims(claims=[]))])
    extract_rank_claims(
        creator_id="pras",
        creator_name="Pras",
        video_id="vid123",
        video_title="SEASON REVIEW POD",
        transcript=_transcript(),
        channel_context="joint show with co-hosts",
    )
    call = calls[0]
    content = call["messages"][0]["content"]
    assert "Creator (the ONLY person whose claims count): Pras" in content
    assert "Channel context: joint show with co-hosts" in content
    assert "[4s] so this season i finished" in content  # rendered transcript reaches the model
    (system_block,) = call["system"]
    assert system_block["cache_control"] == {"type": "ephemeral"}
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 16_000


def test_max_tokens_and_no_output_branches(monkeypatch):
    _fake_client(monkeypatch, [_response(None, stop_reason="max_tokens")])
    with pytest.raises(ClaimExtractionError, match="truncated"):
        extract_rank_claims(
            creator_id="c",
            creator_name="C",
            video_id="v",
            video_title="T",
            transcript=_transcript(),
        )

    _fake_client(monkeypatch, [_response(None)])
    with pytest.raises(ClaimExtractionError, match="no parseable"):
        extract_rank_claims(
            creator_id="c",
            creator_name="C",
            video_id="v",
            video_title="T",
            transcript=_transcript(),
        )


class TestRunClaimExtraction:
    def _manifest(self, tmp_path, entries):
        import json

        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(entries))
        return path

    def _entries(self):
        return [
            {
                "creator_id": "lets-talk-fpl",
                "video_id": "vid123",
                "title": "SEASON REVIEW",
                "published_at": "2026-06-01T00:00:00+00:00",
                "transcript_available": True,
            },
            {
                "creator_id": "fpl-focal",
                "video_id": "vid999",
                "title": "ANOTHER REVIEW",
                "published_at": None,
                "transcript_available": True,
            },
        ]

    def test_failure_continues_and_is_listed(self, monkeypatch, tmp_path):
        from fpl_oracle.roster import claim_extract as ce

        monkeypatch.setattr(ce, "fetch_transcript", lambda vid: _transcript())

        def fake_extract(**kwargs):
            if kwargs["video_id"] == "vid999":
                raise ClaimExtractionError("model refused claim extraction for video vid999")
            return [
                # reuse the real construction path via the wire claim
            ]

        monkeypatch.setattr(ce, "extract_rank_claims", fake_extract)
        review_path = tmp_path / "claims_review.md"
        monkeypatch.setattr(
            ce,
            "write_claims_review",
            lambda cands, creator_names=None: (
                review_path.write_text("# review\n"),
                review_path,
            )[1],
        )

        result = ce.run_claim_extraction(manifest_path=self._manifest(tmp_path, self._entries()))

        assert result == review_path
        text = review_path.read_text()
        assert "FAILED extractions" in text
        assert "vid999" in text

    def test_unknown_creator_id_fails_loudly(self, tmp_path):
        from fpl_oracle.roster import claim_extract as ce

        with pytest.raises(ValueError, match="not in the harvest manifest"):
            ce.run_claim_extraction(
                manifest_path=self._manifest(tmp_path, self._entries()),
                creator_ids=["definitely-a-typo"],
            )

    def test_vanished_transcript_is_skipped_and_listed(self, monkeypatch, tmp_path):
        from fpl_oracle.roster import claim_extract as ce

        monkeypatch.setattr(ce, "fetch_transcript", lambda vid: None)
        review_path = tmp_path / "claims_review.md"
        monkeypatch.setattr(
            ce,
            "write_claims_review",
            lambda cands, creator_names=None: (
                review_path.write_text("# review\n"),
                review_path,
            )[1],
        )
        ce.run_claim_extraction(manifest_path=self._manifest(tmp_path, self._entries()))
        text = review_path.read_text()
        assert "transcript vanished" in text


def test_strip_marker_artifacts():
    from fpl_oracle.roster.claim_extract import strip_marker_artifacts

    assert (
        strip_marker_artifacts("You can see from [37s] my rank, 1.1 million.")
        == "You can see from my rank, 1.1 million."
    )
    assert strip_marker_artifacts("no markers here") == "no markers here"
    assert strip_marker_artifacts("[0s] leading and trailing [123s]") == "leading and trailing"


def test_marker_polluted_quote_now_verifies_and_locates(monkeypatch):
    # the exact failure from the first live sweep: quote spans segments and
    # the model copied the next segment's [Ns] marker into it
    polluted = "so this season i finished [7s] 588th in the world"
    claim = _wire_claim(quote=polluted, timestamp_hint_s=999)
    _fake_client(monkeypatch, [_response(_WireClaims(claims=[claim]))])
    (candidate,) = extract_rank_claims(
        creator_id="c",
        creator_name="C",
        video_id="vid123",
        video_title="T",
        transcript=_transcript(),
    )
    assert "[7s]" not in candidate.quote
    assert candidate.timestamp_s == 4  # derived, not the 999 hint
    (verified,) = verify_candidates([candidate], {"vid123": _transcript()})
    assert verified.quote_verified is True


def test_save_and_reverify_round_trip(monkeypatch, tmp_path):
    from fpl_oracle.roster import claim_extract as ce
    from fpl_oracle.roster.claims import RankClaimCandidate

    candidate = RankClaimCandidate(
        creator_id="lets-talk-fpl",
        video_id="vid123",
        video_title="SEASON REVIEW",
        timestamp_s=4,
        quote="this season i finished 588th in the world",
        claimed_season="2025/26",
        claimed_rank=588,
        claim_kind="overall_rank",
    )
    saved = ce.save_candidates([candidate], path=tmp_path / "cands.json")

    monkeypatch.setattr(ce, "fetch_transcript", lambda vid: _transcript())
    review_path = tmp_path / "review.md"
    written = {}
    monkeypatch.setattr(
        ce,
        "write_claims_review",
        lambda cands, creator_names=None: (written.update(c=cands), review_path)[1],
    )
    assert ce.reverify_saved_candidates(candidates_path=saved) == review_path
    (verified,) = written["c"]
    assert verified.quote_verified is True
