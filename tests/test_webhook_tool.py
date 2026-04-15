from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import journeysdk as journey_sdk
import pytest

from journeysdk.tools._webhook_local import (
    build_poll_url,
    ensure_local_host,
    is_port_open,
    reset_local_hosts,
)
from journeysdk.tools.webhook import host_webhook_endpoint


@pytest.fixture(autouse=True)
def _reset_hosts() -> None:
    reset_local_hosts()
    yield
    reset_local_hosts()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: dict[str, object]) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def _send_webhook_later(url: str, delay: float) -> bool:
    def worker() -> None:
        time.sleep(delay)
        _post_json(url, {"event": "endpoint_a", "source": "test"})

    threading.Thread(target=worker, daemon=True).start()
    return True


def _poll_response(url: str) -> tuple[int, dict[str, object] | None]:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status == 204:
            return response.status, None
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
def test_planning_does_not_start_the_local_webhook_host():
    port = _free_port()

    def journey():
        receive_endpoint_a = host_webhook_endpoint(port=port, path="/endpoint-a")
        journey_sdk.step(receive_endpoint_a)

    assert not is_port_open(port)

    plan = journey_sdk.compile_journey(journey)

    assert not is_port_open(port)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
    assert labels == ["receive_webhook_endpoint_a"]


def test_host_rejects_duplicate_registrations_in_one_journey_definition():
    port = _free_port()

    def journey():
        host_webhook_endpoint(port=port, path="/endpoint-a")
        host_webhook_endpoint(port=port, path="/endpoint-a")

    with pytest.raises(ValueError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "registered the same endpoint twice" in str(exc_info.value)


def test_host_rejects_reserved_control_paths():
    with pytest.raises(ValueError):
        host_webhook_endpoint(port=8080, path="/_journey/webhooks/poll")


def test_local_host_accepts_requests_and_polls_them_fifo():
    port = _free_port()
    path = "/endpoint-a"
    ensure_local_host(port=port, path=path)
    poll_url = build_poll_url(port=port, path=path)

    _post_json(f"http://localhost:{port}{path}?run=1", {"sequence": 1})
    _post_json(f"http://localhost:{port}{path}?run=2", {"sequence": 2})

    status_1, payload_1 = _poll_response(poll_url)
    status_2, payload_2 = _poll_response(poll_url)
    status_3, payload_3 = _poll_response(poll_url)

    assert status_1 == 200
    assert payload_1 is not None
    assert payload_1["method"] == "POST"
    assert payload_1["path"] == path
    assert payload_1["query"] == {"run": ["1"]}
    assert payload_1["body_json"] == {"sequence": 1}
    assert payload_1["body_text"] == '{"sequence": 1}'
    assert payload_1["body_base64"] == "eyJzZXF1ZW5jZSI6IDF9"
    assert payload_1["headers"]["content-type"] == "application/json"
    assert isinstance(payload_1["received_at"], str)

    assert status_2 == 200
    assert payload_2 is not None
    assert payload_2["query"] == {"run": ["2"]}
    assert payload_2["body_json"] == {"sequence": 2}

    assert status_3 == 204
    assert payload_3 is None


def test_receive_webhook_retries_until_the_background_sender_posts():
    port = _free_port()

    def assert_webhook(request_payload: dict[str, object]) -> bool:
        assert request_payload["path"] == "/endpoint-a"
        assert request_payload["body_json"] == {"event": "endpoint_a", "source": "test"}
        return True

    def journey():
        receive_endpoint_a = host_webhook_endpoint(
            port=port,
            path="/endpoint-a",
            timeout=0.05,
            poll_interval=0.01,
        )
        journey_sdk.step(_send_webhook_later, receive_endpoint_a.url, 0.08)
        request_payload = journey_sdk.step(receive_endpoint_a, retry=2, retry_delay=0)
        journey_sdk.step(assert_webhook, request_payload)

    plan = journey_sdk.compile_journey(journey)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
    assert labels == [
        "_send_webhook_later",
        "receive_webhook_endpoint_a",
        "assert_webhook",
    ]

    report = journey_sdk.execute(journey)

    record_labels = [
        record.label
        for record in report.case_reports[0].records
        if record.label is not None
    ]
    assert record_labels == [
        "_send_webhook_later",
        "receive_webhook_endpoint_a",
        "assert_webhook",
    ]


def test_webhook_journey_supports_targeted_execution_and_resume(tmp_path: Path):
    port = _free_port()
    file_target = tmp_path / "stored-message.txt"
    wait_attempts = {"count": 0}
    interrupt_enabled = {"value": False}

    def queue_file_write(target: str) -> bool:
        def worker() -> None:
            time.sleep(0.05)
            Path(target).write_text("stored from test\n", encoding="utf-8")

        threading.Thread(target=worker, daemon=True).start()
        return True

    def wait_for_file(target: str) -> dict[str, str]:
        wait_attempts["count"] += 1
        if interrupt_enabled["value"] and wait_attempts["count"] == 1:
            raise KeyboardInterrupt()
        path = Path(target)
        if not path.exists():
            raise FileNotFoundError(f"{target} is not ready")
        content = path.read_text(encoding="utf-8")
        if content != "stored from test\n":
            raise FileNotFoundError(f"{target} content is not ready")
        return {
            "path": target,
            "content": content,
        }

    def assert_webhook(request_payload: dict[str, object]) -> bool:
        assert request_payload["path"] == "/endpoint-a"
        return True

    def assert_file(file_info: dict[str, str]) -> bool:
        assert file_info["content"] == "stored from test\n"
        return True

    def journey():
        receive_endpoint_a = host_webhook_endpoint(
            port=port,
            path="/endpoint-a",
            timeout=0.05,
            poll_interval=0.01,
        )
        after_setup = journey_sdk.checkpoint()
        if journey_sdk.branch(start_from=after_setup):
            journey_sdk.step(_send_webhook_later, receive_endpoint_a.url, 0.01)
            request_payload = journey_sdk.step(receive_endpoint_a, retry=1, retry_delay=0)
            journey_sdk.step(assert_webhook, request_payload)
        elif journey_sdk.branch(start_from=after_setup):
            retry_anchor = journey_sdk.checkpoint()
            journey_sdk.step(queue_file_write, str(file_target))
            file_info = journey_sdk.step(
                wait_for_file,
                str(file_target),
                retry=10,
                retry_delay=0.01,
                retry_from=retry_anchor,
            )
            journey_sdk.step(assert_file, file_info)

    plan = journey_sdk.compile_journey(journey)
    assert len(plan.case_plans) == 2

    targeted_report = journey_sdk.execute(journey, step="assert_file")
    assert len(targeted_report.case_reports) == 1
    assert targeted_report.case_reports[0].stopped_at_label == "assert_file"
    assert targeted_report.case_reports[0].replay_anchor == "cp_1"

    state_file = tmp_path / "journey.state"
    wait_attempts["count"] = 0
    interrupt_enabled["value"] = True
    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, step="assert_file", state=state_file)

    interrupt_enabled["value"] = False
    resumed_report = journey_sdk.execute(journey, step="assert_file", state=state_file)
    record_labels = [
        record.label
        for record in resumed_report.case_reports[0].records
        if record.label is not None
    ]
    assert record_labels == ["queue_file_write", "wait_for_file", "assert_file"]
