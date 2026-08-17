"""Resolve YouTube channel IDs for every creator in the seed roster.

Usage: `uv run python -m fpl_oracle.roster.resolve_channels`

For each creator, searches `youtube_hint` via the YouTube Data API, takes
the top channel result, and records its subscriber count. Flags any
match whose channel title doesn't clearly correspond to the creator
name (rapidfuzz partial-ratio below threshold) so a human can spot-check
it rather than silently trusting the top search hit. Search results are
cached on disk (see `ingest.youtube_client`) keyed by the exact hint
text, so re-running this after a roster edit only spends API quota on
hints that actually changed — no separate "reuse" bookkeeping needed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from rapidfuzz import fuzz

from fpl_oracle.ingest.youtube_client import search_top_channel
from fpl_oracle.roster.seed_roster import SEED_ROSTER, SeedCreator

TITLE_MATCH_THRESHOLD = 60


@dataclass
class ResolvedChannel:
    creator_name: str
    channel_id: str | None
    channel_title: str | None
    subscriber_count: int | None
    flagged: bool
    flag_reason: str | None


def resolve_channel(creator: SeedCreator) -> ResolvedChannel:
    match = search_top_channel(creator.youtube_hint)
    if match is None:
        return ResolvedChannel(creator.name, None, None, None, True, "No YouTube channel found.")

    score = fuzz.partial_ratio(creator.name.lower(), match.title.lower())
    title_mismatch = score < TITLE_MATCH_THRESHOLD
    # A 0-subscriber "exact name" hit is more often a squatter/decoy channel
    # than the real creator's — flag it even though the title matches.
    zero_subs = not match.hidden_subscriber_count and match.subscriber_count == 0
    flagged = title_mismatch or zero_subs
    reason = None
    if title_mismatch:
        reason = f"title '{match.title}' vs creator name '{creator.name}' (fuzzy score {score})"
    elif zero_subs:
        reason = "0 subscribers on an exact-title match — likely a squatter/decoy channel"

    return ResolvedChannel(
        creator.name, match.channel_id, match.title, match.subscriber_count, flagged, reason
    )


def main() -> None:
    resolved = [resolve_channel(c) for c in SEED_ROSTER]
    print(json.dumps([asdict(r) for r in resolved], indent=2))


if __name__ == "__main__":
    main()
