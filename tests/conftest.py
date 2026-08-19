"""Shared pytest fixtures.

The fast suite must pass on a clean checkout with no credentials: CI has no
`.env`, and so does anyone who has just cloned the repo. Without this, a test
that monkeypatches the HTTP layer but still reaches `youtube_client._api_key()`
while building request params passes locally (a developer `.env` is present)
and fails in CI — which is exactly what happened to
`TestListUploadsStopBeforeCutoff`.

Placeholder keys also stop a real `.env` from silently authenticating a test
that was never meant to talk to a live API: with a fake key such a request
fails loudly instead of quietly succeeding.
"""

from __future__ import annotations

import pytest

# Deliberately not key-shaped — nothing here should ever be mistaken for,
# or accepted as, a real credential.
_PLACEHOLDER_KEYS = {
    "YOUTUBE_API_KEY": "placeholder-not-a-real-key",
    "ANTHROPIC_API_KEY": "placeholder-not-a-real-key",
    "GEMINI_API_KEY": "placeholder-not-a-real-key",
}


@pytest.fixture(autouse=True)
def _placeholder_api_keys(request: pytest.FixtureRequest, monkeypatch) -> None:
    """Give every non-network test placeholder credentials.

    Skipped for `@pytest.mark.network` tests, which hit live APIs and need the
    real keys from the environment or `.env`.
    """
    if request.node.get_closest_marker("network"):
        return
    for name, value in _PLACEHOLDER_KEYS.items():
        monkeypatch.setenv(name, value)
