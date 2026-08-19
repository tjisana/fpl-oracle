"""SELF_CLAIMED harvest sweep: locate each creator's season-review /
season-recap / rank-reveal style videos by title, fetch transcripts via
the existing `ingest/` machinery, and write a manifest of what was
found.

Per PLAN.md's Phase 2 harvest-sweep item and the module boundary set for
this task: this module builds harvest INFRASTRUCTURE only. It does not
extract rank claims from transcripts — that LLM call belongs to
`extract/` and is owned by the main session, deliberately out of scope
here. It also never writes to `roster/registry.py` or any other
trust-model data; the pipeline built here ends at human-reviewable
files under `data/harvest/` (this module's `manifest.json`, and
`roster/claims.py`'s `claims_review.md`). The owner approves every
claim by hand before it can move a creator's weight.

Run as:

    uv run python -m fpl_oracle.roster.harvest [creator_id ...]

With no arguments, sweeps every eligible creator in the registry. With
one or more creator_ids, sweeps only those (still respecting
shared-channel eligibility/attribution against the full registry).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from fpl_oracle.ingest.run_ingest import eligible_creators, videos_for_creator
from fpl_oracle.ingest.transcripts import fetch_transcript, save_transcript
from fpl_oracle.ingest.youtube_client import VideoInfo, list_uploads
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY

# Season-review videos are typically posted 2-3 months after a season
# ends, well past a channel's freshest handful of uploads by the time
# this sweep runs — page deep enough to reliably reach them.
MAX_UPLOADS_PER_CHANNEL = 75

DEFAULT_MANIFEST_PATH = Path("data/harvest/manifest.json")

console = Console()

# Title patterns for videos worth harvesting for a SELF_CLAIMED rank
# claim. Kept as a module-level constant, deliberately easy to extend as
# more real title conventions turn up during the actual sweep.
#
# Every pattern below pairs a distinctive word ("season", "rank", "final")
# with a second word, specifically to stay out of ordinary weekly-content
# noise: a bare "review" (e.g. "GW38 REVIEW") or "reveal" (e.g. "GW1 Team
# Reveal") is far too common a word in FPL titles to match alone.
SEASON_REVIEW_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"season\s*review", re.IGNORECASE),
    re.compile(r"season\s+in\s+review", re.IGNORECASE),
    re.compile(r"season\s*recap", re.IGNORECASE),
    re.compile(r"season\s+opener", re.IGNORECASE),
    re.compile(r"season\s+wrap", re.IGNORECASE),
    re.compile(r"end\s+of\s+season", re.IGNORECASE),
    re.compile(r"my\s+fpl\s+season", re.IGNORECASE),
    re.compile(r"how\s+i\s+finished", re.IGNORECASE),
    re.compile(r"where\s+i\s+finished", re.IGNORECASE),
    re.compile(r"my\s+final\s+rank", re.IGNORECASE),
    re.compile(r"rank\s+reveal", re.IGNORECASE),
    re.compile(r"finishing\s+(?:in\s+the\s+)?top\s*\d+\s*[km]?", re.IGNORECASE),
    re.compile(r"what\s+i\s+learned\s+this\s+season", re.IGNORECASE),
]


def is_season_review_candidate(title: str) -> bool:
    """True if `title` looks like a season-review / recap / rank-reveal
    style video worth harvesting for a SELF_CLAIMED rank claim.

    Deliberately conservative-but-recall-leaning, per PLAN.md: a false
    positive here just means a human reviewer sees one extra
    (quickly-skippable, since the LLM extraction step out of scope here
    would simply find no rank claim in it and produce no candidate)
    video; a false negative silently drops a creator's past-rank claim
    from the sweep entirely, and per PLAN.md that's a much bigger cost —
    SELF_CLAIMED evidence has to land before the GW1 weight freeze or it
    waits a full year. So this over-matches on purpose (e.g. a "season
    opener" hype video with no rank claim in it will slip through and
    get filtered out at the human-review stage, not here) rather than
    trying to be precise.

    Explicitly does NOT match ordinary weekly content — see the pattern
    list's docstring note for why "review"/"reveal" alone are excluded
    (e.g. "GW38 REVIEW", "GW1 Team Reveal" must not match).
    """
    return any(pattern.search(title) for pattern in SEASON_REVIEW_TITLE_PATTERNS)


class HarvestManifestEntry(BaseModel):
    creator_id: str
    video_id: str
    title: str
    published_at: str
    transcript_available: bool


def find_candidates_for_creator(
    creator: Creator,
    registry: list[Creator],
    max_uploads: int = MAX_UPLOADS_PER_CHANNEL,
) -> list[VideoInfo]:
    """List up to `max_uploads` of `creator`'s uploads (deep paging via
    `youtube_client.list_uploads`, not just the freshest few), attribute
    them per the shared-channel rule in
    `ingest.run_ingest.videos_for_creator` (a no-op for a sole-owned
    channel — reused as-is, not reimplemented, per the shared-channel
    setup for Fantasy Football Hub personas / The FPL Wire), then filter
    to season-review-style titles via `is_season_review_candidate`.
    """
    assert creator.channel_id is not None
    videos = list_uploads(creator.channel_id, max_results=max_uploads)
    co_creators = [
        c
        for c in registry
        if c.channel_id == creator.channel_id and c.creator_id != creator.creator_id
    ]
    attributed = videos_for_creator(creator, co_creators, videos)
    return [v for v in attributed if is_season_review_candidate(v.title)]


def harvest_creator(
    creator: Creator,
    registry: list[Creator],
    max_uploads: int = MAX_UPLOADS_PER_CHANNEL,
) -> list[HarvestManifestEntry]:
    """Find `creator`'s season-review-style videos and fetch+save a
    transcript for each (via the existing `ingest.transcripts` cache/
    store, reused as-is), recording one manifest entry per candidate
    video regardless of whether a transcript was actually available."""
    candidates = find_candidates_for_creator(creator, registry, max_uploads)
    entries: list[HarvestManifestEntry] = []
    for video in candidates:
        transcript = fetch_transcript(video.video_id)
        if transcript is not None:
            save_transcript(transcript)
        entries.append(
            HarvestManifestEntry(
                creator_id=creator.creator_id,
                video_id=video.video_id,
                title=video.title,
                published_at=video.published_at.isoformat(),
                transcript_available=transcript is not None,
            )
        )
    return entries


def write_manifest(entries: list[HarvestManifestEntry], path: Path = DEFAULT_MANIFEST_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([e.model_dump() for e in entries], indent=2))
    return path


def run_harvest(
    creators: list[Creator],
    registry: list[Creator] = REGISTRY,
    max_uploads: int = MAX_UPLOADS_PER_CHANNEL,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> list[HarvestManifestEntry]:
    """Run the harvest sweep across `creators`, write the resulting
    manifest to `manifest_path`, and return the entries."""
    all_entries: list[HarvestManifestEntry] = []
    for creator in creators:
        entries = harvest_creator(creator, registry, max_uploads)
        all_entries.extend(entries)
        if entries:
            print(
                f"harvest: {creator.name} — {len(entries)} season-review candidate(s) found",
                file=sys.stderr,
            )
        else:
            print(f"harvest: {creator.name} — no season-review candidates found", file=sys.stderr)
    write_manifest(all_entries, manifest_path)
    return all_entries


def print_summary(entries: list[HarvestManifestEntry], registry: list[Creator] = REGISTRY) -> None:
    names = {c.creator_id: c.name for c in registry}
    table = Table(title="Season-review harvest sweep")
    table.add_column("Creator")
    table.add_column("Video title")
    table.add_column("Published")
    table.add_column("Transcript")

    for entry in entries:
        table.add_row(
            names.get(entry.creator_id, entry.creator_id),
            entry.title,
            entry.published_at,
            "yes" if entry.transcript_available else "no",
        )

    if not entries:
        console.print("No season-review candidates found.")
        return
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    eligible = eligible_creators(REGISTRY)
    if argv:
        creators = [c for c in eligible if c.creator_id in argv]
        missing = set(argv) - {c.creator_id for c in creators}
        if missing:
            print(f"harvest: unknown/ineligible creator_id(s): {sorted(missing)}", file=sys.stderr)
            return 1
    else:
        creators = eligible

    entries = run_harvest(creators)
    print_summary(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
