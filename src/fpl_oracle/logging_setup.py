"""One safe way to turn on INFO logging.

Every module that wants its own `logger.info()` progress visible has to
raise the root logger to INFO — and every one that has done so has leaked
the YouTube Data API key, because httpx logs the full request URL at INFO
and that API takes its key as a URL QUERY PARAMETER. It has now happened
three times (`deadline.py`, `ingest/warm_cache.py`, `extract/run_extract.py`),
which is two times too many for a "remember to also silence httpx"
convention. CLAUDE.md's rule is absolute: secrets are never printed.

So: no module calls `logging.basicConfig` directly. They call this.
"""

from __future__ import annotations

import logging

# Transports that log full request URLs (and therefore query-string
# secrets) at INFO. `anthropic` is included because it wraps httpx and
# can surface the same request lines.
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore", "anthropic", "urllib3")


def configure_logging(level: int = logging.INFO) -> None:
    """Raise the root logger to `level` for our own progress output, while
    keeping URL-logging transports at WARNING so no query-string secret is
    ever printed."""
    logging.basicConfig(level=level, format="%(message)s")
    for library in _URL_LOGGING_LIBRARIES:
        logging.getLogger(library).setLevel(logging.WARNING)
