"""Claude extraction of self-reported rank claims from season-review
transcripts — the LLM half of the SELF_CLAIMED harvest sweep.

Follows the same shape as `extract/extractor.py` (and the lessons from
its review): the LLM emits an UNCONSTRAINED wire schema, so the SDK's
eager `messages.parse()` only raises on truncated/malformed JSON;
everything else is validated here. The model supplies quote + claim
fields only — video/creator metadata is stamped by the pipeline.

Timestamps are DERIVED, not trusted: when a quote verifies against the
stored transcript, `locate_quote_timestamp` maps the quote's position
back to the segment it starts in, mechanically. The model's
`timestamp_hint_s` is used only as a fallback pointer for quotes that
fail verification (the owner still needs somewhere to look).

Nothing here writes to `roster/registry.py` or any trust-model data:
the output is `RankClaimCandidate`s, verified and rendered into
`data/harvest/claims_review.md` for OWNER APPROVAL — per PLAN.md, no
claim enters the trust model without it. (The registry is imported
READ-ONLY, for creator display names and shared-channel context.)

Cache note: the system prompt sits near claude-opus-5's 512-token
cacheable minimum. On the first real sweep, verify
`usage.cache_read_input_tokens > 0` across consecutive videos before
assuming the cache saving.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fpl_oracle.ingest.transcripts import Transcript, fetch_transcript
from fpl_oracle.roster.claim_verify import collapse_whitespace
from fpl_oracle.roster.claims import (
    RankClaimCandidate,
    verify_candidates,
    write_claims_review,
)
from fpl_oracle.roster.harvest import DEFAULT_MANIFEST_PATH
from fpl_oracle.roster.registry import REGISTRY

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
# Hard cap on thinking + output together (thinking is ON by default on
# claude-opus-5). The claims JSON is tiny, but a 30-40 minute transcript
# can draw thousands of thinking tokens; headroom is free and truncation
# is a hard error. Levers if it ever bites: output_config effort
# "medium", or streaming with a higher cap.
MAX_TOKENS = 16_000

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "extract_rank_claims.txt").read_text()

_client: anthropic.Anthropic | None = None


class ClaimExtractionError(RuntimeError):
    """The model could not produce a usable claim extraction."""


class _WireClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(
        description=(
            "Verbatim substring of the transcript carrying the claim — "
            "character-for-character, no corrections, no timestamp markers."
        )
    )
    timestamp_hint_s: int = Field(
        description="The [Ns] marker of the segment where the quote begins."
    )
    claimed_season: str = Field(description='Season the claim is about, e.g. "2025/26".')
    claimed_rank: int | None = Field(
        description="Exact rank, K-boundary for a band (top 10k -> 10000), or null."
    )
    claim_kind: str = Field(description='"overall_rank", "top_k", or "vague".')
    notes: str | None = Field(
        description="Anything the human reviewer should know; null otherwise."
    )


class _WireClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_WireClaim]


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        load_dotenv()
        _client = anthropic.Anthropic()
    return _client


def render_timestamped_transcript(transcript: Transcript) -> str:
    """Render segments as '[123s] text' lines for the model."""
    return "\n".join(f"[{int(s.start)}s] {s.text}" for s in transcript.segments)


def locate_quote_timestamp(quote: str, transcript: Transcript) -> int | None:
    """Mechanically find the start time of the segment a quote begins in.

    Uses the same normalization as `quote_in_transcript` (whitespace
    collapse + casefold) so a quote that verifies always locates. Returns
    None when the quote isn't present (first occurrence wins otherwise).
    """
    norm_quote = collapse_whitespace(quote).casefold()
    if not norm_quote:
        return None

    # Build the normalized full text while recording, for each segment,
    # its start offset within that normalized text.
    offsets: list[tuple[int, float]] = []  # (normalized char offset, segment start s)
    parts: list[str] = []
    pos = 0
    for seg in transcript.segments:
        norm_seg = collapse_whitespace(seg.text).casefold()
        if not norm_seg:
            continue
        offsets.append((pos, seg.start))
        parts.append(norm_seg)
        pos += len(norm_seg) + 1  # +1 for the joining space
    full = " ".join(parts)

    idx = full.find(norm_quote)
    if idx == -1:
        return None
    start = 0.0
    for offset, seg_start in offsets:
        if offset > idx:
            break
        start = seg_start
    return int(start)


def channel_context_for(creator_id: str) -> str | None:
    """Shared-channel context line for the model, from the registry:
    joint-show primaries and title-filtered personas both mean other
    voices appear on the channel, so speaker attribution needs care."""
    creator = next((c for c in REGISTRY if c.creator_id == creator_id), None)
    if creator is None:
        return None
    if creator.channel_primary:
        return (
            "This channel hosts joint discussions with multiple regular "
            "co-hosts; other voices than the named creator appear often."
        )
    if creator.title_filter:
        return (
            "This creator is one of several personas sharing one channel; "
            "other people's videos and voices appear on it."
        )
    return None


def extract_rank_claims(
    *,
    creator_id: str,
    creator_name: str,
    video_id: str,
    video_title: str,
    transcript: Transcript,
    published_at: datetime | None = None,
    channel_context: str | None = None,
) -> list[RankClaimCandidate]:
    """Extract rank-claim candidates from one season-review transcript.
    Quotes are verified downstream (`verify_candidates`); timestamps are
    derived mechanically here whenever the quote locates."""
    try:
        response = _get_client().messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Creator (the ONLY person whose claims count): {creator_name}\n"
                        f"Video title: {video_title}\n"
                        + (f"Published: {published_at:%Y-%m-%d}\n" if published_at else "")
                        + (f"Channel context: {channel_context}\n" if channel_context else "")
                        + f"Transcript:\n{render_timestamped_transcript(transcript)}"
                    ),
                }
            ],
            output_format=_WireClaims,
        )
    except ValidationError as e:
        raise ClaimExtractionError(
            f"malformed model output for video {video_id} "
            f"(likely truncated at {MAX_TOKENS} tokens): {e}"
        ) from e

    if response.stop_reason == "refusal":
        raise ClaimExtractionError(f"model refused claim extraction for video {video_id}")
    if response.stop_reason == "max_tokens":
        raise ClaimExtractionError(
            f"claim extraction truncated at {MAX_TOKENS} tokens for video {video_id}"
        )
    wire = response.parsed_output
    if wire is None:
        raise ClaimExtractionError(f"no parseable output for video {video_id}")

    candidates = []
    for claim in wire.claims:
        located = locate_quote_timestamp(claim.quote, transcript)
        candidates.append(
            RankClaimCandidate(
                creator_id=creator_id,
                video_id=video_id,
                video_title=video_title,
                timestamp_s=located if located is not None else claim.timestamp_hint_s,
                quote=claim.quote,
                claimed_season=claim.claimed_season,
                claimed_rank=claim.claimed_rank,
                claim_kind=claim.claim_kind,
                notes=claim.notes,
                published_at=published_at,
            )
        )
    return candidates


def run_claim_extraction(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    creator_ids: list[str] | None = None,
) -> Path:
    """Extract claims for every transcript-bearing entry in the harvest
    manifest, verify quotes, and write the owner-review markdown. Returns
    the review file path.

    Per-video extraction failures are non-fatal: they're logged, listed
    in a FAILED section of the review file, and never discard the
    candidates (and API spend) of videos that succeeded."""
    entries = json.loads(manifest_path.read_text())
    known_ids = {e["creator_id"] for e in entries}
    if creator_ids is not None:
        unknown = sorted(set(creator_ids) - known_ids)
        if unknown:
            raise ValueError(f"creator id(s) not in the harvest manifest: {', '.join(unknown)}")

    names = {c.creator_id: c.name for c in REGISTRY}
    all_candidates: list[RankClaimCandidate] = []
    transcripts: dict[str, Transcript] = {}
    failures: list[tuple[str, str, str]] = []  # (creator_id, video_id, error)

    for entry in entries:
        if creator_ids is not None and entry["creator_id"] not in creator_ids:
            continue
        if not entry.get("transcript_available"):
            continue
        transcript = fetch_transcript(entry["video_id"])  # cache-hit for harvested videos
        if transcript is None:
            logger.warning("transcript vanished for video %s — skipping", entry["video_id"])
            failures.append((entry["creator_id"], entry["video_id"], "transcript vanished"))
            continue
        transcripts[entry["video_id"]] = transcript
        published_at = (
            datetime.fromisoformat(entry["published_at"]) if entry.get("published_at") else None
        )
        try:
            candidates = extract_rank_claims(
                creator_id=entry["creator_id"],
                creator_name=names.get(entry["creator_id"], entry["creator_id"]),
                video_id=entry["video_id"],
                video_title=entry["title"],
                transcript=transcript,
                published_at=published_at,
                channel_context=channel_context_for(entry["creator_id"]),
            )
        except ClaimExtractionError as e:
            logger.warning(
                "claims: %s — extraction FAILED for video %s: %s",
                entry["creator_id"],
                entry["video_id"],
                e,
            )
            failures.append((entry["creator_id"], entry["video_id"], str(e)))
            continue
        logger.info(
            "claims: %s — %d candidate(s) from %r",
            entry["creator_id"],
            len(candidates),
            entry["title"],
        )
        all_candidates.extend(candidates)

    verified = verify_candidates(all_candidates, transcripts)
    path = write_claims_review(verified, creator_names=names)
    if failures:
        failed_lines = ["", "## FAILED extractions — review these videos manually", ""]
        failed_lines += [
            f"- {creator_id} — video `{video_id}`: {error}"
            for creator_id, video_id, error in failures
        ]
        with path.open("a") as f:
            f.write("\n".join(failed_lines) + "\n")
        logger.warning("claims: %d video(s) failed extraction — listed in %s", len(failures), path)
    return path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Extract rank claims from harvested transcripts")
    parser.add_argument("creator_ids", nargs="*", help="limit to these creator ids")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = run_claim_extraction(creator_ids=args.creator_ids or None)
    print(f"claims review written to {path} — every claim needs owner approval")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
