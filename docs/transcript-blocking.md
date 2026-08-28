# YouTube transcript blocking: what happened, and the proxy question

## Incident

On 2026-08-27, `warm_cache` pulled 34 transcripts in 59 seconds (~1.7
requests/second, sustained, unauthenticated, from one residential IP) while
harvesting for GW2. YouTube returned `IpBlocked`. `warm_cache` stopped the
run on first block rather than continuing to hammer it, but the run had
already cost three creators: `fpl-blackbox`, `fpltips`, `fpl-dylan`.

### Observed timeline

- **Block onset: ~21:51:47 local (EDT).** The last transcript files written
  before the gap cluster at `21:51:3x`–`21:51:47` — 34 files land inside the
  `21:50`–`21:51` minute bucket in `data/transcripts/`, matching "34
  transcripts in 59 seconds" exactly. Nothing was written after 21:51:47.
- **Block confirmed still active: 23:31:56, 23:51:58, 01:18:23** (three
  probes logged in `/tmp/fpl-retry.log` by `scripts/retry_blocked_transcripts.sh`,
  all "still blocked").
- **Still active as of this writing (~01:24, the same night).** That's a
  minimum of **3h 33m** of confirmed downtime, with no recovery yet observed.
  This document does not claim a total duration because the block had not
  lifted at the time it was written — treat any number above as a lower
  bound, not the final figure.

The retry poller itself was not running on a perfectly flat 20-minute
cadence in practice (the gap between the 2nd and 3rd probe was ~86 minutes,
not 20 — likely the machine slept or the process was otherwise delayed), but
its *intent* was flat 20-minute polling, which is the behavior Task 3 in
this change replaces with exponential backoff.

## What actually caused the block

Volume was not the problem in absolute terms — 34 transcripts is a small
harvest. The problem was **rate**: ~1.7 req/s sustained, unauthenticated,
against YouTube's undocumented anti-bot thresholds, with no backoff when
early responses started degrading. Mitigations already landed in
`src/fpl_oracle/ingest/warm_cache.py` address exactly this:

- `--delay` (default `2.0s`, ±40% jitter) applied to every network fetch —
  turns the same 34-transcript harvest into ~60-95s of paced requests
  instead of a 59s burst.
- A consecutive-failure circuit breaker that stops the run when several
  fetches in a row fail in ways that look like an environmental block in
  disguise (`PoTokenRequired`, `YouTubeDataUnparsable` — both
  `CouldNotRetrieveTranscript` subclasses that don't get YouTube's honest
  `RequestBlocked`/`IpBlocked` treatment, but mean the same thing).

## What `youtube-transcript-api` actually supports for proxies

Checked against the **installed version, 1.2.4** (`youtube_transcript_api`
in `.venv`), not assumed from memory. The library has first-class proxy
support — no need to hand-roll a `requests` session:

- `YouTubeTranscriptApi(proxy_config=...)` — the constructor takes an
  optional `ProxyConfig`. When `None` (the default), behavior is exactly
  what it's always been: no proxy, a plain `requests.Session`.
  (`youtube_transcript_api/_api.py`)
- `youtube_transcript_api.proxies` defines two concrete `ProxyConfig`
  implementations:
  - **`GenericProxyConfig(http_url=..., https_url=...)`** — any HTTP/HTTPS/
    SOCKS proxy the `requests` library can address. At least one URL is
    required; the other defaults to it.
  - **`WebshareProxyConfig(proxy_username=..., proxy_password=..., filter_ip_locations=..., retries_when_blocked=10, ...)`**
    — first-class support for Webshare's rotating residential proxy product
    specifically. It defaults `retries_when_blocked` to 10 (a blocked
    request triggers a retry, which — because Webshare rotates IPs per
    request — plausibly lands on a non-blocked IP next time) and sets
    `Connection: close` so a rotating pool isn't defeated by keep-alive.
- Both are plain constructor calls, no extra dependency beyond
  `youtube-transcript-api` itself (`requests` and `urllib3` are already
  transitive deps of it).

This is documented in the package's own README under "Working around IP
bans" and mirrored in the `ProxyConfig`/`GenericProxyConfig`/
`WebshareProxyConfig` docstrings in `proxies.py`, which is where the above
was actually verified.

## Is a proxy warranted at our volume?

**Recommendation: no — don't buy a proxy yet.** Ship the pacing fix
(already done in `warm_cache`) and the plumbing in this change (proxy
support that's OFF by default), and only revisit if blocks recur *despite*
polite pacing.

Reasoning:

- **The failure mode was rate, not volume.** ~19 creators × up to 5 videos
  is a ceiling of ~95 videos, but that's not the daily marginal cost — most
  of that is already on disk in `data/transcripts/` (a permanent cache; see
  the module docstring in `transcripts.py`). A daily/twice-daily harvest is
  fetching *new* videos only, realistically a handful per creator per day.
  At `--delay 2.0s` with jitter, even a worst-case burst of ~20 new videos
  takes ~40s spread out, nothing like the 1.7 req/s that got us blocked.
- **A proxy doesn't fix a rate problem — it just changes whose IP eats the
  block.** If the harvester still bursts requests, a rotating residential
  proxy with `retries_when_blocked` will paper over individual blocked
  requests, but that's treating a symptom your own client controls
  (request rate) with an ongoing recurring cost, instead of just not
  bursting.
- **A proxy earns its cost when the constraint changes, not before.**
  The two situations where it would become warranted:
  1. The deployment moves off a residential IP (cloud egress IPs are
     shared and pre-flagged by anti-bot systems far more aggressively —
     see the correction below).
  2. Polite pacing is empirically insufficient — i.e., blocks recur even
     at `--delay 2.0s`+jitter with the circuit breaker in place. That
     hasn't been tested yet, because we're still inside the *current*
     block, so there's no fresh data point on whether the new pacing alone
     would have prevented it.
- **Cost is trivial either way (~$3.50/mo for Webshare residential)**, so
  this isn't a budget call — it's "don't add a moving part / a credential
  to manage / a new failure surface until the simpler fix is shown
  insufficient."

If a proxy does become warranted, `WebshareProxyConfig` is the concrete
recommendation over a generic proxy: it's what `youtube-transcript-api`
itself is built and tuned for (rotation + `retries_when_blocked`), and it's
exactly what ROADMAP.md already priced at ~$3.50/mo.

## Correcting the falsified ROADMAP claim

ROADMAP.md's deployment section previously asserted, as the reasoning for
"fetch transcripts on a local machine":

> residential IP, never blocked

**This is false, and was falsified on this very machine on 2026-08-27.**
This machine *is* a residential IP, and it *was* blocked — for hours, and
counting as of this writing. The claim wasn't reasoning about proxy-vs-no-
proxy correctly: a residential IP is *much less likely* to be blocked than
a shared cloud/datacenter IP (that part still holds — it's why the "local
machine" plan is still reasonable), but "never" was always an overclaim,
and burst rate defeats it regardless of IP class. ROADMAP.md has been
updated to point here instead of repeating "never blocked" as fact — the
original reasoning is preserved there, marked as tested and how it failed,
not deleted.

The corrected framing: a residential IP lowers the *baseline* likelihood of
being blocked (still true, still worth keeping "local machine" as the
recommended split), but does not make blocking impossible, and rate is a
bigger factor than IP class within the range this project actually
operates at. The fix for *this* failure was pacing, not IP sourcing.

## Configuring a proxy (when/if it's warranted)

Off by default — unset any of the below and `fetch_transcript` behaves
exactly as it did before this change: no proxy, no new failure mode.

Set in `.env` (never commit; never printed or logged by
`fpl_oracle.ingest.transcripts` — only the resulting proxy object is used,
never the raw credential strings):

```
# Webshare residential proxy (checked first if both username and password are set)
YT_PROXY_WEBSHARE_USERNAME=...
YT_PROXY_WEBSHARE_PASSWORD=...
YT_PROXY_WEBSHARE_LOCATIONS=GB,DE        # optional, comma-separated location codes

# Generic HTTP/HTTPS/SOCKS proxy (used only if no Webshare credentials are set)
YT_PROXY_HTTP_URL=http://user:pass@proxyhost:port
YT_PROXY_HTTPS_URL=https://user:pass@proxyhost:port
```

See `_proxy_config_from_env` in `src/fpl_oracle/ingest/transcripts.py` for
the exact precedence and behavior.
