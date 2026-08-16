---
name: fpl-domain
description: >
  Fantasy Premier League domain knowledge for the fpl-oracle project: FPL API
  endpoints and quirks, official squad/budget/formation rules for the solver,
  pick-extraction schema conventions, player-name fuzzy-matching rules, and
  creator weighting. Use this skill whenever writing or reviewing code that
  touches the FPL API, the squad solver, transcript pick extraction, consensus
  scoring, or creator rank data — even if the task doesn't mention "FPL rules"
  explicitly (e.g. "fix the optimizer", "why is Saka missing", "add a captain
  column").
---

# FPL Domain Knowledge

## The FPL API (unofficial, read-only, no auth for public data)

Base: `https://fantasy.premierleague.com/api/`

| Endpoint | What it gives you |
|---|---|
| `bootstrap-static/` | THE core payload: all players (`elements`), teams, gameweeks (`events`), current prices, positions (`element_type`), ownership %. Cache aggressively; refetch daily and near deadlines. |
| `entry/{team_id}/` | A manager's public profile: name, overall rank, team value. |
| `entry/{team_id}/history/` | Gameweek-by-gameweek points/rank this season + `past` array of prior-season finishes. **This is how creator skill is verified.** |
| `entry/{team_id}/event/{gw}/picks/` | A manager's actual 15 picks for a gameweek (public after deadline). |
| `element-summary/{player_id}/` | Per-player fixture list + history. |
| `event/{gw}/live/` | Live points during a gameweek. |
| `fixtures/` | Full fixture list with difficulty ratings. |
| `leagues-classic/{league_id}/standings/` | League tables (paginated). |

Quirks:
- Player position comes as `element_type` int: 1=GK, 2=DEF, 3=MID, 4=FWD.
  (If the current season's bootstrap contains a 5th type, the game added a
  position — verify before hardcoding.)
- Prices are ints in tenths of a million: `55` means £5.5m.
- `web_name` is the short display name ("Saka"); `first_name`/`second_name`
  hold the full name. Match against BOTH.
- The API sometimes 403s generic user agents. Send a browser-like `User-Agent`.
- Team IDs in `bootstrap-static` `teams` are season-specific ints, not stable
  across seasons.
- Be polite: cache, throttle (~1 req/s), never hammer near deadline.

## Squad rules (solver constraints)

Verify against current-season `bootstrap-static` before finalizing, but the
long-stable rules:

- Budget: £100.0m for the initial 15-man squad.
- Squad: exactly 15 = 2 GK, 5 DEF, 5 MID, 3 FWD.
- Max 3 players from any one real club.
- Starting XI (11 of the 15): exactly 1 GK, ≥3 DEF, ≥1 FWD, 11 total.
- Captain scores double; vice-captain substitutes if captain doesn't play.
- 1 free transfer per gameweek (bankable, cap has been 5); extra transfers
  cost -4 points each.
- Chips: Wildcard (x2/season), Free Hit, Bench Boost, Triple Captain —
  verify current-season chip set at season start; the game adds/changes chips
  some years.
- Scoring changed in 2025/26 to add defensive-contribution points; do not
  assume scoring rules from older training data — check the current rules page.

## Pick extraction schema conventions

Extract from transcripts into this shape (Pydantic, `extract/schemas.py`):

- `creator_id`, `video_id`, `published_at`, `gameweek`
- `picks: list[Pick]` where Pick = `player_name_raw` (verbatim from transcript),
  `team_inferred` + `position_inferred` (LLM infers from context/PL knowledge —
  the COMPOSITE KEY name+team+position is what gets matched, never the bare string),
  `player_id` (resolved, nullable until matched), `action`
  (`squad_include | transfer_in | transfer_out | captain | vice | bench | avoid | watchlist`),
  `conviction` (1–5, judged from language: "nailed", "punt", "locked in" = high;
  "maybe", "monitoring" = low), `time_horizon` (int, gameweeks: 1 = this-week punt,
  6 = long-term hold; infer from language like "for the run of fixtures"),
  `reasoning` (≤1 sentence, creator's own logic).
- Creators narrate multi-week STRATEGY ("banking a transfer for Palmer GW4",
  "wildcard GW8-9") — extract these as watchlist/planning picks with the right
  `time_horizon`, they steer future gameweeks.
- One video may reveal a full 15-man squad → 15 `squad_include` picks + 1 captain.
- Creators change their minds mid-video ("I had X but switched to Y") —
  extract the FINAL stated position only.
- Hedged non-picks ("if you already own him, hold") are `watchlist`, not
  `transfer_in`.

## Player name matching (the #1 data-quality risk)

Transcripts mangle names: "Sacca"→Saka, "Hall and"→Haaland, "M'bappe",
"Van Dyke"→van Dijk, "Gakpo/Gapko". Rules:

1. Resolve `player_name_raw` against bootstrap `elements` with rapidfuzz
   (`token_set_ratio` on web_name AND full name; try phonetic fallback for
   score <85).
2. Match on the composite key (name + inferred team + inferred position),
   never the bare string — "Gabriel" alone matches Magalhaes, Martinelli AND
   Jesus; with team+position it's unambiguous.
3. NEVER auto-accept a match below threshold; queue for review with the
   surrounding transcript sentence.
4. NEVER store an unresolved raw name as if it were a player. Hallucinated
   players poison the consensus.

## Consensus & solver design rules (post-review decisions)

- PRICE BANDS: votes only compare within similar price brackets ("weight
  classes"). A cheap enabler's 18 votes mean "best budget option", never
  "better than Salah". Score within bands; the solver shops across bands.
- CAPTAINCY IS A SEPARATE ELECTION: `captain` picks aggregate in their own
  vote table, never blended into general scores (captaincy = ceiling, squad
  = floor). Solver constraint: squad must contain >=2 of the top consensus
  captain options. Report gives captaincy its own section with dissent.
- OPINIONS AGGREGATE, FACTS VETO: recency-decay opinion votes gently (later
  videos saw more news). Player availability is a FILTER before the solver,
  never a vote: bootstrap-static carries `status` and
  `chance_of_playing_next_round` per player — doubtful players get
  capped/excluded regardless of vote totals. Full pipeline reruns deadline
  morning.
- CONSTRAINTS ARE FOR RULES, PREFERENCES ARE FOR SCORES: hard solver
  constraints only for actual game rules (budget, quotas, 3-per-club) +
  the captain-options constraint. External signals (e.g. v2 bookmaker odds)
  enter as score adjustments, never pass/fail gates.

## Creator weighting

- Skill is verified via `entry/{team_id}/history/` `past` seasons.
  Popularity (subscribers) is discovery signal only, never weight.
- Baseline weight: monotone in past-season finishes (e.g. log-rank based),
  recency-weighted (last 3 seasons matter most).
- Creators herd; opinions are correlated. Consensus math should expect
  effective N << actual N. Diversity of style matters more than roster size.
- V1 FREEZES WEIGHTS: multi-season history sets them before GW1; they do
  not move in-season (weekly realized points reward variance, not skill —
  proper grading needs expected-stats data, which is v2 scope). V1 only
  LOGS every creator's stated picks + outcomes, building the v2 dataset.
- SHADOW BENCHMARK: track a "just copy the best creator" team all season
  (their picks are public via entry/{id}/event/{gw}/picks/ after each
  deadline) and print it in every gameweek report next to ours — the null
  hypothesis stays on the scoreboard.
