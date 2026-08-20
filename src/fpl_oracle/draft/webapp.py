"""Build a self-contained draft-room web app.

Emits ONE html file with the whole board embedded — no server, no network, no
dependencies at run time. That is deliberate: a draft is live and unrepeatable,
so the tool must not be able to fail because a process died or wifi dropped.
Python does the projection maths once, here; the browser handles only what has
to be live (who is gone, whose turn it is, what to take next), and picks are
persisted to localStorage so a refresh or a closed laptop costs nothing.

    uv run python -m fpl_oracle.draft.webapp --teams 10 --open
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from pydantic import BaseModel

from fpl_oracle.draft import client
from fpl_oracle.draft.board import build_board
from fpl_oracle.draft.expert_rankings import ALL_BOARDS

TEMPLATE = Path(__file__).parent / "templates" / "draft_app.html"
DEFAULT_OUTPUT = Path("data/draft/draft_app.html")


class Manager(BaseModel):
    """One entry in the draft league."""

    team: str
    manager: str
    me: bool = False


# The 2026/27 league. Order here is only the initial setup order — the real
# snake order is set in the app before the draft starts.
LEAGUE: list[Manager] = [
    Manager(team="Achilles & Saltfish", manager="Jason Kwang"),
    Manager(team="A-14", manager="James Parris"),
    Manager(team="SOCA WARRIORS", manager="Gerik Whittington"),
    Manager(team="Zirzkee Ya Later", manager="Youssef Rofail"),
    Manager(team="Just Mo With The Flo", manager="Richie Ramchand"),
    Manager(team="ZubiArsenal 2221", manager="Femi Adeyemi"),
    Manager(team="Third Kit", manager="Tjisana Kerr", me=True),
    Manager(team="Damn", manager="James Atewo"),
    Manager(team="Bombo Squad", manager="Maurice Phillips"),
    Manager(team="Harar Hotspur", manager="Samir Addus"),
]


def render(teams: int, *, refresh: bool = False, league: list[Manager] | None = None) -> str:
    """Build the board and inject it into the template."""
    league = league or LEAGUE
    board = build_board(client.fetch_bootstrap(refresh=refresh), teams, ALL_BOARDS)

    players = [
        {
            "id": p.player_id,
            "name": p.web_name,
            "full": p.full_name,
            "pos": p.position.value,
            "team": p.team_short,
            "proj": round(p.projected_points, 1),
            "value": round(p.value, 1),
            "tier": p.tier,
            "rank": p.fpl_draft_rank,
            "status": p.status,
            "flag": p.available_flag,
            "experts": p.expert_ranks,
        }
        # Trim the long tail: nobody drafts the 400th-ranked player, and a
        # smaller payload keeps autocomplete instant.
        for p in sorted(board.players, key=lambda x: x.consensus_rank)[:320]
    ]

    html = TEMPLATE.read_text()
    for token, value in (
        ("__PLAYERS__", players),
        ("__MANAGERS__", [m.model_dump() for m in league]),
        ("__BASELINES__", {k.value: round(v, 1) for k, v in board.baselines.items()}),
    ):
        html = html.replace(token, json.dumps(value, ensure_ascii=False))

    if board.unresolved_expert_names:
        print(f"warn: unmatched creator names: {board.unresolved_expert_names}", file=sys.stderr)
    return html


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the draft web app")
    ap.add_argument("--teams", type=int, default=10)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--refresh", action="store_true", help="refetch injury news first")
    ap.add_argument("--open", action="store_true", help="open it in a browser")
    args = ap.parse_args(argv)

    html = render(args.teams, refresh=args.refresh)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    resolved = args.out.resolve()
    print(f"wrote {resolved}  ({len(html) // 1024} KB, self-contained)")
    if args.open:
        webbrowser.open(resolved.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
