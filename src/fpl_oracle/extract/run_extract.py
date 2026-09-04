"""Gameweek-agnostic extraction runner: find each creator's team video for
a TARGET gameweek, extract picks with Claude, and resolve every name
against the FPL player DB.

Per the project's hard rule, an extracted pick never keeps a bare
unresolved name: MATCHED picks get their `player_id` stamped; UNMATCHED /
AMBIGUOUS picks are kept but flagged, with the resolver's candidates
attached, in the per-video JSON (`data/extractions/{video_id}.json`) and
surfaced in the match-quality report
(`data/extractions/match_quality.md`) — the artifact the PLAN.md "review
match quality" step reads. Nothing unresolved flows further downstream;
consensus (Phase 3) consumes only MATCHED picks.

VIDEO SELECTION, GW1 vs. IN-SEASON: GW1 content was reliably titled "GW1
TEAM" — an explicit gameweek number in the title. In-season content often
drops the number ("Final Thoughts", "Team News", "My Team"), which is
dangerous to match on title alone: it would happily pick up LAST
gameweek's video too. So `is_gameweek_video` treats an explicit
"gw{N}"/"gameweek {N}"/"game week {N}" match for the TARGET gameweek as
an unconditional signal, but only accepts a GENERIC pattern (no gameweek
number) when the video was published at or after the previous gameweek's
deadline (`published_after`) — and rejects outright anything carrying an
explicit number for a DIFFERENT gameweek, even alongside a generic match.
See `is_gameweek_video`'s docstring for the exact rule.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field, ValidationError, model_validator
from youtube_transcript_api import RequestBlocked, YouTubeRequestFailed

from fpl_oracle.extract.extractor import ExtractionError, extract_picks
from fpl_oracle.extract.schemas import SquadContext, VideoExtraction
from fpl_oracle.fpl.players import MatchStatus, PlayerDB
from fpl_oracle.ingest.run_ingest import (
    eligible_creators,
    filter_min_duration,
    videos_for_creator,
)
from fpl_oracle.ingest.transcripts import Transcript, fetch_transcript, load_transcript
from fpl_oracle.ingest.youtube_client import (
    VideoInfo,
    YouTubeApiError,
    get_video_durations,
    list_recent_videos,
)
from fpl_oracle.logging_setup import configure_logging
from fpl_oracle.roster.claim_extract import channel_context_for
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.registry import REGISTRY

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("data/extractions")
VIDEOS_PER_CHANNEL = 10

# In-season titles that carry no explicit gameweek number. Dangerous on
# their own (could be LAST gameweek's video) — see `is_gameweek_video`,
# which only accepts these inside the `published_after` window. Kept as a
# module-level constant so it's reviewable/tunable on its own.
#
# "draft" is deliberately absent: FPL Draft is a different game mode whose
# picks would poison classic-mode consensus.
GENERIC_IN_SEASON_PATTERNS = (
    "final thoughts",
    "team news",
    "transfer",
    "my team",
    "wildcard",
    "team reveal",
    "team selection",
)

# Explicit gameweek-number mentions in a title, any of the four styles the
# patterns below produce ("gw2", "gw 2", "gameweek 2", "game week 2").
# Used to find OTHER gameweek numbers a title mentions (see
# `is_gameweek_video`), independent of the target gameweek.
_EXPLICIT_GW_NUMBER_RE = re.compile(
    # The optional leading letter catches BGW/DGW (blank and double
    # gameweeks). Without it `\bgw` cannot match inside "bgw3"/"dgw29" —
    # there is no word boundary between the b/d and the g — so a "BGW3
    # Team Selection" title mentioned no gameweek as far as rule 2 was
    # concerned, and the generic "team selection" pattern then accepted it
    # as THIS gameweek's video. Harmless in August, guaranteed to bite from
    # ~GW18 when BGW/DGW titling starts.
    r"\b[a-z]?gw ?(\d{1,2})\b|\bgameweek (\d{1,2})\b|\bgame week (\d{1,2})\b"
)


def gameweek_title_patterns(gw: int) -> tuple[str, ...]:
    """The explicit-number title patterns for gameweek `gw`, e.g.
    `gameweek_title_patterns(2) == ("gw2", "gw 2", "gameweek 2", "game week 2")`.
    """
    return (f"gw{gw}", f"gw {gw}", f"gameweek {gw}", f"game week {gw}")


def _gameweek_false_stems(gw: int) -> tuple[str, ...]:
    """Guard against a single-digit target matching inside a two-digit
    gameweek that starts with the same digit: "gw2" must not match "gw20"
    through "gw29" (etc. for the other three pattern prefixes). Built for
    every pattern prefix x every trailing digit 0-9. For `gw >= 10` there's
    no such collision (no three-digit gameweek exists in a 38-gameweek
    season), so the stem tuple is empty."""
    if gw >= 10:
        return ()
    prefixes = (f"gw{gw}", f"gw {gw}", f"gameweek {gw}", f"game week {gw}")
    return tuple(f"{prefix}{d}" for prefix in prefixes for d in "0123456789")


def _matches_explicit_gameweek(title: str, gw: int) -> bool:
    lowered = title.lower()
    # Strip gw20-gw29-style stems (for single-digit gw) first, then check
    # ALL patterns against the stripped title: "GW2 TEAM + GW20 PLANS"
    # must still match via its genuine "gw2", which survives the
    # stripping ("gw20" does not).
    for stem in _gameweek_false_stems(gw):
        lowered = lowered.replace(stem, " ")
    return any(p in lowered for p in gameweek_title_patterns(gw))


def _explicit_gameweek_numbers(title: str) -> set[int]:
    """Every gameweek number explicitly mentioned in `title`, via any of
    the "gw{N}" / "gw {N}" / "gameweek {N}" / "game week {N}" forms."""
    lowered = title.lower()
    numbers: set[int] = set()
    for m in _EXPLICIT_GW_NUMBER_RE.finditer(lowered):
        numbers.add(int(m.group(1) or m.group(2) or m.group(3)))
    return numbers


def _matches_generic_pattern(title: str) -> bool:
    lowered = title.lower()
    return any(p in lowered for p in GENERIC_IN_SEASON_PATTERNS)


def is_gameweek_video(
    title: str,
    gameweek: int,
    *,
    published_at: datetime | None = None,
    published_after: datetime | None = None,
) -> bool:
    """True iff `title` should be treated as `gameweek`'s team/pick video.

    1. An explicit "gw{N}"/"gameweek {N}"/"game week {N}" pattern for
       THIS gameweek matches unconditionally — the strong signal.
    2. Otherwise, an explicit gameweek number for a DIFFERENT gameweek is
       rejected outright, even if the title also carries a generic
       pattern — it's unambiguously about some other week.
    3. Otherwise, a GENERIC in-season pattern (no gameweek number at all,
       see `GENERIC_IN_SEASON_PATTERNS`) matches only if `published_at` is
       at or after `published_after` (the previous gameweek's deadline) —
       outside that window it's most likely last gameweek's video, not
       this one. `published_after` of `None` (gameweek 1: no previous
       deadline) imposes no lower bound, so a generic match is accepted
       unconditionally in that case.
    """
    if _matches_explicit_gameweek(title, gameweek):
        return True
    if _explicit_gameweek_numbers(title) - {gameweek}:
        return False
    if not _matches_generic_pattern(title):
        return False
    if published_after is None:
        return True
    return published_at is not None and published_at >= published_after


def _resolve_gameweek_and_window(
    events: list[dict], gameweek: int | None
) -> tuple[int, datetime | None]:
    """Pure resolution logic over a bootstrap-static `events` payload —
    split out from `resolve_target_gameweek` so it's unit-testable with a
    fake events list, no network.

    `gameweek=None` resolves to the event with `is_next` true. The
    returned `published_after` is the PREVIOUS event's `deadline_time`
    (`None` for gameweek 1, or if that event/its deadline is missing)."""
    if gameweek is None:
        next_ids = [e["id"] for e in events if e.get("is_next")]
        if not next_ids:
            raise ValueError(
                "FPL bootstrap-static has no event marked is_next — season may be "
                "over, or the payload is malformed"
            )
        gameweek = next_ids[0]

    if gameweek <= 1:
        return gameweek, None

    previous = next((e for e in events if e.get("id") == gameweek - 1), None)
    deadline_str = previous.get("deadline_time") if previous else None
    if deadline_str is None:
        return gameweek, None
    # bootstrap-static's deadline_time is ISO-8601 UTC ("...Z") —
    # fromisoformat parses the trailing Z into an aware datetime (3.11+),
    # which is what `is_gameweek_video` compares `published_at` against.
    # An accidental naive value here would silently crash that comparison.
    return gameweek, datetime.fromisoformat(deadline_str)


def resolve_target_gameweek(
    gameweek: int | None = None, *, force_refresh: bool = False
) -> tuple[int, datetime | None]:
    """Resolve the target gameweek (default: the FPL API's `is_next`
    event) and its `published_after` window (the PREVIOUS event's
    `deadline_time`, `None` for gameweek 1)."""
    from fpl_oracle.fpl.client import get_bootstrap_static

    bootstrap = get_bootstrap_static(force_refresh=force_refresh)
    return _resolve_gameweek_and_window(bootstrap["events"], gameweek)


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


def pick_gameweek_video(
    creator: Creator,
    registry: list[Creator],
    gameweek: int,
    published_after: datetime | None,
    force_refresh: bool = False,
) -> list[VideoInfo]:
    """`gameweek`-qualifying, non-Short videos attributed to `creator`,
    NEWEST FIRST — see `is_gameweek_video` for the explicit-vs-generic
    match rule and what `published_after` gates.

    Returns the ranked list rather than just the winner so the caller can
    fall back down it. The newest video is the right first choice, but
    "newest" and "reachable" are different things: on a YouTube IP block
    an uncached newest video loses the creator entirely, even when an
    older cached team reveal is sitting on disk. That happened for real
    on the GW2 run.

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
        v
        for v in filter_min_duration(attributed, durations)
        if is_gameweek_video(
            v.title, gameweek, published_at=v.published_at, published_after=published_after
        )
    ]
    return candidates


class VideoSelection(BaseModel):
    """One creator's video-selection outcome for a target gameweek — the
    shared unit between `run_extraction`'s selection phase and
    `--dry-run`, which stops here, before any transcript fetch or Claude
    call."""

    candidates: list[VideoInfo] = Field(
        default_factory=list,
        description=(
            "every qualifying video, newest first. `video` is the newest; the rest are "
            "fallbacks for when it cannot be read (see `_resolve_transcript`)."
        ),
    )

    @model_validator(mode="after")
    def _default_candidates_to_the_chosen_video(self) -> VideoSelection:
        """Keep `candidates` and `video` consistent in one place, so a
        caller that only knows about `video` (every pre-fallback caller,
        and every existing test) still yields a usable one-element chain
        rather than silently selecting nothing."""
        if not self.candidates and self.video is not None:
            self.candidates = [self.video]
        return self

    creator: Creator
    video: VideoInfo | None
    error: str | None = None


def select_videos(
    gameweek: int,
    published_after: datetime | None,
    creator_ids: list[str] | None = None,
    force_refresh: bool = False,
) -> list[VideoSelection]:
    """Video selection only for every eligible creator (optionally
    narrowed to `creator_ids`) — no transcript fetch, no Claude call, no
    money spent. Pure I/O-in-a-loop shared by `run_extraction` and
    `--dry-run` so the dry run reflects exactly what a real run would
    pick."""
    creators = eligible_creators(REGISTRY)
    if creator_ids is not None:
        known = {c.creator_id for c in creators}
        unknown = sorted(set(creator_ids) - known)
        if unknown:
            raise ValueError(f"unknown/ineligible creator id(s): {', '.join(unknown)}")
        creators = [c for c in creators if c.creator_id in creator_ids]

    selections = []
    for creator in creators:
        try:
            candidates = pick_gameweek_video(
                creator, REGISTRY, gameweek, published_after, force_refresh=force_refresh
            )
        except YouTubeApiError as e:
            logger.warning("run_extract: %s — video listing failed: %s", creator.creator_id, e)
            selections.append(VideoSelection(creator=creator, video=None, error=str(e)))
            continue
        selections.append(
            VideoSelection(
                creator=creator,
                video=candidates[0] if candidates else None,
                candidates=candidates,
                error=None,
            )
        )
    return selections


def _reuse_existing(
    video: VideoInfo, output_dir: Path, force: bool, creator_id: str
) -> ResolvedExtraction | None:
    """The stored extraction for `video`, if one exists and is readable.

    A truncated or corrupt file must NOT abort the run: this read used to
    sit outside the extraction try/except, so an uncaught ValidationError
    escaped run_extraction entirely and `deadline.py` reported STAGE
    FAILED — losing every creator not yet processed, and doing it again on
    every retry until a human deleted the file. Treat it as a cache miss.
    """
    path = output_dir / f"{video.video_id}.json"
    if force or not path.exists():
        return None
    try:
        return ResolvedExtraction.model_validate_json(path.read_text())
    except (ValidationError, OSError) as e:
        logger.warning(
            "run_extract: %s — cached extraction %s is unreadable (%s); re-extracting",
            creator_id,
            path.name,
            type(e).__name__,
        )
        return None


def _resolve_transcript(
    candidates: list[VideoInfo], creator_id: str
) -> tuple[VideoInfo | None, Transcript | None, bool]:
    """Walk `candidates` newest-first and return the first (video,
    transcript) pair we can actually read, plus whether a block was hit.

    NEWEST IS NOT ALWAYS REACHABLE. On the GW2 run, gianni-buttice's
    newest video was uncached, YouTube was blocking, and the creator was
    lost outright — while five older cached GW2 videos of his, including a
    deadline-day team reveal, sat on disk.

    Once a block is seen we stop making network calls for this creator and
    consider only videos already on disk. That is the important
    constraint: falling back must never mean throwing MORE requests at an
    active block, which is what prolongs it.
    """
    blocked = False
    for video in candidates:
        cached = load_transcript(video.video_id)
        if cached is not None:
            return video, cached, blocked
        if blocked:
            continue  # network is off-limits; keep scanning for a cached one
        try:
            fetched = fetch_transcript(video.video_id)
        except (RequestBlocked, YouTubeRequestFailed) as e:
            logger.warning(
                "run_extract: %s — transcript BLOCKED (%s); falling back to cached videos only",
                creator_id,
                type(e).__name__,
            )
            blocked = True
            continue
        if fetched is not None:
            return video, fetched, blocked
    return None, None, blocked


def render_match_quality(resolved: list[ResolvedExtraction], gameweek: int) -> str:
    """Markdown report for the human 'review match quality' pass."""
    lines = [f"# GW{gameweek} extraction — match quality", ""]
    total = matched = 0
    for r in resolved:
        e = r.extraction
        n_matched = sum(1 for res in r.resolutions if res.status is MatchStatus.MATCHED)
        total += len(r.resolutions)
        matched += n_matched
        gw_flag = "" if e.gameweek in (gameweek, None) else " — **WRONG GAMEWEEK?**"
        n_free_hit = sum(1 for pick in e.picks if pick.squad_context is SquadContext.FREE_HIT)
        free_hit_flag = f", free-hit picks: {n_free_hit}" if n_free_hit > 0 else ""
        lines += [
            f"## {e.creator_id} — {e.video_title} (`{e.video_id}`)",
            "",
            (
                f"- Picks: {len(r.resolutions)}, matched: {n_matched}, "
                f"gameweek: {e.gameweek}{gw_flag}{free_hit_flag}"
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
    skipped: list[str] = Field(
        default_factory=list,
        description=(
            "creator_ids whose Claude call was SKIPPED because an extraction for the "
            "chosen video already existed on disk (see --force). A skip counts toward "
            "`succeeded` (a stored extraction did result), but is tracked separately here "
            "so it's never misreported as a fresh success."
        ),
    )

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def skip_count(self) -> int:
        return len(self.skipped)


def run_extraction(
    gameweek: int,
    published_after: datetime | None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    creator_ids: list[str] | None = None,
    force_refresh: bool = False,
    force: bool = False,
) -> ExtractionRunSummary:
    """Extract + resolve across all eligible creators' `gameweek` videos
    (see `is_gameweek_video` for what qualifies, gated by
    `published_after`). Per-video failures are non-fatal and listed in the
    report; the returned `ExtractionRunSummary` carries the counts so a
    caller can tell a healthy run from one where nothing worked.

    `force_refresh` bypasses BOTH stale caches on the deadline-morning
    path: the YouTube uploads listing (so a video posted an hour ago is
    visible) and the FPL player DB that names resolve against.

    SKIP-IF-RECENT (the known money bug: "re-extraction re-pays for
    creators already done"): if `data/extractions/{video_id}.json` already
    exists for the video selection just picked, the paid Claude call is
    SKIPPED and that existing extraction is reused — unless `force` is
    passed, which always re-extracts. A retry after a mid-run failure must
    not re-bill every creator that already succeeded."""
    db = PlayerDB.load(force_refresh=force_refresh)
    selections = select_videos(
        gameweek, published_after, creator_ids=creator_ids, force_refresh=force_refresh
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[ResolvedExtraction] = []
    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    blocked_creators: list[str] = []
    report_path = output_dir / "match_quality.md"

    # The report is flushed in `finally`: even if a fail-fast error escapes
    # mid-run (e.g. RequestBlocked from transcript fetching, deliberately
    # environmental), completed extractions still produce the deliverable.
    try:
        for sel in selections:
            creator = sel.creator
            if sel.error is not None:
                failures.append((creator.creator_id, f"video listing failed: {sel.error}"))
                continue
            if not sel.candidates:
                logger.info(
                    "run_extract: %s — no gameweek %d video found", creator.creator_id, gameweek
                )
                failures.append((creator.creator_id, f"no gameweek {gameweek} video found"))
                continue

            # Skip-check the NEWEST candidate before touching the network:
            # a warm skip must cost nothing at all, not even a cached
            # transcript read.
            newest = sel.candidates[0]
            reused = _reuse_existing(newest, output_dir, force, creator.creator_id)
            if reused is not None:
                skipped.append(creator.creator_id)
                logger.info(
                    "run_extract: %s — SKIPPED (existing extraction for %s reused; "
                    "pass --force to re-extract)",
                    creator.creator_id,
                    newest.video_id,
                )
                resolved.append(reused)
                continue

            video, transcript, was_blocked = _resolve_transcript(sel.candidates, creator.creator_id)
            if video is None or transcript is None:
                reason = (
                    "transcript blocked and no cached fallback"
                    if was_blocked
                    else "no transcript for any qualifying video"
                )
                failures.append((creator.creator_id, reason))
                if was_blocked:
                    blocked_creators.append(creator.creator_id)
                continue
            if video.video_id != sel.candidates[0].video_id:
                logger.info(
                    "run_extract: %s — newest video unreadable; using cached %r (%s)",
                    creator.creator_id,
                    video.title,
                    video.published_at.date(),
                )

            # The fallback may have landed on an older video we already
            # extracted; re-checking here stops the fallback path re-paying.
            if video.video_id != newest.video_id:
                reused = _reuse_existing(video, output_dir, force, creator.creator_id)
                if reused is not None:
                    skipped.append(creator.creator_id)
                    logger.info(
                        "run_extract: %s — SKIPPED (fallback video %s already extracted)",
                        creator.creator_id,
                        video.video_id,
                    )
                    resolved.append(reused)
                    continue
            extraction_path = output_dir / f"{video.video_id}.json"

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
            # Atomic: this file is what the skip-if-recent guard trusts and
            # what the pipeline votes on. A half-written one is a paid call
            # turned into a landmine.
            tmp_path = extraction_path.with_suffix(".json.tmp")
            tmp_path.write_text(r.model_dump_json(indent=2))
            os.replace(tmp_path, extraction_path)
            logger.info(
                "run_extract: %s — %d picks (%d matched) from %r",
                creator.creator_id,
                len(r.resolutions),
                sum(1 for res in r.resolutions if res.status is MatchStatus.MATCHED),
                video.title,
            )
            resolved.append(r)
    finally:
        report = render_match_quality(resolved, gameweek)
        if failures:
            report += (
                "\n## No extraction\n\n"
                + "\n".join(f"- {cid}: {reason}" for cid, reason in failures)
                + "\n"
            )
        report_path.write_text(report)
    if blocked_creators:
        logger.warning(
            "run_extract: %d creator(s) LOST to a transcript block: %s. "
            "These paid nothing and produced nothing — retry once the block lifts.",
            len(blocked_creators),
            ", ".join(blocked_creators),
        )
    return ExtractionRunSummary(
        report_path=report_path,
        attempted=len(selections),
        succeeded=len(resolved),
        failures=failures,
        skipped=skipped,
    )


def _print_dry_run(selections: list[VideoSelection], gameweek: int) -> None:
    print(f"gameweek {gameweek} — video selection (dry run, no Claude calls, nothing spent)\n")
    found = 0
    for sel in selections:
        if sel.error is not None:
            print(f"  {sel.creator.creator_id}: ERROR — {sel.error}")
        elif sel.video is None:
            print(f"  {sel.creator.creator_id}: no video found")
        else:
            found += 1
            print(
                f"  {sel.creator.creator_id}: {sel.video.title!r} "
                f"({sel.video.video_id}, published {sel.video.published_at.isoformat()})"
            )
    print(f"\n{found}/{len(selections)} creators resolved to a video")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Extract + resolve gameweek team-reveal picks")
    parser.add_argument("creator_ids", nargs="*", help="limit to these creator ids")
    parser.add_argument(
        "--gameweek",
        type=int,
        default=None,
        help="target gameweek (default: the FPL API's next event)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="select videos and print them per creator; no transcript fetch, no Claude call",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help=(
            "bypass the 6h YouTube uploads-listing cache. REQUIRED on deadline morning: "
            "without it a run at 11:40 can reuse a 05:40 listing and never see the "
            "'final thoughts' video posted at 09:00 — silently extracting yesterday's team."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-extract even if data/extractions/{video_id}.json already exists",
    )
    args = parser.parse_args(argv)

    configure_logging()
    gameweek, published_after = resolve_target_gameweek(args.gameweek)

    if args.dry_run:
        selections = select_videos(
            gameweek,
            published_after,
            creator_ids=args.creator_ids or None,
            force_refresh=args.force_refresh,
        )
        _print_dry_run(selections, gameweek)
        return 0

    summary = run_extraction(
        gameweek,
        published_after,
        creator_ids=args.creator_ids or None,
        force=args.force,
        force_refresh=args.force_refresh,
    )
    print(
        f"extracted {summary.succeeded}/{summary.attempted} creators "
        f"({summary.failed} produced nothing, {summary.skip_count} skipped/reused)"
    )
    print(f"match-quality report written to {summary.report_path}")
    return 0 if summary.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
