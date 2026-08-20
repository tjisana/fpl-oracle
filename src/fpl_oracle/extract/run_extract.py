"""GW1 extraction runner: find each creator's team-reveal video, extract
picks with Claude, and resolve every name against the FPL player DB.

Per the project's hard rule, an extracted pick never keeps a bare
unresolved name: MATCHED picks get their `player_id` stamped; UNMATCHED /
AMBIGUOUS picks are kept but flagged, with the resolver's candidates
attached, in the per-video JSON (`data/extractions/{video_id}.json`) and
surfaced in the match-quality report
(`data/extractions/match_quality.md`) — the artifact the PLAN.md "review
match quality" step reads. Nothing unresolved flows further downstream;
consensus (Phase 3) consumes only MATCHED picks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from fpl_oracle.extract.extractor import ExtractionError, extract_picks
from fpl_oracle.extract.schemas import VideoExtraction
from fpl_oracle.fpl.players import MatchStatus, PlayerDB
from fpl_oracle.ingest.run_ingest import (
    eligible_creators,
    filter_min_duration,
    videos_for_creator,
)
from fpl_oracle.ingest.transcripts import fetch_transcript
from fpl_oracle.ingest.youtube_client import (
    VideoInfo,
    YouTubeApiError,
    get_video_durations,
    list_recent_videos,
)
from fpl_oracle.roster.claim_extract import channel_context_for
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("data/extractions")
VIDEOS_PER_CHANNEL = 10

# Recall-leaning by design: extracting from a non-team video just yields
# fewer picks; missing a creator's actual reveal loses their GW1 vote.
GW1_TITLE_PATTERNS = (
    "gw1",
    "gw 1",
    "gameweek 1",
    "team reveal",
    "my team",
    "team selection",
    "final team",
    # Titles often wedge "fpl" between the words ("FPL Salah's FINAL FPL
    # Team") — caught live on the FFH personas, whose reveals this style
    # of title would otherwise hide from the substring patterns above.
    "final fpl team",
    "my fpl team",
    "fpl team reveal",
    "wildcard",
    # "draft" is deliberately absent: FPL Draft is a different game mode
    # whose picks would poison classic-mode consensus, and GW1 reveal
    # titles overwhelmingly carry a gw1/team pattern anyway.
)
# Guard against matching later gameweeks: "gw1" must not be "gw10"-"gw19" etc.
_GW1_FALSE_STEMS = (
    tuple(f"gw1{d}" for d in "0123456789")
    + tuple(f"gw 1{d}" for d in "0123456789")
    + tuple(f"gameweek 1{d}" for d in "0123456789")
)


def is_gw1_team_video(title: str) -> bool:
    lowered = title.lower()
    # Strip gw10-gw19-style stems first, then check ALL patterns against
    # the stripped title: "GW1 TEAM + GW17 PLANS" must still match via its
    # genuine "gw1", which survives the stripping ("gw17" does not).
    for stem in _GW1_FALSE_STEMS:
        lowered = lowered.replace(stem, " ")
    return any(p in lowered for p in GW1_TITLE_PATTERNS)


class PickResolution(BaseModel):
    """Resolution outcome for one pick (parallel to extraction.picks)."""

    pick_index: int
    player_name_raw: str
    status: MatchStatus
    score: float
    matched_web_name: str | None
    candidates: list[str]


class ResolvedExtraction(BaseModel):
    """One video's extraction plus per-pick resolution — the on-disk unit."""

    extraction: VideoExtraction
    resolutions: list[PickResolution]


def resolve_extraction(extraction: VideoExtraction, db: PlayerDB) -> ResolvedExtraction:
    """Resolve every pick; stamp `player_id` on MATCHED picks only."""
    resolutions = []
    picks = []
    for i, pick in enumerate(extraction.picks):
        result = db.resolve(pick.player_name_raw, pick.team_inferred, pick.position_inferred)
        if result.status is MatchStatus.MATCHED and result.player is not None:
            picks.append(pick.model_copy(update={"player_id": result.player.player_id}))
            matched_web_name = result.player.web_name
        else:
            picks.append(pick)  # player_id stays None — flagged, never fabricated
            matched_web_name = None
        resolutions.append(
            PickResolution(
                pick_index=i,
                player_name_raw=pick.player_name_raw,
                status=result.status,
                score=result.score,
                matched_web_name=matched_web_name,
                candidates=[p.web_name for p in result.candidates],
            )
        )
    return ResolvedExtraction(
        extraction=extraction.model_copy(update={"picks": picks}), resolutions=resolutions
    )


def pick_gw1_video(
    creator: Creator, registry: list[Creator], force_refresh: bool = False
) -> VideoInfo | None:
    """Most recent GW1-team-titled, non-Short video attributed to `creator`.

    `force_refresh` bypasses the uploads-listing cache — see
    `list_recent_videos`. Needed on deadline morning, when the video that
    matters may have been posted since the cache was written."""
    assert creator.channel_id is not None
    videos = list_recent_videos(
        creator.channel_id, max_results=VIDEOS_PER_CHANNEL, force_refresh=force_refresh
    )
    co_creators = [
        c
        for c in registry
        if c.channel_id == creator.channel_id and c.creator_id != creator.creator_id
    ]
    attributed = videos_for_creator(creator, co_creators, videos)
    try:
        durations = get_video_durations([v.video_id for v in attributed])
    except YouTubeApiError as e:
        logger.warning("run_extract: duration lookup failed (%s); keeping all", e)
        durations = {}
    candidates = [
        v for v in filter_min_duration(attributed, durations) if is_gw1_team_video(v.title)
    ]
    return candidates[0] if candidates else None


def render_match_quality(resolved: list[ResolvedExtraction]) -> str:
    """Markdown report for the human 'review match quality' pass."""
    lines = ["# GW1 extraction — match quality", ""]
    total = matched = 0
    for r in resolved:
        e = r.extraction
        n_matched = sum(1 for res in r.resolutions if res.status is MatchStatus.MATCHED)
        total += len(r.resolutions)
        matched += n_matched
        gw_flag = "" if e.gameweek in (1, None) else " — **WRONG GAMEWEEK?**"
        lines += [
            f"## {e.creator_id} — {e.video_title} (`{e.video_id}`)",
            "",
            (
                f"- Picks: {len(r.resolutions)}, matched: {n_matched}, "
                f"gameweek: {e.gameweek}{gw_flag}"
            ),
        ]
        for res in r.resolutions:
            if res.status is not MatchStatus.MATCHED:
                pick = e.picks[res.pick_index]
                lines.append(
                    f"- **{res.status.value.upper()}**: {res.player_name_raw!r} "
                    f"(team={pick.team_inferred}, pos={pick.position_inferred}, "
                    f"score={res.score:.1f}) candidates: {', '.join(res.candidates) or '-'}"
                )
        lines.append("")
    pct = (100 * matched / total) if total else 0
    lines.insert(1, f"\n**{matched}/{total} picks matched ({pct:.0f}%)**\n")
    return "\n".join(lines)


class ExtractionRunSummary(BaseModel):
    """The outcome of one `run_extraction`, not just where it wrote the
    report.

    Per-creator failures are non-fatal by design — one creator's missing
    video must not cost the other seventeen. But that means a run in which
    EVERY creator failed returns just as quietly as a perfect one, and a
    caller that treats "no exception raised" as success (the deadline-
    morning chain did) goes on to solve whatever extractions happen to be
    on disk — yesterday's. The counts are the only thing that can tell
    those two runs apart, so they are returned rather than merely logged.
    """

    report_path: Path
    attempted: int = Field(description="creators this run tried to extract")
    succeeded: int = Field(description="creators that produced a stored extraction")
    failures: list[tuple[str, str]] = Field(
        default_factory=list, description="(creator_id, reason) for each creator that produced none"
    )

    @property
    def failed(self) -> int:
        return len(self.failures)


def run_extraction(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    creator_ids: list[str] | None = None,
    force_refresh: bool = False,
) -> ExtractionRunSummary:
    """Extract + resolve across all eligible creators' GW1 videos. Per-video
    failures are non-fatal and listed in the report; the returned
    `ExtractionRunSummary` carries the counts so a caller can tell a
    healthy run from one where nothing worked.

    `force_refresh` bypasses BOTH stale caches on the deadline-morning
    path: the YouTube uploads listing (so a video posted an hour ago is
    visible) and the FPL player DB that names resolve against."""
    db = PlayerDB.load(force_refresh=force_refresh)
    creators = eligible_creators(REGISTRY)
    if creator_ids is not None:
        known = {c.creator_id for c in creators}
        unknown = sorted(set(creator_ids) - known)
        if unknown:
            raise ValueError(f"unknown/ineligible creator id(s): {', '.join(unknown)}")
        creators = [c for c in creators if c.creator_id in creator_ids]

    output_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[ResolvedExtraction] = []
    failures: list[tuple[str, str]] = []
    report_path = output_dir / "match_quality.md"

    # The report is flushed in `finally`: even if a fail-fast error escapes
    # mid-run (e.g. RequestBlocked from transcript fetching, deliberately
    # environmental), completed extractions still produce the deliverable.
    try:
        for creator in creators:
            try:
                video = pick_gw1_video(creator, REGISTRY, force_refresh=force_refresh)
            except YouTubeApiError as e:
                logger.warning("run_extract: %s — video listing failed: %s", creator.creator_id, e)
                failures.append((creator.creator_id, f"video listing failed: {e}"))
                continue
            if video is None:
                logger.info("run_extract: %s — no GW1 team video found", creator.creator_id)
                failures.append((creator.creator_id, "no GW1 team video found"))
                continue
            transcript = fetch_transcript(video.video_id)
            if transcript is None:
                failures.append((creator.creator_id, f"no transcript for video {video.video_id}"))
                continue
            try:
                extraction = extract_picks(
                    creator_id=creator.creator_id,
                    creator_name=creator.name,
                    video_id=video.video_id,
                    video_title=video.title,
                    published_at=video.published_at,
                    transcript_text=transcript.full_text,
                    channel_context=channel_context_for(creator.creator_id),
                )
            except (ExtractionError, anthropic.APIError) as e:
                logger.warning("run_extract: %s — extraction FAILED: %s", creator.creator_id, e)
                failures.append((creator.creator_id, str(e)))
                continue
            r = resolve_extraction(extraction, db)
            (output_dir / f"{video.video_id}.json").write_text(r.model_dump_json(indent=2))
            logger.info(
                "run_extract: %s — %d picks (%d matched) from %r",
                creator.creator_id,
                len(r.resolutions),
                sum(1 for res in r.resolutions if res.status is MatchStatus.MATCHED),
                video.title,
            )
            resolved.append(r)
    finally:
        report = render_match_quality(resolved)
        if failures:
            report += (
                "\n## No extraction\n\n"
                + "\n".join(f"- {cid}: {reason}" for cid, reason in failures)
                + "\n"
            )
        report_path.write_text(report)
    return ExtractionRunSummary(
        report_path=report_path,
        attempted=len(creators),
        succeeded=len(resolved),
        failures=failures,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Extract + resolve GW1 team-reveal picks")
    parser.add_argument("creator_ids", nargs="*", help="limit to these creator ids")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = run_extraction(creator_ids=args.creator_ids or None)
    print(
        f"extracted {summary.succeeded}/{summary.attempted} creators "
        f"({summary.failed} produced nothing)"
    )
    print(f"match-quality report written to {summary.report_path}")
    return 0 if summary.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
