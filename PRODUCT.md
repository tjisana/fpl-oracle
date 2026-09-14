# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing codebase: monorepo. `web/` is Vite + React + TypeScript (scaffold
committed 2026-09-12); Tailwind + shadcn/ui are the decided UI stack
(docs/web-app-design.md, decision 8) but not yet installed. The API will be
FastAPI (read-only), with OpenAPI-generated TypeScript types under
`web/src/api/`. Deploy target: S3 + CloudFront for `web/`, later.

## Users

Primary: the owner, a single FPL manager, in two situations of equal weight:
- On a phone on match days and at the deadline: live points for the fifteen,
  and the change checklist before 12:30/17:30 UTC.
- On a desktop during the week: reading the report, the creator ownership
  grid and the evidence trail while planning.

Secondary, once a public URL exists: recruiters and hiring managers viewing
the site as a portfolio showpiece (TypeScript/React, AWS, API design, applied
AI). They read; they do not have a squad.

## Product Purpose

Tells the owner what to do with THEIR squad each gameweek, derived from a
weighted consensus of Fantasy Premier League YouTube creators. A pipeline
reads creators' videos, extracts every player pick with conviction, weights
creators by their real, API-verified FPL track records, and fits the result
to the owner's actual squad, bank and free transfers. The site is where that
output is read: the recommendation, the reasoning, and the evidence behind
it. Success is a better weekly decision than the owner would make alone, and
one place to look instead of ten videos.

## Positioning

Every recommendation is traceable to named people saying specific things:
which creator backed a pick, with what conviction, and (where the transcript
carries trusted timing) a deep link to the moment in the video. Weights come
from verified multi-season FPL history, not follower counts. No other FPL
tool shows the evidence trail; predicted-points tables are everywhere and
deliberately not built here.

## Operating Context

- Weekly gameweek cycle: creators post team-reveal videos in the two days
  before a deadline; the pipeline runs on deadline eve and deadline morning;
  a run produces a markdown report and, after the owner acts, a decision
  record. Runs are archived per gameweek.
- Two clocks: the gameweek deadline, and FPL's nightly price change
  (~01:30 UTC), snapshotted daily to S3.
- Live window: first kickoff to last whistle each gameweek, where the site
  shows live points from FPL's public endpoints.
- The owner acts in the official FPL app or site. This product never writes
  to FPL (decision 19): it presents a hand-off checklist and a link.
- Data: 14 active creators (3 in reserve, 1 retired), verified FPL entry IDs,
  transcripts read via Gemini, picks extracted via Claude, real run records
  since GW1 of 2026/27.

## Capabilities and Constraints

- Read-only site in release one over the pipeline's output: four tabs —
  My squad (live state or planning state by calendar), Report, Creator
  ownership, Evidence trail. Tab specs in docs/web-app-design.md.
- My squad: pitch view (eleven in formation, bench beneath) with a table
  toggle. Live state shows points so far, match state, provisional bonus
  and predicted autosubs, each LABELLED as provisional/predicted. No live
  rank (FPL publishes rank only after each day; estimates are out of scope).
- Report tab renders the run's markdown as-is first; sections are rebuilt
  from structured data one at a time.
- Evidence trail: per player, each creator's quote, conviction, personal
  vs group view, and a video link. Verbatim quotes and timestamped deep
  links are NOT yet produced by the pipeline (paraphrase + video link until
  quote extraction and timing enrichment land). Gemini-read transcripts
  carry no trusted timestamps today.
- Consensus scores are band-relative and carry no points unit: the product
  never claims a hit "pays for itself" or shows predicted points.
- Terminology: gameweek (GW), deadline, free transfer (FT), hit (-4),
  wildcard / free hit / bench boost / triple captain (chips), bank, team
  value, captain (C) / vice (VC), backers (creators voting for a player),
  wildcard gap (distance between owned squad and the consensus £100m squad,
  in single-transfer units), band score.
- Later releases, designed for now, not built: pipeline controls (enqueue
  warm/extract/solve with visible job status), an assistant text box over
  the site's own API, run-over-run ownership change, consensus momentum.
- Undecided: how the public showcase differs from the owner's private view
  (a landing page is likely; its design is a separate surface).

## Brand Commitments

- Name: **fpl-oracle** (decided 2026-09-14). "Blame the Algorithm", the
  scaffold's placeholder heading, is not the name; it may survive as a
  tagline candidate, not binding.
- No logo, no palette, no typeface exists. Nothing visual is inherited.
- Voice (from the report, which is the product's existing text): plain,
  evidence-first, willing to say what it does not know ("this system will
  not price the hit for you"). Labels provisional numbers as provisional.

## Evidence on Hand

- Real run records: `data/runs/<run_id>/{report.md, run.json, squad.json,
  decision.md}` for GW2–GW4 of 2026/27 (gitignored; available locally and
  usable as fixture data).
- Real extractions with per-creator match quality: `data/extractions/`.
- Roster with verified FPL histories and weights: `src/fpl_oracle/roster/registry.py`.
- Daily price snapshots: `data/snapshots/` and the S3 bucket.
- Reference product the owner named as the vision: livefpl.net (live rank,
  live bonus, price changes, effective ownership). This product overlaps on
  live points, price changes and creator ownership, and differs on the
  evidence trail.
- Absent, never to be fabricated: testimonials, user counts, press,
  accuracy claims, predicted points.

## Product Principles

1. Evidence before verdict: every number can be traced to who said what.
2. The owner's squad, not the creators': recommendations are fitted to the
   fifteen the owner actually holds.
3. Say what is not known: provisional, predicted and untimed are labelled;
   nothing is invented to fill a gap.
4. One place to look: the site replaces watching the videos, not the FPL
   app.
5. Read-only by design: the site informs the decision and hands off; it
   never acts on the owner's account.
