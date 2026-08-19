"""Human-review artifact for the SELF_CLAIMED harvest sweep.

This module owns the candidate schema an (out-of-scope-here) LLM
extraction call would populate — extracting rank claims from a
transcript is the main session's call, not built here — plus rendering
candidates, together with their quote-verification result, into a
markdown file the owner approves claims from.

Nothing here writes to `roster/registry.py` or any other trust-model
data. Per PLAN.md, every claim needs the owner's manual approval before
it can affect a creator's weight — this module's output
(`data/harvest/claims_review.md`) is exactly what the owner approves
from: quote, timestamped link, season, rank, and verified flag, never
the raw transcript. Per the quote-anchoring rule, the owner judges
interpretation only from quote + timestamp + link, never by rereading
whole transcripts.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from fpl_oracle.ingest.transcripts import Transcript
from fpl_oracle.roster.claim_verify import quote_in_transcript

DEFAULT_CLAIMS_REVIEW_PATH = Path("data/harvest/claims_review.md")


class RankClaimCandidate(BaseModel):
    """A candidate SELF_CLAIMED rank claim, pending owner approval.

    `claim_kind` is a free-form tag describing what sort of claim this
    is (e.g. "overall_rank", "top_k", "vague") — kept as `str` rather
    than an enum since the extraction prompt that populates this (out of
    scope here) may need to evolve its own vocabulary independently of
    this schema.
    """

    creator_id: str
    video_id: str
    video_title: str
    video_url_at_timestamp: str
    timestamp_s: int
    quote: str
    claimed_season: str
    claimed_rank: int | None
    claim_kind: str
    quote_verified: bool = False
    notes: str | None = None


def video_url_at_timestamp(video_id: str, timestamp_s: int) -> str:
    """Build a `&t=<seconds>` deep link into a video. Factored out so
    every candidate-construction site (wherever the extraction call
    ends up living) and tests share one place that gets the URL shape
    right."""
    return f"https://www.youtube.com/watch?v={video_id}&t={timestamp_s}"


def verify_candidates(
    candidates: list[RankClaimCandidate],
    transcripts: dict[str, Transcript],
) -> list[RankClaimCandidate]:
    """Return `candidates` with `quote_verified` set by running
    `quote_in_transcript` against each candidate's stored transcript
    (looked up in `transcripts` by `video_id`).

    A candidate whose video isn't in `transcripts` at all verifies
    False rather than raising — a missing transcript is exactly the
    kind of thing the human reviewer should see flagged in
    `claims_review.md`, not a crash mid-run.
    """
    verified: list[RankClaimCandidate] = []
    for candidate in candidates:
        transcript = transcripts.get(candidate.video_id)
        is_verified = (
            quote_in_transcript(candidate.quote, transcript.full_text)
            if transcript is not None
            else False
        )
        verified.append(candidate.model_copy(update={"quote_verified": is_verified}))
    return verified


def _format_timestamp(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_claims_review(
    candidates: list[RankClaimCandidate],
    creator_names: dict[str, str] | None = None,
) -> str:
    """Render `candidates` into the human-review markdown: one `##`
    section per creator (sorted by `creator_id` for a stable order),
    each claim as its own subsection showing the quote, a timestamped
    link, season, claimed rank, claim kind, and verified flag.

    `creator_names` maps creator_id -> display name; a creator missing
    from it (or when the whole map is omitted) falls back to showing
    the bare creator_id.
    """
    creator_names = creator_names or {}
    lines = ["# SELF_CLAIMED harvest — claim review", ""]

    if not candidates:
        lines.append("No candidates.")
        return "\n".join(lines) + "\n"

    by_creator: dict[str, list[RankClaimCandidate]] = {}
    for candidate in candidates:
        by_creator.setdefault(candidate.creator_id, []).append(candidate)

    for creator_id in sorted(by_creator):
        name = creator_names.get(creator_id, creator_id)
        lines.append(f"## {name} (`{creator_id}`)")
        lines.append("")
        for candidate in by_creator[creator_id]:
            marker = "VERIFIED" if candidate.quote_verified else "UNVERIFIED — check manually"
            rank_display = candidate.claimed_rank if candidate.claimed_rank is not None else "n/a"
            lines.append(f"### {candidate.video_title}")
            lines.append("")
            lines.append(f"- Quote verified: **{marker}**")
            lines.append(f"- Season: {candidate.claimed_season}")
            lines.append(f"- Claimed rank: {rank_display}")
            lines.append(f"- Claim kind: {candidate.claim_kind}")
            timestamp_label = _format_timestamp(candidate.timestamp_s)
            lines.append(f"- Timestamp: [{timestamp_label}]({candidate.video_url_at_timestamp})")
            lines.append(f"- Quote: > {candidate.quote}")
            if candidate.notes:
                lines.append(f"- Notes: {candidate.notes}")
            lines.append("")

    return "\n".join(lines) + "\n"


def write_claims_review(
    candidates: list[RankClaimCandidate],
    creator_names: dict[str, str] | None = None,
    path: Path = DEFAULT_CLAIMS_REVIEW_PATH,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_claims_review(candidates, creator_names))
    return path
