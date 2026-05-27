from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from typing import Any

import journeysdk as journey_sdk
import pytest

from journeysdk.errors import InvalidBranchUsageError
from journeysdk.touchpoints import http as journey_http


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = BytesIO(body)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


def _urlopen_sequence(responses: list[tuple[int, bytes]]):
    calls: list[tuple[str, str, bytes | None, dict[str, str], float | None]] = []

    def fake_urlopen(request: Any, *, timeout: float | None = None):
        url = request.full_url
        method = request.get_method()
        data = request.data
        headers = dict(request.header_items())
        calls.append((url, method, data, headers, timeout))
        status, body = responses.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(
                url,
                status,
                "error",
                hdrs={},
                fp=BytesIO(body),
            )
        return _FakeResponse(status=status, body=body)

    fake_urlopen.calls = calls  # type: ignore[attr-defined]
    return fake_urlopen


def test_http_helpers_run_inside_steps(monkeypatch: pytest.MonkeyPatch):
    fake_urlopen = _urlopen_sequence([(200, b'{"ok": true}')])
    monkeypatch.setattr(journey_http.urllib.request, "urlopen", fake_urlopen)

    def fetch_status():
        return journey_http.get_json("http://example.test/status")

    def journey():
        journey_sdk.step(fetch_status)

    report = journey_sdk.execute(journey)

    assert report.case_reports[0].records[-1].result == {"ok": True}
    assert fake_urlopen.calls == [  # type: ignore[attr-defined]
        ("http://example.test/status", "GET", None, {}, 10.0)
    ]


def test_post_json_sends_json_and_decodes_response(monkeypatch: pytest.MonkeyPatch):
    fake_urlopen = _urlopen_sequence([(200, b'{"created": "watch_123"}')])
    monkeypatch.setattr(journey_http.urllib.request, "urlopen", fake_urlopen)

    def create_watch():
        return journey_http.post_json(
            "http://example.test/watches",
            {"url": "http://fixture.test/page"},
        )

    def journey():
        journey_sdk.step(create_watch)

    report = journey_sdk.execute(journey)

    assert report.case_reports[0].records[-1].result == {"created": "watch_123"}
    [(url, method, data, headers, timeout)] = fake_urlopen.calls  # type: ignore[attr-defined]
    assert url == "http://example.test/watches"
    assert method == "POST"
    assert json.loads(data.decode("utf-8")) == {"url": "http://fixture.test/page"}
    assert headers["Content-type"] == "application/json"
    assert timeout == 10.0


def test_wait_for_http_polls_until_expected_status(monkeypatch: pytest.MonkeyPatch):
    fake_urlopen = _urlopen_sequence([(503, b"not ready"), (200, b"ready")])
    monkeypatch.setattr(journey_http.urllib.request, "urlopen", fake_urlopen)

    def wait_for_app():
        response = journey_http.wait_for_http(
            "http://example.test/healthz",
            timeout=1,
            poll_interval=0.01,
        )
        return response.text()

    def journey():
        journey_sdk.step(wait_for_app)

    report = journey_sdk.execute(journey)

    assert report.case_reports[0].records[-1].result == "ready"
    assert len(fake_urlopen.calls) == 2  # type: ignore[attr-defined]


def test_wait_for_http_times_out(monkeypatch: pytest.MonkeyPatch):
    fake_urlopen = _urlopen_sequence([(503, b"not ready")])
    monkeypatch.setattr(journey_http.urllib.request, "urlopen", fake_urlopen)

    def wait_for_app():
        return journey_http.wait_for_http(
            "http://example.test/healthz",
            timeout=0,
            poll_interval=0.01,
        )

    def journey():
        journey_sdk.step(wait_for_app)

    with pytest.raises(journey_sdk.CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "Timed out waiting for http://example.test/healthz" in str(exc_info.value)


def test_http_helpers_reject_outside_step():
    with pytest.raises(InvalidBranchUsageError):
        journey_http.http_request("http://example.test")


def test_http_helpers_validate_arguments():
    with pytest.raises(ValueError):
        journey_http.http_request("")
    with pytest.raises(ValueError):
        journey_http.http_request("http://example.test", body=b"x", json_body={})
    with pytest.raises(TypeError):
        journey_http.wait_for_http("http://example.test", expected_status=["200"])
