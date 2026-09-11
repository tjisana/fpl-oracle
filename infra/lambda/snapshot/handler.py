"""Daily FPL price snapshot, cloud edition.

The Lambda twin of `fpl_oracle.fpl.snapshot.write_snapshot`: fetch
`bootstrap-static`, keep every player's `id` + `now_cost`, and write ONE
object per UTC date to `s3://{DATA_BUCKET}/{SNAPSHOT_PREFIX}{YYYY-MM-DD}.json`.

Deliberately dependency-free (stdlib + the boto3 that the Lambda runtime
ships) so the deployment is a folder upload, not a packaging exercise.
It therefore does NOT import `fpl_oracle`; instead the repo's test suite
(`tests/test_snapshot_lambda.py`) parses this handler's output with the
real `PriceSnapshot` model, so the two writers cannot drift apart
silently.

Contract, mirrored from the local module:
- Idempotent per UTC date: if today's object already exists it is left
  untouched (a retry or a second schedule firing never overwrites or
  re-dates a snapshot).
- Refuses to write a snapshot with no players: a 200 with an empty
  `elements` would occupy today's date for good.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
# The FPL API 403s generic clients; same browser-like UA as fpl_oracle.fpl.client.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5.0


def fetch_bootstrap(*, sleep: Any = time.sleep) -> dict[str, Any]:
    """Mirror `fpl_oracle.fpl.client._fetch_bootstrap_with_retries`: the FPL
    API 429s/503s in blips, and a blip must not cost the day's snapshot."""
    req = urllib.request.Request(BOOTSTRAP_URL, headers=HEADERS)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            logger.warning(
                "bootstrap-static fetch failed (attempt %d/%d): %s — retrying in %.0fs",
                attempt,
                MAX_ATTEMPTS,
                exc,
                RETRY_DELAY_SECONDS,
            )
            sleep(RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


def build_snapshot(bootstrap: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The exact JSON shape of `fpl_oracle.fpl.snapshot.PriceSnapshot`."""
    prices = [
        {"player_id": e["id"], "now_cost": e["now_cost"]} for e in bootstrap.get("elements", [])
    ]
    if not prices:
        raise ValueError(
            "bootstrap-static returned no players — refusing to write an empty price "
            "snapshot, which would occupy today's date and block a real one"
        )
    # Pydantic writes a UTC timestamp as `...Z`; match it byte-for-byte.
    return {
        "taken_at": now.isoformat().replace("+00:00", "Z"),
        "snapshot_date": now.date().isoformat(),
        "prices": prices,
    }


def _s3_client() -> Any:
    import boto3  # ty: ignore[unresolved-import]  # Lambda runtime ships it; lazy so tests need no boto3

    return boto3.client("s3")


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def write_snapshot(
    *,
    bucket: str,
    prefix: str,
    now: datetime | None = None,
    s3: Any | None = None,
    fetch: Any = fetch_bootstrap,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    s3 = s3 or _s3_client()
    key = f"{prefix}{now.date().isoformat()}.json"
    if _object_exists(s3, bucket, key):
        logger.info("snapshot: s3://%s/%s already exists — leaving it untouched", bucket, key)
        return {"bucket": bucket, "key": key, "written": False, "players": None}

    snapshot = build_snapshot(fetch(), now)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(snapshot, indent=2).encode(),
        ContentType="application/json",
    )
    logger.info("snapshot: wrote s3://%s/%s (%d players)", bucket, key, len(snapshot["prices"]))
    return {"bucket": bucket, "key": key, "written": True, "players": len(snapshot["prices"])}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return write_snapshot(
        bucket=os.environ["DATA_BUCKET"],
        prefix=os.environ.get("SNAPSHOT_PREFIX", "snapshots/"),
    )
