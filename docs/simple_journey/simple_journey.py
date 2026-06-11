"""Quick-start journey showing browser steps, branching, and webhook waiting."""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import quote

from journeysdk import branch, journey, step
from journeysdk.touchpoints.browser import open_page
from journeysdk.touchpoints.webhook import CloudWebhookEndpoint, get_webhook_endpoint, wait_for_webhook_request

_DEMO_PAGE_URL = Path(__file__).with_name("demo_site.html").resolve().as_uri()
_STORED_FILE_NAME = "stored-message.txt"
_STORED_FILE_CONTENT = "Stored by the journey demo page.\n"
_STORED_FILE = Path(tempfile.gettempdir()) / "journey-demo-downloads" / _STORED_FILE_NAME


def demo_homepage_ready() -> bool:
    page = open_page(_DEMO_PAGE_URL)
    title = page.title()
    buttons = page.get_by_role("button")
    if title != "journey demo":
        raise AssertionError(f"Expected page title 'journey demo', got '{title}'.")
    if buttons.count() != 2:
        raise AssertionError(f"Expected exactly 2 buttons, got {buttons.count()}.")
    if not page.get_by_role("button", name="Trigger endpoint A").is_visible():
        raise AssertionError("Missing 'Trigger endpoint A' button.")
    if not page.get_by_role("button", name="Store a local file").is_visible():
        raise AssertionError("Missing 'Store a local file' button.")
    return True


def _click_trigger_endpoint_a(endpoint_url: str) -> None:
    page_url = f"{_DEMO_PAGE_URL}?webhookUrl={quote(endpoint_url, safe='')}"
    page = open_page(page_url)
    page.get_by_role("button", name="Trigger endpoint A").click()
    page.wait_for_function(
        "() => document.getElementById('status').textContent === 'Endpoint A sent'"
    )
    status_text = page.locator("#status").text_content()
    if status_text != "Endpoint A sent":
        raise AssertionError(f"Expected status 'Endpoint A sent', got '{status_text}'.")


def _download_stored_file() -> None:
    _STORED_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _STORED_FILE.exists():
        _STORED_FILE.unlink()

    page = open_page(_DEMO_PAGE_URL)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Store a local file").click()
    download = download_info.value
    download.save_as(str(_STORED_FILE))
    page.wait_for_function(
        "() => document.getElementById('status').textContent === 'Local file saved'"
    )
    status_text = page.locator("#status").text_content()
    if status_text != "Local file saved":
        raise AssertionError(f"Expected status 'Local file saved', got '{status_text}'.")


def _local_file_contents() -> dict[str, str]:
    if not _STORED_FILE.exists():
        raise FileNotFoundError(f"Local demo file '{_STORED_FILE}' was not downloaded.")
    return {
        "path": str(_STORED_FILE),
        "content": _STORED_FILE.read_text(encoding="utf-8"),
    }


def _assert_endpoint_a_webhook(request_payload: dict[str, object]) -> None:
    if request_payload.get("method") != "GET":
        raise AssertionError(f"Expected GET webhook, got {request_payload.get('method')!r}.")
    if request_payload.get("path") != "/endpoint-a":
        raise AssertionError(f"Expected webhook path '/endpoint-a', got {request_payload.get('path')!r}.")
    if request_payload.get("query") != {
        "event": ["endpoint_a"],
        "source": ["journey_demo"],
    }:
        raise AssertionError(f"Unexpected webhook query: {request_payload.get('query')!r}")


def _assert_local_file_contents(file_info: dict[str, str]) -> None:
    if file_info.get("content") != _STORED_FILE_CONTENT:
        raise AssertionError(
            f"Expected local file content {_STORED_FILE_CONTENT!r}, got {file_info.get('content')!r}"
        )


def trigger_endpoint_a_and_verify_webhook() -> bool:
    endpoint: CloudWebhookEndpoint = get_webhook_endpoint(path="/endpoint-a")
    _click_trigger_endpoint_a(endpoint.url)
    request_payload = wait_for_webhook_request(endpoint)
    _assert_endpoint_a_webhook(request_payload)
    return True


def store_local_file_and_verify_contents() -> bool:
    _download_stored_file()
    _assert_local_file_contents(_local_file_contents())
    return True


@journey
def simple_journey() -> None:
    homepage = step(demo_homepage_ready)

    if branch(replay_from=homepage):
        step(trigger_endpoint_a_and_verify_webhook)
    elif branch(replay_from=homepage):
        step(store_local_file_and_verify_contents)
