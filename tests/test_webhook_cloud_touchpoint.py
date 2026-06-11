from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import journeysdk as journey_sdk
import pytest

from journeysdk.errors import CallableExecutionError
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


def reserve_invoice_paid_webhook_endpoint() -> CloudWebhookEndpoint:
    return get_webhook_endpoint(path="/invoice-paid")


def receive_invoice_paid_webhook(endpoint: CloudWebhookEndpoint) -> dict[str, object]:
    return wait_for_webhook_request(
        endpoint,
        timeout=0.05,
        poll_interval=0.01,
    )


def test_cloud_webhook_planning_does_not_require_env_or_network(monkeypatch: pytest.MonkeyPatch):
    original_urlopen = urllib.request.urlopen

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the cloud service.")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    def journey():
        endpoint = journey_sdk.step(reserve_invoice_paid_webhook_endpoint)
        journey_sdk.step(receive_invoice_paid_webhook, endpoint)

    plan = journey_sdk.compile_journey(journey)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]

    assert labels == [
        "reserve_invoice_paid_webhook_endpoint",
        "receive_invoice_paid_webhook",
    ]
    monkeypatch.setattr(urllib.request, "urlopen", original_urlopen)


def test_cloud_webhook_helpers_validate_inputs_and_endpoint_handles():
    def invalid_path_type():
        return get_webhook_endpoint(path=object())  # type: ignore[arg-type]

    def invalid_path_value():
        return get_webhook_endpoint(path="/")

    def invalid_timeout(endpoint: CloudWebhookEndpoint):
        return wait_for_webhook_request(endpoint, path="/invoice-paid", timeout=True)  # type: ignore[arg-type]

    def invalid_poll_interval(endpoint: CloudWebhookEndpoint):
        return wait_for_webhook_request(endpoint, path="/invoice-paid", poll_interval=0)

    def wrong_endpoint_path(endpoint: CloudWebhookEndpoint):
        return wait_for_webhook_request(endpoint, path="/invoice-paid")

    def execute_step(fn, *args):
        def journey():
            journey_sdk.step(fn, *args)

        return journey_sdk.execute(journey)

    with pytest.raises(CallableExecutionError) as exc_info:
        execute_step(invalid_path_type)
    assert isinstance(exc_info.value.__cause__, TypeError)

    with pytest.raises(CallableExecutionError) as exc_info:
        execute_step(invalid_path_value)
    assert isinstance(exc_info.value.__cause__, ValueError)

    endpoint = CloudWebhookEndpoint(
        endpoint_id="endpoint-1",
        path="/wrong",
        url="http://example.test/webhooks/endpoint-1/wrong",
        api_base_url="http://example.test",
    )

    with pytest.raises(CallableExecutionError) as exc_info:
        execute_step(invalid_timeout, endpoint)
    assert isinstance(exc_info.value.__cause__, TypeError)

    with pytest.raises(CallableExecutionError) as exc_info:
        execute_step(invalid_poll_interval, endpoint)
    assert isinstance(exc_info.value.__cause__, ValueError)

    with pytest.raises(CallableExecutionError) as exc_info:
        execute_step(wrong_endpoint_path, endpoint)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "expected '/invoice-paid'" in str(exc_info.value)


def test_cloud_webhook_helpers_fail_clearly_when_env_is_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    def journey():
        journey_sdk.step(reserve_invoice_paid_webhook_endpoint)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "JOURNEY_CLOUD_API_KEY" in str(exc_info.value)

    monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, "test-key")

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "JOURNEY_CLOUD_BASE_URL" in str(exc_info.value)


def test_cloud_webhook_payload_has_expected_shape(monkeypatch: pytest.MonkeyPatch):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        def post_sequence_webhook(endpoint: CloudWebhookEndpoint) -> bool:
            _post_json(f"{endpoint.url}?source=test", {"sequence": 1})
            return True

        def journey():
            endpoint = journey_sdk.step(reserve_invoice_paid_webhook_endpoint)
            journey_sdk.step(post_sequence_webhook, endpoint)
            journey_sdk.step(receive_invoice_paid_webhook, endpoint)

        report = journey_sdk.execute(journey)

    endpoint = report.case_reports[0].records[0].result
    received = report.case_reports[0].records[2].result

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
            endpoint = journey_sdk.step(reserve_invoice_paid_webhook_endpoint)
            journey_sdk.step(_send_cloud_webhook_later, endpoint.url, 0.08)
            request_payload = journey_sdk.step(
                receive_invoice_paid_webhook,
                endpoint,
                retry=2,
                retry_delay=0,
            )
            journey_sdk.step(assert_webhook, request_payload)

        plan = journey_sdk.compile_journey(journey)
        labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
        assert labels == [
            "reserve_invoice_paid_webhook_endpoint",
            "_send_cloud_webhook_later",
            "receive_invoice_paid_webhook",
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
            endpoint = journey_sdk.step(reserve_invoice_paid_webhook_endpoint)
            if journey_sdk.branch(replay_from=endpoint):
                journey_sdk.step(_send_cloud_webhook_later, endpoint.url, 0.01)
                request_payload = journey_sdk.step(
                    receive_invoice_paid_webhook,
                    endpoint,
                    retry=2,
                    retry_delay=0,
                )
                journey_sdk.step(assert_webhook, request_payload)
            elif journey_sdk.branch(replay_from=endpoint):
                journey_sdk.step(noop)

        targeted_report = journey_sdk.execute(journey, target_step="assert_webhook")
        assert len(targeted_report.case_reports) == 1
        assert targeted_report.case_reports[0].stopped_at_label == "assert_webhook"
        assert targeted_report.case_reports[0].replay_anchor == "reserve_invoice_paid_webhook_endpoint"


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
            endpoint = journey_sdk.step(reserve_invoice_paid_webhook_endpoint)
            journey_sdk.step(_send_cloud_webhook_later, endpoint.url, 0.01)
            journey_sdk.step(pause_once, endpoint)
            request_payload = journey_sdk.step(
                receive_invoice_paid_webhook,
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
            "reserve_invoice_paid_webhook_endpoint",
            "_send_cloud_webhook_later",
            "pause_once",
            "receive_invoice_paid_webhook",
            "assert_webhook",
        ]
        assert len(seen_endpoint_ids) == 2
        assert seen_endpoint_ids[0] != seen_endpoint_ids[1]
