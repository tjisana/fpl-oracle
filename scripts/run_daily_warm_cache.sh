#!/usr/bin/env bash
# Daily transcript cache warm job — meant to be fired by launchd, but safe
# to run by hand too. See docs/daily-jobs.md for install instructions.
#
# run_warm_cache() (src/fpl_oracle/ingest/warm_cache.py) is safe to run
# more than once a day: `load_transcript` is checked before every fetch,
# so an already-cached video is skipped and only genuine cache misses hit
# the network. A launchd misfire or a manual rerun the same day mostly
# just re-confirms what's already on disk.
#
# --delay is DELIBERATELY conservative. On 2026-08-27 an unthrottled run
# pulled 34 transcripts in 59 seconds and got this IP blocked by YouTube;
# that block was still in force as of the day this job was written. This
# job has all night to run, so there is no reason to hurry — 6s (well
# above the documented 4s floor) plus warm_cache's own jitter.
#
# On a block, warm_cache's circuit breaker stops the run and it exits 2
# (see WarmCacheRunSummary.blocked / warm_cache.main). This script does
# NOT retry — it just exits non-zero so a wrapper/monitor can alert, and
# tomorrow's scheduled run is the next attempt. Do not add a retry loop
# here: hammering a live block is exactly what caused it. For a patient,
# manual recovery once a block is known to have lifted, use
# scripts/retry_blocked_transcripts.sh instead.
set -u

WARM_CACHE_DELAY_SECONDS=6

# launchd runs jobs with a minimal environment — no shell rc files are
# sourced, so `uv` is very likely not on PATH without this.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-}"

REPO_DIR="/Users/tjisanakerr/Dev/fpl-oracle"
cd "$REPO_DIR" || exit 1

LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/warm_cache_$(date -u '+%Y%m%d_%H%M%S').log"

{
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] warm_cache job starting (delay=${WARM_CACHE_DELAY_SECONDS}s)"
    uv run python -m fpl_oracle.ingest.warm_cache --delay "$WARM_CACHE_DELAY_SECONDS"
    status=$?
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] warm_cache job finished (exit ${status})"
    exit "$status"
} >>"$LOG_FILE" 2>&1
