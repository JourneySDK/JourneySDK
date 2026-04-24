from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
from types import MethodType

import journeysdk as journey_sdk
import pytest
from journeysdk.errors import CallableExecutionError, InvalidBranchUsageError

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page as PlaywrightPage

from journeysdk.tools import _playwright_prompt as journey_playwright_prompt
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
        executable_path: str | None = None,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self.executable_path = executable_path

    def launch(self, *, headless: bool) -> _FakeBrowser:
        self._events.append(("launch", headless))
        return _FakeBrowser(self._events, fail_new_page=self._fail_new_page)


class _FakePlaywright:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        executable_path: str | None = None,
    ) -> None:
        self.chromium = _FakeBrowserType(
            events,
            fail_new_page=fail_new_page,
            executable_path=executable_path,
        )


class _FakeManager:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        executable_path: str | None = None,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._executable_path = executable_path

    def __enter__(self) -> _FakePlaywright:
        self._events.append("playwright_enter")
        return _FakePlaywright(
            self._events,
            fail_new_page=self._fail_new_page,
            executable_path=self._executable_path,
        )

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._events.append("playwright_exit")
        return False


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    *,
    fail_new_page: bool = False,
    executable_path: str | None = None,
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
        lambda: _FakeManager(
            events,
            fail_new_page=fail_new_page,
            executable_path=executable_path,
        ),
    )
    monkeypatch.setattr(
        journey_playwright.JourneyPlaywrightPage,
        "_attach_live_page",
        attach_live_page,
    )


class _FakePromptContext:
    def __init__(self) -> None:
        self.pages: list[journey_playwright.JourneyPlaywrightPage] = []


class _FakePromptLocator:
    def __init__(
        self,
        page: journey_playwright.JourneyPlaywrightPage,
        selector: str,
        *,
        events: list[object],
    ) -> None:
        self._page = page
        self._selector = selector
        self._events = events

    @property
    def first(self) -> _FakePromptLocator:
        return self

    def click(self, *, timeout: int) -> None:
        self._events.append(("prompt_click", self._page._fake_prompt_title, self._selector, timeout))
        handler = self._page._fake_prompt_click_handlers.get(self._selector)
        if handler is None:
            raise AssertionError(f"No click handler registered for {self._selector!r}")
        handler()

    def fill(self, value: str, *, timeout: int) -> None:
        self._events.append(
            ("prompt_fill", self._page._fake_prompt_title, self._selector, value, timeout)
        )
        self._page._fake_prompt_field_values[self._selector] = value

    def press(self, value: str, *, timeout: int) -> None:
        self._events.append(
            ("prompt_press", self._page._fake_prompt_title, self._selector, value, timeout)
        )

    def wait_for(self, *, state: str, timeout: int) -> None:
        self._events.append(
            ("prompt_wait_for", self._page._fake_prompt_title, self._selector, state, timeout)
        )
        if self._selector.startswith("text="):
            assert self._selector.removeprefix("text=") in self._page._fake_prompt_visible_texts


class _FakeCompletion:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if not self._responses:
            raise AssertionError("No fake LLM responses remaining.")
        return {
            "choices": [
                {
                    "message": {
                        "content": self._responses.pop(0),
                    }
                }
            ]
        }


def _prompt_element(
    selector: str,
    *,
    name: str,
    role: str = "button",
    text: str | None = None,
    tag_name: str = "button",
    element_type: str = "",
    placeholder: str = "",
) -> dict[str, str]:
    return {
        "selector": selector,
        "role": role,
        "name": name,
        "text": text or name,
        "tag_name": tag_name,
        "type": element_type,
        "placeholder": placeholder,
    }


def _make_prompt_page(
    *,
    title: str,
    url: str,
    context: _FakePromptContext,
    events: list[object],
    elements: list[dict[str, str]] | None = None,
    visible_texts: set[str] | None = None,
    click_handlers: dict[str, Callable[[], None]] | None = None,
) -> journey_playwright.JourneyPlaywrightPage:
    page = journey_playwright.JourneyPlaywrightPage._from_snapshot(
        journey_playwright._PageSnapshot.from_payload(_state_payload(url=url))
    )
    page._journey_snapshot = journey_playwright._PageSnapshot.from_payload(_state_payload(url=url))
    page._journey_step_closed = False
    page._impl_obj = _FakePageImpl(page)
    page._journey_prompt_context = context
    page._fake_url = url
    page._fake_prompt_title = title
    page._fake_prompt_elements = list(elements or [])
    page._fake_prompt_visible_texts = set(visible_texts or set())
    page._fake_prompt_click_handlers = dict(click_handlers or {})
    page._fake_prompt_field_values = {}

    def title_method(self) -> str:
        return self._fake_prompt_title

    def screenshot(self, *, type: str) -> bytes:
        assert type == "png"
        events.append(("prompt_screenshot", self._fake_prompt_title))
        return f"png:{self._fake_prompt_title}".encode("utf-8")

    def evaluate(self, script: str, items: object | None = None) -> object:
        if "const MAX_ELEMENTS = 25;" in script:
            events.append(("prompt_collect_elements", self._fake_prompt_title))
            return [dict(element) for element in self._fake_prompt_elements]
        if items is None and "window.localStorage" in script:
            return {}
        if items is not None and "window.localStorage.clear" in script:
            return None
        raise AssertionError(f"Unexpected evaluate() script: {script!r}")

    def locator(self, selector: str) -> _FakePromptLocator:
        return _FakePromptLocator(self, selector, events=events)

    def get_by_text(self, text: str) -> _FakePromptLocator:
        return _FakePromptLocator(self, f"text={text}", events=events)

    def wait_for_url(self, target: str, *, timeout: int) -> None:
        events.append(("prompt_wait_for_url", self._fake_prompt_title, target, timeout))
        assert target in {self._fake_url, "**/dashboard"}

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        events.append(("prompt_wait_for_load_state", self._fake_prompt_title, state, timeout))

    page.title = MethodType(title_method, page)
    page.screenshot = MethodType(screenshot, page)
    page.evaluate = MethodType(evaluate, page)
    page.locator = MethodType(locator, page)
    page.get_by_text = MethodType(get_by_text, page)
    page.wait_for_url = MethodType(wait_for_url, page)
    page.wait_for_load_state = MethodType(wait_for_load_state, page)
    return page


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


def test_open_page_installs_missing_browser_automatically(monkeypatch, tmp_path: Path):
    events: list[object] = []
    executable_path = tmp_path / "ms-playwright" / "chromium"
    _install_fake_playwright(
        monkeypatch,
        events,
        executable_path=str(executable_path),
    )
    install_commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        install_commands.append(command)
        executable_path.parent.mkdir(parents=True, exist_ok=True)
        executable_path.write_text("installed", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(journey_playwright.subprocess, "run", fake_run)

    def open_login() -> journey_playwright.JourneyPlaywrightPage:
        return journey_playwright.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey)

    assert install_commands == [
        [sys.executable, "-m", "playwright", "install", "chromium"]
    ]
    assert events == [
        "playwright_enter",
        ("launch", True),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        ("capture_state", "http://example.test/login"),
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


def test_ensure_browser_installed_reports_automatic_install_failure(
    monkeypatch,
    tmp_path: Path,
):
    events: list[object] = []
    executable_path = tmp_path / "ms-playwright" / "chromium"
    _install_fake_playwright(
        monkeypatch,
        events,
        executable_path=str(executable_path),
    )

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(journey_playwright.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="could not automatically install"):
        journey_playwright.ensure_browser_installed()

    assert events == [
        "playwright_enter",
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


def test_journey_playwright_prompt_rejects_blank_instruction(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)

    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_litellm_completion",
        lambda: _FakeCompletion(['{"action":"finish","target":"","value":"done"}']),
    )

    with pytest.raises(ValueError, match="non-blank instruction"):
        page.prompt("   ", model="openai/gpt-4.1-mini")


def test_journey_playwright_prompt_rejects_saved_page(tmp_path: Path):
    saved_page = journey_playwright.JourneyPlaywrightPage.__restore__(
        _state_payload(url="http://example.test/login"),
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    with pytest.raises(RuntimeError, match="Call open_page\\(saved_page\\) first"):
        saved_page.prompt("click sign in", model="openai/gpt-4.1-mini")


def test_journey_playwright_prompt_requires_model_or_env(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    monkeypatch.delenv(journey_playwright_prompt.JOURNEY_PLAYWRIGHT_PROMPT_MODEL_ENV, raising=False)

    with pytest.raises(RuntimeError, match="requires model=..."):
        page.prompt("click sign in")


def test_journey_playwright_prompt_reports_broken_litellm_install(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)

    original_import_module = journey_playwright_prompt.importlib.import_module

    def fail_import(name: str):
        if name == "litellm":
            raise ImportError("missing litellm")
        return original_import_module(name)

    monkeypatch.setattr(journey_playwright_prompt.importlib, "import_module", fail_import)

    with pytest.raises(RuntimeError, match="installation is incomplete or broken"):
        page.prompt("click sign in", model="openai/gpt-4.1-mini")


def test_journey_playwright_prompt_clicks_popup_and_returns_structured_result(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    popup_page: journey_playwright.JourneyPlaywrightPage | None = None

    def open_popup() -> None:
        nonlocal popup_page
        if popup_page is None:
            popup_page = _make_prompt_page(
                title="Welcome popup",
                url="http://example.test/sign-in-popup",
                context=context,
                events=events,
                visible_texts={"Welcome popup"},
            )
            context.pages.append(popup_page)

    page = _make_prompt_page(
        title="Login page",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[
            _prompt_element("#sign-in", name="Sign in"),
        ],
        click_handlers={"#sign-in": open_popup},
    )
    context.pages.append(page)

    fake_completion = _FakeCompletion(
        [
            '{"action":"click","target":"e1","value":null}',
            '{"action":"switch_page","target":"1","value":null}',
            '{"action":"finish","target":"","value":"The opened popup title is Welcome popup."}',
        ]
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_litellm_completion",
        lambda: fake_completion,
    )

    result = page.prompt(
        'click on a "Sign in" button and get the title of the opened popup',
        model="anthropic/claude-sonnet-4-5",
    )

    assert result.text == "The opened popup title is Welcome popup."
    assert result.model == "anthropic/claude-sonnet-4-5"
    assert result.active_page_index == 1
    assert result.pages == (
        journey_playwright.JourneyPlaywrightPromptPage(
            index=0,
            url="http://example.test/login",
            title="Login page",
            is_original=True,
        ),
        journey_playwright.JourneyPlaywrightPromptPage(
            index=1,
            url="http://example.test/sign-in-popup",
            title="Welcome popup",
            is_original=False,
        ),
    )
    assert result.steps == (
        journey_playwright.JourneyPlaywrightPromptStep(
            index=1,
            page_index=0,
            action="click",
            target="e1",
            status="ok",
            detail="Clicked e1 Sign in.",
        ),
        journey_playwright.JourneyPlaywrightPromptStep(
            index=2,
            page_index=1,
            action="switch_page",
            target="1",
            status="ok",
            detail="Switched to page 1 (Welcome popup).",
        ),
        journey_playwright.JourneyPlaywrightPromptStep(
            index=3,
            page_index=1,
            action="finish",
            target="",
            status="ok",
            detail="The opened popup title is Welcome popup.",
        ),
    )
    assert fake_completion.calls[0]["model"] == "anthropic/claude-sonnet-4-5"
    assert (
        fake_completion.calls[1]["messages"][1]["content"][0]["text"].find('"title": "Welcome popup"')
        != -1
    )
    assert events == [
        ("prompt_collect_elements", "Login page"),
        ("prompt_screenshot", "Login page"),
        ("prompt_collect_elements", "Login page"),
        ("prompt_click", "Login page", "#sign-in", 5000),
        ("prompt_wait_for_load_state", "Welcome popup", "load", 5000),
        ("prompt_collect_elements", "Login page"),
        ("prompt_collect_elements", "Welcome popup"),
        ("prompt_screenshot", "Login page"),
        ("prompt_collect_elements", "Login page"),
        ("prompt_collect_elements", "Welcome popup"),
        ("prompt_screenshot", "Welcome popup"),
    ]


def test_journey_playwright_prompt_enforces_max_steps(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[
            _prompt_element("#sign-in", name="Sign in"),
        ],
        click_handlers={"#sign-in": lambda: None},
    )
    context.pages.append(page)
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_litellm_completion",
        lambda: _FakeCompletion(['{"action":"click","target":"e1","value":null}']),
    )

    with pytest.raises(RuntimeError, match="reached max_steps=1"):
        page.prompt("click sign in", model="openai/gpt-4.1-mini", max_steps=1)


def test_journey_playwright_prompt_rejects_invalid_model_json(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_litellm_completion",
        lambda: _FakeCompletion(["not json"]),
    )

    with pytest.raises(RuntimeError, match="return one JSON action"):
        page.prompt("click sign in", model="openai/gpt-4.1-mini")


def test_journey_playwright_prompt_rejects_unsupported_action(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_litellm_completion",
        lambda: _FakeCompletion(['{"action":"hover","target":"e1","value":null}']),
    )

    with pytest.raises(RuntimeError, match="unsupported action 'hover'"):
        page.prompt("click sign in", model="openai/gpt-4.1-mini")
