#!/usr/bin/env bash
# Daily FPL price snapshot job — meant to be fired by launchd, but safe to
# run by hand too. See docs/daily-jobs.md for install instructions.
#
# write_snapshot() (src/fpl_oracle/fpl/snapshot.py) is idempotent per UTC
# calendar date: if data/snapshots/{today}.json already exists it is left
# untouched and the existing path is returned. That means this script is
# safe to run more than once in a day — a launchd misfire or a manual
# rerun just no-ops the second time, it never overwrites or re-dates a
# snapshot already on disk.
#
# Run this AFTER ~01:30 UTC — FPL prices change overnight around then, and
# a snapshot taken before the change would freeze in yesterday's prices
# under today's date for good (idempotency means nothing later in the day
# can fix that). See docs/daily-jobs.md for the exact local schedule time.
set -u

# launchd runs jobs with a minimal environment — no shell rc files are
# sourced, so `uv` is very likely not on PATH without this.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-}"

REPO_DIR="/Users/tjisanakerr/Dev/fpl-oracle"
cd "$REPO_DIR" || exit 1

LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/snapshot_$(date -u '+%Y%m%d_%H%M%S').log"

{
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] snapshot job starting"
    uv run python -m fpl_oracle.fpl.snapshot
    status=$?
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] snapshot job finished (exit ${status})"
    exit "$status"
} >>"$LOG_FILE" 2>&1
