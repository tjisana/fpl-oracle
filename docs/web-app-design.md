# Web app — design decisions

Decided 2026-09-04 in a question-by-question interview after the GW3
deadline. Each line is a decision, its alternative, and why. Change a line
here before changing the code it describes.

## Purpose

Two audiences, in this order: the owner's weekly FPL decision, and a
portfolio showpiece for recruiters and hiring managers. Target skills the
build must exercise: TypeScript, React, API design, AWS, programmatic AI.

## Decisions

| # | Decision | Alternative rejected | Why |
| --- | --- | --- | --- |
| 1 | Private, owner-only first; public showcase later | Public multi-user now | Zero cost, no ToS/rate-limit questions; my-squad code already takes an entry id, so multi-user is a later switch |
| 2 | Four tabs, in order: **My squad**, Report, Creator ownership, Evidence trail | Report as home | My squad is what the owner opens on a Thursday night |
| 3 | Owner writes the front end; Claude owns the API and deployment | Claude generates everything | TS/React fluency has to be earned to survive an interview; API/deploy are learned by reading diffs and KT sessions |
| 4 | Hosting under **$10/month** | — | Rules out always-on servers; steers to scheduled jobs, object storage, serverless API |
| 5 | A working URL within **one month** (early October 2026) | 6–8 weeks | Forces the API contract early |
| 6 | **Local first**, AWS after the tabs work | AWS from day one | Owner's call; keep the AWS path designed in so it does not rot |
| 7 | **Vite + React + TypeScript** SPA | Next.js | Smaller surface for a one-month, local-first build; Next.js as a later project that needs its server side |
| 8 | **FastAPI** service; OpenAPI schema → generated TS types | Static JSON files | The "API design" line on the skills list; moves to AWS unchanged |
| 9 | **Live gameweek view in release one** (amended 2026-09-04): per-player points so far, match state (finished / in play / kickoff time + opponent), gameweek total with captain multiplier, live average; provisional bonus and predicted autosubs LABELLED as such. **No live rank** — FPL publishes rank only after each day's matches; livefpl's live rank is an estimate from a sample. We show rank as of FPL's last update, plus points vs live average | Live in release two; estimated live rank | Owner wants to be greeted by the live page; the plumbing is three public endpoints (`event/{gw}/live/`, `fixtures/`, picks) and needs no scraping |
| 10 | Database from the start: **SQLite now**, behind an ORM with migrations | Files only | Owner wants history and grading of decisions; schema/migrations are the credited skill |
| 11 | **Postgres via Docker Compose** is the target; swap is a connection string | Postgres from day one | Owner's call; SQLite is right for single-user local, wrong for serverless |
| 12 | **Monorepo**: `web/` next to the Python package; separate tooling roots | Two repos | One-system story; generated types land next to their consumer |
| 13 | Repo goes **public when the first URL exists**, after a clean-up pass (creator entry ids in particular) | Public now | No audience for the first front-end commits; one deliberate sensitivity pass |
| 14 | Site is **read-only** first; designed toward full pipeline control (incl. paid extraction) later | Control buttons now | Can't run up an Anthropic bill by accident; background jobs are their own design topic |
| 15 | Creator ownership tab: ownership/captaincy table **plus a per-creator grid** | Table only; run-over-run change | The grid is what livefpl cannot show; change-over-time needs history → release two, but the schema keeps run-over-run votes |
| 16 | Evidence trail: extend pick extraction to **verbatim quote + derived timestamp**, deep-linking to the video; tab renders paraphrase-only for older gameweeks | Paraphrase only | The differentiator; reuses `roster/claim_verify.py`'s mechanical quote check; first real gameweek = GW4 |
| 17 | **Tailwind + shadcn/ui** | MUI/Mantine; plain CSS | Most common in current postings; copied-in components the owner can explain line by line |
| 18 | Draft-room app stays a standalone tool, one README paragraph | Rebuild as a tab | Works, used in a real draft, smaller audience |
| 19 | **Never write to FPL.** The site produces a change checklist and opens the FPL transfers page in a new tab; the owner makes the clicks there | Automating transfers/captain/chips through the owner's FPL session | No official write API; against FPL's terms with the account as the penalty; their login has moved to a bot-protected identity service and breaks automation without warning; a site holding an FPL login is a different security problem. Treated as a permanent no, not a "later" |
| 20 | The **My squad tab switches state by the calendar**: live view while the gameweek's matches run, planning view (availability, backing, change checklist) between the last match and the next deadline | Separate Live and My squad tabs | It is what the owner wants on each day; keeps four tabs; teaches rendering different views from the same data |

## Facts that shaped these

- Only existing web code is the draft-room app (`draft/serve.py`, stdlib
  server, one 565-line HTML file, localhost only). Nothing serves the main
  pipeline.
- FPL live endpoints (`event/{gw}/live/`, `fixtures/`, bonus/bps) are
  documented as one-liners and implemented nowhere.
- Pick extraction stores a one-sentence paraphrase per pick, no quote, no
  timestamp. The rank-claim extractor already does quote + timestamp.
- Gemini-read transcripts carry no usable timestamps (`:untimed`).
- livefpl.net's feature set: Live Rank, Live Leagues, Live Bonus, What-if
  rank, Auto-subs, Price Change predictor/actuals, Effective Ownership,
  Chip usage & templates, Top-10k captains, Transfer Planner, Season Ticker.
  The three that fit our creator angle: creator EO, price changes (we
  snapshot daily), live points for the creators' real teams (release two).

## Work split for the month

Claude (engine, API, data):
0. Live module in `fpl/`: live points, fixtures/match state, provisional bonus from bps, predicted autosubs; exposed by the API.
1. Database: schema for runs, picks, creator votes, chip plans, decisions;
   SQLite via SQLAlchemy + Alembic; pipeline writes each run; backfill the
   run folders already on disk.
2. FastAPI read-only service over the database + latest FPL availability
   and prices; OpenAPI schema; a script that regenerates `web/src/api/`
   types.
3. Pick extraction: verbatim `quote` + derived `timestamp_s` per pick
   (mechanical substring check, model timestamp only as fallback), live
   for GW4.
4. Dev loop: `docker compose` file (Postgres for later), one command to run
   API + web locally, sample fixture data for front-end work without a run.

Owner (front end, `web/`):
1. Vite + React + TS scaffold, Tailwind, shadcn/ui, tabs shell.
2. My squad tab against the generated types.
3. Report tab (transfer, wildcard gap, chip consensus, solver squad).
4. Creator ownership grid.
5. Evidence trail (quote + deep link, paraphrase fallback).

Deployment (after the tabs work): S3 + CloudFront for `web/`, Lambda or a
small container for the API, scheduled pipeline, managed Postgres free tier.
Infrastructure as code in TypeScript (CDK) to hit two target skills at once.

## Explicitly not building

A predicted-points table (every site has one; ours would be worse) and
auto-apply/autopilot (FPL has no write API).
