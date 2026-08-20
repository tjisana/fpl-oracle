# fpl-oracle roadmap

**PLAN.md is the build log — what was done and why.** This file is the short
list of what's NEXT. Keep it skimmable: one line per item, detail lives in the
linked code or in PLAN.md.

**End goal:** a daily-running service that harvests FPL YouTube creators,
extracts their picks/transfers/chip plans, and tells me what to do about MY
squad this week — surfaced in a web app, deployed in the cloud.

---

## NOW — GW1 deadline (Fri 2026-08-21, 17:30 UTC)

- [ ] Run the deadline command late Friday afternoon (~16:00 UTC) to catch the
      "final GW1 thoughts" videos. `uv run python -m fpl_oracle.deadline --dry-run`
      first. See `docs/deadline-runbook.md`.
- [ ] A team is already entered from an earlier run — Friday's run is to REVISE it.

## NEXT — in-season (the actual product)

The GW1 system answers a cold-start question: "pick 15 from nothing, £100m."
In-season inverts it: "given the squad I own, is any change worth making, and
is it worth -4?" Different objective, much smaller search space.

- [ ] **My-squad awareness** — pull my actual squad via `entry/{id}/event/{gw}/picks/`,
      plus bank, team value, free transfers. Everything below depends on this.
- [ ] **Transfer recommender** — replace the 15-man ILP with a delta optimizer:
      rank candidate transfers by consensus gain vs. the -4 hit cost.
- [ ] **Extraction scope widening** — creators' videos cover transfers in/out,
      chip strategy, and "how I set up for next week". v1 extraction only handled
      squad reveals; `time_horizon` and chip plans are the gaps.
- [ ] **Price-change awareness** — the FPL API now carries `price_change_projections`
      (with `likelihood`, 3-day offsets), `price_change_hourly_rate`,
      `price_change_locked_until`. Dormant until the season starts. This creates a
      DAILY cadence need: acting before a rise/fall is a real decision.
- [ ] **Creator scoreboard** — 19/20 creators have verified FPL entry ids. Track
      their live ranks + whether their picks paid off. Makes the weights honest.

## THEN — deployment + web app

- [ ] **Durable storage FIRST.** `data/` is gitignored and ephemeral on Lambda/Fargate.
      If the transcript cache doesn't survive between runs, every run re-fetches the
      whole season and gets IP-blocked. This is a precondition, not an optimization.
- [ ] **Split the pipeline at the cache boundary** (recommended, £0): fetch transcripts
      on a local machine (residential IP, never blocked) → sync to S3; run extract →
      solve → report in the cloud. Fallback: Webshare *residential* proxy ~$3.50/mo.
      Verify with `scratchpad/yt_block_probe.py` from an EC2 box before building.
- [ ] **Web app** (single-user, no auth). Four screens worth building:
      1. Action queue — ranked "do this by then", with TWO clocks (nightly price
         change ~01:30 UTC, and the gameweek deadline).
      2. Evidence trail — click a recommendation, get the creator quotes with
         timestamped YouTube deep links. The differentiator; no other tool has it.
      3. Consensus momentum — who's gaining/losing backing over days. This is the
         entire payoff of daily harvesting; a weekly snapshot destroys it.
      4. My squad — availability, price risk, sentiment per player.
- [ ] Explicitly NOT building: a predicted-points table (every site has one, ours
      would be worse), and auto-apply/autopilot (FPL has no write API — it means
      automating against their session, an account-risk question).

## KNOWN ISSUES

- [ ] **Phonetic name mangles still unresolved** — ~71 of 478 picks. `'Virgil van Djk'`,
      `'Dominic Calvalu'`, `'In Burmo'`, `'Victor Yres'`. First-name references were
      fixed (`b12e4a1`); this is the remaining class. Re-run `scripts/name_probe.py`
      after any resolver change — it is the safety net.
- [ ] **Uneven resolution across creators** distorts weighting: FPL Salah loses 41%
      of picks to name resolution, Gianni Buttice 0%.
- [ ] **Re-extraction re-pays for creators already done** — no skip-if-recent check,
      so a stage-2 retry re-bills every creator.
- [ ] **Tier-2 phonetic matching is loose** — 2 wrong matches remain in the probe;
      a first name that sounds like a teammate's surname can still slip through.
- [ ] 4 eligible creators contributed nothing to the GW1 run (FPL Heisenberg,
      FPL Matthew, Planet FPL, Pras). Pras matters most — he's The FPL Wire's
      channel primary, so his absence silently costs Zophar and Lateriser too.
