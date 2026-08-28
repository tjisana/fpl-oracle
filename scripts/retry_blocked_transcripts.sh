#!/usr/bin/env bash
# Poll for the YouTube IP block to lift, then warm the three creators we
# lost to it. Deliberately patient: the block was CAUSED by hammering
# (34 transcripts in 59s), so this probes once every 20 minutes with a
# single cheap request and only then runs the throttled warmer.
set -u
cd /Users/tjisanakerr/Dev/fpl-oracle
MISSING="fpl-blackbox fpltips fpl-dylan"
for attempt in $(seq 1 30); do
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
  echo "[$(date '+%H:%M:%S')] probe ${attempt}: still blocked, sleeping 20m"
  sleep 1200
done
echo "[$(date '+%H:%M:%S')] gave up after 30 probes (~10h) — still blocked"
exit 1
