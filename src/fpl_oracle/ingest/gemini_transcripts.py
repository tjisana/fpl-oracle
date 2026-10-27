"""Read a YouTube video's speech via Gemini, as an OFFICIAL alternative to
scraping caption tracks.

WHY THIS EXISTS. There is no official way to read a third-party YouTube
video's captions: `captions.download` on the YouTube Data API requires
OAuth as the video's owner and 403s for everyone else. So
`ingest/transcripts.py` reverse-engineers the internal endpoint YouTube's
own player calls — which works, has no quota because it has no contract,
and gets the IP blocked when it looks like scraping. It did, for hours,
costing real creators from a deadline harvest.

Gemini accepts a YouTube URL as a first-class input. Google owns both
sides, so this is supported access rather than a scrape: no bot
detection, no IP block. Two properties matter beyond that:

* It WATCHES the video, so it can read a name off an on-screen team sheet
  where the audio is ambiguous. Our worst-known quality problem is ASR
  mangling ('Burmo' for Mbeumo, 'Virgil van Djk'), which costs ~20% of
  picks at name resolution.
* Verbatim, not summarised — measured at 203 wpm against a 209 wpm
  caption baseline on comparable videos.

WHAT IT IS NOT. The YouTube-URL feature is in PREVIEW and free of charge
for now, with pricing and limits explicitly subject to change; the free
tier caps at 8 hours of video per day. This is a governed dependency
rather than a permanent one.

As of 2026-09-10 this IS the default transcript source, via
`ingest/sourcing.fetch_transcript_from` — see that module's docstring for
why: `ingest/transcripts.fetch_transcript`'s caption scrape has IP-blocked
this machine three gameweeks running, while Gemini is supported access
with no bot detection and (on GW2) resolved more picks by name. The
caption scraper is kept as an explicit opt-in (`source="captions"`) for
callers that need its real per-word timestamps and are willing to risk
the block — see `sourcing.py`.

TIMESTAMPS ARE REQUESTED, VALIDATED, AND USUALLY DISCARDED. Segment
timings power the report's timestamped YouTube deep links — the evidence
trail that lets a human check a pick against what the creator actually
said. So this asks Gemini for timed segments; it then CHECKS them against
the video's real duration from the YouTube Data API, and throws them away
when they don't fit.

They usually don't. Measured on the two videos this was first used for:
a 317s video came back with segments spanning 461s, and a 1832s video
came back as six segments spanning 2638s. The text was accurate in both
cases; the timings were not. A wrong deep link is worse than no deep
link — it looks right and sends a human to the wrong place — so an
untimed transcript is marked as such and the links degrade visibly
instead of silently lying.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from dotenv import load_dotenv
from pydantic import BaseModel

from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment

logger = logging.getLogger(__name__)

GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"

# Flash tier: this is transcription, not reasoning, and the roster is ~18
# videos a day. Pinned rather than "latest" so a model swap is a decision
# someone makes, not something that happens to a deadline run.
MODEL = "gemini-3.5-flash"

# The API returns 503 "high demand" intermittently — observed on 2 of 4
# calls during evaluation. Worth a couple of retries; not worth a long
# backoff on a deadline morning.
MAX_ATTEMPTS = 3

# Per-request timeout in milliseconds (the SDK's unit). See `_client`.
REQUEST_TIMEOUT_MS = 10 * 60 * 1000

# The free tier caps input at 250k tokens per MINUTE and a video costs
# ~100 tokens per second, so a single request cannot cover more than ~40
# minutes of video: FPL BlackBox's 87-minute show 429'd on every attempt
# on 2026-09-11, then ReadError'd, then hung a deadline-day run for 6.5
# hours. The answer is to read long videos in WINDOWS — the API takes a
# start/end offset per request — and to space the requests a quota window
# apart, since the cap is per minute, not per video. 30-minute windows
# (~180k tokens) leave margin under the cap; 60 s between request STARTS
# is the quota period itself.
CHUNK_SECONDS = 30 * 60
CHUNK_SPACING_SECONDS = 60.0

# Sanity ceiling, not a quota: nobody's team reveal is a three-hour stream,
# and a 3 h video is six chunks / six minutes of wall clock. Raise it with
# the environment variable if a creator's format ever warrants it.
MAX_VIDEO_SECONDS_ENV_VAR = "GEMINI_MAX_VIDEO_SECONDS"
DEFAULT_MAX_VIDEO_SECONDS = 3 * 3600


def max_video_seconds() -> float:
    load_dotenv()
    raw = os.environ.get(MAX_VIDEO_SECONDS_ENV_VAR)
    if not raw:
        return DEFAULT_MAX_VIDEO_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "gemini_transcripts: %s=%r is not a number — using the default %ds",
            MAX_VIDEO_SECONDS_ENV_VAR,
            raw,
            DEFAULT_MAX_VIDEO_SECONDS,
        )
        return DEFAULT_MAX_VIDEO_SECONDS


_PROMPT = (
    "Transcribe everything spoken in this video, verbatim, in order.\n"
    "Return timed segments covering the whole video.\n"
    "Where the audio is ambiguous, spell football player and club names using what "
    "is shown on screen (team sheets, graphics, lower thirds) — correct spelling of "
    "player names matters more than anything else in this transcript.\n"
    "Do not summarise, do not editorialise, do not skip sections."
)


class _WireSegment(BaseModel):
    """Gemini's wire shape. Deliberately loose — `start_seconds` as a float
    and nothing else required — so a slightly-off response is repaired
    below rather than failing the whole video."""

    start_seconds: float
    text: str


class _WireTranscript(BaseModel):
    language: str
    segments: list[_WireSegment]


def _client():
    load_dotenv()
    api_key = os.environ.get(GEMINI_API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(
            f"{GEMINI_API_KEY_ENV_VAR} is not set — needed to read YouTube video via Gemini"
        )
    from google import genai
    from google.genai import types

    # A hard per-request timeout. On the GW4 deadline-day run (2026-09-11)
    # a read failed with ReadError, and the retry then hung for 35+ minutes
    # with no timeout at all — the SDK's default is none. A 20-minute video
    # reads in 30-150 s; an 87-minute one in a few minutes. Ten minutes is
    # generous for any video the free tier can read, and short enough that
    # a wedged connection costs one retry, not the deadline.
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


# Gemini's segment starts may overrun the real video. Anything beyond the
# true duration by more than this is treated as fabricated timing, and ALL
# timings for that video are discarded rather than partially trusted —
# drift that large means the whole timeline is unreliable, not just its
# tail.
_TIMING_TOLERANCE_SECONDS = 5.0


def _to_transcript(
    video_id: str,
    wire: _WireTranscript,
    duration_seconds: float | None = None,
    *,
    chunked: bool = False,
    model: str = MODEL,
) -> Transcript:
    """Convert wire segments to our `Transcript`, deriving each segment's
    duration from the NEXT segment's start.

    Gemini reports where a segment begins; it does not report how long it
    lasts. Deriving duration from the following start is exact for
    contiguous speech and is the only honest option — inventing a fixed
    duration would put wrong offsets on the deep links the segments exist
    to power. The final segment gets a nominal duration since nothing
    follows it."""
    # A chunked read arrives in window order and its timings are per-window
    # (relative to each clip, or absolute, or invented — Gemini does not say
    # which), so they are never trusted and the segments are NOT re-sorted:
    # sorting by an unreliable start would interleave the windows.
    ordered = wire.segments if chunked else sorted(wire.segments, key=lambda s: s.start_seconds)

    # Validate the timeline before trusting any of it.
    timings_ok = True
    if chunked:
        timings_ok = False
    elif duration_seconds is not None and ordered:
        span = ordered[-1].start_seconds
        if span > duration_seconds + _TIMING_TOLERANCE_SECONDS:
            logger.warning(
                "gemini_transcripts: %s — segment timings span %.0fs but the video is "
                "%.0fs; discarding ALL timings as unreliable (text is kept)",
                video_id,
                span,
                duration_seconds,
            )
            timings_ok = False
    elif duration_seconds is None:
        logger.warning(
            "gemini_transcripts: %s — no known duration to validate timings against; "
            "discarding them rather than shipping unverified deep links",
            video_id,
        )
        timings_ok = False
    segments: list[TranscriptSegment] = []
    for i, seg in enumerate(ordered):
        text = seg.text.strip()
        if not text:
            continue
        if not timings_ok:
            # Zeroed, not guessed. A consumer building a deep link from
            # 0.0 lands at the video start, which is obviously a
            # non-answer; a plausible-looking wrong offset is not.
            segments.append(TranscriptSegment(text=text, start=0.0, duration=0.0))
            continue
        duration = (
            max(0.0, ordered[i + 1].start_seconds - seg.start_seconds)
            if i + 1 < len(ordered)
            else 5.0
        )
        segments.append(TranscriptSegment(text=text, start=seg.start_seconds, duration=duration))
    return Transcript(
        video_id=video_id,
        language=wire.language or "en",
        is_generated=True,
        segments=segments,
        source=(
            f"gemini:{model}:untimed:chunked"
            if chunked
            else f"gemini:{model}"
            if timings_ok
            else f"gemini:{model}:untimed"
        ),
    )


def _call_with_deadline[T](fn: Callable[[], T], seconds: float) -> T:
    """Run `fn` with a WALL-CLOCK deadline, independent of the socket.

    The SDK's `HttpOptions.timeout` becomes an httpx per-read timeout, and
    on 2026-09-11 that did not fire: the server kept the connection
    ESTABLISHED for 6.5 hours (each read returning something, so no read
    ever timed out) while a deadline-day run sat on one video. A daemon
    thread plus `join(timeout)` bounds the wait regardless of what the
    connection does; on expiry the thread is abandoned (daemon, so it
    cannot hold the interpreter open) and `TimeoutError` is raised, which
    the retry loop treats like any other per-attempt failure."""
    result: list[T] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(fn())
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            error.append(e)

    worker = threading.Thread(target=run, name="gemini-generate", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"Gemini request exceeded the {seconds:.0f}s wall-clock deadline")
    if error:
        raise error[0]
    return result[0]


def chunk_windows(
    duration_seconds: float, chunk_seconds: float = CHUNK_SECONDS
) -> list[tuple[float, float]]:
    """Consecutive (start, end) windows covering the video. One window when
    it fits; otherwise `chunk_seconds`-long windows with a short tail."""
    if duration_seconds <= chunk_seconds:
        return [(0.0, float(duration_seconds))]
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_seconds:
        end = min(start + chunk_seconds, float(duration_seconds))
        windows.append((start, end))
        start = end
    return windows


class _GateAbortedError(Exception):
    """Internal: `before_request` raised; unwrapped and re-raised as-is."""


def gemini_api_key_present() -> bool:
    """True if a Gemini API key is configured (never reveals the value)."""
    load_dotenv()
    return bool(os.environ.get(GEMINI_API_KEY_ENV_VAR))


def _read_window(
    video_id: str,
    window: tuple[float, float] | None,
    part: tuple[int, int] | None,
    *,
    model: str = MODEL,
    max_attempts: int | None = None,
    before_request: Callable[[], None] | None = None,
    retry_delay: Callable[[Exception, int], float | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> _WireTranscript:
    """One request with retries. `window=None` reads the whole video (the
    short-video path, unchanged); otherwise only [start, end) via the
    API's video offsets, with the prompt told which part it is reading.
    Raises the last error after `max_attempts` (default `MAX_ATTEMPTS`).

    The keyword-only parameters are the seam the unattended harvester
    (`ingest/harvest.py`) uses to own Gemini's scarce request budget;
    defaults leave every existing caller's behaviour unchanged.
    `before_request` runs before EVERY API attempt, retries included, and
    may raise to abort (it is outside the retry try-block, so its error
    propagates untouched). `retry_delay(error, attempt)` replaces the blind
    retry: it returns seconds to sleep before the next attempt, or None to
    stop retrying and raise."""
    attempts_allowed = MAX_ATTEMPTS if max_attempts is None else max_attempts
    from google.genai import types

    url = f"https://www.youtube.com/watch?v={video_id}"
    prompt = _PROMPT
    video_part = types.Part(file_data=types.FileData(file_uri=url))
    if window is not None and part is not None:
        start, end = window
        video_part = types.Part(
            file_data=types.FileData(file_uri=url),
            video_metadata=types.VideoMetadata(
                start_offset=f"{int(start)}s", end_offset=f"{int(end)}s"
            ),
        )
        prompt = (
            f"{_PROMPT}\n"
            f"This request covers ONLY minutes {start / 60:.0f} to {end / 60:.0f} of a longer "
            f"video (part {part[0]} of {part[1]}). Transcribe everything spoken in that "
            f"window and nothing outside it."
        )
    last_error: Exception | None = None
    for attempt in range(1, attempts_allowed + 1):
        try:
            # Client construction is INSIDE the loop deliberately. It can
            # fail for configuration reasons (no API key), and the caller
            # promises never to raise — a caller mid-run must get a None it
            # can record and step over. The cause is logged either way.
            client = _client()
            # The gate fires only AFTER the client is built, so a request
            # that can never reach the API (missing key) is never counted
            # against a budget. Its own errors abort the read untouched.
            if before_request is not None:
                try:
                    before_request()
                except Exception as gate_error:
                    raise _GateAbortedError from gate_error
            response = _call_with_deadline(
                lambda client=client: client.models.generate_content(
                    model=model,
                    contents=types.Content(parts=[video_part, types.Part(text=prompt)]),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_WireTranscript,
                    ),
                ),
                seconds=REQUEST_TIMEOUT_MS / 1000,
            )
            parsed = response.parsed
            if parsed is None:
                raise ValueError("Gemini returned no parseable transcript")
            wire = (
                parsed
                if isinstance(parsed, _WireTranscript)
                else _WireTranscript.model_validate(parsed)
            )
            if not any(seg.text.strip() for seg in wire.segments):
                raise ValueError("Gemini returned zero usable segments")
            return wire
        except _GateAbortedError as aborted:
            assert aborted.__cause__ is not None
            raise aborted.__cause__ from None
        except Exception as e:
            last_error = e
            logger.warning(
                "gemini_transcripts: %s%s — attempt %d/%d failed: %s",
                video_id,
                f" part {part[0]}/{part[1]}" if part else "",
                attempt,
                attempts_allowed,
                type(e).__name__,
            )
            if retry_delay is not None:
                delay = retry_delay(e, attempt)
                if delay is None:
                    raise
                if delay > 0:
                    sleep(delay)
    assert last_error is not None
    raise last_error


class VideoTooLongError(Exception):
    """The video exceeds `max_video_seconds()`; refused before any request."""


def read_video_via_gemini(
    video_id: str,
    duration_seconds: float | None = None,
    *,
    model: str = MODEL,
    max_attempts: int | None = None,
    before_request: Callable[[], None] | None = None,
    retry_delay: Callable[[Exception, int], float | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Transcript:
    """Like `fetch_transcript_via_gemini` but RAISES instead of returning
    None, so a caller that budgets requests can classify the failure
    (503 vs per-minute 429 vs daily 429). `model`, `max_attempts`,
    `before_request` and `retry_delay` are documented on `_read_window`;
    `before_request` fires once per API attempt across all windows.
    Raises `VideoTooLongError` (no request made), or whatever the last
    failed attempt raised, or ValueError for an unusable response."""
    limit = max_video_seconds()
    if duration_seconds is not None and duration_seconds > limit:
        logger.warning(
            "gemini_transcripts: %s — %.0f min video exceeds the %.0f min ceiling "
            "(%s); skipping without a request",
            video_id,
            duration_seconds / 60,
            limit / 60,
            MAX_VIDEO_SECONDS_ENV_VAR,
        )
        raise VideoTooLongError(video_id)

    def read(window: tuple[float, float] | None, part: tuple[int, int] | None) -> _WireTranscript:
        return _read_window(
            video_id,
            window,
            part,
            model=model,
            max_attempts=max_attempts,
            before_request=before_request,
            retry_delay=retry_delay,
            sleep=sleep,
        )

    windows = chunk_windows(duration_seconds) if duration_seconds is not None else None
    chunked = windows is not None and len(windows) > 1
    if not chunked:
        wire = read(None, None)
        transcript = _to_transcript(video_id, wire, duration_seconds, model=model)
    else:
        assert windows is not None
        merged: list[_WireSegment] = []
        language = "en"
        previous_start: float | None = None
        for i, window in enumerate(windows, start=1):
            if previous_start is not None:
                wait = CHUNK_SPACING_SECONDS - (clock() - previous_start)
                if wait > 0:
                    sleep(wait)
            previous_start = clock()
            wire = read(window, (i, len(windows)))
            language = wire.language or language
            merged.extend(wire.segments)
            logger.info(
                "gemini_transcripts: %s — part %d/%d (%.0f-%.0f min): %d segments",
                video_id,
                i,
                len(windows),
                window[0] / 60,
                window[1] / 60,
                len(wire.segments),
            )
        transcript = _to_transcript(
            video_id,
            _WireTranscript(language=language, segments=merged),
            duration_seconds,
            chunked=True,
            model=model,
        )

    if not transcript.segments:
        raise ValueError("no usable segments")
    logger.info(
        "gemini_transcripts: %s — %d segments, %d words (%s%s)",
        video_id,
        len(transcript.segments),
        len(transcript.full_text.split()),
        model,
        f", {len(windows)} windows" if chunked and windows else "",
    )
    return transcript


def fetch_transcript_via_gemini(
    video_id: str,
    duration_seconds: float | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Transcript | None:
    """Read `video_id`'s speech via Gemini. Returns None if it can't be
    read after `MAX_ATTEMPTS` (per request) — a per-video failure, never
    fatal, matching `fetch_transcript`'s contract so the two are
    interchangeable.

    Videos longer than `CHUNK_SECONDS` are read in consecutive windows
    (see the constants above for why), one request per window, with
    request starts spaced `CHUNK_SPACING_SECONDS` apart so the free tier's
    per-minute cap is never exceeded. A chunked read is all-or-nothing: a
    window that fails after its retries loses the video, because a
    transcript with a silent hole in the middle could be missing the very
    team reveal the video exists for. Chunked transcripts are always
    `:untimed:chunked` — window timings cannot be trusted or stitched.
    An unknown duration is read as a single request, as before."""
    try:
        return read_video_via_gemini(video_id, duration_seconds, sleep=sleep, clock=clock)
    except VideoTooLongError:
        return None  # already warned in read_video_via_gemini
    except Exception as e:  # noqa: BLE001 - per-video failure, never fatal
        logger.error("gemini_transcripts: %s — giving up: %s", video_id, e)
        return None
