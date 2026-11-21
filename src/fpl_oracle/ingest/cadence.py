"""When is an autopilot tick due to scan? Pure functions, no clock reads.

Cadence by hours to the target gameweek deadline (spec "Cadence"):

| hours_to          | scan                                    |
|-------------------|-----------------------------------------|
| > 72              | once a day, at 08:10 UTC                |
| 72 down to 6      | every 3 h                               |
| 6 down to 0       | every hour                              |
| < 0               | the next gameweek is targeted; daily    |

On top of that, the 08:10 UTC daily scan runs in EVERY band, so a Gemini
quota reset is never wasted.

The scheduler fires hourly with jitter, so "every 3 h" is judged with a
`slack` (default 5 min): a tick arriving 2 h 57 min after the last scan
counts as 3 h. Without it a scan at 10:10:30 would miss the 13:10:05 tick
and slip to 14:10. The daily rule is anchor-based, not interval-based: due
when the last scan predates the most recent 08:10 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from fpl_oracle.ingest.ledger import GameweekDeadline

DAILY_SCAN_TIME = time(8, 10, tzinfo=UTC)
FAR_HOURS = 72.0
NEAR_HOURS = 6.0
MID_INTERVAL = timedelta(hours=3)
NEAR_INTERVAL = timedelta(hours=1)
DEFAULT_SLACK = timedelta(minutes=5)


def next_gameweek(deadlines: list[GameweekDeadline], now: datetime) -> GameweekDeadline | None:
    """The first gameweek whose deadline is strictly in the future, or None
    (season over / no data)."""
    future = [d for d in deadlines if d.deadline > now]
    return min(future, key=lambda d: d.deadline) if future else None


def latest_daily_anchor(now: datetime) -> datetime:
    """The most recent 08:10 UTC at or before `now`."""
    utc_now = now.astimezone(UTC)
    anchor = datetime.combine(utc_now.date(), DAILY_SCAN_TIME)
    return anchor if anchor <= utc_now else anchor - timedelta(days=1)


def is_due(
    now: datetime,
    deadline: datetime,
    last_scan_at: datetime | None,
    *,
    slack: timedelta = DEFAULT_SLACK,
) -> bool:
    """Whether a tick at `now` should scan, for the target gameweek's
    `deadline` and the time of the last scan (None = never scanned)."""
    if last_scan_at is None:
        return True
    if last_scan_at < latest_daily_anchor(now):
        return True  # the daily 08:10 UTC scan, every band
    hours_to = (deadline - now).total_seconds() / 3600
    if hours_to > FAR_HOURS or hours_to < 0:
        return False  # daily only, and today's is done
    interval = MID_INTERVAL if hours_to > NEAR_HOURS else NEAR_INTERVAL
    return now - last_scan_at >= interval - slack
