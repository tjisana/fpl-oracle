# fpl-oracle — Phase 1 design review

A design-level walkthrough of the project as of the end of Phase 1
(commit `992d164`, 2026-08-17). Between a high-level overview and a deep
dive: the *why* behind each design, with code references where they earn
their place.

Status: sections are filled in as the review conversation progresses.

## Topics

1. **The big picture** — the pipeline as an assembly line, and the core
   bet the whole project makes.
2. **The roster & the trust model** — who counts as a creator, the
   verification tiers, why brands are excluded, and what the 0/20
   API-verification failure taught us.
3. **Weights: turning skill into a number** — log-rank scoring, recency,
   the documented-tier discount, and the philosophy of the 0.2 floor.
4. **Ingestion** — channels → videos → transcripts; the caching
   philosophy (quota, freshness, transcripts-as-cache); secret hygiene.
5. **Shared channels & attribution** — the one-creator-per-video
   invariant, `channel_primary` vs `title_filter`, and the
   group-as-entity debate.
6. **Testing & guardrails** — what the 55 tests actually pin down, the
   network-test boundary, and the review/model-routing workflow as part
   of the design.
7. **Risks & open questions going into Phase 2** — what Phase 1 hands to
   Phase 2, and where it's most likely to hurt.

---

## 1. The big picture

### The assembly line

```
roster/     WHO do we trust, and how much?      (people + weights)
ingest/     WHAT did they say?                  (videos → transcripts)
extract/    WHAT did they actually recommend?   (transcripts → structured picks)   [Phase 2]
fpl/        Reality check                       (official player DB, prices, availability)
consensus/  Tally the votes                     (weighted per-player scores)       [Phase 3]
solver/     Build the best legal squad          (PuLP integer program)             [Phase 3]
report/     Explain the answer                  (markdown gameweek report)         [Phase 4]
```

Each stage is a package under `src/fpl_oracle/`, and data crosses each
boundary as a typed Pydantic model — never a raw dict. That rule
(CLAUDE.md, "Conventions") is what keeps a seven-stage pipeline debuggable:
every stage can be run, tested, and reasoned about alone.

### The core bet

**A weighted aggregate of proven FPL creators beats any single creator —
including the best one.** Every individual expert has blind spots, pet
players, and variance week to week. Aggregation cancels the noise and
keeps the signal, *if* you weight by demonstrated skill rather than
popularity. Everything else in the design is downstream of this bet.

ELI5: ask 20 fantasy managers with receipts for their GW1 teams. Count
votes, but a manager who finished top-1k gets a louder voice than one
with no track record. Then hand the vote totals to an accountant (the
solver) who builds the best squad the rules allow, and let an editor
(the LLM nuance pass) flag anything the voters were nervous about.

### Three principles that fall out of the bet

1. **Opinions vote, facts veto.** Creator picks are opinions — they get
   weighted votes in `consensus/`. Player availability (injured,
   suspended) is a fact from the FPL API — it vetoes, unconditionally,
   before the solver ever sees the player (`fpl/availability.py`,
   Phase 3). No number of enthusiastic votes overrides a fact.
2. **The LLM lives at the fuzzy edges, never in the core.** LLMs are
   used exactly twice: turning messy transcripts into structured picks
   (extraction) and sanity-checking the final squad (nuance). The middle
   — scoring, aggregation, optimization — is deterministic math. You can
   rerun it, test it, and trust that the same inputs give the same squad.
3. **The FPL API is the only source of truth for reality.** A name in a
   transcript ("Isak", "the big Swede", "Alexander") is a *claim*; it
   only becomes data after resolving against the official player DB
   (rapidfuzz match, validated by team + position — Phase 2). Reject or
   flag; never store an unresolved name.

### 1a. A worked toy example (from the review conversation)

Three creators, five-man draft squads. Weights: creator 1 (top historical
ranks) 0.8, creator 2 0.5, creator 3 (new, unverified) 0.2 — the last is
`DEFAULT_TIER2_WEIGHT` in `roster/weights.py`. A player's consensus score
is the sum of the weights of everyone who picked him:

| Player | Picked by | Score |
|---|---|---|
| Haaland, Guehi | all three | 1.5 |
| Isak, Cherki | creators 1+2 | 1.3 |
| Eze | creator 1 | 0.8 |
| Saka | creator 2 | 0.5 |
| Wirtz, Rashford, Palmer | creator 3 | 0.2 |

Lessons the example carries:

- **Tiebreaks aren't a rule, they're addition.** "Prefer the proven
  creator's one-off pick" emerges from the weighted sum on its own
  (Eze 0.8 > Saka 0.5).
- **The solver exists because "top 15 by score" is usually illegal** —
  budget, formation, and max-3-per-club constraints mean the real
  question is "best total score among *legal* squads", which is an
  integer program, not a sort.
- **Votes only compete within price bands** (Phase 3): a £4.5m enabler
  pick answers a different question than a premium pick, so pooling all
  votes in one table would systematically undervalue budget players.
- **Unproven voices aggregate.** One new kid at 0.2 is noise; three new
  kids agreeing (0.6) outvote a mid-tier creator. No single unproven
  voice matters, but an unexpected chorus does.

### Why "draft mode first"

PLAN.md scopes v1 to the GW1 draft: every creator publishes a
from-scratch team at the same time, answering the same question, with
the same £100m budget. That's the cleanest possible input for a
consensus system. The harder in-season problem (transfer advice relative
to each creator's own squad — the "vacuum" problem) is deliberately
deferred to post-GW1.


---

## 2. The roster & the trust model

### A creator is a person, never a brand

`Tier`'s docstring (`roster/models.py`) carries the rule: CORE requires a
real person's track record. Brands (Fantasy Football Scout, Fantasy
Football Hub) were dropped from the roster entirely — rotating staff
means no individual whose finishes can be verified. No person, no track
record, no CORE claim.

### Two orthogonal axes

- **`Tier`** — how loud the voice is (CORE = 1, SECONDARY = 2). The
  conclusion.
- **`Verification`** — what evidence backs it. The evidence class:
  - **API**: history pulled from `entry/{id}/history/` — pullable,
    auditable, re-checkable. Gold standard ("payroll records").
  - **DOCUMENTED**: reputable third party (BBC, FFS Pro Pundits)
    publicly attributed finishes to the person's real name. Strong but
    not independently re-pullable, so shrunk (see §3).
  - **UNVERIFIED**: no substantiated claim. Default 0.2 — "no evidence
    of skill", not "known bad".

### The 0/20 story — the bar didn't move, the taxonomy grew

No creator is API-verified. Two causes, recorded in `registry.py`'s
docstring as institutional memory:

1. **Methodological**: the first pass matched candidate IDs against
   channel names, not real names — silently discarding correct IDs.
   Corrected order: real name first (BBC/FFS/Companies House/LinkedIn),
   then candidate matching.
2. **Environmental**: even corrected, every candidate failed live
   verification — years-old article IDs now belong to unrelated
   managers. FPL entry IDs from old sources go stale. Standing risk.

Design response: instead of lowering the bar ("close enough, call it
verified"), a new honest evidence class was added. `DOCUMENTED` says
exactly what it is; 6/20 qualify (CORE), 14 default to SECONDARY.
`ClaimedFinish` encodes conservatism structurally: "a top-10k finish"
becomes `rank=10_000` — the worst rank consistent with the claim — and
every claim carries a `source_url`.

### Two encoded lessons

- **Fabricated data is a live threat**: one research tool returned a
  plausible, fully-fabricated table of FPL entry IDs (every ID resolved
  to an unrelated manager). Rule: no numeric FPL ID enters the registry
  without a live API check, regardless of citations. The
  source-of-truth principle applies to our own research, not just
  transcripts.
- **Weights are frozen for v1**: computed once at registry load, never
  updated in-season. Grading skill on realized weekly points mostly
  grades luck; v2 plans to grade on process stats (xG/xA) instead.

### 2a. The SELF_CLAIMED tier (added during this review)

The review conversation surfaced that the DOCUMENTED tier was quietly
holding one entry it didn't honestly describe: Andy's 588th-overall
finish, sourced to *his own* season-review video. The owner proposed an
"honor system" for creators' on-record self-reported finishes; the
refined version became a fourth evidence class:

- **`Verification.SELF_CLAIMED`** — the creator's own on-record claim
  (their video, their screenshot). Admissibility is about the claim's
  *shape* (specific rank + season + own named account, on the record),
  **not follower count** — popularity stays out of the trust model.
- Shrink **0.75** vs DOCUMENTED's 0.85: self-report cherry-picks best
  seasons (third parties at least curate), so it's discounted harder.
- Claims still enter the registry by hand — no automatic loop where
  ingested content adjusts the trust model that weights ingested content.

Two things this episode demonstrated:

1. **The pipeline can corroborate claims.** Andy's 588 was verified
   against his video's own auto-generated transcript using
   `ingest.transcripts` ("my final rank ... is 588th in the world") —
   the ingestion machinery doing verification work three phases early.
   The same transcript held his full 16-season recap, which became a
   second conservative claim (four further top-10k finishes, the 588
   excluded to avoid double-counting).
2. **FPL entry IDs are per-season** (owner's correction, now in the
   registry docstring): fresh registration every season, sequential
   assignment. Old article IDs pointing at strangers is structural, so
   API verification effectively requires current-season self-disclosure
   — making SELF_CLAIMED the primary realistic evidence channel, not a
   fallback.

Outcome: Andy sits 3rd of 20 (0.543) under the same rules as everyone
else — reclassified honestly, then evidenced properly. The ordering
invariant (documented > self_claimed > default for the same claim, until
the 0.2 floor compresses it at weak ranks) is pinned in
`tests/test_weights.py`.

---

## 3. Weights: turning skill into a number

*(Much of this section was covered live during the Andy/SELF_CLAIMED
discussion — see §2a. Recorded here in one place.)*

### The scale: log-rank ("how many digits is your rank?")

`_rank_score(rank) = 1 − log10(rank) / log10(10_000_000)` — rank 1
scores 1.0, and every 10× worsening costs 1/7 ≈ 0.143: 10 → 0.857,
100 → 0.714, 1k → 0.571, 10k → 0.429, 100k → 0.286, 10M → 0. The log
encodes the domain truth that 100th-vs-1,000th is a chasm while
1.0M-vs-1.9M is noise. The 10M pool constant is deliberately rough —
it only anchors the scale.

### Tier-by-tier machinery (`roster/weights.py`)

- **API tier** (`compute_weight`): recency-weighted average of the last
  3 seasons' rank scores, weights [1.0, 0.6, 0.3], older seasons dropped
  entirely. Currently unexercised — no creator has a verifiable ID
  (entry IDs are per-season; see §2a) — but the machinery is ready.
- **Claim tiers** (`_claimed_weight`, shared): best claim's rank score
  + consistency bonus (0.03 per extra finish, capped at 0.15 so
  repetition alone can't fake elite) → capped at 1.0 → × shrink
  (DOCUMENTED 0.85, SELF_CLAIMED 0.75) → floored at 0.2.
- **UNVERIFIED**: flat 0.2.

### Asymmetries that are features

- **Verification can hurt you; claims can't.** An API-verified history
  of genuinely bad finishes scores below 0.2 — real evidence of
  weakness. Claim tiers are floored at 0.2: reaching them at all means a
  real documented achievement, and cherry-picked evidence can't prove
  badness, only goodness.
- **Claim tiers have no recency term.** Andy's pre-588 decline
  (35k/44k/77k) is invisible to the formula, as is everyone else's
  trajectory — claims arrive without reliable season-by-season
  structure. Acknowledged simplification; a v2 refinement candidate.
- **The ordering invariant** documented > self_claimed > default holds
  for equivalent claims until the floor compresses it (below roughly
  rank 136k the tiers begin collapsing onto 0.2) — pinned in
  `tests/test_weights.py::TestEvidenceOrdering`.

### The comment bug, and why it mattered

The original `DEFAULT_TIER2_WEIGHT` comment claimed 0.2 sits below a
top-400k verified finish; the actual math (`_rank_score(400k) ≈ 0.1997`)
says the opposite. Caught by the test-writing subagent on day one,
ruled a stale comment (constant kept, example corrected to top-300k).
Lesson kept: design-rationale comments get audited like code, because
they're where the next reader learns what the constants *mean*.
