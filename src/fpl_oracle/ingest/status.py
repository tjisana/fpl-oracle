"""Harvest readiness report: "which creators' videos are in, how fresh?"

    uv run python -m fpl_oracle.ingest.status [--gameweek N] [--from-s3]

Spec: docs/autopilot-spec.md, "harvest status". Per creator (weight order),
for the gameweek's RELEVANT videos (kinds selection / final / transfers /
plan; `other` is harvested opportunistically but is not evidence of picks):

- FRESH    the newest relevant video is transcribed
- STALE    the newest is not, but an older relevant one is
- MISSING  no relevant video yet, or relevant videos exist but none has
           been transcribed yet (the note says which)

Verdict: READY if >= 80% of creator weight is FRESH, PARTIAL if >= 50% is
FRESH + STALE, else THIN. Also written to `ingest_state/readiness.json`.
Informational: the exit code is 0.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from fpl_oracle.ingest.autopilot_budget import daily_remaining
from fpl_oracle.ingest.classify import RELEVANT_KINDS
from fpl_oracle.ingest.harvest import (
    DEFAULT_BACKSTOP_HOURS,
    DEFAULT_GEMINI_DAILY_LIMITS,
    DEFAULT_STATE_DIR,
    WARN_BELOW_WEIGHT_FRACTION,
    atomic_write,
    load_usage,
    quota_day,
)
from fpl_oracle.ingest.ledger import GameweekDeadline, Ledger, LedgerRow, load_ledger
from fpl_oracle.ingest.run_ingest import eligible_creators
from fpl_oracle.roster.models import Creator
from fpl_oracle.roster.weights import DEFAULT_TIER2_WEIGHT

READY_FRESH_SHARE = 0.8
PARTIAL_COVERED_SHARE = 0.5
_EPS = 1e-9


class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


class Verdict(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    THIN = "THIN"


class CreatorReadiness(BaseModel):
    creator_id: str
    name: str
    weight: float
    weight_share: float
    state: Freshness
    newest_title: str | None = None
    newest_hours_old: float | None = None
    transcribed_title: str | None = None
    transcribed_hours_old: float | None = None
    source: str | None = None
    note: str = ""


class ReadinessReport(BaseModel):
    gameweek: int
    generated_at: datetime
    hours_to_deadline: float | None
    fresh_share: float
    covered_share: float  # FRESH + STALE
    verdict: Verdict
    gemini_budget_left: int
    # LOW COVERAGE (spec Budget policy 6): within the backstop window and the
    # creator weight with a transcript (FRESH + STALE) is below the Phase 1
    # warning threshold. Only evaluated when the caller asks (AWS mode).
    low_coverage: bool = False
    creators: list[CreatorReadiness] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def readiness_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return state_dir / "readiness.json"


def verdict_for(fresh_share: float, covered_share: float) -> Verdict:
    if fresh_share >= READY_FRESH_SHARE - _EPS:
        return Verdict.READY
    if covered_share >= PARTIAL_COVERED_SHARE - _EPS:
        return Verdict.PARTIAL
    return Verdict.THIN


def _hours_old(published_at: datetime, now: datetime) -> float:
    return (now - published_at).total_seconds() / 3600


def _retry_note(row: LedgerRow) -> str:
    if row.retry_after is None:
        return ""
    return f", retry after {row.retry_after.isoformat(timespec='minutes')}"


def _creator_readiness(
    creator: Creator, weight: float, share: float, rows: Sequence[LedgerRow], now: datetime
) -> CreatorReadiness:
    relevant = sorted(
        (
            r
            for r in rows
            if r.creator_id == creator.creator_id
            and r.kind in RELEVANT_KINDS
            and r.status != "skipped"
        ),
        key=lambda r: r.published_at,
        reverse=True,
    )
    entry = CreatorReadiness(
        creator_id=creator.creator_id,
        name=creator.name,
        weight=weight,
        weight_share=share,
        state=Freshness.MISSING,
        note="no relevant video yet for this gameweek",
    )
    if not relevant:
        return entry
    newest = relevant[0]
    done = next((r for r in relevant if r.status == "ok"), None)
    entry.newest_title = newest.title
    entry.newest_hours_old = _hours_old(newest.published_at, now)
    if done is None:
        entry.note = f"newest video is {newest.status}{_retry_note(newest)}"
        return entry
    entry.transcribed_title = done.title
    entry.transcribed_hours_old = _hours_old(done.published_at, now)
    entry.source = done.source
    if done.video_id == newest.video_id:
        entry.state, entry.note = Freshness.FRESH, ""
    else:
        entry.state = Freshness.STALE
        entry.note = f"newer video is {newest.status}{_retry_note(newest)}"
    return entry


def build_readiness(
    gameweek: int,
    deadline: datetime | None,
    ledger: Ledger,
    registry: Sequence[Creator],
    *,
    now: datetime,
    gemini_left: int,
    backstop_hours: float | None = None,
) -> ReadinessReport:
    creators = eligible_creators(list(registry))
    weights = {
        c.creator_id: (c.weight if c.weight is not None else DEFAULT_TIER2_WEIGHT) for c in creators
    }
    total = sum(weights.values())
    rows = ledger.for_gameweek(gameweek)
    entries = [
        _creator_readiness(
            c,
            weights[c.creator_id],
            (weights[c.creator_id] / total if total > 0 else 0.0),
            rows,
            now,
        )
        for c in creators
    ]
    entries.sort(key=lambda e: (-e.weight, e.creator_id))
    fresh = sum(e.weight_share for e in entries if e.state is Freshness.FRESH)
    covered = sum(e.weight_share for e in entries if e.state is not Freshness.MISSING)
    hours = None if deadline is None else (deadline - now).total_seconds() / 3600
    low = (
        backstop_hours is not None
        and hours is not None
        and 0 <= hours <= backstop_hours
        and covered < WARN_BELOW_WEIGHT_FRACTION
    )
    return ReadinessReport(
        gameweek=gameweek,
        generated_at=now,
        hours_to_deadline=hours,
        low_coverage=low,
        fresh_share=fresh,
        covered_share=covered,
        verdict=verdict_for(fresh, covered),
        gemini_budget_left=gemini_left,
        creators=entries,
        missing=[e.creator_id for e in entries if e.state is Freshness.MISSING],
    )


def write_readiness(report: ReadinessReport, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    path = readiness_path(state_dir)
    atomic_write(path, report.model_dump_json(indent=2))
    return path


def print_status(report: ReadinessReport, console: Console | None = None) -> None:
    console = console or Console()
    hours = report.hours_to_deadline
    deadline_txt = f"{hours:.1f} h to deadline" if hours is not None else "deadline unknown"
    table = Table(title=f"GW{report.gameweek} harvest readiness - {deadline_txt}")
    for col in ("Creator", "Weight", "Share", "State", "Newest relevant video", "Transcribed"):
        table.add_column(col)
    style = {Freshness.FRESH: "green", Freshness.STALE: "yellow", Freshness.MISSING: "red"}
    for e in report.creators:
        newest = (
            f"{e.newest_title} ({e.newest_hours_old:.0f} h old)"
            if e.newest_title is not None and e.newest_hours_old is not None
            else "-"
        )
        done = (
            f"{e.source or '?'}, {e.transcribed_hours_old:.0f} h old"
            if e.transcribed_hours_old is not None
            else e.note or "-"
        )
        table.add_row(
            e.creator_id,
            f"{e.weight:.2f}",
            f"{e.weight_share * 100:.0f}%",
            f"[{style[e.state]}]{e.state.value}[/]",
            newest,
            f"{done}; {e.note}" if e.transcribed_hours_old is not None and e.note else done,
        )
    console.print(table)
    console.print(
        f"{deadline_txt}. FRESH {report.fresh_share * 100:.0f}% of weight, "
        f"FRESH+STALE {report.covered_share * 100:.0f}%. "
        f"Gemini requests left today: {report.gemini_budget_left}."
    )
    console.print(f"Verdict: {report.verdict.value}")
    if report.low_coverage:
        console.print(
            f"!!! LOW COVERAGE: {report.covered_share * 100:.0f}% of weight has a transcript "
            f"inside the backstop window !!!",
            style="bold red",
        )
    if report.missing:
        console.print(f"Missing: {', '.join(report.missing)}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Harvest readiness report for a gameweek")
    parser.add_argument("--gameweek", type=int, default=None, help="default: the next gameweek")
    parser.add_argument(
        "--from-s3", action="store_true", help="(M2, not implemented) pull the ledger from S3"
    )
    args = parser.parse_args(argv)
    if args.from_s3:
        raise NotImplementedError(
            "--from-s3 arrives with M2 (the AWS stack); run status against the local "
            "data/ingest_state/ledger.json for now"
        )

    from fpl_oracle.fpl.client import get_bootstrap_static
    from fpl_oracle.ingest.cadence import next_gameweek
    from fpl_oracle.ingest.ledger import deadlines_from_events
    from fpl_oracle.roster.registry import REGISTRY

    now = datetime.now(UTC)
    deadlines = deadlines_from_events(get_bootstrap_static()["events"])
    gameweek = args.gameweek
    deadline = _deadline_of(deadlines, gameweek) if gameweek is not None else None
    if gameweek is None:
        nxt = next_gameweek(deadlines, now)
        if nxt is None:
            print("status: no upcoming gameweek", file=sys.stderr)
            return 0
        gameweek, deadline = nxt.gameweek, nxt.deadline
    usage = load_usage(DEFAULT_STATE_DIR)
    report = build_readiness(
        gameweek,
        deadline,
        load_ledger(DEFAULT_STATE_DIR),
        REGISTRY,
        now=now,
        backstop_hours=DEFAULT_BACKSTOP_HOURS,
        gemini_left=daily_remaining(usage, DEFAULT_GEMINI_DAILY_LIMITS, quota_day(now)),
    )
    write_readiness(report, DEFAULT_STATE_DIR)
    print_status(report)
    return 0


def _deadline_of(deadlines: Sequence[GameweekDeadline], gameweek: int) -> datetime | None:
    return next((d.deadline for d in deadlines if d.gameweek == gameweek), None)


if __name__ == "__main__":
    sys.exit(main())
