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
      previously-rejected candidate. **RESOLVED 2026-08-19: 19/20 API-verified** — found via the
      leagues-classic `admin_entry` technique (a creator's own branded league names its
      creator as admin); every ID confirmed live, and the histories independently
      reproduce the third-party claim ledger. Weights now come from `compute_weight()`
      on real multi-season history. Only FPL Dylan has no discoverable entry.
      HISTORICALLY (when this line was written) 0/20 were verified — every candidate,
      old and new, failed live verification (a few because a years-old article's ID has since been
      reassigned to an unrelated manager — a standing risk, not a one-off; see
      `roster/registry.py` module docstring). Added a `Verification.DOCUMENTED` tier for
      creators BBC/FFS have publicly attributed specific finishes to under their real name —
      5/20 qualify. A `Verification.SELF_CLAIMED` tier also exists for a creator's own
      on-record self-reported finishes (their own screenshot/intro video, no third party),
      shrunk harder (0.75 vs DOCUMENTED's 0.85) for self-report selection bias; Andy (Let's
      Talk FPL) was reclassified from DOCUMENTED to SELF_CLAIMED for his own season-review
      video's claim — the honest tier for that evidence. So 5 DOCUMENTED + 1 SELF_CLAIMED =
      6/20 sat at `Tier.CORE` (now 19/20, post-API-verification). Full
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
- [x] `fpl/client.py` + `fpl/players.py`: bootstrap-static client (6h-fresh cache),
      player DB, two-tier composite-key resolver. Tier 1: fuzzy >= 85 with a
      POSITION contradiction veto (always fatal), a TEAM contradiction veto that an
      EXACT name overrides (the extractor's team knowledge goes stale across the
      transfer window — 20 correct picks lost that way in the first live run), and
      subset-100 hardening (token_set_ratio
      scores 100 on token-subset names — "Hall and" vs the real "Hall"). Tier 2
      (fuzzy 40-85): phonetic (jellyfish metaphone) + team + position must ALL
      agree. Team aliases for full club names (Tottenham→Spurs etc, built from the
      live payload). Empirically verified on the real 592-player roster: 1,780-case
      mangling probe = 0 wrong matches, garbage 20/20 UNMATCHED. Reviewed + merged.
- [x] Extraction prompt + Claude structured-output call — `extract/extractor.py` +
      `extract/prompts/extract_picks.txt`. claude-opus-5 via `messages.parse` with an
      UNCONSTRAINED wire schema (no player_id, no bounds — parse then only fails on
      truncation); strict validation into Pick/VideoExtraction happens in a
      retry-on-ValidationError loop (max 2) with error feedback. Reviewed, fixes
      verified, merged. On the first real batch run: verify cache_read_input_tokens > 0
      (system prompt sits just above the 512-token cache minimum).
- [x] Run across all available "My GW1 Team" videos; review match quality — DONE:
      14 creators extracted, 390/478 picks resolved (82%) after two resolver fixes
      found by the human review (exact-name-vs-matched_target; stale team hints
      across the transfer window). Owner reviewed 2026-08-19. Remaining unresolved
      are genuine ambiguity ("Bruno"), extraction noise, and descriptive phrases.
      4 creators had no GW1 video yet — deadline-morning rerun should catch them.
      RUNNER MERGED (reviewed + verified): `extract/run_extract.py` — per creator:
      GW1-title-matched video (gw1X false-stem guard, Shorts filter, shared-channel
      attribution), extract, resolve every pick (MATCHED → player_id stamped;
      UNMATCHED/AMBIGUOUS kept-but-flagged with candidates, never fabricated).
      Per-creator failures non-fatal incl. anthropic.APIError; report flushed in
      finally. Outputs data/extractions/{video_id}.json + match_quality.md.
      REMAINING: the live run + HUMAN review of match_quality.md.
- [x] SELF_CLAIMED harvest sweep — DONE: 31 quote-verified claims across 5 creators,
      owner-approved 2026-08-19 (Andy = no-op, already in registry; Raptor and Harry
      promoted to SELF_CLAIMED; Pras rejected as vague/mis-attributed). SUPERSEDED
      IN PRACTICE by the API-verification breakthrough — 19/20 creators now have
      real history, so claim tiers are inert for weighting (kept as provenance).
      Original spec: INFRASTRUCTURE MERGED (reviewed + verified):
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
- [x] `consensus/scoring.py` — band-relative scores (position + £1m bucket),
      strongest-vote-per-creator, saturating conviction, avoid as negative not veto,
      no herding discount in v1 (reasoning recorded). Reviewed + merged.
- [x] `consensus/captaincy.py` — separate election, 30% support floor, options drawn
      from the post-availability pool, relative thin-evidence flag. Reviewed + merged.
- [x] `fpl/availability.py` — facts veto: EXCLUDED is structural (not a threshold),
      verdicts returned for ALL players so DOUBTFUL multipliers can be applied.
      Reviewed + verified + merged.
- [x] `solver/squad.py` — PuLP ILP, x (squad) + y (XI) variable sets so budget goes
      to starters; price-anchored objective (price + 1.0*band_score); captaincy
      constraint relaxes rather than failing. Review verified all rules by SOLVING
      and confirmed consensus is NOT decorative: with it off, 7/15 squad members have
      no creator support; with it on, 15/15 — 10 of 15 members change.
- [x] First full squad output — produced 2026-08-19: £100.0m exactly, 3-4-3,
      B.Fernandes (C) / Haaland (VC), XI £83.5m / bench £16.5m (minimum legal).
- [x] Single pipeline entry point + run log (see Auditing note below)

### Auditing (gap found 2026-08-19)
Provenance is strong — every squad player traces back through
`data/extractions/{video_id}.json` (pick + resolution + candidates) to the stored
transcript, and `PlayerScore.votes` retains which creator voted, with what weight,
action and conviction. What's MISSING is a run record: nothing ties a shipped squad
to the inputs and code version that produced it, and a re-run silently overwrites.
Build before the deadline-morning rerun so that rerun is trustworthy.

**RESOLVED 2026-08-19/20**: `src/fpl_oracle/pipeline.py` —
`uv run python -m fpl_oracle.pipeline` runs stored extractions -> availability
filter -> consensus -> captaincy election -> solver in one command, writing
`data/runs/{run_id}/{run.json,squad.json}` (`run_id` = UTC timestamp + short
git SHA, with a numeric-suffix fallback so a same-second rerun still can't
collide) plus a `data/runs/latest.json` pointer. `run.json` (`RunRecord`)
records git commit/branch/dirty, every consumed extraction file's path +
sha256, the creator weights actually used, every EXCLUDED player + reason,
load/pick/pool counts, and the solver outcome (cost, formation, captain,
vice-captain, whether the >=2 captaincy guarantee was relaxed). Captain/vice
are picked in the pipeline layer (not the solver) from the captaincy
election's ranking filtered to who actually made the squad — a reporting
decision, no solver/consensus logic changed. Verified end-to-end against the
live 14-creator extraction set: reproduces the prior manually-verified
output exactly (£100.0m, 3-4-3, B.Fernandes (C) / Haaland (VC)). Tests in
`tests/test_pipeline.py` (run_id uniqueness/no-overwrite, input sha256,
unresolved-pick skip counting, EXCLUDED veto reaching the solver, relaxed-
captaincy surfacing, graceful empty/missing `data/extractions/`) — all
network-free via injected fake `PlayerDB`/registry.

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
