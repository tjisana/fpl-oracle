"""Claude structured-output extraction of picks from a transcript.

The LLM emits pick-level content only, against an UNCONSTRAINED wire
schema (`_WireExtraction`): same fields and descriptions as the real
schemas, minus `player_id` (so the model can never emit an FPL element
id) and minus numeric/length bounds. Enums stay in the wire schema —
the API enforces those in the output grammar. Because the wire model
has no client-side constraints, `messages.parse()` only raises when the
output isn't valid JSON at all (in practice: max_tokens truncation).

Strict validation happens here, converting wire picks into the real
`Pick`/`VideoExtraction` models. A constraint failure (over-long
`reasoning`, out-of-range `conviction`/`gameweek`) is retried with the
validation error fed back, so one bad field can't cost a whole video's
picks. Video metadata is stamped by the pipeline, never by the model.

Refusal fallbacks (`fallbacks=` beta) are deliberately not wired up:
football transcript extraction has negligible refusal risk, and a
refusal here raises `ExtractionError` loudly instead of degrading.

Cache note: the system prompt (~600 tokens) sits just above
claude-opus-5's 512-token cacheable minimum. On the first real batch
run, verify `usage.cache_read_input_tokens > 0` across consecutive
videos before assuming the cache saving.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

import anthropic
from anthropic.types import MessageParam
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fpl_oracle.extract.schemas import Pick, PickAction, Provenance, VideoExtraction

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
# Hard cap on thinking + output together (thinking is ON by default on
# claude-opus-5). A 15-pick reveal is ~3-5K output tokens; if truncation
# bites on real videos, the levers are output_config effort "medium"
# (extraction is not deep-reasoning work) or streaming with a higher cap.
MAX_TOKENS = 16_000
MAX_VALIDATION_RETRIES = 2

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "extract_picks.txt").read_text()

_client: anthropic.Anthropic | None = None


class ExtractionError(RuntimeError):
    """The model could not produce a valid extraction (refusal, truncation,
    or schema-invalid output after retries)."""


def _desc(model: type[BaseModel], name: str) -> str:
    return model.model_fields[name].description or ""


class _WirePick(BaseModel):
    """LLM-facing pick: `Pick` minus `player_id`, minus constraints.
    Descriptions are shared with `Pick` so the two contracts can't drift."""

    model_config = ConfigDict(extra="forbid")

    player_name_raw: str = Field(description=_desc(Pick, "player_name_raw"))
    team_inferred: str | None = Field(description=_desc(Pick, "team_inferred"))
    position_inferred: Literal["GK", "DEF", "MID", "FWD"] | None = Field(
        description=_desc(Pick, "position_inferred")
    )
    action: PickAction = Field(description=_desc(Pick, "action"))
    conviction: int = Field(description=_desc(Pick, "conviction"))
    time_horizon: int = Field(description=_desc(Pick, "time_horizon"))
    reasoning: str = Field(description=_desc(Pick, "reasoning"))
    provenance: Provenance = Field(description=_desc(Pick, "provenance"))


class _WireExtraction(BaseModel):
    """The slice of VideoExtraction the LLM is allowed to emit."""

    model_config = ConfigDict(extra="forbid")

    gameweek: int | None = Field(description=_desc(VideoExtraction, "gameweek"))
    picks: list[_WirePick]


# The SDK defaults (read timeout 600s, max_retries=2) compound badly here:
# 3 validation attempts x 3 HTTP attempts x 600s is 90 minutes on ONE
# creator, and `run_extraction` walks ~18 of them sequentially against a
# hard deadline wall. A GW1 reveal extraction that hasn't answered in three
# minutes is not going to; failing fast lets the next creator run, and
# per-creator failures are already non-fatal.
REQUEST_TIMEOUT_SECONDS = 180.0
MAX_HTTP_RETRIES = 1


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        load_dotenv()
        _client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_HTTP_RETRIES)
    return _client


def build_user_message(
    video_title: str, creator_name: str, transcript_text: str, channel_context: str | None
) -> str:
    """Pure prompt-assembly, split out for testability."""
    context_line = f"Channel context: {channel_context}\n" if channel_context else ""
    return (
        f"Creator: {creator_name}\n"
        f"Video title: {video_title}\n"
        f"{context_line}"
        f"Transcript:\n{transcript_text}"
    )


def _to_strict(
    wire: _WireExtraction,
    *,
    creator_id: str,
    video_id: str,
    video_title: str,
    published_at: datetime,
) -> VideoExtraction:
    """Strictly validate wire output into the real schemas; player_id is
    forced to None (the fpl/ resolver runs post-extraction). Raises
    pydantic.ValidationError on any constraint violation."""
    return VideoExtraction(
        creator_id=creator_id,
        video_id=video_id,
        video_title=video_title,
        published_at=published_at,
        gameweek=wire.gameweek,
        picks=[Pick.model_validate({**p.model_dump(), "player_id": None}) for p in wire.picks],
    )


def extract_picks(
    *,
    creator_id: str,
    creator_name: str,
    video_id: str,
    video_title: str,
    published_at: datetime,
    transcript_text: str,
    channel_context: str | None = None,
) -> VideoExtraction:
    """Extract picks from one transcript into a validated VideoExtraction."""
    messages: list[MessageParam] = [
        {
            "role": "user",
            "content": build_user_message(
                video_title, creator_name, transcript_text, channel_context
            ),
        }
    ]

    for attempt in range(1 + MAX_VALIDATION_RETRIES):
        try:
            response = _get_client().messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
                output_format=_WireExtraction,
            )
        except ValidationError as e:
            # The wire schema is unconstrained, so the SDK's eager parse only
            # fails on non-JSON output — with json_schema-constrained
            # generation that effectively means max_tokens truncation.
            raise ExtractionError(
                f"malformed model output for video {video_id} "
                f"(likely truncated at {MAX_TOKENS} tokens): {e}"
            ) from e

        if response.stop_reason == "refusal":
            raise ExtractionError(f"model refused extraction for video {video_id}")
        if response.stop_reason == "max_tokens":
            raise ExtractionError(
                f"extraction truncated at {MAX_TOKENS} tokens for video {video_id}"
            )
        wire = response.parsed_output
        if wire is None:
            raise ExtractionError(f"no parseable output for video {video_id}")

        try:
            extraction = _to_strict(
                wire,
                creator_id=creator_id,
                video_id=video_id,
                video_title=video_title,
                published_at=published_at,
            )
        except ValidationError as e:
            if attempt == MAX_VALIDATION_RETRIES:
                raise ExtractionError(
                    f"schema-invalid extraction for video {video_id} "
                    f"after {MAX_VALIDATION_RETRIES} retries: {e}"
                ) from e
            text = next((b.text for b in response.content if b.type == "text"), "")
            if text:
                messages.append({"role": "assistant", "content": text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous output failed schema validation:\n"
                        f"{e}\n"
                        "Re-emit the full extraction, corrected to satisfy the schema."
                    ),
                }
            )
            continue

        if not extraction.picks:
            logger.warning(
                "zero picks extracted from video %s (%r) — worth a human look",
                video_id,
                video_title,
            )
        return extraction

    raise AssertionError("unreachable")  # loop always returns or raises
