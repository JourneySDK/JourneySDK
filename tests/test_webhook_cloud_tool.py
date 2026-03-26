from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import journey as journey_sdk
import pytest

from journey.tools._webhook_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from journey.tools._webhook_local import (
    build_poll_url,
    ensure_local_host,
    reset_local_hosts,
)
from journey.tools.webhook import (
    CloudWebhookEndpoint,
    get_webhook_endpoint,
    wait_for_webhook_request,
)
from tests._cloud_stub import serve_in_background


@pytest.fixture(autouse=True)
def _reset_local_hosts() -> None:
    reset_local_hosts()
    yield
    reset_local_hosts()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def _poll_response(url: str) -> tuple[int, dict[str, object] | None]:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status == 204:
            return response.status, None
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload


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


def test_cloud_webhook_payload_matches_local_webhook_shape(monkeypatch: pytest.MonkeyPatch):
    local_port = _free_port()
    local_path = "/invoice-paid"
    ensure_local_host(port=local_port, path=local_path)
    local_public_url = f"http://localhost:{local_port}{local_path}?source=test"
    local_poll_url = build_poll_url(port=local_port, path=local_path)

    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)
        endpoint = get_webhook_endpoint(path="/invoice-paid")()

        _post_json(local_public_url, {"sequence": 1})
        _post_json(f"{endpoint.url}?source=test", {"sequence": 1})

        _, local_payload = _poll_response(local_poll_url)
        received = wait_for_webhook_request(path="/invoice-paid")(endpoint)

    assert local_payload is not None
    assert set(received) == set(local_payload)
    assert received["method"] == local_payload["method"]
    assert received["path"] == local_payload["path"]
    assert received["query"] == local_payload["query"]
    assert received["headers"]["content-type"] == local_payload["headers"]["content-type"]
    assert received["body_text"] == local_payload["body_text"]
    assert received["body_json"] == local_payload["body_json"]
    assert received["body_base64"] == local_payload["body_base64"]
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
            after_setup = journey_sdk.checkpoint()
            webhook_branch = journey_sdk.branch(start_from=after_setup)
            noop_branch = journey_sdk.branch(start_from=after_setup)
            selected = journey_sdk.checkpoint(branches=[webhook_branch, noop_branch])

            if selected.is_(webhook_branch):
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
            elif selected.is_(noop_branch):
                journey_sdk.step(noop)

        targeted_report = journey_sdk.execute(journey, step="assert_webhook")
        assert len(targeted_report.case_reports) == 1
        assert targeted_report.case_reports[0].stopped_at_label == "assert_webhook"
        assert targeted_report.case_reports[0].replay_anchor == "cp_1"


def test_cloud_webhook_endpoint_handle_survives_resume(
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
        assert seen_endpoint_ids[0] == seen_endpoint_ids[1]
