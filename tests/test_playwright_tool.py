from __future__ import annotations

from pathlib import Path
from types import MethodType

import journeysdk as journey_sdk
import pytest
from journeysdk.errors import CallableExecutionError, InvalidBranchUsageError

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page as PlaywrightPage

from journeysdk.tools import playwright as journey_playwright


def _state_payload(
    *,
    url: str = "http://example.test/dashboard",
    cookies: list[dict[str, object]] | None = None,
    local_storage: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "url": url,
        "cookies": cookies or [],
        "local_storage": local_storage or {},
    }


class _FakeNativePage:
    def __init__(self, context: object) -> None:
        self.context = context


class _FakePageImpl:
    def __init__(self, page: journey_playwright.JourneyPlaywrightPage) -> None:
        self._page = page

    @property
    def url(self) -> str:
        return self._page._fake_url


def _attach_fake_live_page(
    page: journey_playwright.JourneyPlaywrightPage,
    native_page: object,
    *,
    events: list[object],
    fallback_snapshot: object,
    initial_url: str = "about:blank",
) -> None:
    context = getattr(native_page, "context")
    page._journey_snapshot = fallback_snapshot
    page._journey_step_closed = False
    page._fake_context = context
    page._fake_url = initial_url
    page._fake_local_storage = {}
    page._impl_obj = _FakePageImpl(page)

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

    def snapshot_for_storage(self):
        events.append(("capture_state", self._fake_url))
        self._journey_snapshot = journey_playwright._PageSnapshot.from_payload(
            _state_payload(
                url=self._fake_url,
                cookies=self._fake_context.cookies(),
                local_storage=self._fake_local_storage,
            )
        )
        return self._journey_snapshot

    page.goto = MethodType(goto, page)
    page.evaluate = MethodType(evaluate, page)
    page.reload = MethodType(reload, page)
    page._snapshot_for_storage = MethodType(snapshot_for_storage, page)


class _FakeContext:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self.page = _FakeNativePage(self)
        self._cookies: list[dict[str, object]] = []

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self._cookies = list(cookies)
        self._events.append(("add_cookies", list(cookies)))

    def new_page(self) -> _FakeNativePage:
        self._events.append("new_page")
        if self._fail_new_page:
            raise RuntimeError("new page failed")
        return self.page

    def cookies(self) -> list[dict[str, object]]:
        return list(self._cookies)

    def close(self) -> None:
        self._events.append("context_close")


class _FakeBrowser:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
    ) -> None:
        self._events = events
        self.context = _FakeContext(events, fail_new_page=fail_new_page)

    def new_context(self) -> _FakeContext:
        self._events.append("new_context")
        return self.context

    def close(self) -> None:
        self._events.append("browser_close")


class _FakeBrowserType:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page

    def launch(self, *, headless: bool) -> _FakeBrowser:
        self._events.append(("launch", headless))
        return _FakeBrowser(self._events, fail_new_page=self._fail_new_page)


class _FakePlaywright:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
    ) -> None:
        self.chromium = _FakeBrowserType(events, fail_new_page=fail_new_page)


class _FakeManager:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page

    def __enter__(self) -> _FakePlaywright:
        self._events.append("playwright_enter")
        return _FakePlaywright(self._events, fail_new_page=self._fail_new_page)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._events.append("playwright_exit")
        return False


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    *,
    fail_new_page: bool = False,
) -> None:
    def attach_live_page(
        self: journey_playwright.JourneyPlaywrightPage,
        native_page: object,
        *,
        fallback_snapshot: object,
    ) -> None:
        _attach_fake_live_page(
            self,
            native_page,
            events=events,
            fallback_snapshot=fallback_snapshot,
        )

    monkeypatch.setattr(
        journey_playwright,
        "sync_playwright",
        lambda: _FakeManager(events, fail_new_page=fail_new_page),
    )
    monkeypatch.setattr(
        journey_playwright.JourneyPlaywrightPage,
        "_attach_live_page",
        attach_live_page,
    )


def test_journey_playwright_page_round_trips_rehydration_payload(tmp_path: Path):
    assert issubclass(journey_playwright.JourneyPlaywrightPage, PlaywrightPage)

    state = _state_payload(
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
    page = journey_playwright.JourneyPlaywrightPage.__restore__(
        state,
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

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

    assert isinstance(payload, dict)
    assert isinstance(restored, journey_playwright.JourneyPlaywrightPage)
    assert restored.url == "http://example.test/dashboard"
    assert restored.__store__(
        journey_sdk.JourneyStoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        )
    ) == state


def test_open_page_opens_url_string_and_cleans_returned_page(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def open_login() -> journey_playwright.JourneyPlaywrightPage:
        page = journey_playwright.open_page("http://example.test/login")
        assert isinstance(page, journey_playwright.JourneyPlaywrightPage)
        events.append("inside")
        return page

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey)

    assert events == [
        "playwright_enter",
        ("launch", True),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "inside",
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_rehydrates_in_expected_order_and_cleans_nested_page(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    saved_page = journey_playwright.JourneyPlaywrightPage.__restore__(
        _state_payload(
            cookies=[
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            local_storage={"journey_session_token": "demo-token"},
        ),
        journey_sdk.JourneyRestoreContext(
            artifact_root=Path("."),
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    def open_dashboard() -> dict[str, object]:
        page = journey_playwright.open_page(saved_page, headless=False)
        assert isinstance(page, journey_playwright.JourneyPlaywrightPage)
        events.append("inside")
        return {"page": page}

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


def test_open_page_cleans_partial_allocations_on_failure(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events, fail_new_page=True)

    def open_fails() -> bool:
        journey_playwright.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "new page failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True),
        "new_context",
        "new_page",
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_rejects_outside_step():
    with pytest.raises(InvalidBranchUsageError):
        journey_playwright.open_page("http://example.test/login")


def test_open_page_rejects_unsupported_input_type():
    with pytest.raises(TypeError, match="URL string or JourneyPlaywrightPage"):
        journey_playwright.open_page(object())


def test_journey_playwright_page_rejects_legacy_json_payload(tmp_path: Path):
    with pytest.raises(TypeError, match="dictionary payload"):
        journey_playwright.JourneyPlaywrightPage.__restore__(
            '{"url":"http://example.test"}',
            journey_sdk.JourneyRestoreContext(
                artifact_root=tmp_path,
                boundary_kind="binding",
                boundary_id="step:n_1",
            ),
        )


def test_execute_resume_rehydrates_saved_journey_playwright_page(tmp_path, monkeypatch):
    state_file = tmp_path / "journey.state"
    attempts = {"count": 0}
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def login() -> journey_playwright.JourneyPlaywrightPage:
        page = journey_playwright.open_page("http://example.test/login")
        page.goto("http://example.test/dashboard", wait_until="load")
        return page

    def continue_from_page(
        saved_page: journey_playwright.JourneyPlaywrightPage,
    ) -> journey_playwright.JourneyPlaywrightPage:
        attempts["count"] += 1
        events.append(f"continue:{attempts['count']}:{saved_page.url}")
        if attempts["count"] == 1:
            raise KeyboardInterrupt()
        return journey_playwright.open_page(saved_page)

    def assert_page(
        saved_page: journey_playwright.JourneyPlaywrightPage,
    ) -> journey_playwright.JourneyPlaywrightPage:
        page = journey_playwright.open_page(saved_page)
        events.append(f"assert:{page.url}")
        return page

    def journey():
        page = journey_sdk.step(login)
        continued = journey_sdk.step(continue_from_page, page)
        journey_sdk.step(assert_page, continued)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert [
        record.label
        for record in report.case_reports[0].records
        if record.label is not None
    ] == [
        "login",
        "continue_from_page",
        "assert_page",
    ]
    assert "continue:1:http://example.test/dashboard" in events
    assert "continue:2:http://example.test/dashboard" in events
    assert "assert:http://example.test/dashboard" in events
