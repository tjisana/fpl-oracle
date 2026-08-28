#!/usr/bin/env bash
# Poll for the YouTube IP block to lift, then warm the three creators we
# lost to it. Deliberately patient: the block was CAUSED by hammering
# (34 transcripts in 59s), and probing an ACTIVE block on a flat interval
# is the same mistake in miniature — a fixed-rate knock is exactly the
# pattern that extends a block. So this backs off exponentially (20m, 40m,
# 80m, then capped at 120m) between single cheap probe requests, and only
# runs the throttled warmer once the block has actually lifted.
set -u
cd /Users/tjisanakerr/Dev/fpl-oracle
MISSING="fpl-blackbox fpltips fpl-dylan"
INITIAL_SLEEP_MIN=20
MAX_SLEEP_MIN=120
MAX_ATTEMPTS=7

sleep_min=$INITIAL_SLEEP_MIN
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  if uv run python -c "
import logging, sys; logging.disable(logging.CRITICAL)
from fpl_oracle.ingest.transcripts import fetch_transcript
from pathlib import Path
try:
    fetch_transcript('Gii8GGjZWFc', dir=Path('/tmp/fpl-probe'))
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] block lifted after ${attempt} probe(s) — warming ${MISSING}"
    uv run python -m fpl_oracle.ingest.warm_cache --since 2026-08-22 --delay 4 \
      --creator-ids ${MISSING} 2>&1 | tail -20
    echo "[$(date '+%H:%M:%S')] retry run finished"
    exit 0
  fi
  echo "[$(date '+%H:%M:%S')] probe ${attempt}: still blocked, sleeping ${sleep_min}m"
  sleep $((sleep_min * 60))
  sleep_min=$((sleep_min * 2))
  if [ "$sleep_min" -gt "$MAX_SLEEP_MIN" ]; then
    sleep_min=$MAX_SLEEP_MIN
  fi
done
echo "[$(date '+%H:%M:%S')] gave up after ${MAX_ATTEMPTS} probes (~10h20m of exponential backoff, 20m/40m/80m/120m-cap) — still blocked"
exit 1
