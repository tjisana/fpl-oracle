# fpl-oracle

AI system that wins FPL leagues by aggregating YouTube creator expertise.
Pipeline: creator roster → transcript ingestion → LLM pick extraction →
weighted consensus → PuLP squad solver → LLM nuance pass → gameweek report.

## Commands

- Install/sync deps: `uv sync`
- Run anything: `uv run python -m fpl_oracle.<module>`
- Tests: `uv run pytest` (fast suite; `-m "not network"` skips live-API tests)
- Lint/format: `uv run ruff check --fix . && uv run ruff format .`
- Type check: `uv run ty check` (ty is beta; if it fights a Pydantic construct,
  flag it rather than contorting the code)
- Add a dependency: `uv add <pkg>` (never edit pyproject deps by hand)

## Architecture

- `src/fpl_oracle/roster/` — creator registry, FPL team IDs, rank-based weights
- `src/fpl_oracle/ingest/` — YouTube channel monitoring, transcript fetching
- `src/fpl_oracle/extract/` — LLM extraction of picks from transcripts (Pydantic schemas)
- `src/fpl_oracle/fpl/` — FPL API client + player database (the source of truth for names/prices)
- `src/fpl_oracle/consensus/` — weighted vote aggregation into per-player scores
- `src/fpl_oracle/solver/` — PuLP integer program: optimal 15 under FPL constraints
- `src/fpl_oracle/report/` — gameweek recommendation output (markdown)
- `data/` — SQLite db + raw transcripts. Gitignored. Never commit data.
- `PLAN.md` — the living build plan. Read it at session start; update it when scope changes.

## Conventions

- Python 3.12, uv-managed. src layout. Type hints everywhere.
- Pydantic models at every module boundary — no raw dicts crossing package lines.
- Every extracted player name MUST be validated against the FPL player DB
  (rapidfuzz match) before it enters the database. Reject or flag non-matches;
  never store an unresolved name.
- LLM calls live behind thin client wrappers in the module that owns them;
  prompts are module-level constants or `.txt` files next to the code, not inline f-string soup.
- Network calls to FPL/YouTube: always through the clients in `fpl/` and `ingest/`,
  with on-disk caching in `data/cache/`. Tests that hit live APIs are marked `@pytest.mark.network`.
- Secrets in `.env` (ANTHROPIC_API_KEY, GEMINI_API_KEY, YOUTUBE_API_KEY). Never commit. Never print.

## Domain knowledge

Consult the `fpl-domain` skill for FPL API endpoints, squad rules,
extraction schema conventions, and name-matching gotchas before writing
code in `fpl/`, `extract/`, `consensus/`, or `solver/`.

## Working style

- Before treating a task as done, review the diff in a fresh Opus subagent
  (see Model routing) against PLAN.md: every requirement implemented, edge
  cases tested, nothing out of scope changed. Then apply the ship rule.
- Prefer research subagents for exploring APIs/transcripts.
- Commit per logical change with conventional-commit messages (`feat:`, `fix:`, `chore:`).

## Model routing

Three tiers (set by the owner 2026-09-12; main sessions run on Fable 5.1):

- **Fable 5.1 (main session)** — the brain: planning, design decisions,
  prompt design, integration, and the tricky modules (`solver/`,
  `consensus/`, `extract/` are implemented here, not delegated). The
  `architect` subagent is the same model with a clean context, for a
  second opinion on a hard call; brief it self-contained.
- **Sonnet (`implementer` subagent)** — the worker: well-specified
  implementation from a written spec, test fixes, mechanical refactors,
  boilerplate. Only hand it a change in the tricky modules if the exact
  logic is already decided and written out in the task.
- **Opus (reviewer)** — every diff is reviewed in a fresh-context subagent
  run on Opus (`Agent` with `model: "opus"`), against PLAN.md and the
  task: requirements met, edge cases tested, nothing out of scope. It
  reports findings; the main session decides and fixes.

### Ship rule

When the Opus review comes back clean (or its findings are fixed and
re-reviewed) AND the fast test suite, ruff, and ty all pass, commit and
**push immediately** — no confirmation needed. Pushing is part of "done".
Anything outward-facing beyond a push (opening a PR, deploying to AWS,
deleting cloud resources) still gets confirmed first.

### Clarification loop (implementer ↔ main session)

- The implementer reports ambiguities instead of guessing. When it does, the
  main session resolves the ambiguity (consulting the architect if it's
  genuinely hard) and sends the clarified task back to the SAME agent via
  SendMessage, so it keeps its context.
- Hard cap: 3 clarification rounds on one task. If it's still unresolved after
  3, STOP and escalate to the user — repeated failure to converge means the
  task framing or the design itself is wrong, and more rounds won't fix it.
- Exception — skip the loop and go straight to the user when the ambiguity is
  a decision only the user can make: scope changes, personal preferences,
  anything involving money or external accounts. Models looping on those just
  burns rounds guessing at the user's intent.

## Compaction

When compacting this conversation, always preserve: the full list of modified
files, the test commands, current PLAN.md task status, and any FPL API quirks
discovered this session.
