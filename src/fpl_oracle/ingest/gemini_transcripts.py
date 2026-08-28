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
rather than a permanent one, and it is deliberately NOT the default path
— `ingest/transcripts.fetch_transcript` still is. Use this as a fallback
when scraping is blocked, or deliberately per-creator.

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

    return genai.Client(api_key=api_key)


# Gemini's segment starts may overrun the real video. Anything beyond the
# true duration by more than this is treated as fabricated timing, and ALL
# timings for that video are discarded rather than partially trusted —
# drift that large means the whole timeline is unreliable, not just its
# tail.
_TIMING_TOLERANCE_SECONDS = 5.0


def _to_transcript(
    video_id: str, wire: _WireTranscript, duration_seconds: float | None = None
) -> Transcript:
    """Convert wire segments to our `Transcript`, deriving each segment's
    duration from the NEXT segment's start.

    Gemini reports where a segment begins; it does not report how long it
    lasts. Deriving duration from the following start is exact for
    contiguous speech and is the only honest option — inventing a fixed
    duration would put wrong offsets on the deep links the segments exist
    to power. The final segment gets a nominal duration since nothing
    follows it."""
    ordered = sorted(wire.segments, key=lambda s: s.start_seconds)

    # Validate the timeline before trusting any of it.
    timings_ok = True
    if duration_seconds is not None and ordered:
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
        source=f"gemini:{MODEL}" if timings_ok else f"gemini:{MODEL}:untimed",
    )


def fetch_transcript_via_gemini(
    video_id: str, duration_seconds: float | None = None
) -> Transcript | None:
    """Read `video_id`'s speech via Gemini. Returns None if it can't be
    read after `MAX_ATTEMPTS` — a per-video failure, never fatal, matching
    `fetch_transcript`'s contract so the two are interchangeable."""
    from google.genai import types

    url = f"https://www.youtube.com/watch?v={video_id}"
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # Client construction is INSIDE the loop deliberately. It can
            # fail for configuration reasons (no API key), and this
            # function promises never to raise — a caller mid-run must get
            # a None it can record and step over, not an exception that
            # ends the run. The cause is logged either way.
            client = _client()
            response = client.models.generate_content(
                model=MODEL,
                contents=types.Content(
                    parts=[
                        types.Part(file_data=types.FileData(file_uri=url)),
                        types.Part(text=_PROMPT),
                    ]
                ),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_WireTranscript,
                ),
            )
            parsed = response.parsed
            if parsed is None:
                raise ValueError("Gemini returned no parseable transcript")
            wire = (
                parsed
                if isinstance(parsed, _WireTranscript)
                else _WireTranscript.model_validate(parsed)
            )
            transcript = _to_transcript(video_id, wire, duration_seconds)
            if not transcript.segments:
                raise ValueError("Gemini returned zero usable segments")
            logger.info(
                "gemini_transcripts: %s — %d segments, %d words (%s)",
                video_id,
                len(transcript.segments),
                len(transcript.full_text.split()),
                MODEL,
            )
            return transcript
        except Exception as e:  # noqa: BLE001 - per-video failure, never fatal
            last_error = e
            logger.warning(
                "gemini_transcripts: %s — attempt %d/%d failed: %s",
                video_id,
                attempt,
                MAX_ATTEMPTS,
                type(e).__name__,
            )

    logger.error("gemini_transcripts: %s — giving up: %s", video_id, last_error)
    return None
