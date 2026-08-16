# fpl-oracle build plan — GW1 draft-mode sprint

Deadline: GW1, ~1 week out. Ship draft mode first; weekly pipeline evolves after.

## Phase 1 — Roster (days 1–2)
- [x] Seed roster drafted: `roster/seed_roster.py` (20 creators, tiered)
- [ ] Resolve channel_ids via YouTube Data API
- [ ] Resolve each creator's real FPL team ID (video descriptions / X bios / creators-league lookup)
- [ ] `roster/weights.py`: pull `entry/{id}/history/`, compute rank-based weights
- [ ] Transcript ingestion working end-to-end on 3 videos

## Phase 2 — Extraction (days 3–4)
- [ ] `extract/schemas.py`: Pick / VideoExtraction Pydantic models
      (incl. composite key: name + inferred team + inferred position; and `time_horizon`)
- [ ] `fpl/client.py` + `fpl/players.py`: bootstrap-static client, player DB,
      composite-key resolver (rapidfuzz on name, validated against team+position)
- [ ] Extraction prompt + Claude structured-output call
- [ ] Run across all available "My GW1 Team" videos; review match quality

## Phase 3 — Consensus + solver (day 5)
- [ ] `consensus/scoring.py`: weighted per-player scores from picks,
      scored WITHIN price bands (see fpl-domain skill: weight classes)
- [ ] `consensus/captaincy.py`: separate captaincy election from `captain` picks
- [ ] `fpl/availability.py`: pre-solver filter on `status` /
      `chance_of_playing_next_round` — facts veto, opinions vote
- [ ] `solver/squad.py`: PuLP ILP — 15 players, £100m, 2/5/5/3, max 3/club,
      plus: squad must include >=2 of top consensus captain options
- [ ] First full squad output

## Phase 4 — Nuance + ship (days 6–7)
- [ ] LLM nuance pass over solver output (flag concerns creators voiced)
- [ ] `report/gameweek.py`: markdown report — squad, captaincy section
      (own consensus + dissent), reasoning, per-pick dissent notes
- [ ] Deadline-morning rerun: refresh videos + availability flags, re-extract, re-solve

## Later (post-GW1)
- Weekly transfer recommender aware of my actual squad/budget/free transfers
  (in-season, score transfer advice within price bands — the "vacuum" problem
  bites here, not in draft videos)
- Shadow benchmark: "copy the best creator" team tracked via
  entry/{id}/event/{gw}/picks/, printed in every gameweek report
- Pick/outcome logging for every creator (v2 weight-grading dataset);
  weights stay FROZEN in v1 — no in-season updates
- `time_horizon`-aware consensus: this week's solve softly steered by
  aggregated multi-week plans (wildcard windows, banked transfers)

## v2 candidates (explicitly out of v1 scope)
- Creator weight updates graded on expected stats (xG/xA), not realized points
- Bookmaker odds as SCORE NUDGES (never solver constraints)
- Gemini multimodal fallback for visual-only team reveals
- One-click-approve "autopilot" staging
- League-specific risk adjustment (variance up when chasing, down when leading)
