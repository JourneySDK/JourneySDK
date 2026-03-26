from __future__ import annotations

import builtins
import importlib
import json
import sys
import types

import journey as journey_sdk
import pytest

from journey.tools import playwright as journey_playwright


def test_playwright_page_state_round_trips_json():
    state = journey_playwright.PlaywrightPageState.from_json(
        json.dumps(
            {
                "url": "http://example.test/dashboard",
                "cookies": [
                    {
                        "name": "journey_session",
                        "value": "demo-session",
                        "domain": "example.test",
                        "path": "/",
                    }
                ],
                "local_storage": {
                    "journey_session_token": "demo-token",
                },
            }
        )
    )

    assert state.url == "http://example.test/dashboard"
    assert state.cookies == (
        {
            "name": "journey_session",
            "value": "demo-session",
            "domain": "example.test",
            "path": "/",
        },
    )
    assert state.local_storage == {"journey_session_token": "demo-token"}
    assert journey_playwright.PlaywrightPageState.from_json(state.to_json()) == state


def test_capture_page_state_reads_url_cookies_and_local_storage():
    class FakeContext:
        def cookies(self) -> list[dict[str, str]]:
            return [
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]

    class FakePage:
        url = "http://127.0.0.1:8765/dashboard"
        context = FakeContext()

        def evaluate(self, script: str) -> dict[str, str]:
            assert "window.localStorage" in script
            return {"journey_session_token": "demo-token"}

    state = journey_playwright.capture_page_state(FakePage())

    assert state.url == "http://127.0.0.1:8765/dashboard"
    assert state.cookies == (
        {
            "name": "journey_session",
            "value": "demo-session",
            "domain": "127.0.0.1",
            "path": "/",
        },
    )
    assert state.local_storage == {"journey_session_token": "demo-token"}


def test_open_page_rehydrates_in_expected_order_and_cleans_up(monkeypatch):
    events: list[object] = []

    class FakePage:
        def goto(self, url: str, *, wait_until: str) -> None:
            events.append(("goto", url, wait_until))

        def evaluate(self, script: str, items: dict[str, str]) -> None:
            assert "window.localStorage.clear" in script
            events.append(("evaluate", dict(items)))

        def reload(self, *, wait_until: str) -> None:
            events.append(("reload", wait_until))

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        def add_cookies(self, cookies: list[dict[str, object]]) -> None:
            events.append(("add_cookies", list(cookies)))

        def new_page(self) -> FakePage:
            events.append("new_page")
            return self.page

        def close(self) -> None:
            events.append("context_close")

    class FakeBrowser:
        def __init__(self) -> None:
            self.context = FakeContext()

        def new_context(self) -> FakeContext:
            events.append("new_context")
            return self.context

        def close(self) -> None:
            events.append("browser_close")

    class FakeBrowserType:
        def launch(self, *, headless: bool) -> FakeBrowser:
            events.append(("launch", headless))
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeBrowserType()

    class FakeManager:
        def __enter__(self) -> FakePlaywright:
            events.append("playwright_enter")
            return FakePlaywright()

        def __exit__(self, exc_type, exc, tb) -> bool:
            events.append("playwright_exit")
            return False

    fake_package = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: FakeManager()

    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    state = journey_playwright.PlaywrightPageState.from_json(
        json.dumps(
            {
                "url": "http://example.test/dashboard",
                "cookies": [
                    {
                        "name": "journey_session",
                        "value": "demo-session",
                        "domain": "example.test",
                        "path": "/",
                    }
                ],
                "local_storage": {"journey_session_token": "demo-token"},
            }
        )
    )

    with journey_playwright.open_page(state.to_json(), headless=False) as page:
        assert isinstance(page, FakePage)
        events.append("inside")

    assert events == [
        "playwright_enter",
        ("launch", False),
        "new_context",
        (
            "add_cookies",
            [
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
        ),
        "new_page",
        ("goto", "http://example.test/dashboard", "load"),
        ("evaluate", {"journey_session_token": "demo-token"}),
        ("reload", "load"),
        "inside",
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_playwright_tool_import_is_lazy(monkeypatch):
    module = importlib.import_module("journey.tools.playwright")
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "playwright" or name.startswith("playwright."):
            raise AssertionError("Importing the tool should not import Playwright.")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    reloaded = importlib.reload(module)
    state = reloaded.PlaywrightPageState.from_url("http://example.test/login")

    assert state.url == "http://example.test/login"
    assert state.cookies == ()
    assert state.local_storage == {}


def test_execute_resume_rehydrates_saved_playwright_page_state(tmp_path):
    state_file = tmp_path / "journey.state"
    attempts = {"count": 0}
    events: list[str] = []

    def create_page_state() -> journey_playwright.PlaywrightPageState:
        events.append("create_page_state")
        return journey_playwright.PlaywrightPageState.from_json(
            json.dumps(
                {
                    "url": "http://example.test/dashboard",
                    "cookies": [],
                    "local_storage": {
                        "journey_session_token": "demo-token",
                    },
                }
            )
        )

    def continue_from_state(page_state: journey_playwright.PlaywrightPageState) -> str:
        attempts["count"] += 1
        events.append(f"continue:{attempts['count']}:{page_state.url}")
        if attempts["count"] == 1:
            raise KeyboardInterrupt()
        return page_state.to_json()

    def assert_page_state(result: str) -> bool:
        restored = journey_playwright.PlaywrightPageState.from_json(result)
        events.append(f"assert:{restored.url}")
        assert restored.local_storage == {"journey_session_token": "demo-token"}
        return True

    def journey():
        session = journey_sdk.step(create_page_state)
        result = journey_sdk.step(continue_from_state, session)
        journey_sdk.step(assert_page_state, result)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert [record.label for record in report.case_reports[0].records if record.label is not None] == [
        "create_page_state",
        "continue_from_state",
        "assert_page_state",
    ]
    assert events == [
        "create_page_state",
        "continue:1:http://example.test/dashboard",
        "continue:2:http://example.test/dashboard",
        "assert:http://example.test/dashboard",
    ]
