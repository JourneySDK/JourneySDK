from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import journeysdk as journey_sdk
import pytest

from journeysdk.touchpoints._webhook_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from journeysdk.touchpoints.webhook import (
    CloudWebhookEndpoint,
    get_webhook_endpoint,
    wait_for_webhook_request,
)
from tests._cloud_stub import serve_in_background


def _post_json(url: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def _send_cloud_webhook_later(url: str, delay: float) -> bool:
    def worker() -> None:
        time.sleep(delay)
        _post_json(url, {"event": "invoice_paid", "source": "test"})

    threading.Thread(target=worker, daemon=True).start()
    return True


def _configure_cloud_env(monkeypatch: pytest.MonkeyPatch, *, api_key: str, base_url: str) -> None:
    monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, api_key)
    monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, base_url)


def test_cloud_webhook_planning_does_not_require_env_or_network(monkeypatch: pytest.MonkeyPatch):
    original_urlopen = urllib.request.urlopen

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the cloud service.")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    def journey():
        endpoint = get_webhook_endpoint(path="/invoice-paid")
        wait_for_invoice = wait_for_webhook_request(path="/invoice-paid")
        handle = journey_sdk.step(endpoint)
        journey_sdk.step(wait_for_invoice, handle)

    plan = journey_sdk.compile_journey(journey)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]

    assert labels == ["get_webhook_invoice_paid", "receive_webhook_invoice_paid"]
    monkeypatch.setattr(urllib.request, "urlopen", original_urlopen)


def test_cloud_webhook_helpers_validate_inputs_and_endpoint_handles():
    with pytest.raises(TypeError):
        get_webhook_endpoint(path=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        get_webhook_endpoint(path="/")

    with pytest.raises(TypeError):
        wait_for_webhook_request(path="/invoice-paid", timeout=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        wait_for_webhook_request(path="/invoice-paid", poll_interval=0)

    endpoint = CloudWebhookEndpoint(
        endpoint_id="endpoint-1",
        path="/wrong",
        url="http://example.test/webhooks/endpoint-1/wrong",
        api_base_url="http://example.test",
    )
    receive_webhook = wait_for_webhook_request(path="/invoice-paid")

    with pytest.raises(ValueError) as exc_info:
        receive_webhook(endpoint)

    assert "expected '/invoice-paid'" in str(exc_info.value)


def test_cloud_webhook_helpers_fail_clearly_when_env_is_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        get_webhook_endpoint(path="/invoice-paid")()

    assert "JOURNEY_CLOUD_API_KEY" in str(exc_info.value)

    monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, "test-key")

    with pytest.raises(RuntimeError) as exc_info:
        get_webhook_endpoint(path="/invoice-paid")()

    assert "JOURNEY_CLOUD_BASE_URL" in str(exc_info.value)


def test_cloud_webhook_payload_has_expected_shape(monkeypatch: pytest.MonkeyPatch):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)
        endpoint = get_webhook_endpoint(path="/invoice-paid")()

        _post_json(f"{endpoint.url}?source=test", {"sequence": 1})

        received = wait_for_webhook_request(path="/invoice-paid")(endpoint)

    assert set(received) == {
        "method",
        "url",
        "path",
        "query",
        "headers",
        "body_text",
        "body_json",
        "body_base64",
        "received_at",
    }
    assert received["method"] == "POST"
    assert received["url"] == f"{endpoint.url}?source=test"
    assert received["path"] == "/invoice-paid"
    assert received["query"] == {"source": ["test"]}
    assert received["headers"]["content-type"] == "application/json"
    assert received["body_text"] == '{"sequence": 1}'
    assert received["body_json"] == {"sequence": 1}
    assert received["body_base64"] == "eyJzZXF1ZW5jZSI6IDF9"
    assert isinstance(received["received_at"], str)


def test_cloud_webhook_retries_until_background_sender_posts(monkeypatch: pytest.MonkeyPatch):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        def assert_webhook(request_payload: dict[str, object]) -> bool:
            assert request_payload["path"] == "/invoice-paid"
            assert request_payload["body_json"] == {
                "event": "invoice_paid",
                "source": "test",
            }
            return True

        def journey():
            endpoint = journey_sdk.step(get_webhook_endpoint(path="/invoice-paid"))
            journey_sdk.step(_send_cloud_webhook_later, endpoint.url, 0.08)
            request_payload = journey_sdk.step(
                wait_for_webhook_request(
                    path="/invoice-paid",
                    timeout=0.05,
                    poll_interval=0.01,
                ),
                endpoint,
                retry=2,
                retry_delay=0,
            )
            journey_sdk.step(assert_webhook, request_payload)

        plan = journey_sdk.compile_journey(journey)
        labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
        assert labels == [
            "get_webhook_invoice_paid",
            "_send_cloud_webhook_later",
            "receive_webhook_invoice_paid",
            "assert_webhook",
        ]

        report = journey_sdk.execute(journey)
        record_labels = [
            record.label
            for record in report.case_reports[0].records
            if record.label is not None
        ]
        assert record_labels == labels


def test_cloud_webhook_journey_supports_targeted_execution(monkeypatch: pytest.MonkeyPatch):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        def assert_webhook(request_payload: dict[str, object]) -> bool:
            assert request_payload["path"] == "/invoice-paid"
            return True

        def noop() -> bool:
            return True

        def journey():
            endpoint = journey_sdk.step(get_webhook_endpoint(path="/invoice-paid"))
            if journey_sdk.branch(start_from=endpoint):
                journey_sdk.step(_send_cloud_webhook_later, endpoint.url, 0.01)
                request_payload = journey_sdk.step(
                    wait_for_webhook_request(
                        path="/invoice-paid",
                        timeout=0.05,
                        poll_interval=0.01,
                    ),
                    endpoint,
                    retry=2,
                    retry_delay=0,
                )
                journey_sdk.step(assert_webhook, request_payload)
            elif journey_sdk.branch(start_from=endpoint):
                journey_sdk.step(noop)

        targeted_report = journey_sdk.execute(journey, step="assert_webhook")
        assert len(targeted_report.case_reports) == 1
        assert targeted_report.case_reports[0].stopped_at_label == "assert_webhook"
        assert targeted_report.case_reports[0].replay_anchor == "get_webhook_invoice_paid"


def test_cloud_webhook_endpoint_reruns_without_explicit_replay_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        seen_endpoint_ids: list[str] = []
        interrupt_once = {"enabled": True}

        def pause_once(endpoint: CloudWebhookEndpoint) -> bool:
            seen_endpoint_ids.append(endpoint.endpoint_id)
            if interrupt_once["enabled"]:
                interrupt_once["enabled"] = False
                raise KeyboardInterrupt()
            return True

        def assert_webhook(request_payload: dict[str, object]) -> bool:
            assert request_payload["path"] == "/invoice-paid"
            return True

        def journey():
            endpoint = journey_sdk.step(get_webhook_endpoint(path="/invoice-paid"))
            journey_sdk.step(_send_cloud_webhook_later, endpoint.url, 0.01)
            journey_sdk.step(pause_once, endpoint)
            request_payload = journey_sdk.step(
                wait_for_webhook_request(
                    path="/invoice-paid",
                    timeout=0.05,
                    poll_interval=0.01,
                ),
                endpoint,
                retry=3,
                retry_delay=0,
            )
            journey_sdk.step(assert_webhook, request_payload)

        state_file = tmp_path / "journey-cloud.state"

        with pytest.raises(KeyboardInterrupt):
            journey_sdk.execute(journey, state=state_file)

        assert state_file.exists()

        report = journey_sdk.execute(journey, state=state_file)
        record_labels = [
            record.label
            for record in report.case_reports[0].records
            if record.label is not None
        ]

        assert record_labels == [
            "get_webhook_invoice_paid",
            "_send_cloud_webhook_later",
            "pause_once",
            "receive_webhook_invoice_paid",
            "assert_webhook",
        ]
        assert len(seen_endpoint_ids) == 2
        assert seen_endpoint_ids[0] != seen_endpoint_ids[1]
