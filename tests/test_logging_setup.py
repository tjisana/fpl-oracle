"""The secret-leak guard. This invariant has been broken three separate
times (deadline.py, ingest/warm_cache.py, extract/run_extract.py), each
time printing the YouTube Data API key — which travels as a URL query
parameter — into the terminal via httpx's INFO-level request logging.
These tests exist so a fourth time fails CI instead of a deadline run."""

from __future__ import annotations

import logging
from pathlib import Path

from fpl_oracle.logging_setup import _URL_LOGGING_LIBRARIES, configure_logging

SRC = Path(__file__).parent.parent / "src" / "fpl_oracle"


def test_configure_logging_requests_info_level(monkeypatch):
    # Asserting on the root logger's level directly is unreliable under
    # pytest, which installs its own handlers (basicConfig is a no-op once
    # handlers exist). Check what we ASK for instead.
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    configure_logging()
    assert seen["level"] == logging.INFO


def test_url_logging_transports_are_silenced_below_info():
    configure_logging()
    for library in _URL_LOGGING_LIBRARIES:
        assert logging.getLogger(library).level >= logging.WARNING, (
            f"{library} logs full request URLs at INFO — a query-string API key would leak"
        )


def test_httpx_is_covered():
    # The specific transport that leaked, named explicitly so a refactor
    # of the tuple can't quietly drop it.
    assert "httpx" in _URL_LOGGING_LIBRARIES


def test_no_module_calls_basicConfig_directly():
    """`configure_logging` is the only sanctioned way to turn logging on.
    A bare `logging.basicConfig` re-opens the leak, so it must not appear
    anywhere outside the helper itself."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "logging_setup.py" and "logging.basicConfig" in path.read_text()
    ]
    assert offenders == [], (
        "these modules call logging.basicConfig directly instead of "
        f"logging_setup.configure_logging(): {offenders}"
    )
