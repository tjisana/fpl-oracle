"""Local server for the draft app, with a same-origin proxy to the draft API.

The official draft API exposes a league's picks publicly — no login, no
credentials, no session cookie:

    GET draft.premierleague.com/api/draft/{league_id}/choices

but it sends no `Access-Control-Allow-Origin`, so a browser cannot read it from
a page. This server sits in front: it serves the app and re-exposes those two
endpoints under its own origin, which makes them fetchable. Everything stays
read-only and public — nothing here authenticates as anybody.

    uv run python -m fpl_oracle.draft.serve --league 12345

With a league id the app syncs itself: picks are pulled straight from the draft
room, so there is nothing to type and a page refresh is harmless.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from fpl_oracle.draft.board import build_board
from fpl_oracle.draft.client import fetch_bootstrap
from fpl_oracle.draft.expert_rankings import ALL_BOARDS
from fpl_oracle.draft.simulate import Simulator
from fpl_oracle.draft.webapp import LEAGUE, render

API = "https://draft.premierleague.com/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
UPSTREAM_TIMEOUT = 12.0


def _upstream(path: str) -> tuple[int, dict]:
    """Fetch one public draft endpoint. Never raises — the draft must not stop."""
    try:
        resp = httpx.get(f"{API}/{path}", headers=HEADERS, timeout=UPSTREAM_TIMEOUT)
    except httpx.HTTPError as exc:
        return 502, {"error": f"upstream unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        return resp.status_code, {"error": f"upstream returned {resp.status_code}"}
    try:
        return 200, resp.json()
    except ValueError:
        return 502, {"error": "upstream returned non-JSON"}


class Handler(BaseHTTPRequestHandler):
    html: str = ""
    sim: Simulator | None = None

    def log_message(self, format: str, *args: object) -> None:
        """Silence request logging — the terminal is the user's draft console."""

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path in ("/", "/index.html"):
            self._send(200, self.html.encode(), "text/html; charset=utf-8")
            return

        if self.sim is not None and url.path in ("/api/picks", "/api/league"):
            payload = (
                self.sim.choices_payload()
                if url.path == "/api/picks"
                else self.sim.details_payload()
            )
            self._json(200, payload)
            return

        if self.sim is not None and url.path == "/api/sim/pick":
            element = (query.get("element") or [""])[0]
            if not element.isdigit():
                self._json(400, {"error": "element must be numeric"})
                return
            ok, message = self.sim.user_pick(int(element))
            self._json(200 if ok else 409, {"ok": ok, "message": message})
            return

        if url.path in ("/api/picks", "/api/league"):
            league = (query.get("league") or [""])[0]
            if not league.isdigit():
                self._json(400, {"error": "league id must be numeric"})
                return
            endpoint = (
                f"draft/{league}/choices"
                if url.path == "/api/picks"
                else f"league/{league}/details"
            )
            code, payload = _upstream(endpoint)
            self._json(code, payload)
            return

        self._json(404, {"error": "not found"})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve the draft app with a live API proxy")
    ap.add_argument("--teams", type=int, default=10)
    ap.add_argument("--league", type=int, default=None, help="draft league id, for auto-sync")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--refresh", action="store_true", help="refetch injury news first")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument(
        "--simulate",
        action="store_true",
        help="practice draft: bots pick and it pauses on your turn",
    )
    ap.add_argument("--pick-seconds", type=float, default=2.5)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    html = render(args.teams, refresh=args.refresh)
    if args.league:
        # Hand the league id to the page so it can start polling immediately.
        html = html.replace("const LEAGUE_ID = null;", f"const LEAGUE_ID = {args.league};")

    if args.simulate:
        entries = [m.team for m in LEAGUE]
        me = next((m.team for m in LEAGUE if m.me), entries[0])
        if args.league:
            code, payload = _upstream(f"league/{args.league}/details")
            if code == 200 and payload.get("league_entries"):
                entries = [e["entry_name"] for e in payload["league_entries"]]
        board = build_board(fetch_bootstrap(), len(entries), ALL_BOARDS)
        Handler.sim = Simulator(
            entries, me, board, seconds_per_pick=args.pick_seconds, seed=args.seed
        )
        Handler.sim.start()
        # The page must poll this server, not the real league.
        # A string sentinel, not 0: the page has several truthiness checks on
        # the league id, and 0 is falsy. The sim branch in do_GET runs before
        # the numeric validation, so a non-numeric id is fine here.
        html = html.replace("const LEAGUE_ID = null;", 'const LEAGUE_ID = "practice";')
        html = html.replace(f"const LEAGUE_ID = {args.league};", 'const LEAGUE_ID = "practice";')
        html = html.replace("const SIM_MODE = false;", "const SIM_MODE = true;")
        print(f"PRACTICE DRAFT — bots pick every {args.pick_seconds}s and stop on your turn ({me})")
        print("  order: " + " > ".join(Handler.sim.order))

    Handler.html = html
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"

    if args.league and not args.simulate:
        code, payload = _upstream(f"league/{args.league}/details")
        if code == 200:
            league = payload.get("league", {})
            entries = payload.get("league_entries", [])
            print(f"league {args.league}: {league.get('name')!r} — {len(entries)} entries")
            print(f"  draft status: {league.get('draft_status')}  at {league.get('draft_dt')}")
        else:
            print(f"warn: could not read league {args.league} ({payload.get('error')})")
            print("  the app still works — enter picks manually")

    print(f"\n  draft app: {url}")
    print("  auto-sync: " + ("on" if args.league else "off (pass --league <id>)"))
    print("  ctrl-c to stop\n")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
