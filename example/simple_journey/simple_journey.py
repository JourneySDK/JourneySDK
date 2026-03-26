"""Quick-start journey showing Playwright steps, branching, and webhook waiting."""

from __future__ import annotations

import tempfile
from pathlib import Path

import journey
from journey.tools.webhook import host_webhook_endpoint

_DEMO_PAGE_URL = Path(__file__).with_name("demo_site.html").resolve().as_uri()
_STORED_FILE_NAME = "stored-message.txt"
_STORED_FILE_CONTENT = "Stored by the journey demo page.\n"
_STORED_FILE = Path(tempfile.gettempdir()) / "journey-demo-downloads" / _STORED_FILE_NAME


def assert_demo_homepage() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(_DEMO_PAGE_URL, wait_until="load")
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
        browser.close()
    return True


def click_trigger_endpoint_a() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(_DEMO_PAGE_URL, wait_until="load")
        page.get_by_role("button", name="Trigger endpoint A").click()
        page.wait_for_function(
            "() => document.getElementById('status').textContent === 'Endpoint A sent'"
        )
        status_text = page.locator("#status").text_content()
        browser.close()
    if status_text != "Endpoint A sent":
        raise AssertionError(f"Expected status 'Endpoint A sent', got '{status_text}'.")
    return True


def click_store_local_file() -> bool:
    _STORED_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _STORED_FILE.exists():
        _STORED_FILE.unlink()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(_DEMO_PAGE_URL, wait_until="load")
        with page.expect_download() as download_info:
            page.get_by_role("button", name="Store a local file").click()
        download = download_info.value
        download.save_as(str(_STORED_FILE))
        page.wait_for_function(
            "() => document.getElementById('status').textContent === 'Local file saved'"
        )
        status_text = page.locator("#status").text_content()
        context.close()
        browser.close()
    if status_text != "Local file saved":
        raise AssertionError(f"Expected status 'Local file saved', got '{status_text}'.")
    return True


def local_file_is_written() -> dict[str, str]:
    if not _STORED_FILE.exists():
        raise FileNotFoundError(f"Local demo file '{_STORED_FILE}' was not downloaded.")
    return {
        "path": str(_STORED_FILE),
        "content": _STORED_FILE.read_text(encoding="utf-8"),
    }


def assert_endpoint_a_webhook(request_payload: dict[str, object]) -> bool:
    if request_payload.get("method") != "GET":
        raise AssertionError(f"Expected GET webhook, got {request_payload.get('method')!r}.")
    if request_payload.get("path") != "/endpoint-a":
        raise AssertionError(f"Expected webhook path '/endpoint-a', got {request_payload.get('path')!r}.")
    if request_payload.get("query") != {
        "event": ["endpoint_a"],
        "source": ["journey_demo"],
    }:
        raise AssertionError(f"Unexpected webhook query: {request_payload.get('query')!r}")
    return True


def assert_local_file_contents(file_info: dict[str, str]) -> bool:
    if file_info.get("content") != _STORED_FILE_CONTENT:
        raise AssertionError(
            f"Expected local file content {_STORED_FILE_CONTENT!r}, got {file_info.get('content')!r}"
        )
    return True


@journey.journey
def simple_journey() -> None:
    receive_endpoint_a = host_webhook_endpoint(port=8765, path="/endpoint-a")

    journey.step(assert_demo_homepage)

    after_setup = journey.checkpoint()
    webhook_branch = journey.branch(start_from=after_setup)
    file_branch = journey.branch(start_from=after_setup)
    selected = journey.checkpoint(branches=[webhook_branch, file_branch])

    if selected.is_(webhook_branch):
        journey.step(click_trigger_endpoint_a)
        request_payload = journey.step(receive_endpoint_a)
        journey.step(assert_endpoint_a_webhook, request_payload)
    elif selected.is_(file_branch):
        journey.step(click_store_local_file)
        file_info = journey.step(local_file_is_written)
        journey.step(assert_local_file_contents, file_info)
