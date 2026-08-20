"""Value-based draft board: projections, positional baselines, tiers.

The method (value-based drafting, from NFL fantasy via Draft FC) is that a
player's worth on draft day is not his projected points but how far he clears
the baseline at HIS OWN position, because that is the player you would field
instead of him.

Two decisions here are easy to get wrong, and both were verified against live
data before being written down:

1. BASELINE = LAST STARTER, NOT LAST ROSTERED. A 10-team league rosters 20
   goalkeepers, so a replacement-level baseline lands on the 21st keeper — a
   backup who scored ~20 points. That inflates every starting keeper's value to
   nonsense (+142 for the best in the game) and would have you drafting one in
   round 1. Only ONE keeper starts per team, so the real baseline is the 10th.

2. ORDER BY draft_rank, SIZE BY LAST SEASON'S CURVE. Ranking on raw last-season
   points buries anyone who missed time — Isak is FPL's 5th-ranked player on 41
   points from 694 minutes. So the ORDERING comes from `draft_rank` (and
   creator boards, blended in below) while the MAGNITUDE comes from last
   season's positional points curve. The Nth-best forward is projected at what
   the Nth-best forward actually scored, which keeps the real cliff shape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from fpl_oracle.draft.expert_rankings import ExpertBoard
from fpl_oracle.fpl.players import Position

# Official FPL Draft squad shape (draft bootstrap `settings.squad.select_*`).
ROSTER_SLOTS: dict[Position, int] = {
    Position.GK: 2,
    Position.DEF: 5,
    Position.MID: 5,
    Position.FWD: 3,
}

# Players actually fielded each week, per team. Drives the baseline: these are
# the ones whose points count. A 1-4-4-2 shape, which sums to the 11 starters
# the game requires and sits inside the min/max play limits
# (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD).
STARTERS: dict[Position, int] = {
    Position.GK: 1,
    Position.DEF: 4,
    Position.MID: 4,
    Position.FWD: 2,
}

_UNRANKED = 9999


class DraftPlayer(BaseModel):
    """A player on the draft board, projected and valued."""

    player_id: int
    web_name: str
    first_name: str
    second_name: str
    team_short: str
    position: Position
    fpl_draft_rank: int
    last_season_points: int
    minutes: int
    status: str
    news: str = ""

    consensus_rank: float = 0.0  # blended FPL + creator ordering
    position_rank: int = 0
    projected_points: float = 0.0
    value: float = 0.0  # projected minus the last-starter baseline
    tier: int = 0
    expert_ranks: dict[str, int] = Field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.second_name}"

    @property
    def available_flag(self) -> str:
        """Injury/suspension marker, straight from the draft API's own status."""
        if self.status == "u":
            return "UNAVAILABLE"
        if self.status in {"i", "s"}:
            return "INJURED/SUSPENDED"
        if self.status == "d":
            return "DOUBT"
        return ""


class Board(BaseModel):
    """A fully valued draft board plus the baselines it was scored against."""

    players: list[DraftPlayer]  # best value first
    baselines: dict[Position, float]
    teams: int
    unresolved_expert_names: dict[str, list[str]] = Field(default_factory=dict)

    def by_id(self, player_id: int) -> DraftPlayer | None:
        return next((p for p in self.players if p.player_id == player_id), None)


def _parse_elements(data: dict) -> list[DraftPlayer]:
    team_short = {t["id"]: t["short_name"] for t in data["teams"]}
    return [
        DraftPlayer(
            player_id=e["id"],
            web_name=e["web_name"],
            first_name=e["first_name"],
            second_name=e["second_name"],
            team_short=team_short.get(e["team"], "???"),
            position=Position.from_element_type(e["element_type"]),
            fpl_draft_rank=e.get("draft_rank") or _UNRANKED,
            last_season_points=e["total_points"],
            minutes=e["minutes"],
            status=e.get("status", "a"),
            news=(e.get("news") or "").strip(),
        )
        for e in data["elements"]
    ]


def _resolve_expert_names(
    players: list[DraftPlayer], boards: list[ExpertBoard]
) -> tuple[dict[int, dict[str, int]], dict[str, list[str]]]:
    """Map each creator's published names onto player ids.

    Exact `web_name` first, then a full-name containment check. Anything that
    still does not resolve is REPORTED, never guessed at — a creator board is
    small enough that a silent mismatch would meaningfully skew the blend.
    """
    by_web: dict[str, list[DraftPlayer]] = {}
    for p in players:
        by_web.setdefault(p.web_name.casefold(), []).append(p)

    resolved: dict[int, dict[str, int]] = {}
    unresolved: dict[str, list[str]] = {}

    for board in boards:
        for raw_name, rank in board.ranks.items():
            key = raw_name.casefold()
            hits = by_web.get(key, [])
            if len(hits) != 1:
                hits = [p for p in players if key in p.full_name.casefold()]
            if len(hits) != 1:
                unresolved.setdefault(board.creator, []).append(raw_name)
                continue
            resolved.setdefault(hits[0].player_id, {})[board.creator] = rank
    return resolved, unresolved


def build_board(
    data: dict,
    teams: int,
    expert_boards: list[ExpertBoard] | None = None,
) -> Board:
    """Project, value and tier every player in the draft pool.

    `teams` is the league size, which sets the baselines: with more managers,
    the replacement starter is worse and everyone above him is worth more.
    """
    if teams < 2:
        raise ValueError("a draft league needs at least 2 teams")

    players = _parse_elements(data)
    expert_map, unresolved = _resolve_expert_names(players, expert_boards or [])

    # Blend the ordering. A creator's rank is a vote on where a player belongs;
    # absence from a top-20 is not evidence against a player (they only ranked
    # 20), so it simply carries no vote and FPL's rank stands alone.
    for p in players:
        p.expert_ranks = expert_map.get(p.player_id, {})
        votes = [float(p.fpl_draft_rank), *(float(r) for r in p.expert_ranks.values())]
        p.consensus_rank = sum(votes) / len(votes)

    baselines: dict[Position, float] = {}
    for position in Position:
        group = [p for p in players if p.position == position]
        if not group:
            baselines[position] = 0.0
            continue

        # The magnitude curve: last season's real totals at this position.
        curve = sorted((p.last_season_points for p in group), reverse=True)
        # The ordering: blended consensus, best first.
        group.sort(key=lambda p: p.consensus_rank)
        for i, p in enumerate(group):
            p.position_rank = i + 1
            p.projected_points = float(curve[i]) if i < len(curve) else 0.0

        last_starter = STARTERS[position] * teams
        baselines[position] = float(curve[last_starter - 1]) if last_starter <= len(curve) else 0.0

    for p in players:
        p.value = p.projected_points - baselines[p.position]

    ranked = sorted(players, key=lambda p: -p.value)
    _assign_tiers(ranked)
    return Board(
        players=ranked,
        baselines=baselines,
        teams=teams,
        unresolved_expert_names=unresolved,
    )


def _assign_tiers(ranked: list[DraftPlayer]) -> None:
    """Break the board into tiers wherever it falls off a cliff.

    Tiers are what make a board usable under a draft clock: inside a tier the
    players are near-interchangeable, so you take whoever fits your squad; the
    moment a tier empties, the cost of waiting jumps.
    """
    if not ranked:
        return
    cliff = max(6.0, ranked[0].value * 0.06)
    tier = 1
    for i, p in enumerate(ranked):
        if i and (ranked[i - 1].value - p.value) > cliff:
            tier += 1
        p.tier = tier


def value_over_next_available(player: DraftPlayer, available: list[DraftPlayer]) -> float:
    """How far a player clears the next available player at his position.

    Measured against the man immediately BELOW him, not the best one left: the
    live question is "if I pass now, what is still here next round?", so anyone
    ranked above him is irrelevant. A large number means passing is expensive.
    """
    same = sorted(
        (q for q in available if q.position == player.position),
        key=lambda q: -q.value,
    )
    try:
        i = same.index(player)
    except ValueError:
        return player.projected_points
    if i + 1 >= len(same):
        return player.projected_points
    return player.projected_points - same[i + 1].projected_points


def squad_needs(drafted: list[DraftPlayer]) -> dict[Position, int]:
    """Slots still to fill, by position."""
    have: dict[Position, int] = dict.fromkeys(ROSTER_SLOTS, 0)
    for p in drafted:
        have[p.position] += 1
    return {pos: ROSTER_SLOTS[pos] - have[pos] for pos in ROSTER_SLOTS}
