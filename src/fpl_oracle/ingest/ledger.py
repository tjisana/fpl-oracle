"""The autopilot ledger: one row per discovered video.

`data/ingest_state/ledger.json` (spec: docs/autopilot-spec.md, "Ledger").
Rows are keyed by video_id. The per-gameweek harvest state of Phase 1
(`harvest.HarvestState`) stays and is fed from the ledger by the autopilot,
so `harvest.py` is reused, not rewritten.

Gameweek assignment: a video belongs to the first deadline STRICTLY after
its `published_at` (published exactly at a deadline -> the next gameweek).

KNOWN LIMITATION (ROADMAP): a row's gameweek is assigned once, at discovery,
and never re-assigned. If FPL moves a deadline later (postponements), a
video can sit in the wrong gameweek until the row is fixed by hand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from fpl_oracle.ingest.classify import VideoKind
from fpl_oracle.ingest.harvest import DEFAULT_STATE_DIR, atomic_write

LedgerStatus = Literal["new", "pending", "ok", "needs_captions", "skipped"]


class GameweekDeadline(BaseModel):
    gameweek: int
    deadline: datetime


class LedgerRow(BaseModel):
    video_id: str
    creator_id: str
    title: str
    published_at: datetime
    duration_s: int | None = None
    gameweek: int | None = None
    kind: VideoKind
    first_seen_at: datetime
    status: LedgerStatus = "new"
    source: str | None = None
    last_error: str | None = None
    retry_after: datetime | None = None


class Ledger(BaseModel):
    rows: dict[str, LedgerRow] = Field(default_factory=dict)
    # When the last successful discovery scan finished; drives the cadence.
    last_scan_at: datetime | None = None
    updated_at: datetime | None = None

    def for_gameweek(self, gameweek: int) -> list[LedgerRow]:
        return [r for r in self.rows.values() if r.gameweek == gameweek]


def deadlines_from_events(events: Sequence[Mapping[str, object]]) -> list[GameweekDeadline]:
    """Gameweek deadlines from a bootstrap-static `events` payload (ISO-8601
    UTC with trailing Z), sorted by deadline. Events without one are dropped."""
    out: list[GameweekDeadline] = []
    for event in events:
        raw = event.get("deadline_time")
        gw = event.get("id")
        if isinstance(raw, str) and isinstance(gw, int):
            out.append(GameweekDeadline(gameweek=gw, deadline=datetime.fromisoformat(raw)))
    return sorted(out, key=lambda d: d.deadline)


def assign_gameweek(published_at: datetime, deadlines: Sequence[GameweekDeadline]) -> int | None:
    """The gameweek of the first deadline strictly after `published_at`, or
    None if every known deadline has passed."""
    later = [d for d in deadlines if d.deadline > published_at]
    if not later:
        return None
    return min(later, key=lambda d: d.deadline).gameweek


def ledger_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return state_dir / "ledger.json"


def load_ledger(state_dir: Path = DEFAULT_STATE_DIR) -> Ledger:
    """The stored ledger, or an empty one if there is no file. An unreadable
    file raises (writes are atomic, so that means damage or a hand edit)."""
    path = ledger_path(state_dir)
    if not path.exists():
        return Ledger()
    try:
        return Ledger.model_validate_json(path.read_text())
    except (ValidationError, ValueError, OSError) as e:
        raise RuntimeError(
            f"ledger: {path} is unreadable ({type(e).__name__}); fix or delete it by hand"
        ) from e


def save_ledger(ledger: Ledger, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    path = ledger_path(state_dir)
    atomic_write(path, ledger.model_dump_json(indent=2))
    return path
