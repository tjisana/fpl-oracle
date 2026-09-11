# Daily jobs

Two pieces of infrastructure that only have value if they run every day,
unattended: the price snapshot and the transcript cache warm. Both exist
as code already; this doc is how to actually schedule them.

- `scripts/run_daily_snapshot.sh` — wraps `fpl_oracle.fpl.snapshot`
  (`write_snapshot()`), which writes `data/snapshots/{YYYY-MM-DD}.json`.
  Idempotent per UTC date: a second run the same day (a launchd misfire,
  a manual rerun) leaves the existing file untouched.
- `scripts/run_daily_warm_cache.sh` — wraps `fpl_oracle.ingest.warm_cache`
  with a conservative `--delay 6`. Safe to run more than once a day:
  already-cached transcripts are skipped, so a rerun mostly just confirms
  what's on disk. On a YouTube IP block it stops itself (no retry loop)
  and exits non-zero.

Both scripts `cd` to `/Users/tjisanakerr/Dev/fpl-oracle` internally, put
`/opt/homebrew/bin` on `PATH` (launchd doesn't source shell rc files, so
`uv` isn't found otherwise), and append a timestamped log to
`data/logs/` on every run (`data/` is already gitignored — nothing here
needs a `.gitignore` change).

## Chosen schedule

The machine's local timezone is US Eastern (`America/New_York`), which
observes daylight saving — `launchd`'s `StartCalendarInterval` fires on
**local wall-clock time**, so it needs no DST-awareness of its own; a
fixed local hour just needs to land safely after 01:30 UTC year-round.

FPL's nightly price change lands around **01:30 UTC**, which is **21:30
EDT** (UTC-4, summer) or **20:30 EST** (UTC-5, winter) the *previous*
local evening. Both chosen times below fall in the early-morning hours —
comfortably after that window in both offsets, with hours of margin:

| Job | Local time (America/New_York) | Why |
|---|---|---|
| `run_daily_warm_cache.sh` | **1:00 AM** | Not price-dependent. Runs first so a slow/blocked harvest doesn't delay the snapshot. Free, all-night budget — no reason to rush it. |
| `run_daily_snapshot.sh` | **3:00 AM** | 3:00 AM EDT = 07:00 UTC; 3:00 AM EST = 08:00 UTC. Both are well after the ~01:30 UTC price change, and after the warm-cache job has had time to finish. |

If the machine's timezone is ever changed, or a manual run is done far
from either window, re-derive against 01:30 UTC — do not assume these
hour values port to a different timezone.

## Install

1. Save each plist below under `~/Library/LaunchAgents/`, exactly as
   named (the filename must match the `Label` value).

`~/Library/LaunchAgents/com.fpl-oracle.daily-snapshot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.fpl-oracle.daily-snapshot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/tjisanakerr/Dev/fpl-oracle/scripts/run_daily_snapshot.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/tjisanakerr/Dev/fpl-oracle/data/logs/launchd-snapshot.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/tjisanakerr/Dev/fpl-oracle/data/logs/launchd-snapshot.err.log</string>
</dict>
</plist>
```

`~/Library/LaunchAgents/com.fpl-oracle.daily-warm-cache.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.fpl-oracle.daily-warm-cache</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/tjisanakerr/Dev/fpl-oracle/scripts/run_daily_warm_cache.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>1</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/tjisanakerr/Dev/fpl-oracle/data/logs/launchd-warm-cache.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/tjisanakerr/Dev/fpl-oracle/data/logs/launchd-warm-cache.err.log</string>
</dict>
</plist>
```

(`StandardOutPath`/`StandardErrorPath` are a safety net for the sliver of
failure a script can have before it opens its own timestamped log — e.g.
the `cd` to the repo failing. Routine output lives in the timestamped
`data/logs/snapshot_*.log` / `warm_cache_*.log` files instead.)

2. Load both jobs:

```sh
launchctl load ~/Library/LaunchAgents/com.fpl-oracle.daily-snapshot.plist
launchctl load ~/Library/LaunchAgents/com.fpl-oracle.daily-warm-cache.plist
```

3. Verify they're registered:

```sh
launchctl list | grep fpl-oracle
```

## Uninstall / disable

```sh
launchctl unload ~/Library/LaunchAgents/com.fpl-oracle.daily-snapshot.plist
launchctl unload ~/Library/LaunchAgents/com.fpl-oracle.daily-warm-cache.plist
```

## Manual run / smoke test

Both scripts can be run directly at any time — they're idempotent-safe
and won't hammer YouTube even if run back-to-back:

```sh
./scripts/run_daily_snapshot.sh; echo "exit: $?"
./scripts/run_daily_warm_cache.sh; echo "exit: $?"
```

Exit code `0` is success. `run_daily_warm_cache.sh` exits `2` when the
run stops on a block or the consecutive-failure circuit breaker — that's
expected/handled behavior, not a bug, and the script deliberately does
not retry. Any other non-zero exit is a real failure worth investigating
in that run's `data/logs/*.log` file.

## Known gotcha: an active YouTube IP block

As of the day this was written, this machine's IP is blocked by YouTube
after an earlier unthrottled run. Until the block lifts,
`run_daily_warm_cache.sh` will keep exiting non-zero (currently `2`),
having fetched nothing beyond whatever's already cached — this is
correct behavior, not a broken schedule. `scripts/retry_blocked_transcripts.sh`
is the separate, patient tool for confirming a block has lifted and
backfilling what was missed; it is not meant to be scheduled.

## Cloud snapshot (AWS, added 2026-09-10)

The launchd jobs above only fire while the laptop is awake, and the owner
closes it nightly. The price snapshot is the one job that cannot miss a
night (FPL exposes no day-level price history), so it also runs in AWS:

- `infra/` — CDK app (TypeScript). One stack, `FplOracle`: a private,
  versioned S3 bucket (`RETAIN`, never deleted by the stack); a Python 3.12
  Lambda (`infra/lambda/snapshot/handler.py`, stdlib + boto3, no packaging);
  an EventBridge cron at **07:00 UTC** daily with two retries.
- The Lambda writes `s3://<bucket>/snapshots/{YYYY-MM-DD}.json` in exactly
  the shape of `data/snapshots/*.json`. `tests/test_snapshot_lambda.py`
  parses its output with the real `PriceSnapshot` model so the two writers
  cannot drift.
- Idempotent per UTC date within each store, same rule as the local job.
  The two jobs write to DIFFERENT stores (the file tree vs the bucket), so
  on a night the laptop is open both write, holding the same prices with
  different `taken_at` stamps. `scripts/sync_snapshots.sh` reconciles by
  copying only dates missing locally: the local file wins when both exist,
  and nothing is ever overwritten or deleted. Run it before any pipeline
  work that reads snapshots.
- A function error trips a CloudWatch alarm on an SNS topic. Deploy with
  `FPL_ORACLE_ALERT_EMAIL=<address>` to subscribe an email (AWS sends a
  confirmation link that must be clicked once). The EventBridge target's two
  retries cover a failed *invocation*; the alarm covers a run that raised.

Commands (from `infra/`, after `aws login`):

```sh
npm test                   # stack assertions (schedule, bucket policy)
npx cdk diff               # what a deploy would change
npx cdk deploy             # create/update the stack
aws lambda invoke --function-name <SnapshotFunctionName output> /dev/stdout
```

The warm-cache job is deliberately NOT in the cloud: cloud IPs are what
YouTube blocks (see `docs/transcript-blocking.md`). Its laptop-free path is
Gemini ingestion, the open decision in ROADMAP.md.
