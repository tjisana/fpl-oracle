"""Gemini budget policy for the autopilot (spec "Budget policy"). Pure
functions: no clock reads, no I/O.

1. Order candidates by `creator_weight x kind_multiplier`, then newest.
2. Demote a creator's older video to the lowest priority when a newer
   selection/final video from them is already in the ledger.
3. Final-window reserve. A Pacific quota day that overlaps the 36 h before
   the deadline is "protected". On a protected day a video that is neither
   selection/final nor published inside that window may only use requests
   up to `limit - reserve` of the day's total; the others may use all of
   it. On unprotected days there is no reserve (unused budget is lost at
   reset, so spend it).

`limit` / usage are summed over the configured models: the policy is about
the day's total, `harvest.choose_model` still picks the model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta

from pydantic import BaseModel

from fpl_oracle.ingest.classify import FINAL_CLASS_KINDS, VideoKind, priority_multiplier
from fpl_oracle.ingest.harvest import QUOTA_TZ, GeminiUsageFile, quota_day, request_cost
from fpl_oracle.ingest.ledger import LedgerRow

DEFAULT_FINAL_WINDOW_HOURS = 36.0
DEFAULT_RESERVE_REQUESTS = 10


class BudgetPolicy(BaseModel):
    final_window_hours: float = DEFAULT_FINAL_WINDOW_HOURS
    reserve_requests: int = DEFAULT_RESERVE_REQUESTS


class PlannedSpend(BaseModel):
    video_id: str
    creator_id: str
    title: str
    kind: VideoKind
    cost: int | None  # None: too long for Gemini (never sent)
    cap: int


def quota_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the Pacific quota day containing `now`, in UTC."""
    local_date = now.astimezone(QUOTA_TZ).date()
    start = datetime.combine(local_date, time.min, tzinfo=QUOTA_TZ)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=QUOTA_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def is_protected_day(now: datetime, deadline: datetime | None, policy: BudgetPolicy) -> bool:
    """True if the quota day containing `now` overlaps the final window
    (`final_window_hours` before the deadline up to the deadline)."""
    if deadline is None:
        return False
    day_start, day_end = quota_day_bounds(now)
    window_start = deadline - timedelta(hours=policy.final_window_hours)
    return day_start < deadline and day_end > window_start


def in_final_window(published_at: datetime, deadline: datetime, policy: BudgetPolicy) -> bool:
    return published_at >= deadline - timedelta(hours=policy.final_window_hours)


def daily_limit(limits: Mapping[str, int]) -> int:
    return sum(limits.values())


def daily_used(usage: GeminiUsageFile, limits: Mapping[str, int], day: str) -> int:
    return sum(usage.used(m, day) for m in limits)


def daily_remaining(usage: GeminiUsageFile, limits: Mapping[str, int], day: str) -> int:
    """Requests left today across models (a model flagged exhausted by a
    daily 429 counts as zero)."""
    return sum(
        0 if usage.is_exhausted(m, day) else max(0, limits[m] - usage.used(m, day)) for m in limits
    )


def daily_cap(
    row: LedgerRow, now: datetime, deadline: datetime | None, limit: int, policy: BudgetPolicy
) -> int:
    """The most requests (cumulative today) a spend on `row` may bring the
    day's total to."""
    if deadline is None or not is_protected_day(now, deadline, policy):
        return limit
    if row.kind in FINAL_CLASS_KINDS or in_final_window(row.published_at, deadline, policy):
        return limit
    return max(0, limit - policy.reserve_requests)


def may_spend(
    row: LedgerRow,
    cost: int | None,
    *,
    used_today: int,
    now: datetime,
    deadline: datetime | None,
    limit: int,
    policy: BudgetPolicy,
) -> bool:
    """Whether the reserve rule allows `row` to spend `cost` requests now.
    A video that is never sent to Gemini (`cost` None) spends nothing."""
    if cost is None:
        return True
    return used_today + cost <= daily_cap(row, now, deadline, limit, policy)


def demoted_video_ids(rows: Sequence[LedgerRow]) -> set[str]:
    """Videos older than a newer selection/final video of the same creator
    (among `rows`)."""
    newest_final: dict[str, datetime] = {}
    for r in rows:
        if r.kind in FINAL_CLASS_KINDS and r.status != "skipped":
            cur = newest_final.get(r.creator_id)
            if cur is None or r.published_at > cur:
                newest_final[r.creator_id] = r.published_at
    return {
        r.video_id
        for r in rows
        if r.creator_id in newest_final and r.published_at < newest_final[r.creator_id]
    }


def order_candidates(
    candidates: Sequence[LedgerRow],
    weights: Mapping[str, float],
    demoted: set[str] | frozenset[str] = frozenset(),
    *,
    default_weight: float = 0.0,
) -> list[LedgerRow]:
    """Spend order: non-demoted before demoted, then weight x multiplier
    descending, then newest first (video_id breaks exact ties)."""

    def key(r: LedgerRow) -> tuple[int, float, float, str]:
        score = weights.get(r.creator_id, default_weight) * priority_multiplier(r.kind)
        return (1 if r.video_id in demoted else 0, -score, -r.published_at.timestamp(), r.video_id)

    return sorted(candidates, key=key)


def plan_spend(
    ordered: Sequence[LedgerRow],
    *,
    used_today: int,
    limit: int,
    now: datetime,
    deadline: datetime | None,
    policy: BudgetPolicy,
) -> list[PlannedSpend]:
    """Walk `ordered` greedily, adding each video the reserve rule lets fit
    in what is left; a video that does not fit is skipped and cheaper ones
    behind it may still be taken. Assumes every read succeeds."""
    plan: list[PlannedSpend] = []
    used = used_today
    for row in ordered:
        cost = request_cost(row.duration_s)
        if not may_spend(
            row, cost, used_today=used, now=now, deadline=deadline, limit=limit, policy=policy
        ):
            continue
        plan.append(
            PlannedSpend(
                video_id=row.video_id,
                creator_id=row.creator_id,
                title=row.title,
                kind=row.kind,
                cost=cost,
                cap=daily_cap(row, now, deadline, limit, policy),
            )
        )
        used += cost or 0
    return plan


def used_today_total(usage: GeminiUsageFile, limits: Mapping[str, int], now: datetime) -> int:
    return daily_used(usage, limits, quota_day(now))
