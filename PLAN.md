# fpl-oracle build plan — GW1 draft-mode sprint

Deadline: GW1, ~1 week out. Ship draft mode first; weekly pipeline evolves after.

## Phase 1 — Roster (days 1–2)
- [x] Seed roster: `roster/seed_roster.py` — owner-curated canonical 20 (supersedes an
      earlier from-scratch-researched draft). Institutional brands excluded by design (no
      person = no track record = no Tier.CORE claim).
- [x] Resolve channel_ids via YouTube Data API — 20/20 resolved (3 of them — FPL Salah, FPL
      Matthew, Big Man Bakar — turned out to be personas whose content lives on Fantasy
      Football Hub's channel, not a personal one; flagged in `roster/registry.py` for
      title-filtered ingestion later). See `roster/resolve_channels.py`.
- [x] Resolve each creator's real FPL team ID — corrected methodology (real name first, THEN
      match against `entry/{id}/history/`, not channel/brand name) and re-checked every
      previously-rejected candidate. **Still 0/20 API-verified** — every candidate, old and
      new, failed live verification (a few because a years-old article's ID has since been
      reassigned to an unrelated manager — a standing risk, not a one-off; see
      `roster/registry.py` module docstring). Added a `Verification.DOCUMENTED` tier for
      creators BBC/FFS have publicly attributed specific finishes to under their real name —
      6/20 qualify and sit at `Tier.CORE`; the other 14 are `Tier.SECONDARY` at the default
      weight. Full per-creator sourcing in `roster/registry.py` notes.
- [x] `roster/weights.py`: `compute_weight()` (API tier) + `compute_documented_weight()`
      (documented tier) + `weight_for()` dispatcher — exercised on the 6 documented creators;
      still unexercised on real API history since no one has a verified ID yet.
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
