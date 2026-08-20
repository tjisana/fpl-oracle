"""The nuance pass: what the creators WORRIED about, in their own words.

The pipeline up to here is arithmetic. Weighted votes decide who makes
the squad, and the run record can already tell you that 11 creators
backed Haaland and 2 argued against him. What it cannot tell you is what
those 2 actually said — that lives in the `reasoning` sentence attached
to each extracted pick, 15 squad players' worth of prose that nobody is
going to read on deadline morning. This module reads it for you.

THREE DESIGN CONSTRAINTS, all pointing the same way.

GROUNDED, NOT CONSULTED. The model is given ONLY the creators' own
reasoning strings — no stats, no fixtures, no prices beyond the label on
the player line. It is a reporter of what was said, never an analyst.
The whole project's premise is that aggregated creator expertise beats a
model's own football take; letting the nuance pass opine would smuggle
exactly the thing we deliberately excluded back into the output at the
last step, wearing the creators' clothes.

ATTRIBUTION IS CHECKED MECHANICALLY; WORDING IS NOT. A concern
attributed to a creator who never voted on that player is dropped by
`validate_nuance()`, not by a promise in the prompt — the same stance as
the name resolver's "reject, never fabricate". Dropped ids are counted
in the `NuanceRecord` so a silent haircut is still visible in the audit
trail. But `validate_nuance()` only ever touches `creator_ids` — a
`Concern.note`, `SquadNuance.captaincy_note`, and `squad_notes` are free
prose it passes through untouched. Nothing mechanically checks that the
TEXT matches what was said, only that the names attached to it did vote.

NON-FATAL BY CONSTRUCTION. This is the one stage of the pipeline that is
pure commentary: the squad is already solved before it runs. An API
failure here must never cost you a squad on deadline morning, so
`generate_nuance()` returns a `failed=True` record rather than raising,
and the report renders without the section.

Dissent COUNTS stay mechanical (`report/gameweek.py` computes them from
`PlayerScore.votes`). This module only supplies the words behind them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import anthropic
from anthropic.types import MessageParam
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fpl_oracle.consensus.captaincy import CaptaincyElection
from fpl_oracle.consensus.scoring import CreatorVote, PlayerScore
from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.fpl.players import PlayerDB
from fpl_oracle.report.models import (
    Concern,
    ConcernKind,
    NuanceRecord,
    PlayerNuance,
    SquadNuance,
)
from fpl_oracle.solver.squad import Squad

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
# The prompt carries at most 15 players x a handful of one-sentence
# reasons, so output is small; the cap is headroom for thinking, which is
# on by default on claude-opus-5.
MAX_TOKENS = 16_000
MAX_VALIDATION_RETRIES = 2

# Supporting votes shown per player. The evidence is already deduped to
# one vote per creator per player upstream, and the tail below the top
# few is near-zero-weight noise that would only dilute the prompt.
MAX_SUPPORTING_VOTES = 6

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "nuance_pass.txt").read_text()

_client: anthropic.Anthropic | None = None


# Same reasoning as extract/extractor.py: the SDK's 600s read timeout x
# max_retries=2 x this module's own validation retries is an unbounded-
# feeling wait against a hard deadline wall. Commentary is the LAST thing
# that should hold up shipping a squad, so it fails fast — and a failure
# here is already non-fatal.
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_HTTP_RETRIES = 1


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        load_dotenv()
        _client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_HTTP_RETRIES)
    return _client


def _desc(model: type[BaseModel], name: str) -> str:
    return model.model_fields[name].description or ""


# ---------------------------------------------------------------------------
# Wire schema — mirrors report/models.py minus the client-side constraints,
# so `messages.parse()` only fails on genuinely malformed output (in
# practice: truncation). Strict validation happens in the retry loop, the
# same split extract/extractor.py uses.
# ---------------------------------------------------------------------------


class _WireConcern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ConcernKind
    note: str = Field(description=_desc(Concern, "note"))
    creator_ids: list[str] = Field(description=_desc(Concern, "creator_ids"))
    severity: int = Field(description=_desc(Concern, "severity"))


class _WirePlayerNuance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: int = Field(description="the player_id exactly as given in the squad list")
    summary: str = Field(description=_desc(PlayerNuance, "summary"))
    concerns: list[_WireConcern]


class _WireNuance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    players: list[_WirePlayerNuance]
    captaincy_note: str | None = Field(description=_desc(SquadNuance, "captaincy_note"))
    squad_notes: list[str] = Field(description=_desc(SquadNuance, "squad_notes"))


def _to_strict(wire: _WireNuance) -> SquadNuance:
    return SquadNuance(
        players=[
            PlayerNuance(
                player_id=p.player_id,
                summary=p.summary,
                concerns=[
                    Concern(
                        kind=c.kind,
                        note=c.note,
                        creator_ids=c.creator_ids,
                        severity=c.severity,
                    )
                    for c in p.concerns
                ],
            )
            for p in wire.players
        ],
        captaincy_note=wire.captaincy_note,
        squad_notes=wire.squad_notes,
    )


# ---------------------------------------------------------------------------
# Prompt assembly — pure, so the evidence bundle can be unit-tested without
# an API key.
# ---------------------------------------------------------------------------


def _reasoning_index(extractions: list[ResolvedExtraction]) -> dict[tuple[str, int], list[str]]:
    """(creator_id, player_id) -> that creator's reasoning sentences for
    that player. A creator can produce several picks for one player (a
    squad_include and a captain pick, say); consensus collapses those to
    one vote, but the WORDS from each are all evidence, so they are kept
    and deduplicated in first-seen order."""
    index: dict[tuple[str, int], list[str]] = {}
    for resolved in extractions:
        creator_id = resolved.extraction.creator_id
        for pick in resolved.extraction.picks:
            if pick.player_id is None:
                continue
            reasons = index.setdefault((creator_id, pick.player_id), [])
            text = pick.reasoning.strip()
            if text and text not in reasons:
                reasons.append(text)
    return index


def _vote_line(
    vote: CreatorVote,
    creator_names: dict[str, str],
    reasons: list[str],
) -> str:
    name = creator_names.get(vote.creator_id, vote.creator_id)
    reason = " | ".join(reasons) if reasons else "(no reasoning captured)"
    return (
        f"    - {vote.creator_id} ({name}, weight {vote.creator_weight:.2f}) "
        f"{vote.action.value}, conviction {vote.conviction}: {reason}"
    )


def build_user_message(
    *,
    squad: Squad,
    scores: dict[int, PlayerScore],
    election: CaptaincyElection,
    extractions: list[ResolvedExtraction],
    db: PlayerDB,
    creator_names: dict[str, str],
) -> str:
    """Assemble the evidence bundle. Pure: no network, no clock.

    Deliberately narrow — player identity, the votes, and the creators'
    own words. No form, no fixtures, no ownership: anything extra here is
    an invitation for the model to reason about football instead of
    reporting what was said.
    """
    reasoning = _reasoning_index(extractions)
    lines: list[str] = ["SQUAD (15 players, already solved — do not re-pick):"]

    for player in squad.players:
        p = db.get(player.player_id)
        name = p.web_name if p else f"id {player.player_id}"
        team = p.team_short if p else "?"
        role = "XI" if player.starting else "BENCH"
        score = scores.get(player.player_id)
        lines.append(
            f"  PLAYER player_id={player.player_id} {name} "
            f"({player.position.value}, {team}, £{player.price_m}m, {role})"
        )
        if score is None:
            lines.append("    (no creator votes — selected on price/structure alone)")
            continue
        lines.append(
            f"    votes: {score.backers} for, {score.detractors} against, "
            f"band_score {score.band_score:.2f} in band {score.band}"
        )
        ranked = sorted(score.votes, key=lambda v: -abs(v.value))[:MAX_SUPPORTING_VOTES]
        for vote in ranked:
            lines.append(
                _vote_line(
                    vote, creator_names, reasoning.get((vote.creator_id, player.player_id), [])
                )
            )

    squad_ids = {p.player_id for p in squad.players}
    lines.append("")
    lines.append("CAPTAINCY ELECTION (best first):")
    for candidate in election.candidates:
        p = db.get(candidate.player_id)
        name = p.web_name if p else f"id {candidate.player_id}"
        in_squad = "in squad" if candidate.player_id in squad_ids else "NOT in squad"
        lines.append(
            f"  {name} (player_id={candidate.player_id}) — score {candidate.score:.2f}, "
            f"{candidate.voters} voter(s), {candidate.share:.0%} of the leader, {in_squad}"
        )
    lines.append(
        f"  {election.total_voters} of {election.contributing_creators} contributing creators "
        f"named a captain{' — THIN evidence' if election.thin else ''}."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mechanical validation of the model's attributions.
# ---------------------------------------------------------------------------


def allowed_creators(squad: Squad, scores: dict[int, PlayerScore]) -> dict[int, set[str]]:
    """Per squad player, the creators who actually voted on him — the only
    ids a concern about that player may name."""
    allowed: dict[int, set[str]] = {}
    for player in squad.players:
        score = scores.get(player.player_id)
        allowed[player.player_id] = {v.creator_id for v in score.votes} if score else set()
    return allowed


def validate_nuance(
    nuance: SquadNuance, allowed: dict[int, set[str]]
) -> tuple[SquadNuance, list[str]]:
    """Strip anything the model made up: players outside the squad, and
    creator ids that did not vote on the player the concern is about.

    A concern left with NO valid attribution is dropped entirely — an
    unattributed caveat is indistinguishable from the model's own
    opinion, which is the one thing this pass must not produce. Returns
    the cleaned nuance plus the dropped `player_id:creator_id` pairs for
    the audit record.
    """
    dropped: list[str] = []
    players: list[PlayerNuance] = []

    for player in nuance.players:
        if player.player_id not in allowed:
            dropped.append(f"{player.player_id}:<not in squad>")
            continue
        permitted = allowed[player.player_id]
        concerns: list[Concern] = []
        for concern in player.concerns:
            kept = [cid for cid in concern.creator_ids if cid in permitted]
            dropped.extend(
                f"{player.player_id}:{cid}" for cid in concern.creator_ids if cid not in permitted
            )
            if not kept:
                continue
            concerns.append(concern.model_copy(update={"creator_ids": kept}))
        players.append(player.model_copy(update={"concerns": concerns}))

    return (
        SquadNuance(
            players=players,
            captaincy_note=nuance.captaincy_note,
            squad_notes=nuance.squad_notes,
        ),
        dropped,
    )


# ---------------------------------------------------------------------------
# The call.
# ---------------------------------------------------------------------------


class NuanceError(RuntimeError):
    """The nuance call could not produce valid output. Caught by
    `generate_nuance()` — never allowed to reach the pipeline."""


def _call_model(user_message: str) -> SquadNuance:
    messages: list[MessageParam] = [{"role": "user", "content": user_message}]

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
                output_format=_WireNuance,
            )
        except ValidationError as e:
            raise NuanceError(f"malformed model output (likely truncated): {e}") from e

        if response.stop_reason == "refusal":
            raise NuanceError("model refused the nuance pass")
        if response.stop_reason == "max_tokens":
            raise NuanceError(f"nuance output truncated at {MAX_TOKENS} tokens")
        wire = response.parsed_output
        if wire is None:
            raise NuanceError("no parseable nuance output")

        try:
            return _to_strict(wire)
        except ValidationError as e:
            if attempt == MAX_VALIDATION_RETRIES:
                raise NuanceError(
                    f"schema-invalid nuance after {MAX_VALIDATION_RETRIES} retries: {e}"
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
                        "Re-emit the full nuance output, corrected to satisfy the schema."
                    ),
                }
            )

    raise AssertionError("unreachable")  # loop always returns or raises


def generate_nuance(
    run_id: str,
    squad: Squad,
    scores: dict[int, PlayerScore],
    election: CaptaincyElection,
    extractions: list[ResolvedExtraction],
    db: PlayerDB,
    creator_names: dict[str, str] | None = None,
) -> NuanceRecord:
    """Run the nuance pass over an already-solved squad.

    NEVER raises: the squad is already decided by the time this runs, so
    a failure returns a `failed=True` record and the report is rendered
    without the section. Costs one Claude call.
    """
    generated_at = datetime.now(UTC)
    user_message = build_user_message(
        squad=squad,
        scores=scores,
        election=election,
        extractions=extractions,
        db=db,
        creator_names=creator_names or {},
    )

    try:
        raw = _call_model(user_message)
    except Exception as e:  # noqa: BLE001 — deliberately broad: the squad is
        # already solved by the time this runs, so ANY failure here (a
        # malformed-response TypeError, not just the anthropic/NuanceError
        # cases we anticipate) must degrade to a missing section, never
        # propagate and cost a deadline-morning squad.
        logger.warning("nuance pass failed (non-fatal, report renders without it): %s", e)
        return NuanceRecord(
            run_id=run_id,
            generated_at=generated_at,
            model=MODEL,
            nuance=SquadNuance(),
            failed=True,
            failure_reason=str(e),
        )

    cleaned, dropped = validate_nuance(raw, allowed_creators(squad, scores))
    if dropped:
        logger.warning(
            "nuance pass: dropped %d unattributable concern attribution(s): %s",
            len(dropped),
            ", ".join(dropped),
        )
    return NuanceRecord(
        run_id=run_id,
        generated_at=generated_at,
        model=MODEL,
        nuance=cleaned,
        dropped_creator_ids=dropped,
    )
