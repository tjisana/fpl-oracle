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
      5/20 qualify. A `Verification.SELF_CLAIMED` tier also exists for a creator's own
      on-record self-reported finishes (their own screenshot/intro video, no third party),
      shrunk harder (0.75 vs DOCUMENTED's 0.85) for self-report selection bias; Andy (Let's
      Talk FPL) was reclassified from DOCUMENTED to SELF_CLAIMED for his own season-review
      video's claim — the honest tier for that evidence. So 5 DOCUMENTED + 1 SELF_CLAIMED =
      6/20 sit at `Tier.CORE`; the other 14 are `Tier.SECONDARY` at the default weight. Full
      per-creator sourcing in `roster/registry.py` notes.
- [x] `roster/weights.py`: `compute_weight()` (API tier) + `compute_documented_weight()` +
      `compute_self_claimed_weight()` (claim-based tiers, sharing a `_claimed_weight()`
      helper) + `weight_for()` dispatcher — unit-tested in `tests/test_weights.py` (formula
      math, band boundaries, caps/floors, dispatcher routing, evidence-ordering invariant
      across tiers); still unexercised on real API history since no one has a verified ID
      yet.
- [x] Transcript ingestion working end-to-end on 3 videos — `ingest/transcripts.py`
      (fetch/save/load, on-disk cache doubling as the read-through store) +
      `ingest/youtube_client.list_recent_videos` (uploads-playlist resolution, 6h-fresh
      cache) + `ingest/run_ingest.py`, now with shared-channel attribution instead of a
      blanket exclusion: The FPL Wire's episodes (joint discussions, no reliable per-co-host
      title split) are ingested once via `channel_primary=True` on Pras (a CORE creator) —
      Zophar and Lateriser stay uningested individually, their content flows in through Pras's
      weight. Fantasy Football Hub's three personas (FPL Salah, FPL Matthew, Big Man Bakar)
      each get a `title_filter` and are attributed a video only when exactly one persona's
      filter matches its title (`ingest/run_ingest.videos_for_creator`, pure + unit-tested).
      Ran for real: 3/3 saved (Let's Talk FPL, Pras / The FPL Wire, FPL Focal), all
      auto-generated English — Pras/FPL Wire content is now flowing in as intended. Diagnostic
      against the FFH channel's 10 most recent uploads found none of the three persona title
      filters currently matching anything — worth a human re-check once FFH next posts
      persona-titled content; not tuned further without title evidence. Unit tests in
      `tests/test_attribution.py` (pure attribution function + registry invariants) and the
      existing `tests/test_transcripts.py`.

## Phase 2 — Extraction (days 3–4)
- [x] `extract/schemas.py`: Pick / VideoExtraction Pydantic models — composite key
      (name_raw + inferred team + inferred position), `time_horizon` (1–38),
      conviction 1–5, required personal-vs-group `provenance` flag (Phase 1
      carry-over). Reviewed + merged. Notes for the extraction call: LLM emits
      picks + gameweek ONLY (pipeline fills video metadata — never let the model
      parrot identifiers), and the call needs a retry-on-ValidationError loop so
      one over-long `reasoning` sentence can't drop a whole video's picks.
      Player-less chip-timing plans ("wildcard GW8") are knowingly out of
      v1 extraction scope.
- [ ] `fpl/client.py` + `fpl/players.py`: bootstrap-static client, player DB,
      composite-key resolver (rapidfuzz on name, validated against team+position)
- [x] Extraction prompt + Claude structured-output call — `extract/extractor.py` +
      `extract/prompts/extract_picks.txt`. claude-opus-5 via `messages.parse` with an
      UNCONSTRAINED wire schema (no player_id, no bounds — parse then only fails on
      truncation); strict validation into Pick/VideoExtraction happens in a
      retry-on-ValidationError loop (max 2) with error feedback. Reviewed, fixes
      verified, merged. On the first real batch run: verify cache_read_input_tokens > 0
      (system prompt sits just above the 512-token cache minimum).
- [ ] Run across all available "My GW1 Team" videos; review match quality
- [ ] SELF_CLAIMED harvest sweep — INFRASTRUCTURE MERGED (reviewed + verified):
      `roster/harvest.py` (season-review title matcher, 200-deep/May-1-cutoff uploads
      paging, transcript fetch via existing ingest machinery, manifest),
      `roster/claim_verify.py` (mechanical casefolded exact-substring quote check),
      `roster/claims.py` (RankClaimCandidate + owner-review markdown with approve
      checkboxes + timestamped links). Also landed: min-duration Shorts filter in
      ingest (180s, non-fatal on API failure) — the Phase 1 carry-over. ALSO MERGED
      (reviewed + verified): `roster/claim_extract.py` + prompt — the Claude
      rank-claim extraction (timestamps DERIVED by locating the verified quote in
      segments, model hint only as fallback; per-video failures non-fatal, listed in
      a FAILED section; creator identity + shared-channel context passed to the
      model). REMAINING (operational): the full 20-creator live sweep
      (`uv run python -m fpl_oracle.roster.harvest` then
      `uv run python -m fpl_oracle.roster.claim_extract`), then OWNER APPROVAL of
      each claim in data/harvest/claims_review.md. Original task spec follows. (MUST land before the GW1 weight freeze, else
      it waits a year): for every creator, locate season-review / season-opener
      videos by title, fetch transcripts (existing `ingest/` machinery), have
      Claude extract rank claims (quote + timestamp), then OWNER APPROVES each
      claim before it enters `roster/registry.py`. Evens the playing field —
      Andy's entry got this treatment manually; the other 19 haven't (selective
      evidence-gathering is itself a bias). No auto-writes to the trust model.
      QUOTE-ANCHORING RULE: every extracted claim must carry a verbatim quote
      that a script verifies is an exact substring of the stored transcript
      (kills fabricated/misread claims mechanically, no LLM in the check);
      the owner then judges only interpretation (overall classic rank? the
      creator speaking, not a guest? admissibly specific?) from quote +
      timestamp + link — never by rereading whole transcripts.

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
