"""Claude structured-output extraction of picks from a transcript.

The LLM emits pick-level content only (`_LLMExtraction`: picks + gameweek);
video metadata is filled in by the pipeline so the model never parrots
identifiers it could mangle. Validation failures are retried with the
error fed back (the schema's numeric/length bounds are enforced
client-side by the SDK, so a single over-long `reasoning` sentence must
not cost a whole video's picks).

Refusal fallbacks (`fallbacks=` beta) are deliberately not wired up:
football transcript extraction has negligible refusal risk, and a
refusal here raises `ExtractionError` loudly instead of degrading.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import anthropic
from anthropic.types import MessageParam
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from fpl_oracle.extract.schemas import Pick, VideoExtraction

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000
MAX_VALIDATION_RETRIES = 2

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "extract_picks.txt").read_text()

_client: anthropic.Anthropic | None = None


class ExtractionError(RuntimeError):
    """The model could not produce a valid extraction (refusal, truncation,
    or schema-invalid output after retries)."""


class _LLMExtraction(BaseModel):
    """The slice of VideoExtraction the LLM is allowed to emit."""

    gameweek: int | None = Field(
        ge=1,
        le=38,
        description=(
            "The gameweek the video is about, if stated or inferable from "
            "title/context; null otherwise."
        ),
    )
    picks: list[Pick]


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        load_dotenv()
        _client = anthropic.Anthropic()
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
    """Extract picks from one transcript; pipeline metadata is stamped here,
    never emitted by the model. `player_id` on every pick stays None — the
    fpl/ resolver runs post-extraction."""
    messages: list[MessageParam] = [
        {
            "role": "user",
            "content": build_user_message(
                video_title, creator_name, transcript_text, channel_context
            ),
        }
    ]

    llm_out: _LLMExtraction | None = None
    for attempt in range(1 + MAX_VALIDATION_RETRIES):
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
            output_format=_LLMExtraction,
        )
        if response.stop_reason == "refusal":
            raise ExtractionError(f"model refused extraction for video {video_id}")
        if response.stop_reason == "max_tokens":
            raise ExtractionError(
                f"extraction truncated at {MAX_TOKENS} tokens for video {video_id}"
            )
        try:
            llm_out = response.parsed_output
            if llm_out is None:  # SDK returns None if the text wasn't parseable
                raise ExtractionError(f"no parseable output for video {video_id}")
            break
        except ValidationError as e:
            if attempt == MAX_VALIDATION_RETRIES:
                raise ExtractionError(
                    f"schema-invalid extraction for video {video_id} "
                    f"after {MAX_VALIDATION_RETRIES} retries: {e}"
                ) from e
            text = next((b.text for b in response.content if b.type == "text"), "")
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

    assert llm_out is not None
    return VideoExtraction(
        creator_id=creator_id,
        video_id=video_id,
        video_title=video_title,
        published_at=published_at,
        gameweek=llm_out.gameweek,
        picks=llm_out.picks,
    )
