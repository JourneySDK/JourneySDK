from __future__ import annotations

import json
from pathlib import Path
from types import MethodType

import journeysdk as journey_sdk
import pytest
from journeysdk.errors import InvalidBranchUsageError

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page as PlaywrightPage

from journeysdk.tools import playwright as journey_playwright


def _state_json(
    *,
    url: str = "http://example.test/dashboard",
    cookies: list[dict[str, object]] | None = None,
    local_storage: dict[str, str] | None = None,
) -> str:
    return json.dumps(
        {
            "url": url,
            "cookies": cookies or [],
            "local_storage": local_storage or {},
        }
    )


def _fake_journey_page(
    *,
    context: object,
    events: list[object],
    initial_url: str = "about:blank",
) -> journey_playwright.JourneyPlaywrightPage:
    page = journey_playwright.JourneyPlaywrightPage._from_state(
        journey_playwright.PlaywrightPageState.from_url(initial_url)
    )
    page._journey_step_closed = False
    page._fake_context = context
    page._fake_url = initial_url
    page._fake_local_storage = {}

    def goto(self, url: str, *, wait_until: str) -> None:
        self._fake_url = url
        events.append(("goto", url, wait_until))

    def evaluate(
        self,
        script: str,
        items: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        if items is None:
            assert "window.localStorage" in script
            events.append(("capture_storage", self._fake_url))
            return dict(self._fake_local_storage)
        assert "window.localStorage.clear" in script
        self._fake_local_storage = dict(items)
        events.append(("evaluate", dict(items)))
        return None

    def reload(self, *, wait_until: str) -> None:
        events.append(("reload", wait_until))

    def state_for_storage(self) -> journey_playwright.PlaywrightPageState:
        events.append(("capture_state", self._fake_url))
        self._journey_page_state = journey_playwright.PlaywrightPageState.from_json(
            _state_json(
                url=self._fake_url,
                cookies=self._fake_context.cookies(),
                local_storage=self._fake_local_storage,
            )
        )
        return self._journey_page_state

    page.goto = MethodType(goto, page)
    page.evaluate = MethodType(evaluate, page)
    page.reload = MethodType(reload, page)
    page._state_for_storage = MethodType(state_for_storage, page)
    return page


def test_playwright_page_state_round_trips_json():
    state = journey_playwright.PlaywrightPageState.from_json(
        _state_json(
            cookies=[
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            local_storage={
                "journey_session_token": "demo-token",
            },
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

    class FakeContext:
        def __init__(self) -> None:
            self.page = _fake_journey_page(context=self, events=events)
            self._cookies: list[dict[str, object]] = []

        def add_cookies(self, cookies: list[dict[str, object]]) -> None:
            self._cookies = list(cookies)
            events.append(("add_cookies", list(cookies)))

        def new_page(self) -> journey_playwright.JourneyPlaywrightPage:
            events.append("new_page")
            return self.page

        def cookies(self) -> list[dict[str, object]]:
            return list(self._cookies)

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

    monkeypatch.setattr(journey_playwright, "sync_playwright", lambda: FakeManager())

    state = journey_playwright.PlaywrightPageState.from_json(
        _state_json(
            cookies=[
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            local_storage={"journey_session_token": "demo-token"},
        )
    )

    def open_dashboard() -> bool:
        page = journey_playwright.open_page(state.to_json(), headless=False)
        assert isinstance(page, journey_playwright.JourneyPlaywrightPage)
        events.append("inside")
        return True

    def journey():
        journey_sdk.step(open_dashboard)

    journey_sdk.execute(journey)

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
        ("capture_state", "http://example.test/dashboard"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_rejects_outside_step():
    with pytest.raises(InvalidBranchUsageError):
        journey_playwright.open_page("http://example.test/login")


def test_journey_playwright_page_is_native_page_subclass_and_rehydrates(tmp_path: Path):
    assert issubclass(journey_playwright.JourneyPlaywrightPage, PlaywrightPage)

    state = journey_playwright.PlaywrightPageState.from_json(
        _state_json(
            cookies=[
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            local_storage={"journey_session_token": "demo-token"},
        )
    )
    page = journey_playwright.JourneyPlaywrightPage._from_state(state)
    payload = page.__store__(
        journey_sdk.JourneyStoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        )
    )

    restored = journey_playwright.JourneyPlaywrightPage.__restore__(
        payload,
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    assert isinstance(restored, journey_playwright.JourneyPlaywrightPage)
    assert restored.url == "http://example.test/dashboard"
    assert journey_playwright.capture_page_state(restored) == state


def test_playwright_tool_builds_empty_state_from_url():
    state = journey_playwright.PlaywrightPageState.from_url("http://example.test/login")
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


def test_execute_resume_rehydrates_saved_journey_playwright_page(tmp_path, monkeypatch):
    state_file = tmp_path / "journey.state"
    attempts = {"count": 0}
    events: list[object] = []

    class FakeContext:
        def __init__(self) -> None:
            self.page = _fake_journey_page(context=self, events=events)
            self._cookies: list[dict[str, object]] = []

        def add_cookies(self, cookies: list[dict[str, object]]) -> None:
            self._cookies = list(cookies)

        def new_page(self) -> journey_playwright.JourneyPlaywrightPage:
            return self.page

        def cookies(self) -> list[dict[str, object]]:
            return list(self._cookies)

        def close(self) -> None:
            events.append("context_close")

    class FakeBrowser:
        def new_context(self) -> FakeContext:
            return FakeContext()

        def close(self) -> None:
            events.append("browser_close")

    class FakeBrowserType:
        def launch(self, *, headless: bool) -> FakeBrowser:
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeBrowserType()

    class FakeManager:
        def __enter__(self) -> FakePlaywright:
            return FakePlaywright()

        def __exit__(self, exc_type, exc, tb) -> bool:
            events.append("playwright_exit")
            return False

    monkeypatch.setattr(journey_playwright, "sync_playwright", lambda: FakeManager())

    def login() -> journey_playwright.JourneyPlaywrightPage:
        page = journey_playwright.open_page("http://example.test/login")
        page.goto("http://example.test/dashboard", wait_until="load")
        return page

    def continue_from_page(
        saved_page: journey_playwright.JourneyPlaywrightPage,
    ) -> journey_playwright.JourneyPlaywrightPage:
        attempts["count"] += 1
        events.append(f"continue:{attempts['count']}:{saved_page.url}")
        page = journey_playwright.open_page(saved_page)
        if attempts["count"] == 1:
            raise KeyboardInterrupt()
        return page

    def assert_page(saved_page: journey_playwright.JourneyPlaywrightPage) -> bool:
        page = journey_playwright.open_page(saved_page)
        events.append(f"assert:{page.url}")
        return True

    def journey():
        page = journey_sdk.step(login)
        continued = journey_sdk.step(continue_from_page, page)
        journey_sdk.step(assert_page, continued)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert [record.label for record in report.case_reports[0].records if record.label is not None] == [
        "login",
        "continue_from_page",
        "assert_page",
    ]
    assert "continue:1:http://example.test/dashboard" in events
    assert "continue:2:http://example.test/dashboard" in events
    assert "assert:http://example.test/dashboard" in events
