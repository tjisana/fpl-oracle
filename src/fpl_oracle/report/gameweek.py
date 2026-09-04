"""Gameweek report: markdown squad + captaincy + reasoning + dissent +
nuance + provenance, rendered from already-computed pipeline outputs.

`render_report` is deliberately PURE: no network, no clock reads (every
timestamp comes from `RunRecord`), no file IO. Same inputs must always
produce byte-identical markdown, so a deadline-morning re-run is
diffable against the previous one. Every number in the output traces
back to an input model — this module never computes a football opinion
of its own, only presentation and the mechanical arithmetic (sorting,
counting, matching a vote to its source `Pick`) needed to lay one out.

MECHANICAL DISSENT (see `report/models.py`'s docstring): who voted
against a squad player is arithmetic over `PlayerScore.votes`, computed
directly here and never asked of an LLM.

VETOED-BY-AVAILABILITY BACKER COUNTS: `record.excluded_players` lists
players the facts-veto (`fpl.availability`) removed from the pool
*before* `consensus.scoring.score_picks` ever ran — so an excluded
player's id structurally never appears in `scores` (that dict is built
only over the post-veto pool). To still answer "how many backers did
the veto cost", this module re-derives backer counts for excluded
players directly from `extractions` + `record.creator_weights`, using
the exact same one-vote-per-creator/`vote_value` arithmetic
`consensus.scoring.score_picks` uses for every other player — see
`_excluded_backers`. This is the same mechanical vote-counting the rest
of the pipeline already trusts, just applied to a player scoring never
got the chance to see.

DOUBTFUL SQUAD MEMBERS ARE NOT THE SAME AS EXCLUDED ONES. The veto above
only ever removes EXCLUDED players (injured/suspended/unavailable) from
the pool; DOUBTFUL players (e.g. `status='d'`) are discounted in the
solver's objective but DO reach the squad — by design, per
`fpl.availability`'s module docstring. That means a squad can legally
contain a 25%-chance starter with nothing in the old squad tables to
show it. `availability` (the per-player verdict map `run_pipeline`
already computes) is threaded in here purely to surface that fact — see
`_render_availability_callout` and the tables' Availability column —
never to change who is in the squad.

SQUAD DIFF / BOOTSTRAP FRESHNESS ARE COMPUTED ELSEWHERE. `diff`
(`pipeline.SquadDiff`) and `record.bootstrap_fetched_at` are both
mechanical, non-football facts about THIS run vs. the last one / vs. the
FPL API — `pipeline.run_pipeline` computes them (see its module
docstring) and hands them here already-built; this module only lays
them out.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from fpl_oracle.consensus.captaincy import CaptaincyElection, required_options
from fpl_oracle.consensus.chips import ChipTally, ChipVote
from fpl_oracle.consensus.scoring import CreatorVote, PlayerScore, vote_value
from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import Chip, Pick, PickAction, Provenance
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict
from fpl_oracle.fpl.players import PlayerDB, Position
from fpl_oracle.ingest.run_ingest import eligible_creators
from fpl_oracle.pipeline import RunRecord, SquadDiff
from fpl_oracle.report.models import SquadNuance
from fpl_oracle.roster.models import Creator
from fpl_oracle.solver.squad import Squad, SquadPlayer

_POSITION_ORDER: dict[Position, int] = {
    Position.GK: 0,
    Position.DEF: 1,
    Position.MID: 2,
    Position.FWD: 3,
}

# How many supporting votes to show per player in "Why these players".
_MAX_SUPPORTING_VOTES = 3


# ---------------------------------------------------------------------------
# Small formatting helpers.
# ---------------------------------------------------------------------------


def _human_utc(dt: datetime) -> str:
    """Format a timestamp as human-readable UTC. `dt` is assumed
    tz-aware (every `RunRecord` timestamp is `datetime.now(UTC)`);
    `astimezone(UTC)` on an already-aware datetime is a pure offset
    conversion, not a system-local-timezone read, so this stays
    deterministic."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _money(value: float) -> str:
    return f"£{value:.1f}m"


def _pct(share: float) -> str:
    return f"{share * 100:.0f}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _creator_name(creators_by_id: dict[str, str], creator_id: str) -> str:
    """Resolve a creator_id to its display name; falls back to the raw
    id for an unknown creator rather than crashing."""
    return creators_by_id.get(creator_id, creator_id)


def _player_name(db: PlayerDB, player_id: int) -> str:
    player = db.get(player_id)
    return player.web_name if player is not None else f"player {player_id}"


# ---------------------------------------------------------------------------
# Header.
# ---------------------------------------------------------------------------


def _modal_gameweek(extractions: list[ResolvedExtraction]) -> int | None:
    """The modal non-None `extraction.gameweek` across every loaded
    extraction. Ties (equally common gameweeks) break on the smallest
    gameweek number, so the result never depends on the input list's
    order — this stays deterministic even if a caller passes
    `extractions` in a different order across two otherwise-identical
    runs."""
    counts = Counter(
        r.extraction.gameweek for r in extractions if r.extraction.gameweek is not None
    )
    if not counts:
        return None
    top = max(counts.values())
    return min(gw for gw, c in counts.items() if c == top)


def _bootstrap_freshness_line(record: RunRecord) -> str:
    """How old the FPL player data behind this run is, relative to
    `record.started_at` — without this, a report built entirely on a
    week-old bootstrap cache is byte-shaped identically to one built on
    this morning's refresh. `bootstrap_fetched_at` is None whenever this
    run's `player_db` was injected rather than freshly loaded (tests, or
    any caller that skips the real fetch) — reported plainly rather than
    silently dropped, since a silently-dropped line is exactly the bug
    this exists to fix."""
    fetched_at = record.bootstrap_fetched_at
    if fetched_at is None:
        return "- FPL data: fetch time unavailable"
    age_minutes = round((record.started_at - fetched_at).total_seconds() / 60)
    if age_minutes < 0:
        # Clock skew (fetched_at after started_at) — report it plainly
        # rather than printing a nonsensical negative age.
        age_text = "after this run started (clock skew)"
    else:
        age_text = f"{age_minutes} minute{'s' if age_minutes != 1 else ''} before this run"
    return f"- FPL data: fetched {_human_utc(fetched_at)} ({age_text})"


def _source_video_range(extractions: list[ResolvedExtraction]) -> str | None:
    """Min/max `published_at` across the extractions actually used this
    run — the other half of "how fresh is this report", alongside the FPL
    data line above. `None` when there are no extractions to range over
    (nothing to report, not an error — mirrors `_modal_gameweek`)."""
    dates = [r.extraction.published_at for r in extractions]
    if not dates:
        return None
    earliest, latest = min(dates), max(dates)
    plural = "s" if len(dates) != 1 else ""
    return f"- Source videos: {len(dates)} video{plural} published {earliest.date()} to {latest.date()}"


def _render_header(
    record: RunRecord, squad: Squad, extractions: list[ResolvedExtraction]
) -> list[str]:
    lines = [f"# Gameweek Report — {record.run_id}", ""]
    lines.append(f"- Run started: {_human_utc(record.started_at)}")
    gameweek = _modal_gameweek(extractions)
    if gameweek is not None:
        lines.append(f"- Gameweek: {gameweek}")
    lines.append(_bootstrap_freshness_line(record))
    video_range = _source_video_range(extractions)
    if video_range is not None:
        lines.append(video_range)
    lines.append(f"- Squad cost: {_money(squad.total_cost_m)}")
    lines.append(f"- Formation: {record.solver.formation}")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Squad tables.
# ---------------------------------------------------------------------------


def _row_name(player: SquadPlayer, db: PlayerDB, record: RunRecord) -> str:
    name = _player_name(db, player.player_id)
    if player.player_id == record.solver.captain:
        name += " (C)"
    elif player.player_id == record.solver.vice_captain:
        name += " (VC)"
    return name


def _availability_cell(verdict: AvailabilityVerdict | None) -> str:
    """Compact table-cell text for one squad member's availability. `-`
    for a fully-available (or unassessed, which should never happen in
    practice) player keeps the common case quiet; a DOUBTFUL player gets
    its verdict, chance of playing, and multiplier spelled out right in
    the row it sits in — no need to cross-reference another section to
    notice a coin-flip starter."""
    if verdict is None or verdict.availability is Availability.AVAILABLE:
        return "-"
    chance = (
        f"{verdict.chance_of_playing_next_round}%"
        if verdict.chance_of_playing_next_round is not None
        else "?"
    )
    return f"{verdict.availability.value} ({chance}, {verdict.multiplier:.2f}x)"


def _squad_row(
    player: SquadPlayer,
    db: PlayerDB,
    record: RunRecord,
    scores: dict[int, PlayerScore],
    availability: dict[int, AvailabilityVerdict],
) -> list[str]:
    fpl_player = db.get(player.player_id)
    team_short = fpl_player.team_short if fpl_player is not None else "?"
    score = scores.get(player.player_id)
    backers = score.backers if score is not None else 0
    detractors = score.detractors if score is not None else 0
    return [
        _row_name(player, db, record),
        team_short,
        player.position.value,
        f"{player.price_m:.1f}",
        f"{player.band_score:.2f}",
        str(backers),
        str(detractors),
        _availability_cell(availability.get(player.player_id)),
    ]


def _sorted_group(players: list[SquadPlayer]) -> list[SquadPlayer]:
    """Position order (GK, DEF, MID, FWD) then descending band_score;
    `player_id` is the final tie-break so equal-scored players always
    land in the same order."""
    return sorted(players, key=lambda p: (_POSITION_ORDER[p.position], -p.band_score, p.player_id))


def _render_squad_tables(
    squad: Squad,
    db: PlayerDB,
    record: RunRecord,
    scores: dict[int, PlayerScore],
    availability: dict[int, AvailabilityVerdict],
) -> list[str]:
    headers = [
        "Player",
        "Team",
        "Pos",
        "Price",
        "Band Score",
        "Backers",
        "Detractors",
        "Availability",
    ]
    lines = ["## Starting XI — solver's £100m squad", ""]
    xi = _sorted_group(squad.starting_xi)
    lines += _table(headers, [_squad_row(p, db, record, scores, availability) for p in xi])
    lines += ["", "## Bench — solver's £100m squad", ""]
    bench = _sorted_group(squad.bench)
    lines += _table(headers, [_squad_row(p, db, record, scores, availability) for p in bench])
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Availability callout — a doubtful starter must be visible above the
# tables, not just as a cell inside one (see the module docstring).
# ---------------------------------------------------------------------------


def _availability_text(verdict: AvailabilityVerdict) -> str:
    chance = (
        f"{verdict.chance_of_playing_next_round}% chance of playing"
        if verdict.chance_of_playing_next_round is not None
        else "no stated chance of playing"
    )
    return f"{verdict.availability.value} — {chance}, {verdict.multiplier:.2f}x multiplier applied"


def _render_availability_callout(
    squad: Squad, availability: dict[int, AvailabilityVerdict], db: PlayerDB
) -> list[str]:
    # NAME THE SQUAD. Since the transfer section landed, this report
    # covers two different squads — the one the owner OWNS and the
    # £100m-optimal one the solver built. Without a qualifier here, a
    # report can say "Chiesa is unavailable" in the transfer section and
    # "every squad member is fully available" four lines later, both
    # true of different squads and together reading as a contradiction.
    lines = ["## Availability — solver's £100m squad", ""]
    flagged = [
        (player, availability[player.player_id])
        for player in squad.players
        if player.player_id in availability
        and availability[player.player_id].availability is not Availability.AVAILABLE
    ]
    if not flagged:
        lines += [
            (
                "Every member of the solver's squad is fully available. "
                "(For YOUR squad, see the transfer section above.)"
            ),
            "",
        ]
        return lines
    flagged.sort(key=lambda pair: (_POSITION_ORDER[pair[0].position], pair[0].player_id))
    for player, verdict in flagged:
        lines.append(f"- **{_player_name(db, player.player_id)}** — {_availability_text(verdict)}")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Captaincy.
# ---------------------------------------------------------------------------


def _render_captaincy(
    election: CaptaincyElection, squad: Squad, db: PlayerDB, record: RunRecord
) -> list[str]:
    lines = ["## Captaincy", ""]
    squad_ids = {p.player_id for p in squad.players}

    if election.candidates:
        headers = ["Player", "Score", "Voters", "Share", "In squad"]
        rows = [
            [
                _player_name(db, c.player_id),
                f"{c.score:.2f}",
                str(c.voters),
                _pct(c.share),
                "yes" if c.player_id in squad_ids else "no",
            ]
            for c in election.candidates
        ]
        lines += _table(headers, rows)
        lines.append("")
    else:
        lines += ["No captaincy candidates were named by any creator.", ""]

    captain_name = (
        _player_name(db, record.solver.captain) if record.solver.captain is not None else None
    )
    vice_name = (
        _player_name(db, record.solver.vice_captain)
        if record.solver.vice_captain is not None
        else None
    )
    if captain_name is not None:
        vc_text = vice_name if vice_name is not None else "none"
        lines.append(
            f"**Captain:** {captain_name} — **Vice-captain:** {vc_text}. These are the "
            "top-ranked election candidates that made the final squad."
        )
    else:
        lines.append(
            "No election candidate made the final squad, so no captain could be assigned "
            "from the armband data."
        )
    lines.append("")

    if election.thin:
        lines.append(
            f"**Warning: thin captaincy evidence.** Only {election.total_voters} of "
            f"{election.contributing_creators} contributing creators named a captain this "
            "run, so the armband consensus above rests on a small sample."
        )
        lines.append("")

    if record.solver.relaxed_captaincy:
        original_required = required_options(election)
        lines.append(
            f"**Captaincy constraint relaxed.** The solver could not build a legal squad "
            f"while holding at least {original_required} of the top captaincy option(s), so "
            f"the requirement was relaxed to {record.solver.captain_options_required}. This "
            "means the squad could not legally hold the originally required number of "
            "election options under the budget/quota/club-limit rules."
        )
        lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Pick lookup shared by "Why these players" and "Dissent".
# ---------------------------------------------------------------------------


def _build_pick_index(extractions: list[ResolvedExtraction]) -> dict[tuple[str, int], list[Pick]]:
    index: dict[tuple[str, int], list[Pick]] = defaultdict(list)
    for resolved in extractions:
        creator_id = resolved.extraction.creator_id
        for pick in resolved.extraction.picks:
            if pick.player_id is not None:
                index[(creator_id, pick.player_id)].append(pick)
    return index


def _find_pick(
    index: dict[tuple[str, int], list[Pick]], creator_id: str, player_id: int, action: PickAction
) -> Pick | None:
    """The `Pick` behind one `CreatorVote`: match on (creator_id,
    player_id), preferring the highest-conviction pick whose action
    equals the vote's action, else the highest-conviction pick for that
    pair.

    `score_picks` keeps the single strongest vote per (creator, player)
    — so if a creator made two picks with the same action at different
    convictions (e.g. two `squad_include`s), the vote's conviction is the
    HIGHER one. Picking the first action-matching candidate rather than
    the strongest one would pair that conviction with the wrong pick's
    `reasoning`/`provenance`, mislabelling a personal view as a group
    one or vice versa."""
    candidates = index.get((creator_id, player_id), [])
    if not candidates:
        return None
    matching = [pick for pick in candidates if pick.action == action]
    if matching:
        return max(matching, key=lambda p: p.conviction)
    return max(candidates, key=lambda p: p.conviction)


def _vote_line(
    vote: CreatorVote,
    pick: Pick | None,
    creators_by_id: dict[str, str],
) -> str:
    creator_name = _creator_name(creators_by_id, vote.creator_id)
    reasoning = pick.reasoning if pick is not None else "(no matching pick recorded)"
    line = f"- {creator_name} — {vote.action.value} (conviction {vote.conviction}): {reasoning}"
    if pick is not None and pick.provenance is Provenance.GROUP:
        line += " *(group view, not personal)*"
    return line


# ---------------------------------------------------------------------------
# Why these players.
# ---------------------------------------------------------------------------


def _render_why_these_players(
    squad: Squad,
    scores: dict[int, PlayerScore],
    db: PlayerDB,
    extractions: list[ResolvedExtraction],
    creators_by_id: dict[str, str],
) -> list[str]:
    lines = ["## Why these players", ""]
    index = _build_pick_index(extractions)
    for player in squad.players:
        lines.append(f"### {_player_name(db, player.player_id)}")
        score = scores.get(player.player_id)
        supporting = [v for v in score.votes if v.value > 0] if score is not None else []
        supporting = sorted(supporting, key=lambda v: -v.value)[:_MAX_SUPPORTING_VOTES]
        if not supporting:
            lines.append("No creator votes recorded for this player.")
        else:
            for vote in supporting:
                pick = _find_pick(index, vote.creator_id, player.player_id, vote.action)
                lines.append(_vote_line(vote, pick, creators_by_id))
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Dissent (mechanical — never from the LLM).
# ---------------------------------------------------------------------------


def _render_dissent(
    squad: Squad,
    scores: dict[int, PlayerScore],
    db: PlayerDB,
    extractions: list[ResolvedExtraction],
    creators_by_id: dict[str, str],
) -> list[str]:
    lines = ["## Dissent", ""]
    index = _build_pick_index(extractions)
    any_dissent = False
    for player in squad.players:
        score = scores.get(player.player_id)
        if score is None or score.detractors == 0:
            continue
        any_dissent = True
        negatives = sorted((v for v in score.votes if v.value < 0), key=lambda v: v.value)
        lines.append(f"### {_player_name(db, player.player_id)}")
        for vote in negatives:
            pick = _find_pick(index, vote.creator_id, player.player_id, vote.action)
            lines.append(_vote_line(vote, pick, creators_by_id))
        lines.append("")
    if not any_dissent:
        lines += ["Nobody argued against this squad.", ""]
    return lines


# ---------------------------------------------------------------------------
# Nuance.
# ---------------------------------------------------------------------------


def _render_nuance(
    squad: Squad, nuance: SquadNuance | None, db: PlayerDB, creators_by_id: dict[str, str]
) -> list[str]:
    lines = ["## Nuance", ""]
    if nuance is None:
        lines += ["The nuance pass did not run for this report.", ""]
        return lines

    lines += [
        (
            "*LLM-written commentary over the creators' own words — creator "
            "attributions below are machine-checked, the wording is not. "
            "Every mechanically-derived number in this report lives in the "
            "other sections.*"
        ),
        "",
    ]

    if nuance.captaincy_note:
        lines += [f"**Captaincy note:** {nuance.captaincy_note}", ""]
    if nuance.squad_notes:
        lines.append("**Squad-wide notes:**")
        lines += [f"- {note}" for note in nuance.squad_notes]
        lines.append("")

    nuance_by_id = {p.player_id: p for p in nuance.players}
    for player in squad.players:
        player_nuance = nuance_by_id.get(player.player_id)
        if player_nuance is None:
            continue
        if not player_nuance.concerns and not player_nuance.summary.strip():
            continue
        lines.append(f"### {_player_name(db, player.player_id)}")
        if player_nuance.summary.strip():
            lines.append(player_nuance.summary)
        for concern in player_nuance.concerns:
            names = ", ".join(_creator_name(creators_by_id, cid) for cid in concern.creator_ids)
            attribution = f" ({names})" if names else ""
            lines.append(
                f"- [{concern.kind.value}, severity {concern.severity}] {concern.note}{attribution}"
            )
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Vetoed by availability.
# ---------------------------------------------------------------------------


def _excluded_backers(
    extractions: list[ResolvedExtraction], player_id: int, creator_weights: dict[str, float]
) -> int:
    """Backer count for a player the availability veto removed before
    `consensus.scoring.score_picks` ever ran on the pool — so `scores`
    never has an entry for it. Re-derives the count directly from
    `extractions`, using the identical one-vote-per-creator (strongest
    by absolute value) arithmetic `score_picks` uses for every other
    player, so this is the same mechanical vote-counting already
    trusted elsewhere in the pipeline, not a new judgement call."""
    strongest: dict[str, float] = {}
    for resolved in extractions:
        creator_id = resolved.extraction.creator_id
        weight = creator_weights.get(creator_id, 0.0)
        for pick in resolved.extraction.picks:
            if pick.player_id != player_id:
                continue
            value = vote_value(weight, pick.action, pick.conviction)
            current = strongest.get(creator_id)
            if current is None or abs(value) > abs(current):
                strongest[creator_id] = value
    return sum(1 for value in strongest.values() if value > 0)


def _render_vetoed(
    record: RunRecord, db: PlayerDB, extractions: list[ResolvedExtraction]
) -> list[str]:
    lines = ["## Vetoed by availability", ""]
    rows: list[list[str]] = []
    for excluded in record.excluded_players:
        backers = _excluded_backers(extractions, excluded.player_id, record.creator_weights)
        if backers == 0:
            continue
        rows.append([_player_name(db, excluded.player_id), excluded.reason, str(backers)])
    if not rows:
        lines += ["No creator-backed player was removed by the availability veto.", ""]
        return lines
    lines += _table(["Player", "Reason", "Backers lost"], rows)
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Evidence quality / caveats.
# ---------------------------------------------------------------------------


def _pick_counts_by_creator(
    extractions: list[ResolvedExtraction],
) -> dict[str, tuple[int, int]]:
    """(resolved, total) pick counts per creator_id, from the same
    `extractions` the rest of the report reads. "Resolved" mirrors
    `pipeline._build_picks`'s own definition — a pick's `player_id` is
    stamped only on a MATCHED resolution (`run_extract.resolve_
    extraction`) — so this table and `record.counts.picks_used` can never
    silently disagree about what counts as resolved. A creator loses a
    big share of their picks to name-resolution silently otherwise: the
    global 18%-skipped figure doesn't say WHOSE picks got dropped."""
    counts: dict[str, tuple[int, int]] = {}
    for resolved in extractions:
        creator_id = resolved.extraction.creator_id
        picks = resolved.extraction.picks
        total = len(picks)
        matched = sum(1 for p in picks if p.player_id is not None)
        prev_matched, prev_total = counts.get(creator_id, (0, 0))
        counts[creator_id] = (prev_matched + matched, prev_total + total)
    return counts


def _render_evidence_quality(
    record: RunRecord,
    creators: list[Creator],
    creators_by_id: dict[str, str],
    extractions: list[ResolvedExtraction],
) -> list[str]:
    lines = ["## Evidence quality / caveats", ""]
    counts = record.counts
    lines += [
        f"- Extractions loaded: {counts.extractions_loaded}",
        f"- Picks used: {counts.picks_used}",
        (
            f"- Picks skipped (unresolved player name): {counts.picks_skipped_unresolved} — "
            "see `data/extractions/match_quality.md` for the per-pick detail"
        ),
        f"- Players in solver pool: {counts.players_in_pool}",
        "",
    ]

    # "Contributing" (voted this run) vs. "eligible" (could have) — without
    # both numbers, "14 creators" reads the same whether everyone showed up
    # or four of them posted nothing.
    #
    # Eligibility comes from `ingest.run_ingest.eligible_creators`, the
    # single source of truth, rather than `len(creators)`. The difference is
    # not cosmetic: a co-host on a shared channel (Zophar and Lateriser on
    # The FPL Wire) is deliberately never ingested on their own — their
    # views flow in through the channel's primary. Counting them against
    # the roster reports the intended architecture as six creators having
    # failed, which is exactly the kind of confident-but-wrong line that
    # makes an owner distrust a report at 8am.
    contributing_ids = set(record.creator_weights)
    contributing = len(contributing_ids)
    eligible_list = eligible_creators(creators)
    eligible_ids = {c.creator_id for c in eligible_list}
    lines.append(
        f"**Creator participation:** {contributing} of {len(eligible_list)} eligible creators "
        f"contributed picks used in this run ({len(creators)} on the roster)."
    )
    non_contributing = sorted(
        (c for c in eligible_list if c.creator_id not in contributing_ids), key=lambda c: c.name
    )
    if non_contributing:
        names = ", ".join(c.name for c in non_contributing)
        lines.append(f"**Eligible but contributed nothing this run:** {names}")
    # Two distinct reasons a rostered creator is not eligible, and calling
    # them the same thing would misreport one of them: a co-host whose
    # views deliberately flow through their channel's primary, versus a
    # creator whose channel was never resolved (a roster gap, not a design
    # choice).
    co_hosts = sorted(
        (c for c in creators if c.creator_id not in eligible_ids and c.channel_id is not None),
        key=lambda c: c.name,
    )
    unresolved = sorted(
        (c for c in creators if c.channel_id is None),
        key=lambda c: c.name,
    )
    if co_hosts:
        names = ", ".join(c.name for c in co_hosts)
        lines.append(
            "**Not ingested by design** (shared-channel co-hosts, counted through their "
            f"channel's primary creator): {names}"
        )
    if unresolved:
        names = ", ".join(c.name for c in unresolved)
        lines.append(f"**No channel resolved** (cannot be ingested at all): {names}")
    lines.append("")

    lines.append(
        f"**Independence disclosure:** this report aggregates opinions from {contributing} "
        "contributing creators, and that headline count overstates the number of "
        "independent opinions behind it. Co-hosts on a shared channel (e.g. The FPL Wire) "
        "are counted once, through the channel's primary creator, rather than once per "
        "co-host; a shared channel's recurring on-camera personas (e.g. Fantasy Football "
        "Hub's) are title-split into separate creators but still come from one production "
        "team. v1 also applies NO herding discount: every creator watched the same "
        "fixtures at the same prices, so agreement between them is not confirmed as "
        "independent inference."
    )
    lines.append("")

    lines.append("**Creator weights used this run (descending):**")
    ordered = sorted(record.creator_weights.items(), key=lambda kv: (-kv[1], kv[0]))
    for creator_id, weight in ordered:
        lines.append(f"- {_creator_name(creators_by_id, creator_id)}: {weight:.3f}")
    lines.append("")

    pick_counts = _pick_counts_by_creator(extractions)
    if pick_counts:
        lines.append("**Picks resolved per creator:**")
        ordered_counts = sorted(pick_counts.items(), key=lambda kv: (-kv[1][1], kv[0]))
        for creator_id, (matched, total) in ordered_counts:
            share = _pct(matched / total) if total else "n/a"
            lines.append(
                f"- {_creator_name(creators_by_id, creator_id)}: {matched}/{total} ({share})"
            )
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Provenance footer.
# ---------------------------------------------------------------------------


def _render_provenance(record: RunRecord) -> list[str]:
    lines = ["## Provenance", ""]
    git = record.git
    if git.commit_sha is None:
        lines.append("- git: unavailable (no git binary, or not run inside a git checkout)")
    else:
        dirty = "unknown" if git.dirty is None else ("yes" if git.dirty else "no")
        lines.append(f"- git: `{git.short_sha}` on `{git.branch}` (dirty: {dirty})")
    lines.append("")

    lines.append("**Input files:**")
    if not record.inputs:
        lines.append("- (none)")
    for item in record.inputs:
        # `published_at` is None only for a run.json written before this
        # field existed (see ExtractionInput's docstring) — reported
        # plainly rather than crashing on an old record.
        published = "unknown" if item.published_at is None else str(item.published_at.date())
        lines.append(
            f"- `{item.path}` — video `{item.video_id}`, creator `{item.creator_id}`, "
            f"published {published}, sha256 `{item.sha256[:12]}`"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Squad diff vs. the previous run — the runbook calls this "the single
# most useful line on deadline morning", so it renders FIRST, above even
# the squad tables. `diff` is computed in `pipeline.run_pipeline` (see
# `pipeline.SquadDiff`/`diff_vs_previous`); this only lays it out.
# ---------------------------------------------------------------------------


def _render_diff(diff: SquadDiff | None, db: PlayerDB) -> list[str]:
    if diff is None:
        return []
    lines = ["## Squad diff vs previous run", ""]
    if diff.previous_run_id is None:
        lines += ["No previous run to diff against (this is the first recorded run).", ""]
        return lines
    in_names = ", ".join(_player_name(db, pid) for pid in diff.players_in) or "none"
    out_names = ", ".join(_player_name(db, pid) for pid in diff.players_out) or "none"
    lines.append(f"- In: {in_names}")
    lines.append(f"- Out: {out_names}")
    if diff.captain_changed:
        prev_cap = (
            "none" if diff.previous_captain is None else _player_name(db, diff.previous_captain)
        )
        new_cap = "none" if diff.new_captain is None else _player_name(db, diff.new_captain)
        lines.append(f"- Captain changed: {prev_cap} -> {new_cap}")
    else:
        lines.append("- Captain unchanged")
    lines.append(f"- Compared against run `{diff.previous_run_id}`")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Chip consensus — creators' own stated chip-timing intentions
# (`consensus.chips.summarize_chip_plans`), tallied per chip. OBSERVATIONAL
# ONLY: nothing here feeds the solver/consensus scoring above; this only
# lays out what `record.chip_consensus` already computed.
# ---------------------------------------------------------------------------


_CHIP_DISPLAY_NAMES: dict[Chip, str] = {
    Chip.WILDCARD: "Wildcard",
    Chip.FREE_HIT: "Free Hit",
    Chip.BENCH_BOOST: "Bench Boost",
    Chip.TRIPLE_CAPTAIN: "Triple Captain",
}


def _chip_vote_name(creators_by_id: dict[str, str], vote: ChipVote) -> str:
    name = _creator_name(creators_by_id, vote.creator_id)
    return f"{name} (group)" if vote.provenance is Provenance.GROUP else name


def _this_gameweek_cell(tally: ChipTally, creators_by_id: dict[str, str]) -> str:
    if not tally.this_gameweek:
        return "—"
    names = ", ".join(_chip_vote_name(creators_by_id, vote) for vote in tally.this_gameweek)
    count = len(tally.this_gameweek)
    return f"**{count}** ({_pct(tally.this_gameweek_share)} of weight): {names}"


def _later_cell(tally: ChipTally, creators_by_id: dict[str, str]) -> str:
    if not tally.later:
        return "—"
    # `tally.later` is already sorted by (target_gameweek, creator_id), so
    # equal-gameweek votes are adjacent — a single pass groups them without
    # needing to re-sort.
    groups: list[tuple[int | None, list[str]]] = []
    for vote in tally.later:
        name = _chip_vote_name(creators_by_id, vote)
        if groups and groups[-1][0] == vote.target_gameweek:
            groups[-1][1].append(name)
        else:
            groups.append((vote.target_gameweek, [name]))
    return " · ".join(f"GW{gw}: {', '.join(names)}" for gw, names in groups)


def _unspecified_cell(tally: ChipTally, creators_by_id: dict[str, str]) -> str:
    if not tally.unspecified:
        return "—"
    return ", ".join(_chip_vote_name(creators_by_id, vote) for vote in tally.unspecified)


def _render_chip_consensus(record: RunRecord, creators_by_id: dict[str, str]) -> list[str]:
    consensus = record.chip_consensus
    if consensus is None:
        return []

    header = (
        f"## Chip consensus — GW{consensus.gameweek}"
        if consensus.gameweek is not None
        else "## Chip consensus"
    )

    if consensus.creators_with_plans == 0:
        lines = [header, "", "_No creator stated a chip intention this week._", ""]
        if consensus.stale:
            lines.append(
                f"_Ignored as past-dated: {len(consensus.stale)} plan(s) targeting an earlier "
                "gameweek._"
            )
            lines.append("")
        return lines

    lines = [
        header,
        "",
        (
            'Stated intentions only: a creator saying "I\'m wildcarding this week", never '
            f"generic advice. {consensus.creators_with_plans} of "
            f"{consensus.contributing_creators} contributing creators stated a chip plan."
        ),
        "",
    ]
    rows = [
        [
            _CHIP_DISPLAY_NAMES.get(tally.chip, tally.chip.value),
            _this_gameweek_cell(tally, creators_by_id),
            _later_cell(tally, creators_by_id),
            _unspecified_cell(tally, creators_by_id),
        ]
        for tally in consensus.tallies
    ]
    lines += _table(["Chip", "This gameweek", "Later", "Unspecified"], rows)
    lines.append("")

    if consensus.stale:
        lines.append(
            f"_Ignored as past-dated: {len(consensus.stale)} plan(s) targeting an earlier "
            "gameweek._"
        )
        lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


_SOLVER_SQUAD_CAVEAT = (
    "> The squad sections below answer a DIFFERENT question: the best 15 for £100m "
    "from scratch. That is the wildcard/free-hit answer, not something you can act "
    "on with one free transfer. Act on the transfer section above."
)


def _free_hit_exclusion_line(record: RunRecord, creators_by_id: dict[str, str]) -> str:
    """Rendered BEFORE the recommendation's reasoning: on a wildcard week
    the withheld picks can be most of the evidence, and a reasoning line
    like "best move has only 0 backers" reads as "nobody likes your squad"
    unless the reader has already been told the evidence was set aside."""
    n = record.counts.picks_free_hit_excluded_from_transfer
    names = ", ".join(_creator_name(creators_by_id, cid) for cid in record.free_hit_sources)
    names = names or "unrecorded creators"
    pick_word = "pick" if n == 1 else "picks"
    return (
        f"_Free-hit drafts kept out of this section: {n} {pick_word} from {names}. "
        "A free-hit team is a one-week fixture play, not a lasting endorsement; those "
        "picks still count toward the £100m squad below._"
    )


def _render_transfer(record: RunRecord, creators_by_id: dict[str, str]) -> list[str]:
    """The in-season answer, rendered FIRST — ahead of the squad tables.

    Everything below it describes a squad built from £100m, which is the
    wildcard question. This section is the one the owner can actually act
    on with the free transfer they have, so it leads.
    """
    rec = record.transfer_recommendation
    if rec is None:
        if not record.transfer_note:
            return []
        lines = ["## This week's transfer", ""]
        if record.counts.picks_free_hit_excluded_from_transfer > 0:
            lines += [_free_hit_exclusion_line(record, creators_by_id), ""]
        lines += [f"_{record.transfer_note}_", ""]
        return lines

    lines = [f"## This week's transfer — {rec.verdict}", ""]
    if record.counts.picks_free_hit_excluded_from_transfer > 0:
        lines += [_free_hit_exclusion_line(record, creators_by_id), ""]
    lines += [rec.reasoning, ""]

    if rec.unavailable_owned:
        lines += [
            f"> **Unavailable in your squad:** {', '.join(rec.unavailable_owned)}",
            "",
        ]

    if rec.recommended is not None:
        m = rec.recommended
        lines += [
            f"**{m.out_name} → {m.in_name}**  ",
            (
                f"Δ{m.delta:+.2f} · {m.price_delta_m:+.1f}m · "
                f"{m.in_backers} backers (was {m.out_backers}) · {m.affordability}"
            ),
            "",
        ]
        for note in m.notes:
            lines += [f"> {note}", ""]

    if rec.shortlist:
        lines += [
            "| Out | In | Δ | Price | Backers | Affordable |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
        for m in rec.shortlist:
            lines.append(
                f"| {m.out_name} | {m.in_name} | {m.delta:+.2f} | "
                f"{m.price_delta_m:+.1f}m | {m.out_backers}→{m.in_backers} | "
                f"{m.affordability} |"
            )
        lines.append("")

    if rec.hit_candidates:
        lines += [
            "### If you are willing to take a −4",
            "",
            (
                "Ranked by the same yardstick, restricted to moves at least three "
                "creators back. This system will not tell you a hit pays for itself — "
                "consensus scores are band-relative and carry no points unit, so any "
                "such claim would be invented. Shown so the judgement is yours to make "
                "on the evidence rather than blocked by a missing number."
            ),
            "",
        ]
        for m in rec.hit_candidates:
            lines.append(
                f"- **{m.out_name} → {m.in_name}** · Δ{m.delta:+.2f} · "
                f"{m.in_backers} backers · {m.affordability}"
            )
        lines.append("")

    if rec.delta_distribution is not None and rec.delta_distribution.moves_considered:
        d = rec.delta_distribution
        lines += [
            (
                f"_Judged {d.moves_considered} legal moves against a bar of "
                f"{d.bar_applied:.2f} (uncalibrated \u2014 recorded each week so it "
                f"can be tuned against outcomes). Best {d.best:+.2f}, "
                f"p90 {d.p90:+.2f}, median {d.median:+.2f}._"
            ),
            "",
        ]

    if rec.worth_considering:
        lines += [
            "### Worth considering, not recommended",
            "",
            (
                "These have real creator backing but would cost a \u22124, and this "
                "system deliberately will not price a hit: consensus scores are "
                "band-relative and carry no points unit, so any 'worth it' threshold "
                "would be invented. Your call, with the evidence above."
            ),
            "",
        ]
        for m in rec.worth_considering:
            lines.append(
                f"- **{m.out_name} → {m.in_name}** ({m.in_backers} backers, Δ{m.delta:+.2f})"
            )
        lines.append("")

    lines += [f"_Free transfers: {rec.free_transfers}. Pricing: {rec.pricing_note}_", ""]
    lines += [_SOLVER_SQUAD_CAVEAT, ""]
    return lines


# Above this, `transfers_equivalent` reads as a real number of transfers.
# Beyond it the ratio is no longer a meaningful count (a tiny or zero
# best-single-transfer delta blows the ratio up) so it's capped in the
# prose rather than printed as a large, spuriously precise number.
_TRANSFERS_EQUIVALENT_CAP = 20


def _render_wildcard_gap(record: RunRecord, db: PlayerDB) -> list[str]:
    """How far the owner's actual squad sits from the solver's £100m
    squad, on the same coefficient yardstick the transfer section above
    uses. Observational — see `solver.wildcard`."""
    gap = record.wildcard_gap
    if gap is None:
        return []

    sentence = (
        f"The solver's £100m squad (£{gap.solver_squad_cost_m:.1f}m) scores "
        f"**{gap.solver_objective:.2f}** against **{gap.my_objective:.2f}** for the squad "
        f"you own (£{gap.my_squad_value_m:.1f}m), a gap of **{gap.gain:+.2f}** on the same "
        "yardstick as the transfer deltas above."
    )
    if gap.gain <= 0:
        sentence += (
            " Your squad already scores at or above the solver's on this yardstick; a "
            "wildcard buys nothing the consensus can see."
        )
    elif gap.transfers_equivalent is not None:
        equivalent_text = (
            f"more than {_TRANSFERS_EQUIVALENT_CAP}"
            if gap.transfers_equivalent > _TRANSFERS_EQUIVALENT_CAP
            else f"{gap.transfers_equivalent:.1f}"
        )
        sentence += (
            f" Your best single transfer is worth {gap.best_single_transfer_delta:+.2f}, "
            f"so the wildcard buys roughly {equivalent_text} transfers' worth of consensus."
        )

    if gap.kept_count == 0:
        sentence += " The solver would keep 0 of your 15."
    else:
        kept_names = ", ".join(_player_name(db, pid) for pid in gap.kept_player_ids)
        sentence += f" The solver would keep {gap.kept_count} of your 15: {kept_names}."

    if gap.unbacked_owned_ids:
        unbacked_names = ", ".join(_player_name(db, pid) for pid in gap.unbacked_owned_ids)
        sentence += f" Owned players with no creator backing this week: {unbacked_names}."
    else:
        sentence += " Owned players with no creator backing this week: none."

    return [
        "## Wildcard gap",
        "",
        sentence,
        "",
        (
            "_Consensus support, not points. Whether a first-half wildcard is worth "
            "spending now is your call; this says how far your squad sits from where "
            "the creators are converging. Price carries most of this yardstick, so a "
            "squad worth more than £100m starts ahead before consensus is counted._"
        ),
        "",
    ]


def render_report(
    *,
    record: RunRecord,
    squad: Squad,
    scores: dict[int, PlayerScore],
    election: CaptaincyElection,
    db: PlayerDB,
    extractions: list[ResolvedExtraction],
    creators: list[Creator],
    availability: dict[int, AvailabilityVerdict] | None = None,
    nuance: SquadNuance | None = None,
    diff: SquadDiff | None = None,
) -> str:
    """Render the full gameweek markdown report. Pure and deterministic:
    no network, no clock reads, no file IO — every value comes from the
    given inputs, and the same inputs always produce byte-identical
    output.

    `availability` and `diff` are both optional so existing callers (and
    tests) that don't care about them keep working: `availability=None`
    is treated as "no verdicts to show" (every squad member renders as
    fully available), and `diff=None` omits the diff section entirely —
    the right behaviour for a first-ever run, which has nothing to diff
    against, distinct from `diff.previous_run_id=None` (a diff WAS
    computed, and it found no previous run — see `_render_diff`)."""
    creators_by_id = {c.creator_id: c.name for c in creators}
    availability = availability or {}

    lines: list[str] = []
    lines += _render_header(record, squad, extractions)
    lines += _render_transfer(record, creators_by_id)
    lines += _render_wildcard_gap(record, db)
    lines += _render_chip_consensus(record, creators_by_id)
    lines += _render_diff(diff, db)
    lines += _render_availability_callout(squad, availability, db)
    lines += _render_squad_tables(squad, db, record, scores, availability)
    lines += _render_captaincy(election, squad, db, record)
    lines += _render_why_these_players(squad, scores, db, extractions, creators_by_id)
    lines += _render_dissent(squad, scores, db, extractions, creators_by_id)
    lines += _render_nuance(squad, nuance, db, creators_by_id)
    lines += _render_vetoed(record, db, extractions)
    lines += _render_evidence_quality(record, creators, creators_by_id, extractions)
    lines += _render_provenance(record)

    return "\n".join(lines).rstrip() + "\n"


def write_report(markdown: str, run_dir: Path) -> Path:
    """Write the rendered report to `run_dir/report.md` and return its
    path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.md"
    path.write_text(markdown)
    return path
