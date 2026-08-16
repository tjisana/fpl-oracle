# fpl-oracle

**An AI consensus engine for Fantasy Premier League** — it aggregates the public
expertise of ~20 proven FPL YouTube creators into one provably-optimal squad
recommendation every gameweek.

![ci](https://github.com/tjisana/fpl-oracle/actions/workflows/ci.yml/badge.svg)

## The idea

Most FPL tools predict player performance from raw data. This one doesn't.
It treats the FPL creator ecosystem as a distributed team of analysts who
already watch every press conference, track every price change, and publish
their reasoning weekly — then does what no human manager can: synthesize all
of them, every week, without ever tilting or missing a deadline.

The core design is a sandwich — **judgment → math → judgment**:

```
 ~20 YouTube creators (weighted by verified multi-season FPL rank)
        │
        ▼  transcripts
 ┌─────────────────┐
 │  LLM EXTRACTION │  language → rigid schema (player, action,
 │   (judgment)    │  conviction, time-horizon, reasoning)
 └────────┬────────┘
          ▼  validated picks (composite-key matched vs FPL player DB)
 ┌─────────────────┐
 │ CONSENSUS + ILP │  price-banded weighted votes → availability
 │     (math)      │  veto → PuLP solver: optimal 15 under
 └────────┬────────┘  £100m / 2-5-5-3 / max-3-per-club
          ▼  provably optimal squad
 ┌─────────────────┐
 │   NUANCE PASS   │  re-reads transcripts against the chosen 15;
 │   (judgment)    │  flags what the numbers couldn't hold
 └────────┬────────┘
          ▼
   Gameweek report: squad, captaincy consensus, dissent notes.
   A human approves every move.
```

## Design decisions worth stealing

- **Verified skill, not popularity.** Creator vote weights come from their
  actual multi-season FPL finishes (public via the FPL API), never subscriber
  counts. Weights are frozen in-season — week-to-week results are too noisy
  to grade experts on.
- **Schema is the contract.** LLM output consumed by code is forced through
  Pydantic models; hallucinated or unresolved player names are rejected at
  the boundary, never stored.
- **Opinions aggregate, facts veto.** Stale Tuesday conviction can't outvote
  Thursday team news: player availability is a pre-solver filter fed by the
  FPL API's own flags, and the whole pipeline reruns deadline morning.
- **Votes compete within price bands.** 18 votes for a £4.0m enabler mean
  "best budget option," not "better than Salah."
- **Captaincy is its own election.** The 2x armband is worth more than any
  squad slot, and ceiling ≠ floor — captain votes are counted separately and
  constrain the solver.
- **The null hypothesis stays on the scoreboard.** Every report tracks a
  shadow "just copy the best creator" team, so the system has to beat the
  obvious alternative in public.

## Stack

Python 3.12 · [uv](https://github.com/astral-sh/uv) ·
[ruff](https://github.com/astral-sh/ruff) · [ty](https://github.com/astral-sh/ty) ·
Pydantic · [PuLP](https://github.com/coin-or/pulp) (ILP solver) ·
Claude API (extraction + nuance) · Gemini API (multimodal fallback) ·
YouTube Data API + youtube-transcript-api · SQLite

## Setup

```bash
uv sync
cp .env.example .env   # ANTHROPIC_API_KEY, GEMINI_API_KEY, YOUTUBE_API_KEY
uv run pytest -m "not network"
```

See [PLAN.md](PLAN.md) for the build roadmap and
[CLAUDE.md](CLAUDE.md) for working conventions — this repo is built
AI-first, and its agent tooling (`.claude/`) is part of the design.

## Status

🚧 Building toward GW1 of the 2026/27 season. Draft mode first; the weekly
transfer recommender, creator-accuracy dataset, and odds-signal integration
are on the [roadmap](PLAN.md).

## License

MIT
