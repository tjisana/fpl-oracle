"""Autopilot tick: decide if a scan is due, discover, classify, spend the
Gemini budget by policy, record everything in the ledger.

    uv run python -m fpl_oracle.ingest.autopilot tick [--now ISO] [--no-captions] [--dry-run]

Spec: docs/autopilot-spec.md. One tick is one short-lived process (an hourly
scheduler runs it; the cadence in `cadence.is_due` decides whether it acts).

Transcript fetching REUSES `harvest.run_harvest` one video at a time (so the
request counters, 429/503 handling, backoff and atomic state writes are
Phase 1's, unchanged). The autopilot adds what Phase 1 does not have:
several videos per creator, the budget policy (priority + final-window
reserve, `autopilot_budget`) and the ledger. The per-gameweek harvest state
file is fed by the harvester itself.

`--no-captions` is the AWS mode: Gemini only. A video Gemini cannot cover
(only "too long for Gemini" today) becomes `needs_captions`, not a failure.
Everything else stays `pending` and is retried: 503s after their backoff,
a Gemini error on the next quota day (Phase 1 semantics), daily-limit and
reserve-skipped videos when budget allows. A key-level Gemini failure or a
missing key stops ALL Gemini spending for the rest of the tick. Without the
flag the Phase 1 fallback applies (captions), and `needs_captions` rows are
retried.

SCAN vs SPEND. Discovery runs only when `cadence.is_due`; the spend step
runs on EVERY tick that has a workable row (retry timer expired), so a 503
at the 08:10 scan is retried by the next hourly tick, not a day later.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from fpl_oracle.ingest.autopilot_budget import (
    BudgetPolicy,
    PlannedSpend,
    daily_cap,
    daily_limit,
    daily_remaining,
    daily_used,
    demoted_video_ids,
    is_protected_day,
    may_spend,
    order_candidates,
    plan_spend,
)
from fpl_oracle.ingest.cadence import is_due, next_gameweek
from fpl_oracle.ingest.classify import Classifier, is_skipped_kind
from fpl_oracle.ingest.discovery import UploadsClient, YouTubeUploadsClient, discover
from fpl_oracle.ingest.gemini_transcripts import gemini_api_key_present
from fpl_oracle.ingest.harvest import (
    REASON_BUDGET,
    REASON_TOO_LONG,
    WARN_BELOW_WEIGHT_FRACTION,
    CaptionReader,
    CreatorTarget,
    GeminiReader,
    GeminiUsageFile,
    HarvestConfig,
    HarvestLockedError,
    RunFlags,
    VideoRef,
    VideoState,
    default_caption_reader,
    default_gemini_reader,
    in_backstop,
    load_state,
    load_usage,
    quota_day,
    request_cost,
    run_harvest,
    run_lock,
)
from fpl_oracle.ingest.ledger import (
    GameweekDeadline,
    LedgerRow,
    load_ledger,
    save_ledger,
)
from fpl_oracle.ingest.status import build_readiness, write_readiness
from fpl_oracle.ingest.transcripts import load_transcript
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.weights import DEFAULT_TIER2_WEIGHT

logger = logging.getLogger(__name__)

# Why a video left Gemini that more waiting will not fix: in no-captions
# mode it becomes `needs_captions`.
NEEDS_CAPTIONS_REASONS = frozenset({REASON_TOO_LONG})

# Statuses a tick will try to spend on.
_WORKABLE = ("new", "pending")


class VideoOutcome(BaseModel):
    video_id: str
    creator_id: str
    title: str
    status: str
    note: str = ""


class TickResult(BaseModel):
    outcome: Literal["locked", "no_gameweek", "not_due", "ran", "dry_run"]
    gameweek: int | None = None
    hours_to_deadline: float | None = None
    protected_day: bool = False
    scan_due: bool = True  # False only on a --dry-run of a tick that is not due
    low_coverage: bool = False
    weight_covered_fraction: float | None = None
    new_videos: int = 0
    channels_failed: int = 0
    requests_this_run: int = 0
    outcomes: list[VideoOutcome] = Field(default_factory=list)
    planned: list[PlannedSpend] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AutopilotConfig(BaseModel):
    harvest: HarvestConfig = Field(default_factory=HarvestConfig)
    policy: BudgetPolicy = Field(default_factory=BudgetPolicy)


def creator_weights(registry: Sequence[Creator]) -> dict[str, float]:
    return {
        c.creator_id: (c.weight if c.weight is not None else DEFAULT_TIER2_WEIGHT) for c in registry
    }


def apply_harvest_state(
    row: LedgerRow, vs: VideoState, *, captions_enabled: bool, now: datetime
) -> None:
    """Fold the harvester's per-video state back into the ledger row."""
    if vs.status == "ok":
        row.status, row.source = "ok", vs.source
        row.last_error, row.retry_after = None, None
        return
    row.retry_after = vs.retry_after if vs.retry_after and vs.retry_after > now else None
    row.last_error = vs.last_error
    if vs.status == "failed":
        row.status = "pending"  # captions found nothing; retried by later runs
    elif (
        not captions_enabled
        and row.retry_after is None
        and vs.fell_back_reason in NEEDS_CAPTIONS_REASONS
    ):
        row.status = "needs_captions"
    else:
        row.status = "pending"


def _sync_cached(rows: Sequence[LedgerRow], transcript_dir: Path) -> None:
    """Reconcile rows with the transcript cache: a cached transcript means
    done (any source); an `ok` row whose cache file is gone goes back to
    `pending` (mirrors Phase 1 `_sync_state_with_cache`). Mutates rows in
    memory only; the caller decides whether to save."""
    for row in rows:
        if row.status == "skipped":
            continue
        cached = load_transcript(row.video_id, dir=transcript_dir)
        if cached is not None:
            row.status, row.source = "ok", cached.source
            row.last_error, row.retry_after = None, None
        elif row.status == "ok":
            row.status, row.source = "pending", None
            row.last_error = "transcript missing from the cache; re-fetching"


def _candidates(
    rows: Sequence[LedgerRow], *, captions_enabled: bool, now: datetime
) -> list[LedgerRow]:
    statuses = _WORKABLE if not captions_enabled else (*_WORKABLE, "needs_captions")
    return [
        r
        for r in rows
        if r.status in statuses
        and not is_skipped_kind(r.kind)
        and (r.retry_after is None or r.retry_after <= now)
    ]


def tick(
    *,
    clock: Callable[[], datetime],
    deadlines: Sequence[GameweekDeadline],
    registry: Sequence[Creator],
    uploads: UploadsClient,
    config: AutopilotConfig | None = None,
    captions_enabled: bool = True,
    dry_run: bool = False,
    classifier: Classifier | None = None,
    gemini_reader: GeminiReader = default_gemini_reader,
    caption_reader: CaptionReader = default_caption_reader,
    gemini_available: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TickResult:
    """One autopilot tick. Takes the shared run lock for the whole tick (a
    tick that finds it held returns outcome "locked" without doing
    anything). `dry_run` still lists uploads (read-only) but writes nothing
    and spends nothing."""
    config = config or AutopilotConfig()
    if dry_run:
        return _tick_body(
            clock, deadlines, registry, uploads, config, captions_enabled, True,
            classifier, gemini_reader, caption_reader, gemini_available, sleep,
        )  # fmt: skip
    try:
        with run_lock(config.harvest.state_dir):
            return _tick_body(
                clock, deadlines, registry, uploads, config, captions_enabled, False,
                classifier, gemini_reader, caption_reader, gemini_available, sleep,
            )  # fmt: skip
    except HarvestLockedError as e:
        logger.info("autopilot: %s", e)
        return TickResult(outcome="locked", errors=[str(e)])


def usage_total(usage: GeminiUsageFile) -> int:
    """Every request ever counted (all models, all days): its growth across a
    tick is the tick's spend, safe across a Pacific midnight."""
    return sum(sum(m.requests.values()) for m in usage.models.values())


def _tick_body(
    clock: Callable[[], datetime],
    deadlines: Sequence[GameweekDeadline],
    registry: Sequence[Creator],
    uploads: UploadsClient,
    config: AutopilotConfig,
    captions_enabled: bool,
    dry_run: bool,
    classifier: Classifier | None,
    gemini_reader: GeminiReader,
    caption_reader: CaptionReader,
    gemini_available: Callable[[], bool] | None,
    sleep: Callable[[float], None],
) -> TickResult:
    hconf, policy = config.harvest, config.policy
    now = clock()
    target = next_gameweek(list(deadlines), now)
    if target is None:
        return TickResult(outcome="no_gameweek")
    gameweek, deadline = target.gameweek, target.deadline
    hours_to = (deadline - now).total_seconds() / 3600

    ledger = load_ledger(hconf.state_dir)
    scan_due = is_due(now, deadline, ledger.last_scan_at)
    result = TickResult(
        outcome="dry_run" if dry_run else "ran",
        scan_due=scan_due,
        protected_day=is_protected_day(now, deadline, policy),
        gameweek=gameweek,
        hours_to_deadline=hours_to,
    )
    # A dry run always scans (read-only, in memory) so it can show what WOULD
    # be planned even when the cadence says it is not due.
    if scan_due or dry_run:
        found = discover(ledger, registry, uploads, deadlines, now, classifier)
        if found.channels_scanned > 0:
            ledger.last_scan_at = now
        result.new_videos = found.new_videos
        result.channels_failed = found.channels_failed
        result.errors = list(found.errors)

    gw_rows = ledger.for_gameweek(gameweek)
    _sync_cached(gw_rows, hconf.transcript_dir)  # dry run: in memory, never saved
    ordered = order_candidates(
        _candidates(gw_rows, captions_enabled=captions_enabled, now=now),
        creator_weights(registry),
        demoted_video_ids(gw_rows),
    )
    if not scan_due and not dry_run and not ordered:
        return TickResult(outcome="not_due", gameweek=gameweek, hours_to_deadline=hours_to)
    limits = hconf.gemini_daily_limits

    if dry_run:
        usage = load_usage(hconf.state_dir)
        result.planned = plan_spend(
            [r for r in ordered if r.duration_s is not None],
            used_today=daily_used(usage, limits, quota_day(now)),
            limit=daily_limit(limits),
            now=now,
            deadline=deadline,
            policy=policy,
        )
        return result

    gemini_usable = (gemini_available or gemini_api_key_present)()
    today = quota_day(clock())
    start_usage = load_usage(hconf.state_dir)
    if limits and all(start_usage.key_failed(m, today) for m in limits):
        gemini_usable = False  # a key-level error already hit today: do not burn a request
    captions_blocked = False
    caption_calls = 0
    total_before = usage_total(load_usage(hconf.state_dir))
    for row in ordered:
        now = clock()
        usage = load_usage(hconf.state_dir)
        cost = request_cost(row.duration_s)
        used = daily_used(usage, limits, quota_day(now))
        limit = daily_limit(limits)
        # An unknown duration cannot be costed: wait for a scan to fill it.
        affordable = row.duration_s is not None and may_spend(
            row, cost, used_today=used, now=now, deadline=deadline, limit=limit, policy=policy
        )
        use_gemini = gemini_usable
        off_reason: str | None = None
        allowance: int | None = None
        if affordable:
            if cost is not None:
                allowance = max(0, daily_cap(row, now, deadline, limit, policy) - used)
        else:
            # Held back by the reserve rule (or an unknown duration). Only the
            # deadline backstop overrides it, and only through captions.
            if not (captions_enabled and in_backstop(deadline, now, hconf.backstop_hours)):
                continue
            use_gemini = False
            off_reason = REASON_BUDGET
        flags = RunFlags(
            gemini_available=use_gemini,
            captions_blocked=captions_blocked,
            max_requests=allowance,
            caption_calls=caption_calls,
            gemini_off_reason=off_reason,
        )
        _spend_one(
            row, gameweek, deadline, hconf, captions_enabled, clock,
            gemini_reader, caption_reader, flags, sleep,
        )  # fmt: skip
        captions_blocked = flags.captions_blocked
        caption_calls = flags.caption_calls
        if use_gemini and not flags.gemini_available:
            gemini_usable = False  # missing key / key-level error: no more Gemini this tick
        result.outcomes.append(
            VideoOutcome(
                video_id=row.video_id,
                creator_id=row.creator_id,
                title=row.title,
                status=row.status,
                note=row.last_error or "",
            )
        )
        ledger.updated_at = clock()
        save_ledger(ledger, hconf.state_dir)
        if not gemini_usable and not captions_enabled:
            break  # nothing else can be done without Gemini or captions

    if not scan_due and not result.outcomes:
        return TickResult(outcome="not_due", gameweek=gameweek, hours_to_deadline=hours_to)
    after = load_usage(hconf.state_dir)
    result.requests_this_run = max(0, usage_total(after) - total_before)
    ledger.updated_at = clock()
    save_ledger(ledger, hconf.state_dir)

    now = clock()
    report = build_readiness(
        gameweek,
        deadline,
        ledger,
        registry,
        now=now,
        gemini_left=daily_remaining(after, limits, quota_day(now)),
        backstop_hours=None if captions_enabled else hconf.backstop_hours,
    )
    write_readiness(report, hconf.state_dir)
    result.low_coverage = report.low_coverage
    result.weight_covered_fraction = report.covered_share
    if report.low_coverage:
        logger.warning(
            "autopilot: LOW COVERAGE gw%d (%.0f%%)", gameweek, report.covered_share * 100
        )
    return result


def _spend_one(
    row: LedgerRow,
    gameweek: int,
    deadline: datetime,
    hconf: HarvestConfig,
    captions_enabled: bool,
    clock: Callable[[], datetime],
    gemini_reader: GeminiReader,
    caption_reader: CaptionReader,
    flags: RunFlags,
    sleep: Callable[[float], None],
) -> None:
    """Run the Phase 1 harvester on ONE video and fold the outcome into the
    ledger row (the lock is already held by `tick`). `flags` carries the
    Gemini-usable / captions-blocked / request-cap state in and out."""
    target = CreatorTarget(
        creator_id=row.creator_id,
        name=row.creator_id,
        weight=1.0,
        video=VideoRef(video_id=row.video_id, title=row.title, duration_seconds=row.duration_s),
    )
    run_harvest(
        gameweek,
        [target],
        deadline,
        hconf,
        now=clock,
        sleep=sleep,
        gemini_reader=gemini_reader,
        caption_reader=caption_reader,
        gemini_available=lambda: bool(flags.gemini_available),
        captions_enabled=captions_enabled,
        take_lock=False,
        flags=flags,
    )
    state = load_state(gameweek, hconf.state_dir)
    vs = state.videos.get(row.video_id)
    if vs is not None:
        apply_harvest_state(row, vs, captions_enabled=captions_enabled, now=clock())


# --------------------------------------------------------------------------
# Report and CLI
# --------------------------------------------------------------------------


def print_tick(result: TickResult, console: Console | None = None) -> None:
    console = console or Console()
    if result.outcome == "locked":
        console.print("autopilot: another tick holds the lock; nothing done.")
        return
    if result.outcome == "no_gameweek":
        console.print("autopilot: no upcoming gameweek deadline; nothing to do.")
        return
    hours = result.hours_to_deadline
    head = f"GW{result.gameweek}, {hours:.1f} h to deadline" if hours is not None else ""
    if result.outcome == "not_due":
        console.print(f"autopilot: scan not due and nothing to retry ({head}).")
        return
    dry = " (DRY RUN, nothing spent or written)" if result.outcome == "dry_run" else ""
    if result.outcome == "dry_run" and not result.scan_due:
        dry += " - scan NOT due now: this is what WOULD be planned if it were"
    console.print(
        f"autopilot tick{dry}: {head}; protected day: {result.protected_day}; "
        f"{result.new_videos} new video(s); {result.channels_failed} channel(s) failed."
    )
    if result.planned:
        table = Table(title="Planned Gemini spend")
        for col in ("Creator", "Kind", "Cost", "Cap", "Title"):
            table.add_column(col)
        for p in result.planned:
            table.add_row(
                p.creator_id,
                p.kind,
                "-" if p.cost is None else str(p.cost),
                str(p.cap),
                p.title,
            )
        console.print(table)
    if result.outcomes:
        table = Table(title="Tick outcomes")
        for col in ("Creator", "Status", "Title", "Note"):
            table.add_column(col)
        for o in result.outcomes:
            table.add_row(o.creator_id, o.status, o.title, o.note)
        console.print(table)
    console.print(f"Gemini requests this run: {result.requests_this_run}.")
    if result.low_coverage and result.weight_covered_fraction is not None:
        console.print(
            f"!!! LOW COVERAGE: only {result.weight_covered_fraction * 100:.0f}% of creator "
            f"weight has a transcript with {hours:.1f} h to the deadline "
            f"(< {WARN_BELOW_WEIGHT_FRACTION * 100:.0f}%) !!!",
            style="bold red",
        )
    for err in result.errors:
        console.print(f"error: {err}", style="red")


def parse_now(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Autopilot: scheduled harvest tick")
    sub = parser.add_subparsers(dest="command", required=True)
    tick_p = sub.add_parser("tick", help="run one tick (scans only if due)")
    tick_p.add_argument("--now", default=None, help="ISO timestamp to act as 'now' (default: real)")
    tick_p.add_argument(
        "--no-captions",
        action="store_true",
        help="Gemini only (AWS mode): uncovered videos become needs_captions",
    )
    tick_p.add_argument(
        "--dry-run", action="store_true", help="plan only: no spend, no state written"
    )
    args = parser.parse_args(argv)
    if args.now is not None and not args.dry_run:
        # A fake "now" would be persisted as last_scan_at (and the harvest
        # usage days), poisoning the cadence with a time that may be in the future.
        parser.error("--now is only allowed together with --dry-run")

    from fpl_oracle.fpl.client import get_bootstrap_static
    from fpl_oracle.ingest.ledger import deadlines_from_events
    from fpl_oracle.logging_setup import configure_logging
    from fpl_oracle.roster.registry import REGISTRY

    configure_logging()
    if args.now is not None:
        fixed = parse_now(args.now)
        clock: Callable[[], datetime] = lambda: fixed
    else:
        clock = lambda: datetime.now(UTC)
    result = tick(
        clock=clock,
        deadlines=deadlines_from_events(get_bootstrap_static()["events"]),
        registry=REGISTRY,
        uploads=YouTubeUploadsClient(),
        captions_enabled=not args.no_captions,
        dry_run=args.dry_run,
    )
    print_tick(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
