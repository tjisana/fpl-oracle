"""The cloud snapshot writer (infra/lambda/snapshot/handler.py) must stay
byte-compatible with the local one: its output has to parse as
`fpl_oracle.fpl.snapshot.PriceSnapshot`, and it must keep the same
idempotency and refuse-empty rules. Network- and AWS-free: the fetch and
the S3 client are both injected.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_oracle.fpl.snapshot import PriceSnapshot

_HANDLER = Path(__file__).resolve().parents[1] / "infra" / "lambda" / "snapshot" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("snapshot_handler", _HANDLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handler = _load_handler()

_CANNED_BOOTSTRAP = {
    "elements": [
        {"id": 1, "web_name": "Saka", "now_cost": 100},
        {"id": 2, "web_name": "Raya", "now_cost": 55},
    ],
}


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    """Just enough of boto3's S3 client: head_object / put_object over a dict."""

    exceptions = SimpleNamespace(ClientError=_ClientError)

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise _ClientError("404")
        return {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict:
        self.objects[Key] = Body
        return {}


NOW = datetime(2026, 9, 11, 7, 0, tzinfo=UTC)


def test_output_parses_as_the_local_price_snapshot_model() -> None:
    s3 = _FakeS3()
    result = handler.write_snapshot(
        bucket="b", prefix="snapshots/", now=NOW, s3=s3, fetch=lambda: _CANNED_BOOTSTRAP
    )

    assert result == {
        "bucket": "b",
        "key": "snapshots/2026-09-11.json",
        "written": True,
        "players": 2,
    }
    parsed = PriceSnapshot.model_validate_json(s3.objects["snapshots/2026-09-11.json"])
    assert parsed.snapshot_date.isoformat() == "2026-09-11"
    assert parsed.taken_at == NOW
    assert {p.player_id: p.now_cost for p in parsed.prices} == {1: 100, 2: 55}


def test_idempotent_for_the_same_utc_date() -> None:
    s3 = _FakeS3()
    calls: list[int] = []

    def fetch() -> dict:
        calls.append(1)
        return _CANNED_BOOTSTRAP

    handler.write_snapshot(bucket="b", prefix="snapshots/", now=NOW, s3=s3, fetch=fetch)
    first = s3.objects["snapshots/2026-09-11.json"]
    later = NOW.replace(hour=9)
    result = handler.write_snapshot(bucket="b", prefix="snapshots/", now=later, s3=s3, fetch=fetch)

    assert result["written"] is False
    assert s3.objects["snapshots/2026-09-11.json"] == first
    assert len(calls) == 1


def test_refuses_to_write_an_empty_snapshot() -> None:
    s3 = _FakeS3()
    with pytest.raises(ValueError, match="no players"):
        handler.write_snapshot(
            bucket="b", prefix="snapshots/", now=NOW, s3=s3, fetch=lambda: {"elements": []}
        )
    assert s3.objects == {}


def test_local_and_cloud_writers_agree_on_the_json_shape() -> None:
    cloud = handler.build_snapshot(_CANNED_BOOTSTRAP, NOW)
    local = json.loads(
        PriceSnapshot(
            taken_at=NOW,
            snapshot_date=NOW.date(),
            prices=[{"player_id": 1, "now_cost": 100}, {"player_id": 2, "now_cost": 55}],
        ).model_dump_json()
    )
    assert cloud == local


def test_a_non_404_s3_error_propagates_rather_than_reading_as_absent() -> None:
    """A 403 on head_object means a permissions bug, not a missing file;
    treating it as absent would put_object over a real snapshot (or fail
    later with a worse message)."""

    class _ForbiddenS3(_FakeS3):
        def head_object(self, *, Bucket: str, Key: str) -> dict:
            raise _ClientError("403")

    s3 = _ForbiddenS3()
    with pytest.raises(_ClientError, match="403"):
        handler.write_snapshot(
            bucket="b", prefix="snapshots/", now=NOW, s3=s3, fetch=lambda: _CANNED_BOOTSTRAP
        )
    assert s3.objects == {}


def test_a_fetch_that_keeps_failing_writes_nothing() -> None:
    s3 = _FakeS3()

    def fetch() -> dict:
        raise urllib.error.URLError("connection reset")

    with pytest.raises(urllib.error.URLError):
        handler.write_snapshot(bucket="b", prefix="snapshots/", now=NOW, s3=s3, fetch=fetch)
    assert s3.objects == {}


def test_fetch_retries_transient_errors_then_succeeds(monkeypatch) -> None:
    attempts: list[int] = []
    slept: list[float] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self) -> bytes:
            return json.dumps(_CANNED_BOOTSTRAP).encode()

    def fake_urlopen(req, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError("503")
        return _Resp()

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    data = handler.fetch_bootstrap(sleep=slept.append)
    assert data == _CANNED_BOOTSTRAP
    assert len(attempts) == 3
    assert slept == [handler.RETRY_DELAY_SECONDS] * 2


def test_handler_reads_bucket_from_the_environment(monkeypatch) -> None:
    s3 = _FakeS3()
    monkeypatch.setenv("DATA_BUCKET", "env-bucket")
    monkeypatch.delenv("SNAPSHOT_PREFIX", raising=False)
    monkeypatch.setattr(handler, "_s3_client", lambda: s3)
    monkeypatch.setattr(handler, "fetch_bootstrap", lambda: _CANNED_BOOTSTRAP)

    result = handler.handler({}, None)

    assert result["bucket"] == "env-bucket"
    assert result["key"].startswith("snapshots/")
    assert result["written"] is True
