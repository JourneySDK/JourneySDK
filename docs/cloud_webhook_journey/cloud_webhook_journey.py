"""Tutorial journey showing how to use a cloud-hosted webhook endpoint."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import journey
from journey.tools.webhook import get_webhook_endpoint, wait_for_webhook_request

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()


def send_invoice_paid_webhook_later(url: str, delay: float = 0.05) -> bool:
    EVENTS.append(f"send_invoice_paid_webhook_later:{url}")

    def worker() -> None:
        time.sleep(delay)
        body = json.dumps({"invoice_id": "inv-001", "status": "paid"}).encode("utf-8")
        request = urllib.request.Request(
            f"{url}?event=invoice_paid&source=journey_demo_cloud",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5):
            pass

    threading.Thread(target=worker, daemon=True).start()
    return True


def assert_invoice_paid_webhook(request_payload: dict[str, object]) -> bool:
    if request_payload.get("method") != "POST":
        raise AssertionError(f"Expected POST webhook, got {request_payload.get('method')!r}.")
    if request_payload.get("path") != "/invoice-paid":
        raise AssertionError(
            f"Expected webhook path '/invoice-paid', got {request_payload.get('path')!r}."
        )
    if request_payload.get("query") != {
        "event": ["invoice_paid"],
        "source": ["journey_demo_cloud"],
    }:
        raise AssertionError(f"Unexpected webhook query: {request_payload.get('query')!r}")
    if request_payload.get("body_json") != {
        "invoice_id": "inv-001",
        "status": "paid",
    }:
        raise AssertionError(f"Unexpected webhook body: {request_payload.get('body_json')!r}")

    EVENTS.append("assert_invoice_paid_webhook")
    return True


@journey.journey
def cloud_webhook_journey() -> None:
    endpoint = journey.step(get_webhook_endpoint(path="/invoice-paid"))
    journey.step(send_invoice_paid_webhook_later, endpoint.url)
    request_payload = journey.step(
        wait_for_webhook_request(
            path="/invoice-paid",
            timeout=0.05,
            poll_interval=0.01,
        ),
        endpoint,
        retry=3,
        retry_delay=0,
    )
    journey.step(assert_invoice_paid_webhook, request_payload)
