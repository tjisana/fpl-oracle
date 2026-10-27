"""Unattended, idempotent transcript harvest for one gameweek.

    uv run python -m fpl_oracle.ingest.harvest --gameweek 9 [--dry-run] [--backstop-hours 6]

Spec: docs/harvest-spec.md. One command that can run many times a day (by
hand now, by a scheduler later) and, for the target gameweek, leaves a
usable transcript in the on-disk cache (`data/transcripts/`) for as many
creators' final videos as possible, spending the scarce Gemini request
budget where it adds the most.

WHY IT EXISTS. On GW8 the Gemini free tier (5 requests/min, ~20/day per
model, plus 503s) failed all 14 transcripts, and ten of twelve final videos
land on the same day. This module treats the daily request budget as a
first-class resource: it counts every attempt that reaches the API (failed
ones too — Google does not say they are free), orders work by creator
weight, and routes anything the budget cannot cover to YouTube captions.

SELECTION, FETCHING, WEIGHTS ARE REUSED, NOT REIMPLEMENTED:
`extract.run_extract.select_videos` picks each creator's video;
`ingest.gemini_transcripts.read_video_via_gemini` reads it (through a thin
seam that lets this module count requests and choose the model);
`ingest.transcripts.fetch_transcript` scrapes captions; `Creator.weight`
(from `roster.weights`) orders the work.

Per-video state lives in `data/ingest_state/{gameweek}.json`. The Gemini
request counters live in ONE shared file, `data/ingest_state/gemini_usage.json`
(model -> Pacific quota day -> count, plus per-model exhausted-for-day flags),
because Google's limits are per project and per model, not per gameweek.
Quota day = the calendar day in America/Los_Angeles (daily limits reset at
midnight Pacific). All writes are atomic (unique temp file in the same
directory + rename), and a non-blocking `flock` on
`data/ingest_state/harvest.lock` is held for the whole run, so overlapping
invocations fail fast instead of double-spending.

KNOWN LIMITATIONS / DEFERRED:
- Gemini calls made by `run_extract` or `warm_cache` do NOT go through this
  counter, so they can spend budget the harvester believes it still has.
- An unknown video duration is costed at 1 request; a long video with an
  unknown duration may cost more (and 429) than planned.
- 429 daily-vs-minute is decided by substring heuristics on the error text
  (`classify_gemini_error`); parsing RetryInfo / quotaId from the structured
  error details is a later improvement.
- Atomic writes use `mkstemp` (0600 permissions) and do not fsync the
  directory after `os.replace`.
- `retry_delay` may sleep after the last attempt; `vs.attempts` is
  incremented even when the budget gate aborts before any request.
- `--dry-run` spends no Gemini request but still calls the YouTube API for
  video selection (the same as `run_extract --dry-run`).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
import requests
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.table import Table
from youtube_transcript_api import RequestBlocked, YouTubeRequestFailed

from fpl_oracle.ingest.gemini_transcripts import (
    CHUNK_SECONDS,
    VideoTooLongError,
    gemini_api_key_present,
    max_video_seconds,
    read_video_via_gemini,
)
from fpl_oracle.ingest.transcripts import (
    DEFAULT_TRANSCRIPT_DIR,
    Transcript,
    fetch_transcript,
    load_transcript,
    save_transcript,
)
from fpl_oracle.roster.weights import DEFAULT_TIER2_WEIGHT

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = Path("data/ingest_state")

# Free-tier cap observed for this model is 20 requests/day; 2 are kept spare.
# Adding a model with its own limit is a change to this mapping only; order
# is the order models are tried.
DEFAULT_GEMINI_DAILY_LIMITS: Mapping[str, int] = {"gemini-3.5-flash": 18}

QUOTA_TZ = ZoneInfo("America/Los_Angeles")

# 503 / timeout backoff, indexed by how many attempts have failed so far
# (the last entry repeats).
RETRY_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)

# One per-minute-quota window plus margin (the cap is per minute).
PER_MINUTE_429_SLEEP_SECONDS = 65.0

# Gap between caption scrapes. Captions have IP-blocked this machine before.
CAPTION_PACING_SECONDS = 2.0

DEFAULT_BACKSTOP_HOURS = 6.0

# "Fewer than half the weight has a transcript within 12 h of the deadline"
# prints a loud warning (the hook for ntfy later).
WARN_WITHIN_HOURS = 12.0
WARN_BELOW_WEIGHT_FRACTION = 0.5

# Reasons recorded in `fell_back_reason` when a video goes to captions.
REASON_BUDGET = "budget"
REASON_DAILY_LIMIT = "gemini_daily_limit"
REASON_TOO_LONG = "too_long"
REASON_BACKSTOP = "deadline_backstop"
REASON_GEMINI_ERROR = "gemini_error"
REASON_GEMINI_UNAVAILABLE = "gemini_unavailable"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


class VideoState(BaseModel):
    video_id: str
    creator_id: str
    status: Literal["pending", "ok", "failed"] = "pending"
    source: str | None = None
    attempts: int = 0
    gemini_requests_used: int = 0
    # 503 / timeout count only; drives the 30 min -> 2 h -> 6 h backoff step.
    transient_failures: int = 0
    # Pacific quota day on which Gemini failed this video with a non-capacity
    # error; Gemini is skipped for it for the rest of that day (captions are
    # still allowed on later runs). Deliberately not `retry_after`, which
    # would also block captions.
    gemini_error_day: str | None = None
    last_error: str | None = None
    retry_after: datetime | None = None
    fell_back_reason: str | None = None


class HarvestState(BaseModel):
    """Per-gameweek state: per-video fields only. The Gemini request
    counters are NOT here — quotas are per project/model/day, not per
    gameweek, so they live in the shared `GeminiUsageFile`."""

    gameweek: int
    updated_at: datetime | None = None
    videos: dict[str, VideoState] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    requests: dict[str, int] = Field(default_factory=dict)  # Pacific quota day -> count
    # Days on which a daily 429 fired: done for that day whatever our count says.
    exhausted_days: list[str] = Field(default_factory=list)


class GeminiUsageFile(BaseModel):
    """data/ingest_state/gemini_usage.json: model -> quota day -> requests,
    shared by every gameweek's harvest (Google's limits are per project and
    per model, per Pacific calendar day)."""

    models: dict[str, ModelUsage] = Field(default_factory=dict)

    def _model(self, model: str) -> ModelUsage:
        return self.models.setdefault(model, ModelUsage())

    def used(self, model: str, day: str) -> int:
        return self.models[model].requests.get(day, 0) if model in self.models else 0

    def is_exhausted(self, model: str, day: str) -> bool:
        return model in self.models and day in self.models[model].exhausted_days

    def add_request(self, model: str, day: str) -> None:
        usage = self._model(model)
        usage.requests[day] = usage.requests.get(day, 0) + 1

    def mark_exhausted(self, model: str, day: str) -> None:
        usage = self._model(model)
        if day not in usage.exhausted_days:
            usage.exhausted_days.append(day)


def state_path(gameweek: int, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return state_dir / f"{gameweek}.json"


def usage_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return state_dir / "gemini_usage.json"


def lock_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return state_dir / "harvest.lock"


def _atomic_write(path: Path, text: str) -> None:
    """Unique temp file in the same directory, fsync, rename over `path`. A
    crash at any point leaves either the old file or the new one, never a
    torso; concurrent writers cannot collide on a temp name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_state(gameweek: int, state_dir: Path = DEFAULT_STATE_DIR) -> HarvestState:
    """The stored per-gameweek state, or a fresh one if there is no file."""
    path = state_path(gameweek, state_dir)
    if not path.exists():
        return HarvestState(gameweek=gameweek)
    try:
        return HarvestState.model_validate_json(path.read_text())
    except (ValidationError, ValueError, OSError) as e:
        raise RuntimeError(
            f"harvest: state file {path} is unreadable ({type(e).__name__}); "
            "fix or delete it by hand"
        ) from e


def save_state(state: HarvestState, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    path = state_path(state.gameweek, state_dir)
    _atomic_write(path, state.model_dump_json(indent=2))
    return path


def load_usage(state_dir: Path = DEFAULT_STATE_DIR) -> GeminiUsageFile:
    """The shared Gemini counters. An UNREADABLE file raises rather than
    resetting: silently starting from zero could overspend the quota.
    Writes are atomic, so this means a hand edit or damage."""
    path = usage_path(state_dir)
    if not path.exists():
        return GeminiUsageFile()
    try:
        return GeminiUsageFile.model_validate_json(path.read_text())
    except (ValidationError, ValueError, OSError) as e:
        raise RuntimeError(
            f"harvest: usage file {path} is unreadable ({type(e).__name__}); "
            "refusing to start from zero (it holds the Gemini request counters) — "
            "fix or delete it by hand"
        ) from e


def save_usage(usage: GeminiUsageFile, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    path = usage_path(state_dir)
    _atomic_write(path, usage.model_dump_json(indent=2))
    return path


class HarvestLockedError(RuntimeError):
    """Another harvest run holds the lock."""


@contextlib.contextmanager
def run_lock(state_dir: Path = DEFAULT_STATE_DIR) -> Iterator[None]:
    """Exclusive, non-blocking flock for the whole run, so overlapping
    invocations (cron + a manual run) cannot both spend the budget or
    clobber state. Fails fast if held."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(state_dir)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise HarvestLockedError(
                f"harvest: another run holds {path}; refusing to overlap (try again later)"
            ) from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# Inputs / config
# --------------------------------------------------------------------------


class VideoRef(BaseModel):
    video_id: str
    title: str
    duration_seconds: int | None = None


class CreatorTarget(BaseModel):
    """One creator's harvest target: the final video selected for them
    (None if none was found yet), and their weight."""

    creator_id: str
    name: str
    weight: float
    video: VideoRef | None = None
    selection_error: str | None = None


class HarvestConfig(BaseModel):
    gemini_daily_limits: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_GEMINI_DAILY_LIMITS)
    )
    backstop_hours: float = DEFAULT_BACKSTOP_HOURS
    state_dir: Path = DEFAULT_STATE_DIR
    transcript_dir: Path = DEFAULT_TRANSCRIPT_DIR


class GeminiFailure(StrEnum):
    TRANSIENT = "transient"  # 5xx / timeout / connection trouble
    RATE_MINUTE = "rate_minute"  # 429, per-minute quota
    RATE_DAILY = "rate_daily"  # 429, per-day quota
    OTHER = "other"


def classify_gemini_error(error: BaseException) -> GeminiFailure:
    """Bucket a Gemini failure by duck-typing (`code` / `status` attributes
    of google.genai's APIError) so this module needs no SDK import and tests
    can raise plain stand-ins.

    A 429 is DAILY if its text names a per-day quota ("GenerateRequestsPerDay...",
    "per day", "daily"); every other 429 is treated as per-minute."""
    code = getattr(error, "code", None)
    status = str(getattr(error, "status", "") or "").upper()
    text = str(error).lower()
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        if "perday" in text or "per day" in text or "daily" in text:
            return GeminiFailure.RATE_DAILY
        return GeminiFailure.RATE_MINUTE
    if (
        code in (500, 502, 503, 504)
        or status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED")
        or isinstance(error, TimeoutError | httpx.TimeoutException | httpx.TransportError)
    ):
        return GeminiFailure.TRANSIENT
    return GeminiFailure.OTHER


def is_key_level_error(error: BaseException) -> bool:
    """True if a Gemini failure says the API KEY / project itself is unusable
    (HTTP 401, API_KEY_INVALID, SERVICE_DISABLED, "API has not been used"):
    every other video would fail the same way, so Gemini is switched off for
    the run. A bare 403 / PERMISSION_DENIED is NOT key-level: a private,
    members-only or not-yet-premiered video produces it, so it is a
    per-video error."""
    code = getattr(error, "code", None)
    text = f"{getattr(error, 'status', '')} {error}".upper()
    return (
        code == 401
        or "API_KEY_INVALID" in text
        or "SERVICE_DISABLED" in text
        or "API HAS NOT BEEN USED" in text
    )


class BudgetExhaustedError(Exception):
    """Raised from the per-request gate when the model's daily budget is
    spent; aborts the read before the request is made."""


class GeminiReader(Protocol):
    def __call__(
        self,
        video_id: str,
        duration_seconds: float | None,
        /,
        *,
        model: str,
        before_request: Callable[[], None],
        retry_delay: Callable[[Exception, int], float | None],
        sleep: Callable[[float], None],
    ) -> Transcript: ...


def default_gemini_reader(
    video_id: str,
    duration_seconds: float | None,
    *,
    model: str,
    before_request: Callable[[], None],
    retry_delay: Callable[[Exception, int], float | None],
    sleep: Callable[[float], None],
) -> Transcript:
    """The real reader: at most 2 attempts per window (the second only after
    a per-minute 429 and its sleep — see `_retry_delay`)."""
    return read_video_via_gemini(
        video_id,
        duration_seconds,
        model=model,
        max_attempts=2,
        before_request=before_request,
        retry_delay=retry_delay,
        sleep=sleep,
    )


type CaptionReader = Callable[[str], Transcript | None]


def default_caption_reader(video_id: str) -> Transcript | None:
    return fetch_transcript(video_id)


def _retry_delay(error: Exception, attempt: int) -> float | None:
    """Gemini retry policy: ONLY a per-minute 429 earns a retry, once (the
    first attempt of a window), after one quota window's sleep. 503s and
    everything else are not retried in-run."""
    if attempt == 1 and classify_gemini_error(error) is GeminiFailure.RATE_MINUTE:
        return PER_MINUTE_429_SLEEP_SECONDS
    return None


# --------------------------------------------------------------------------
# Decision helpers (shared by the real run and --dry-run)
# --------------------------------------------------------------------------


def quota_day(now: datetime) -> str:
    return now.astimezone(QUOTA_TZ).date().isoformat()


def request_cost(duration_seconds: float | None) -> int | None:
    """Gemini requests a video costs: one per 30-minute window
    (`ceil(duration/1800)`); unknown duration is a single request. None
    when the video exceeds the reader's length ceiling (never sent)."""
    if duration_seconds is None:
        return 1
    if duration_seconds > max_video_seconds():
        return None
    return max(1, math.ceil(duration_seconds / CHUNK_SECONDS)) if duration_seconds > 0 else 1


def _remaining(usage: GeminiUsageFile, model: str, limits: Mapping[str, int], day: str) -> int:
    if usage.is_exhausted(model, day):
        return 0
    return max(0, limits[model] - usage.used(model, day))


def choose_model(
    usage: GeminiUsageFile, limits: Mapping[str, int], day: str, cost: int, skip: Sequence[str] = ()
) -> str | None:
    """The first configured model (in config order) whose remaining budget
    covers `cost` whole requests, or None."""
    for model in limits:
        if model in skip:
            continue
        if _remaining(usage, model, limits, day) >= cost:
            return model
    return None


def no_gemini_reason(
    usage: GeminiUsageFile, limits: Mapping[str, int], day: str, cost: int | None
) -> str:
    if cost is None:
        return REASON_TOO_LONG
    if not limits:
        return REASON_GEMINI_UNAVAILABLE
    if all(usage.is_exhausted(m, day) for m in limits):
        return REASON_DAILY_LIMIT
    return REASON_BUDGET


def hours_to(deadline: datetime | None, now: datetime) -> float | None:
    if deadline is None:
        return None
    return (deadline - now).total_seconds() / 3600


def in_backstop(deadline: datetime | None, now: datetime, backstop_hours: float) -> bool:
    hours = hours_to(deadline, now)
    return hours is not None and hours <= backstop_hours


def deadline_for_gameweek(events: Sequence[Mapping[str, object]], gameweek: int) -> datetime | None:
    """`deadline_time` of `gameweek` from a bootstrap-static `events`
    payload (ISO-8601 UTC, trailing Z), or None if absent."""
    for event in events:
        if event.get("id") == gameweek:
            raw = event.get("deadline_time")
            if isinstance(raw, str):
                return datetime.fromisoformat(raw)
    return None


def _order_by_weight(targets: Sequence[CreatorTarget]) -> list[CreatorTarget]:
    return sorted(
        (t for t in targets if t.video is not None), key=lambda t: (-t.weight, t.creator_id)
    )


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


class ReportRow(BaseModel):
    creator_id: str
    weight: float
    video_id: str | None
    title: str | None
    source: str | None
    status: str
    note: str = ""


class HarvestResult(BaseModel):
    gameweek: int
    dry_run: bool
    hours_to_deadline: float | None
    rows: list[ReportRow]
    no_transcript: list[str]
    weight_covered_fraction: float
    warn_low_coverage: bool
    gemini_requests_this_run: int
    gemini_budget_left: dict[str, int]


# --------------------------------------------------------------------------
# The harvest
# --------------------------------------------------------------------------


class _Harvester:
    def __init__(
        self,
        gameweek: int,
        targets: Sequence[CreatorTarget],
        deadline: datetime | None,
        config: HarvestConfig,
        *,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        gemini_reader: GeminiReader,
        caption_reader: CaptionReader,
        gemini_available: bool,
    ) -> None:
        self.gameweek = gameweek
        self.targets = list(targets)
        self.deadline = deadline
        self.config = config
        self.now = now
        self.sleep = sleep
        self.gemini_reader = gemini_reader
        self.caption_reader = caption_reader
        self.state = load_state(gameweek, config.state_dir)
        self.captions_blocked = False
        self.caption_calls = 0
        self.requests_this_run = 0
        self.limits = config.gemini_daily_limits
        self.usage = load_usage(config.state_dir)
        self.gemini_available = gemini_available

    # -- persistence ------------------------------------------------------

    def _save(self) -> None:
        self.state.updated_at = self.now()
        save_state(self.state, self.config.state_dir)

    def _save_usage(self) -> None:
        save_usage(self.usage, self.config.state_dir)

    # -- the run ----------------------------------------------------------

    def run(self) -> None:
        pending = self._sync_state_with_cache()
        for target in _order_by_weight(pending):
            self._harvest_with_gemini_first(target)
        self._backstop_pass()

    def _sync_state_with_cache(self) -> list[CreatorTarget]:
        """Register each target's video in the state, mark videos whose
        transcript is already cached as done, and return the targets still
        needing work. A rerun with everything cached writes nothing."""
        dirty = False
        pending: list[CreatorTarget] = []
        for target in self.targets:
            if target.video is None:
                continue
            vid = target.video.video_id
            vs = self.state.videos.get(vid)
            if vs is None:
                vs = VideoState(video_id=vid, creator_id=target.creator_id)
                self.state.videos[vid] = vs
                dirty = True
            cached = load_transcript(vid, dir=self.config.transcript_dir)
            if cached is not None:
                if vs.status != "ok" or vs.source != cached.source:
                    vs.status = "ok"
                    vs.source = cached.source
                    vs.last_error = None
                    vs.retry_after = None
                    dirty = True
                if cached.source != "youtube-captions" and vs.fell_back_reason is not None:
                    vs.fell_back_reason = None  # a Gemini-read video was not a fallback
                    dirty = True
                continue
            if vs.status in ("ok", "failed"):
                # "ok" with the cache file gone needs work again. "failed"
                # (captions found nothing) is retried on every run: YouTube's
                # auto-captions can appear hours after upload, and Gemini
                # budget may have freed up.
                vs.status = "pending"
                vs.source = None
                vs.fell_back_reason = None
                vs.retry_after = None
                vs.last_error = None
                vs.transient_failures = 0
                dirty = True
            pending.append(target)
        if dirty:
            self._save()
        return pending

    def _harvest_with_gemini_first(self, target: CreatorTarget) -> None:
        assert target.video is not None
        vs = self.state.videos[target.video.video_id]
        if vs.status != "pending":
            return  # a prior iteration or run closed it (e.g. "failed")
        if vs.retry_after is not None and vs.retry_after > self.now():
            return  # waiting out a backoff; only the deadline backstop overrides this

        if not self.gemini_available:
            self._use_captions(target, REASON_GEMINI_UNAVAILABLE)
            return

        day = quota_day(self.now())
        if vs.gemini_error_day == day:
            # Gemini already failed this video (non-capacity) today; don't
            # burn another request on it every run. Captions only.
            self._use_captions(target, REASON_GEMINI_ERROR)
            return
        cost = request_cost(target.video.duration_seconds)
        tried: list[str] = []
        while True:
            model = (
                choose_model(self.usage, self.limits, day, cost, tried)
                if cost is not None
                else None
            )
            if model is None:
                reason = no_gemini_reason(self.usage, self.limits, day, cost)
                self._use_captions(target, reason)
                return
            tried.append(model)
            outcome = self._try_gemini(target, model)
            if outcome == "next_model":
                day = quota_day(self.now())
                continue
            if outcome == "too_long":
                self._use_captions(target, REASON_TOO_LONG)
            elif outcome == "error":
                self._use_captions(target, REASON_GEMINI_ERROR)
            return

    def _try_gemini(
        self, target: CreatorTarget, model: str
    ) -> Literal["done", "next_model", "too_long", "retry_later", "error"]:
        assert target.video is not None
        vs = self.state.videos[target.video.video_id]

        sent_days: list[str] = []  # Pacific day each request was SENT on

        def before_request() -> None:
            day = quota_day(self.now())
            if (
                self.usage.is_exhausted(model, day)
                or self.usage.used(model, day) >= self.limits[model]
            ):
                raise BudgetExhaustedError(model)
            self.usage.add_request(model, day)
            sent_days.append(day)
            vs.gemini_requests_used += 1
            self.requests_this_run += 1
            # Persist before the call: a crash must not lose the count.
            self._save_usage()
            self._save()

        vs.attempts += 1
        try:
            transcript = self.gemini_reader(
                target.video.video_id,
                target.video.duration_seconds,
                model=model,
                before_request=before_request,
                retry_delay=_retry_delay,
                sleep=self.sleep,
            )
        except BudgetExhaustedError:
            vs.last_error = f"{model}: budget exhausted mid-video"
            self._save()
            return "next_model"
        except VideoTooLongError:
            return "too_long"
        except Exception as e:  # noqa: BLE001 - classified below; one video never kills the run
            kind = classify_gemini_error(e)
            vs.last_error = f"{model}: {kind.value}: {type(e).__name__}"
            if kind is GeminiFailure.RATE_DAILY:
                # The day the request was SENT, not the day the 429 was handled.
                self.usage.mark_exhausted(
                    model, sent_days[-1] if sent_days else quota_day(self.now())
                )
                self._save_usage()
                self._save()
                # Another configured model may still have budget.
                return "next_model"
            if kind is GeminiFailure.OTHER:
                # Not a capacity problem: waiting will not fix it. Captions now.
                if is_key_level_error(e):
                    # Every other video would fail the same way.
                    self.gemini_available = False
                    vs.last_error = f"{model}: api key unusable: {type(e).__name__}"
                # Either way this video is not retried on Gemini today (so it
                # cannot re-trigger a switch-off every run). Day = when SENT.
                vs.gemini_error_day = sent_days[-1] if sent_days else quota_day(self.now())
                self._save()
                return "error"
            if kind is GeminiFailure.TRANSIENT:
                vs.transient_failures += 1
                step = min(vs.transient_failures, len(RETRY_BACKOFF)) - 1
            else:  # a per-minute 429 that repeated after its sleep
                step = 0
            vs.retry_after = self.now() + RETRY_BACKOFF[step]
            self._save()
            return "retry_later"

        save_transcript(transcript, dir=self.config.transcript_dir)
        vs.status = "ok"
        vs.source = transcript.source
        vs.last_error = None
        vs.retry_after = None
        vs.fell_back_reason = None
        vs.gemini_error_day = None
        self._save()
        return "done"

    def _use_captions(self, target: CreatorTarget, reason: str) -> None:
        assert target.video is not None
        vs = self.state.videos[target.video.video_id]
        # Keep the FIRST reason: the backstop must not overwrite why this
        # video left Gemini in the first place.
        if vs.fell_back_reason is None or reason != REASON_BACKSTOP:
            vs.fell_back_reason = reason
        if self.captions_blocked:
            vs.last_error = "captions blocked this run (IpBlocked); left pending"
            self._save()
            return
        if self.caption_calls > 0:
            self.sleep(CAPTION_PACING_SECONDS)
        self.caption_calls += 1
        vs.attempts += 1
        try:
            transcript = self.caption_reader(target.video.video_id)
        except (
            RequestBlocked,
            YouTubeRequestFailed,
            requests.RequestException,
            httpx.HTTPError,
            OSError,
        ) as e:
            # Environmental (block or transport failure), not about this
            # video: stop ALL caption attempts this invocation and leave the
            # video pending. Never loop on a block.
            self.captions_blocked = True
            vs.last_error = f"captions unavailable: {type(e).__name__}; left pending"
            self._save()
            return
        except Exception as e:
            # Unexpected, and not evidently environmental: skip THIS video
            # (left pending, retried next run) and carry on with the next.
            # Traceback goes to the log only (no secrets on these paths), so a
            # bug in our own code is not hidden behind the type name.
            logger.warning(
                "harvest: captions for %s failed unexpectedly (%s)",
                target.video.video_id,
                type(e).__name__,
                exc_info=True,
            )
            vs.last_error = f"captions error: {type(e).__name__}; left pending"
            self._save()
            return
        if transcript is None:
            vs.status = "failed"
            vs.last_error = "no English captions available"
            self._save()
            return
        save_transcript(transcript, dir=self.config.transcript_dir)
        vs.status = "ok"
        vs.source = transcript.source
        vs.last_error = None
        vs.retry_after = None
        self._save()

    def _backstop_pass(self) -> None:
        """Deadline backstop: a selected video still without a transcript
        when the deadline is within `backstop_hours` goes to captions
        regardless of budget or retry timers."""
        if not in_backstop(self.deadline, self.now(), self.config.backstop_hours):
            return
        for target in _order_by_weight(self.targets):
            assert target.video is not None
            vs = self.state.videos.get(target.video.video_id)
            if vs is None or vs.status != "pending":
                continue
            if load_transcript(target.video.video_id, dir=self.config.transcript_dir):
                continue
            self._use_captions(target, REASON_BACKSTOP)

    # -- reporting --------------------------------------------------------

    def result(self) -> HarvestResult:
        return build_result(
            self.gameweek,
            self.targets,
            self.state,
            self.usage,
            self.deadline,
            self.now(),
            self.limits,
            dry_run=False,
            requests_this_run=self.requests_this_run,
        )


def build_result(
    gameweek: int,
    targets: Sequence[CreatorTarget],
    state: HarvestState,
    usage: GeminiUsageFile,
    deadline: datetime | None,
    now: datetime,
    limits: Mapping[str, int],
    *,
    dry_run: bool,
    requests_this_run: int,
    planned: Mapping[str, str] | None = None,
) -> HarvestResult:
    rows: list[ReportRow] = []
    no_transcript: list[str] = []
    total_weight = sum(t.weight for t in targets)
    covered_weight = 0.0
    for target in sorted(targets, key=lambda t: (-t.weight, t.creator_id)):
        if target.video is None:
            note = target.selection_error or "no video found yet"
            rows.append(
                ReportRow(
                    creator_id=target.creator_id,
                    weight=target.weight,
                    video_id=None,
                    title=None,
                    source=None,
                    status="no video",
                    note=note,
                )
            )
            no_transcript.append(target.creator_id)
            continue
        vs = state.videos.get(target.video.video_id) or VideoState(
            video_id=target.video.video_id, creator_id=target.creator_id
        )
        note_parts: list[str] = []
        if vs.fell_back_reason:
            note_parts.append(f"captions fallback: {vs.fell_back_reason}")
        if vs.retry_after is not None and vs.status == "pending":
            note_parts.append(f"retry after {vs.retry_after.isoformat(timespec='minutes')}")
        if vs.last_error and vs.status != "ok":
            note_parts.append(vs.last_error)
        source = vs.source
        if planned is not None and vs.status != "ok":
            source = planned.get(target.video.video_id, "-")
        rows.append(
            ReportRow(
                creator_id=target.creator_id,
                weight=target.weight,
                video_id=target.video.video_id,
                title=target.video.title,
                source=source,
                status=vs.status if planned is None or vs.status == "ok" else "planned",
                note="; ".join(note_parts),
            )
        )
        if vs.status == "ok":
            covered_weight += target.weight
        else:
            no_transcript.append(target.creator_id)
    fraction = covered_weight / total_weight if total_weight > 0 else 1.0
    hours = hours_to(deadline, now)
    day = quota_day(now)
    return HarvestResult(
        gameweek=gameweek,
        dry_run=dry_run,
        hours_to_deadline=hours,
        rows=rows,
        no_transcript=no_transcript,
        weight_covered_fraction=fraction,
        warn_low_coverage=(
            hours is not None
            and hours <= WARN_WITHIN_HOURS
            and fraction < WARN_BELOW_WEIGHT_FRACTION
        ),
        gemini_requests_this_run=requests_this_run,
        gemini_budget_left={m: _remaining(usage, m, limits, day) for m in limits},
    )


def run_harvest(
    gameweek: int,
    targets: Sequence[CreatorTarget],
    deadline: datetime | None,
    config: HarvestConfig | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    gemini_reader: GeminiReader = default_gemini_reader,
    caption_reader: CaptionReader = default_caption_reader,
    gemini_available: Callable[[], bool] | None = None,
) -> HarvestResult:
    """Harvest transcripts for `targets`. Idempotent: videos with a cached
    transcript are never touched. Every collaborator (clock, sleep, both
    readers) is injectable so the whole thing runs offline in tests.

    Holds an exclusive lock for the whole run; raises `HarvestLockedError`
    at once if another run holds it."""
    config = config or HarvestConfig()
    check = gemini_available or gemini_api_key_present
    with run_lock(config.state_dir):
        harvester = _Harvester(
            gameweek,
            targets,
            deadline,
            config,
            now=now,
            sleep=sleep,
            gemini_reader=gemini_reader,
            caption_reader=caption_reader,
            gemini_available=check(),
        )
        harvester.run()
        return harvester.result()


def plan_harvest(
    gameweek: int,
    targets: Sequence[CreatorTarget],
    deadline: datetime | None,
    config: HarvestConfig | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    gemini_available: Callable[[], bool] | None = None,
) -> HarvestResult:
    """`--dry-run`: what a run would do right now, assuming every Gemini
    read succeeds. Makes no request, writes no state, creates no files."""
    config = config or HarvestConfig()
    state = load_state(gameweek, config.state_dir)
    usage = load_usage(config.state_dir)
    available = (gemini_available or gemini_api_key_present)()
    current = now()
    day = quota_day(current)
    planned: dict[str, str] = {}
    for target in _order_by_weight(targets):
        assert target.video is not None
        vid = target.video.video_id
        if load_transcript(vid, dir=config.transcript_dir) is not None:
            continue
        vs = state.videos.get(vid)
        if vs is not None and vs.retry_after is not None and vs.retry_after > current:
            planned[vid] = (
                f"waiting (retry after {vs.retry_after.isoformat(timespec='minutes')})"
                + (
                    "; captions at backstop"
                    if in_backstop(deadline, current, config.backstop_hours)
                    else ""
                )
            )
            continue
        if not available:
            planned[vid] = f"captions ({REASON_GEMINI_UNAVAILABLE})"
            continue
        cost = request_cost(target.video.duration_seconds)
        model = (
            choose_model(usage, config.gemini_daily_limits, day, cost) if cost is not None else None
        )
        if model is None:
            reason = no_gemini_reason(usage, config.gemini_daily_limits, day, cost)
            planned[vid] = f"captions ({reason})"
            continue
        assert cost is not None  # a model was chosen, so the video is sendable
        for _ in range(cost):  # virtual: never saved
            usage.add_request(model, day)
        planned[vid] = f"gemini:{model} ({cost} request{'s' if cost != 1 else ''})"
    return build_result(
        gameweek,
        targets,
        state,
        usage,
        deadline,
        current,
        config.gemini_daily_limits,
        dry_run=True,
        requests_this_run=0,
        planned=planned,
    )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def print_report(result: HarvestResult, console: Console | None = None) -> None:
    console = console or Console()
    hours = result.hours_to_deadline
    deadline_txt = f"{hours:.1f} h to deadline" if hours is not None else "deadline unknown"
    title = f"GW{result.gameweek} transcript harvest{' (DRY RUN — nothing spent)' if result.dry_run else ''}"
    table = Table(title=f"{title} — {deadline_txt}")
    table.add_column("Creator")
    table.add_column("Weight", justify="right")
    table.add_column("Video")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Note")
    for row in result.rows:
        video = f"{row.video_id} {row.title}" if row.video_id else "-"
        table.add_row(
            row.creator_id,
            f"{row.weight:.2f}",
            video,
            row.source or "-",
            row.status,
            row.note,
        )
    console.print(table)
    budget = ", ".join(f"{m}: {left} left" for m, left in result.gemini_budget_left.items())
    console.print(
        f"Gemini requests this run: {result.gemini_requests_this_run}. "
        f"Budget today (Pacific day): {budget or 'none configured'}."
    )
    if result.no_transcript:
        console.print(
            f"No transcript yet for {len(result.no_transcript)} creator(s): "
            f"{', '.join(result.no_transcript)} "
            f"({deadline_txt})."
        )
    else:
        console.print("Every creator has a transcript.")
    if result.warn_low_coverage:
        pct = result.weight_covered_fraction * 100
        console.print(
            f"!!! WARNING: only {pct:.0f}% of creator weight has a transcript with "
            f"{hours:.1f} h to the deadline (< {WARN_BELOW_WEIGHT_FRACTION * 100:.0f}% "
            f"threshold within {WARN_WITHIN_HOURS:.0f} h) !!!",
            style="bold red",
        )
        logger.warning("harvest: LOW TRANSCRIPT COVERAGE gw%d (%.0f%%)", result.gameweek, pct)


# --------------------------------------------------------------------------
# Selection (reuses run_extract) and CLI
# --------------------------------------------------------------------------


def build_targets(
    gameweek: int, published_after: datetime | None, *, force_refresh: bool = True
) -> list[CreatorTarget]:
    """One target per eligible creator via `run_extract.select_videos` (the
    same selection extraction will use, so the harvest warms exactly the
    videos extraction will read). The newest qualifying video is the
    target."""
    from fpl_oracle.extract.run_extract import select_videos
    from fpl_oracle.ingest.youtube_client import YouTubeApiError, get_video_durations

    selections = select_videos(gameweek, published_after, force_refresh=force_refresh)
    video_ids = [s.video.video_id for s in selections if s.video is not None]
    try:
        durations = get_video_durations(video_ids) if video_ids else {}
    except (YouTubeApiError, httpx.HTTPError) as e:
        logger.warning("harvest: duration lookup failed (%s); costing videos at 1 request", e)
        durations = {}
    targets: list[CreatorTarget] = []
    for sel in selections:
        weight = sel.creator.weight if sel.creator.weight is not None else DEFAULT_TIER2_WEIGHT
        video = (
            VideoRef(
                video_id=sel.video.video_id,
                title=sel.video.title,
                duration_seconds=durations.get(sel.video.video_id),
            )
            if sel.video is not None
            else None
        )
        targets.append(
            CreatorTarget(
                creator_id=sel.creator.creator_id,
                name=sel.creator.name,
                weight=weight,
                video=video,
                selection_error=sel.error,
            )
        )
    return targets


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Unattended, idempotent transcript harvest for one gameweek"
    )
    parser.add_argument(
        "--gameweek", type=int, default=None, help="default: the next gameweek from bootstrap"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be fetched, from which source, and the budget left; spends nothing",
    )
    parser.add_argument(
        "--backstop-hours",
        type=float,
        default=DEFAULT_BACKSTOP_HOURS,
        help="within this many hours of the deadline, videos still without a transcript "
        "use captions regardless of budget or retry timers (default: %(default)s)",
    )
    parser.add_argument(
        "--cached-listings",
        action="store_true",
        help="allow the 6h YouTube uploads-listing cache (default: refresh, so a video "
        "posted an hour ago is seen)",
    )
    args = parser.parse_args(argv)

    from fpl_oracle.extract.run_extract import resolve_target_gameweek
    from fpl_oracle.fpl.client import get_bootstrap_static
    from fpl_oracle.logging_setup import configure_logging

    configure_logging()
    gameweek, published_after = resolve_target_gameweek(args.gameweek)
    deadline = deadline_for_gameweek(get_bootstrap_static()["events"], gameweek)
    targets = build_targets(gameweek, published_after, force_refresh=not args.cached_listings)
    config = HarvestConfig(backstop_hours=args.backstop_hours)

    if args.dry_run:
        print_report(plan_harvest(gameweek, targets, deadline, config))
        return 0
    try:
        result = run_harvest(gameweek, targets, deadline, config)
    except HarvestLockedError as e:
        print(str(e), file=sys.stderr)
        return 2
    print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
