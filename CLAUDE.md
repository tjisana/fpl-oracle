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

- Before treating a task as done, review the diff in a fresh subagent context
  against PLAN.md: every requirement implemented, edge cases tested,
  nothing out of scope changed.
- Prefer research subagents for exploring APIs/transcripts.
- Commit per logical change with conventional-commit messages (`feat:`, `fix:`, `chore:`).

## Model routing

Three tiers (main sessions are expected to run on Opus):

- **Opus (main session)** — owns planning, day-to-day design decisions, prompt
  design, code review, and integration. The default brain.
- **Sonnet (`implementer` subagent)** — well-specified implementation tasks:
  new module from a detailed spec, test fixes, mechanical refactors,
  boilerplate. EXCEPTION — tricky modules stay on the main model: `solver/`,
  `consensus/`, and `extract/` are implemented in the main session, not
  delegated. Only hand the implementer a change there if the exact logic is
  already decided and written out in the task.
- **Fable (`architect` subagent)** — escalation for the hardest decisions only:
  cross-module design questions, solver formulation trade-offs, scoring
  philosophy, calls where the main session is genuinely torn between defensible
  options. The architect has no conversation context, so the brief must be
  self-contained (the question, the options considered, constraints, relevant
  file paths). It advises; the main session decides and implements. Don't
  route routine questions here.

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
