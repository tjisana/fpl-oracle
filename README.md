# fpl-oracle

**An AI consensus engine for Fantasy Premier League.** It reads what the best
FPL YouTubers say each week, turns their opinions into structured data, and
solves for the single best legal squad you can buy with £100m.

[![ci](https://github.com/tjisana/fpl-oracle/actions/workflows/ci.yml/badge.svg)](https://github.com/tjisana/fpl-oracle/actions/workflows/ci.yml)

---

## The idea in one paragraph

Most FPL tools try to predict football from statistics. This one doesn't try to
out-predict anyone. It notices that a few dozen FPL content creators already do
the hard work every week — they watch every press conference, track every price
change, and publish their reasoning on YouTube — and that no human can actually
watch all of them, weigh them by how good they really are, and act without
bias or fatigue. A computer can. So the machine's job here isn't football
insight; it's **aggregation, arithmetic, and never missing a deadline**.

## How it works

The shape of the system is a sandwich: **judgment → math → judgment.**
Language models do what they're good at (reading messy human speech into
structure). An integer programme does what it's good at (finding the provably
optimal answer under hard constraints). Neither is asked to do the other's job.

```
 ~20 YouTube creators, weighted by their real multi-season FPL finishes
        │
        ▼  transcripts
 ┌──────────────────┐
 │  LLM EXTRACTION  │   speech → rigid schema:
 │    (judgment)    │   player, buy/sell/captain, conviction 1-5,
 └────────┬─────────┘   time horizon, reasoning, personal-vs-group
          │
          ▼  every player name matched against the official FPL database
 ┌──────────────────┐
 │ CONSENSUS + ILP  │   weighted votes, scored within price bands
 │      (math)      │   → injury/availability veto
 └────────┬─────────┘   → PuLP solver picks the optimal 15 under
          │               £100m / 2-5-5-3 / max 3 per club
          ▼
   A squad, a captain, and a full audit trail back to the sentence
   somebody actually said on camera. A human approves every move.
```

## Design decisions worth stealing

Even if you don't care about football, these are the parts that generalise.

- **Weight people by verified skill, not popularity.** A creator's vote weight
  comes from their actual finishing ranks over multiple seasons — public via
  the FPL API — never from subscriber count. 19 of the 20 creators in the
  registry have an API-verified team history behind their weight.
- **Weights are frozen in-season.** One good gameweek is noise. Grading experts
  on weekly results would just chase randomness.
- **The schema is the contract.** Anything an LLM produces that code will
  consume is forced through a Pydantic model at the boundary. A player name
  that can't be matched to the real FPL database is flagged or rejected —
  never quietly stored, never invented.
- **Opinions aggregate; facts veto.** A confident Tuesday recommendation cannot
  outvote a Thursday injury. Availability is a hard pre-solver filter fed by
  the FPL API's own flags, not another opinion in the pile.
- **Votes compete inside price bands.** Eighteen votes for a £4.0m defender
  means "the best budget option", not "better than Haaland". Scoring is
  relative to a player's position and price bucket.
- **Captaincy is its own election.** The armband doubles a score, so it's worth
  more than any single squad slot, and the best captain isn't just the best
  player. Captain votes are counted separately and then constrain the solver.
- **Make the boring baseline explicit.** The obvious alternative — just copy
  the highest-ranked creator — is a tracked design goal, so the system has to
  prove it beats it rather than assuming it does.

## Project status

Built and tested toward the 2026/27 season opener. **297 tests**, CI green
on every push (lint, format, type check, tests).

| Stage | Module | State |
|---|---|---|
| Creator registry + skill weights | `roster/` | ✅ 20 creators, 19 API-verified |
| YouTube monitoring + transcripts | `ingest/` | ✅ working end-to-end |
| LLM pick extraction + name matching | `extract/`, `fpl/` | ✅ 82% of picks auto-resolved on the first live run (390/478) |
| Weighted consensus + captaincy | `consensus/` | ✅ reviewed and merged |
| Availability veto | `fpl/availability.py` | ✅ reviewed and merged |
| Squad solver (PuLP ILP) | `solver/` | ✅ first real squad solved: £100.0m exactly, 3-4-3 |
| Single pipeline command + run log | — | 🚧 in progress |
| LLM nuance pass + markdown report | `report/` | ⬜ not started |

Right now the stages run as individual commands and the solver is called as a
library function — wiring them into one reproducible `run` with a recorded
audit log is the current task. See [PLAN.md](PLAN.md) for the full roadmap.

## Getting started

Requires Python 3.12 and [uv](https://github.com/astral-sh/uv).

```bash
git clone git@github.com:tjisana/fpl-oracle.git
cd fpl-oracle
uv sync
```

Copy the example environment file and fill in your own API keys
(`ANTHROPIC_API_KEY` for extraction, `YOUTUBE_API_KEY` for channel discovery):

```bash
cp .env.example .env
```

Run the test suite — this needs no keys and no network:

```bash
uv run pytest -m "not network"
```

### Running the pipeline

Each stage is its own module. They share an on-disk cache in `data/`, so
re-running is cheap and won't hammer the APIs.

```bash
uv run python -m fpl_oracle.roster.resolve_channels   # creator channels -> IDs
uv run python -m fpl_oracle.ingest.run_ingest         # fetch recent videos + transcripts
uv run python -m fpl_oracle.extract.run_extract       # transcripts -> validated picks
```

`run_extract` writes one JSON file per video to `data/extractions/` plus a
`match_quality.md` review file, so you can see exactly which names resolved,
which were ambiguous, and why. Consensus scoring and the solver are currently
used as library calls (`consensus.score_picks`, `solver.build_squad`).

Nothing in `data/` is committed — transcripts and API responses stay local.

## Repository layout

```
src/fpl_oracle/
  roster/      creator registry, FPL team IDs, skill-based vote weights
  ingest/      YouTube channel monitoring + transcript fetching (cached)
  extract/     LLM pick extraction, Pydantic schemas, prompts
  fpl/         official FPL API client, player database, availability flags
  consensus/   weighted vote aggregation + the captaincy election
  solver/      PuLP integer programme: the optimal 15
  report/      gameweek markdown report (not yet built)
tests/         297 tests; live-API tests marked `network` and skipped by default
```

## Stack

Python 3.12 · [uv](https://github.com/astral-sh/uv) ·
[Pydantic](https://docs.pydantic.dev) · [PuLP](https://github.com/coin-or/pulp)
(integer programming) · [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) +
[jellyfish](https://github.com/jamesturk/jellyfish) (name matching) ·
[Claude API](https://docs.anthropic.com) (extraction) · YouTube Data API +
youtube-transcript-api · [ruff](https://github.com/astral-sh/ruff) ·
[ty](https://github.com/astral-sh/ty)

## A note on how this was built

This repository is developed AI-first, and its agent tooling is checked in as
part of the design: [CLAUDE.md](CLAUDE.md) holds the working conventions and
model-routing rules, and `.claude/` contains the subagent definitions and the
FPL domain skill. [PLAN.md](PLAN.md) is the living build plan and
[docs/](docs/) holds the design reviews. If you're interested in what it looks
like to run a real project this way, those files are the interesting part.

## Disclaimer

An assistant, not an oracle, despite the name. It makes recommendations from
other people's public opinions; a human approves every transfer. No affiliation
with the Premier League, Fantasy Premier League, or any of the creators whose
public content it reads.

## License

[MIT](LICENSE)
