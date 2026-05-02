from __future__ import annotations

from collections.abc import Callable
import json
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


class _FakeAIMessage:
    def __init__(
        self,
        *,
        content: object = "",
        tool_calls: list[dict[str, object]] | None = None,
        invalid_tool_calls: list[object] | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.invalid_tool_calls = invalid_tool_calls or []


class _FakeBoundLangChainModel:
    def __init__(self, prompt_model: _FakeLangChainPromptModel) -> None:
        self._prompt_model = prompt_model

    def invoke(self, messages: list[object]) -> _FakeAIMessage:
        self._prompt_model.calls.append({"messages": list(messages)})
        if not self._prompt_model._responses:
            raise AssertionError("No fake LLM responses remaining.")
        self._prompt_model._response_index += 1
        response = self._prompt_model._responses.pop(0)
        if isinstance(response, str):
            return _FakeAIMessage(content=response)
        message = dict(response)
        tool_calls = message.get("tool_calls")
        normalized_tool_calls: list[dict[str, object]] = []
        if isinstance(tool_calls, list):
            for index, tool_call in enumerate(tool_calls, start=1):
                assert isinstance(tool_call, dict)
                normalized = dict(tool_call)
                normalized.setdefault(
                    "id",
                    f"fake-call-{self._prompt_model._response_index}-{index}",
                )
                normalized.setdefault("type", "tool_call")
                normalized_tool_calls.append(normalized)
        invalid_tool_calls = message.get("invalid_tool_calls")
        assert invalid_tool_calls is None or isinstance(invalid_tool_calls, list)
        return _FakeAIMessage(
            content=message.get("content", ""),
            tool_calls=normalized_tool_calls,
            invalid_tool_calls=invalid_tool_calls,
        )


class _FakeStructuredLangChainModel:
    def __init__(
        self,
        prompt_model: _FakeLangChainPromptModel,
        schema: dict[str, object],
        method: str | None,
    ) -> None:
        self._prompt_model = prompt_model
        self._schema = schema
        self._method = method

    def invoke(self, messages: list[object]) -> object:
        self._prompt_model.structured_calls.append(
            {
                "messages": list(messages),
                "schema": self._schema,
                "method": self._method,
            }
        )
        if not self._prompt_model._structured_responses:
            raise AssertionError("No fake structured LLM responses remaining.")
        return self._prompt_model._structured_responses.pop(0)


class _FakeLangChainPromptModel:
    def __init__(
        self,
        responses: list[str | dict[str, object]],
        *,
        structured_responses: list[object] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._structured_responses = list(structured_responses or [])
        self.calls: list[dict[str, object]] = []
        self.structured_calls: list[dict[str, object]] = []
        self._response_index = 0
        self.base = self
        self.agent = _FakeBoundLangChainModel(self)

    def with_structured_output(
        self,
        schema: dict[str, object],
        *,
        method: str | None = None,
    ) -> _FakeStructuredLangChainModel:
        return _FakeStructuredLangChainModel(self, schema, method)

    def add_structured_responses(self, responses: list[object]) -> None:
        self._structured_responses.extend(responses)


def _prompt_tool_call(
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "content": "",
        "tool_calls": [
            {
                "name": name,
                "args": arguments,
            }
        ],
    }


def _run_code(code: str) -> dict[str, object]:
    return _prompt_tool_call(
        "journey_run_code",
        {"code": code},
    )


def _fail_session(reason: str) -> dict[str, object]:
    return _prompt_tool_call(
        "journey_fail_session",
        {"reason": reason},
    )


def _prompt_element(
    selector: str,
    *,
    name: str,
    role: str = "button",
    text: str | None = None,
    tag_name: str = "button",
    element_type: str = "",
    placeholder: str = "",
    actions: list[str] | None = None,
) -> dict[str, object]:
    if actions is None:
        actions = ["click", "press"]
        if tag_name in {"input", "select", "textarea"} or role in {
            "combobox",
            "searchbox",
            "spinbutton",
            "textbox",
        }:
            actions = ["click", "fill", "press"]
    return {
        "selector": selector,
        "role": role,
        "name": name,
        "text": text or name,
        "tag_name": tag_name,
        "type": element_type,
        "placeholder": placeholder,
        "actions": actions,
    }


def _prompt_html(
    *,
    title: str,
    elements: list[dict[str, object]],
    visible_texts: set[str],
) -> str:
    rendered_elements: list[str] = []
    for element in elements:
        selector = element["selector"]
        if not isinstance(selector, str) or not selector.startswith("#"):
            continue
        element_id = selector.removeprefix("#")
        tag_name = element.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name:
            tag_name = "button"
        role = element.get("role")
        role_attr = f' role="{role}"' if isinstance(role, str) and role else ""
        name = element.get("name")
        text = str(name) if isinstance(name, str) else ""
        if tag_name == "input":
            rendered_elements.append(f'<input id="{element_id}" aria-label="{text}" />')
        else:
            rendered_elements.append(f'<{tag_name} id="{element_id}"{role_attr}>{text}</{tag_name}>')
    rendered_elements.extend(f"<p>{text}</p>" for text in sorted(visible_texts))
    body = "".join(rendered_elements)
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _prompt_visible_text(
    *,
    elements: list[dict[str, object]],
    visible_texts: set[str],
) -> str:
    values: list[str] = []
    for element in elements:
        name = element.get("name")
        if isinstance(name, str) and name:
            values.append(name)
    values.extend(sorted(visible_texts))
    return "\n".join(values)


def _make_prompt_page(
    *,
    title: str,
    url: str,
    context: _FakePromptContext,
    events: list[object],
    elements: list[dict[str, object]] | None = None,
    visible_texts: set[str] | None = None,
    click_handlers: dict[str, Callable[[], None]] | None = None,
    html: str | None = None,
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
    page._fake_prompt_html = html or _prompt_html(
        title=title,
        elements=page._fake_prompt_elements,
        visible_texts=page._fake_prompt_visible_texts,
    )

    def title_method(self) -> str:
        return self._fake_prompt_title

    def screenshot(self, *, type: str) -> bytes:
        assert type == "png"
        events.append(("prompt_screenshot", self._fake_prompt_title))
        return f"png:{self._fake_prompt_title}".encode("utf-8")

    def evaluate(self, script: str, items: object | None = None) -> object:
        if "document.documentElement.outerHTML" in script:
            events.append(("prompt_rendered_html", self._fake_prompt_title))
            return self._fake_prompt_html
        if "document.body.innerText" in script:
            return _prompt_visible_text(
                elements=self._fake_prompt_elements,
                visible_texts=self._fake_prompt_visible_texts,
            )
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

    def wait_for_timeout(self, timeout: int) -> None:
        events.append(("prompt_wait_for_timeout", self._fake_prompt_title, timeout))

    page.title = MethodType(title_method, page)
    page.screenshot = MethodType(screenshot, page)
    page.evaluate = MethodType(evaluate, page)
    page.locator = MethodType(locator, page)
    page.get_by_text = MethodType(get_by_text, page)
    page.wait_for_url = MethodType(wait_for_url, page)
    page.wait_for_load_state = MethodType(wait_for_load_state, page)
    page.wait_for_timeout = MethodType(wait_for_timeout, page)
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


def test_open_page_opens_url_string_and_cleans_returned_page(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
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
    log_output = capsys.readouterr().err
    assert "component=playwright event=open_page_start" in log_output
    assert "component=playwright event=open_page_success" in log_output


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
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(["finish()", "done"]),
    )

    with pytest.raises(ValueError, match="non-blank instruction"):
        page.prompt("   ", model="openai:gpt-4.1-mini")


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
        saved_page.prompt("click sign in", model="openai:gpt-4.1-mini")


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


def test_journey_playwright_prompt_reports_broken_langchain_install(monkeypatch):
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
        if name == "langchain.chat_models":
            raise ImportError("missing langchain")
        return original_import_module(name)

    monkeypatch.setattr(journey_playwright_prompt.importlib, "import_module", fail_import)

    with pytest.raises(RuntimeError, match="project environment") as exc_info:
        page.prompt("click sign in", model="openai:gpt-4.1-mini")
    assert sys.executable in str(exc_info.value)


def test_journey_playwright_prompt_clicks_popup_and_returns_text(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
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

    fake_model = _FakeLangChainPromptModel(
        [
            _run_code('page.locator("#sign-in").click(timeout=timeout_ms)'),
            _run_code("switch_page(1)"),
            "The opened popup title is Welcome popup.",
        ]
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt(
        'click on a "Sign in" button and get the title of the opened popup',
        model="anthropic:claude-sonnet-4-5",
    )
    log_output = capsys.readouterr().err

    assert result.output == "The opened popup title is Welcome popup."
    assert result.model == "anthropic:claude-sonnet-4-5"
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
            action="python",
            target='page.locator("#sign-in").click(timeout=timeout_ms)',
            status="ok",
            detail="Executed Python snippet. Active page index is 0.",
        ),
        journey_playwright.JourneyPlaywrightPromptStep(
            index=2,
            page_index=1,
            action="python",
            target="switch_page(1)",
            status="ok",
            detail="Executed Python snippet. Active page index is 1.",
        ),
        journey_playwright.JourneyPlaywrightPromptStep(
            index=3,
            page_index=1,
            action="finish",
            target="",
            status="ok",
            detail="Prompt marked complete.",
        ),
    )
    assert len(fake_model.calls) == 3
    assert all(
        "Return whether the original browser task is completed."
        not in call["messages"][-1]["content"][0]["text"]
        for call in fake_model.calls
    )
    assert not fake_model.structured_calls
    assert not hasattr(journey_playwright_prompt, "_COLLECT_ELEMENTS_SCRIPT")
    first_prompt_text = fake_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "<journey-rendered-html>" in first_prompt_text
    assert '<button id="sign-in" role="button">Sign in</button>' in first_prompt_text
    assert '"actions": [' not in first_prompt_text
    assert '"id": "e1"' not in first_prompt_text
    assert (
        fake_model.calls[1]["messages"][-1]["content"][0]["text"].find('"title": "Welcome popup"')
        != -1
    )
    final_prompt_text = fake_model.calls[2]["messages"][-1]["content"][0]["text"]
    assert "Executed steps JSON:" in final_prompt_text
    assert '"action": "python"' in final_prompt_text
    assert "return the final answer as plain text" in final_prompt_text
    assert events == [
        ("prompt_rendered_html", "Login page"),
        ("prompt_screenshot", "Login page"),
        ("prompt_click", "Login page", "#sign-in", 5000),
        ("prompt_wait_for_load_state", "Welcome popup", "load", 5000),
        ("prompt_rendered_html", "Login page"),
        ("prompt_screenshot", "Login page"),
        ("prompt_rendered_html", "Welcome popup"),
        ("prompt_screenshot", "Welcome popup"),
        ("prompt_wait_for_load_state", "Welcome popup", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Welcome popup", 500),
        ("prompt_rendered_html", "Welcome popup"),
        ("prompt_screenshot", "Welcome popup"),
    ]
    assert "[journey]" in log_output
    assert "component=playwright-prompt event=prompt_start" in log_output
    assert 'click on a \\"Sign in\\" button and get the title' in log_output
    assert "model='anthropic:claude-sonnet-4-5'" in log_output
    assert "active=page 0 'Login page' at http://example.test/login" in log_output
    assert "step 1/15: inspecting page 0 'Login page'" in log_output
    assert "step 1/15: AI will click selector '#sign-in'" in log_output
    assert "event=prompt_code" in log_output
    assert 'page.locator(\\"#sign-in\\").click(timeout=timeout_ms)' in log_output
    assert "discovered page 1 'Welcome popup' at http://example.test/sign-in-popup" in log_output
    assert "active page changed to page 1 'Welcome popup'" in log_output
    assert (
        "step 3/15: finished with output: The opened popup title is Welcome popup."
        in log_output
    )


def test_journey_playwright_prompt_returns_structured_output(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Welcome popup",
        url="http://example.test/sign-in-popup",
        context=context,
        events=events,
        visible_texts={"Welcome popup"},
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        ["The popup summary is ready."],
        structured_responses=[
            {"popup_title": "Welcome popup", "has_welcome_text": True},
        ],
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt(
        "summarize the popup",
        model="openai:gpt-4.1-mini",
        output={
            "popup_title": "The popup title.",
            "has_welcome_text": {
                "type": "boolean",
                "description": "Whether the popup says welcome.",
            },
        },
    )

    assert result.output == {
        "popup_title": "Welcome popup",
        "has_welcome_text": True,
    }
    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    agent_call = fake_model.calls[0]
    assert fake_model.structured_calls[0]["method"] == "json_schema"
    assert fake_model.structured_calls[0]["schema"] == {
        "title": "journey_prompt_output",
        "description": "Structured output for JourneyPlaywrightPage.prompt(...).",
        "type": "object",
        "properties": {
            "popup_title": {
                "type": "string",
                "description": "The popup title.",
            },
            "has_welcome_text": {
                "type": "boolean",
                "description": "Whether the popup says welcome.",
            },
        },
        "required": ["popup_title", "has_welcome_text"],
        "additionalProperties": False,
    }
    final_prompt_text = agent_call["messages"][1]["content"][0]["text"]
    assert "return the final answer using these output fields JSON" in final_prompt_text
    assert "popup_title" in final_prompt_text
    assert "has_welcome_text" in final_prompt_text


def test_journey_playwright_prompt_final_output_includes_visible_error_text(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        visible_texts={
            "Password is incorrect. Try again, or use another method.",
        },
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        ["The visible error is ready."],
        structured_responses=[
            {"error": ""},
        ],
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt(
        "report the visible sign-in error",
        model="openai:gpt-4.1-mini",
        output={"error": "An error message if found."},
    )

    assert result.output == {
        "error": "Password is incorrect. Try again, or use another method.",
    }
    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    final_prompt_text = fake_model.structured_calls[0]["messages"][1]["content"][0]["text"]
    assert "<journey-visible-text>" in final_prompt_text
    assert "Password is incorrect. Try again, or use another method." in final_prompt_text
    assert (
        "Do not return an empty string for such a field"
        in fake_model.calls[0]["messages"][0]["content"]
    )
    assert ("prompt_wait_for_load_state", "Login", "networkidle", 2000) in events
    assert ("prompt_wait_for_timeout", "Login", 500) in events


def test_journey_playwright_prompt_finish_with_blocking_error_raises(
    monkeypatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)
    reason = (
        "Your account is locked. You will be able to try again in 59 minutes. "
        "For more information, please contact alfie@heyalfie.com."
    )
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        visible_texts={reason},
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        [
            _fail_session(reason),
        ]
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    with pytest.raises(RuntimeError, match="Your account is locked"):
        page.prompt(
            'Sign in as e2etest@heyalfie.com using password "1111"',
            model="openai:gpt-4.1-mini",
            memory="sign-in",
        )
    log_output = capsys.readouterr().err

    assert len(fake_model.calls) == 1
    prompt_text = fake_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "<journey-visible-text>" in prompt_text
    assert reason in prompt_text
    assert "event=prompt_failed" in log_output
    assert not (tmp_path / "sign-in.memory.json").exists()


def test_journey_playwright_prompt_fail_action_raises_without_final_output(
    monkeypatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)
    reason = "Your account is locked. Try again later."
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        visible_texts={reason},
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel([_fail_session(reason)])
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    with pytest.raises(RuntimeError, match="Your account is locked"):
        page.prompt(
            "sign in",
            model="openai:gpt-4.1-mini",
            memory="sign-in",
        )
    log_output = capsys.readouterr().err

    assert len(fake_model.calls) == 1
    assert not fake_model.structured_calls
    assert "event=prompt_failed" in log_output
    assert not (tmp_path / "sign-in.memory.json").exists()


def test_journey_playwright_prompt_rejects_invalid_output_specs(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Welcome popup",
        url="http://example.test/sign-in-popup",
        context=context,
        events=events,
    )
    context.pages.append(page)

    def fail_load_completion() -> object:
        raise AssertionError("output validation should happen before model calls")

    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        fail_load_completion,
    )

    with pytest.raises(ValueError, match="at least one field"):
        page.prompt("summarize", model="openai:gpt-4.1-mini", output={})
    with pytest.raises(ValueError, match="description must be non-empty"):
        page.prompt("summarize", model="openai:gpt-4.1-mini", output={"title": ""})
    with pytest.raises(TypeError, match="JSON-serializable"):
        page.prompt(
            "summarize",
            model="openai:gpt-4.1-mini",
            output={"title": {"type": object()}},
        )


def test_journey_playwright_prompt_rejects_malformed_structured_output(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Welcome popup",
        url="http://example.test/sign-in-popup",
        context=context,
        events=events,
    )
    context.pages.append(page)
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            ["The popup summary is ready."],
            structured_responses=[{"popup_title": "Welcome", "extra": True}],
        ),
    )

    with pytest.raises(RuntimeError, match="unexpected fields"):
        page.prompt(
            "summarize",
            model="openai:gpt-4.1-mini",
            output={"popup_title": "The popup title."},
        )


def test_journey_playwright_prompt_retries_rejected_python(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Chat",
        url="http://example.test/chat",
        context=context,
        events=events,
        elements=[
            _prompt_element("#attach", name="Attach file"),
            _prompt_element(
                "#composer",
                name="Message",
                role="textbox",
                tag_name="div",
                text="",
            ),
        ],
    )
    context.pages.append(page)

    fake_model = _FakeLangChainPromptModel(
        [
            _run_code('page.locator("#attach").click(timeout=timeout_ms)'),
            _run_code(
                'page.locator("#composer").fill("I need to fix a toilet", timeout=timeout_ms)'
            ),
            "Started the chat.",
        ]
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt("say you need to fix a toilet", model="openai:gpt-4.1-mini")
    log_output = capsys.readouterr().err

    assert result.output == "Started the chat."
    assert result.steps[0] == journey_playwright.JourneyPlaywrightPromptStep(
        index=1,
        page_index=0,
        action="python",
        target='page.locator("#attach").click(timeout=timeout_ms)',
        status="rejected",
        detail="AssertionError: No click handler registered for '#attach'",
    )
    assert result.steps[1] == journey_playwright.JourneyPlaywrightPromptStep(
        index=2,
        page_index=0,
        action="python",
        target='page.locator("#composer").fill("I need to fix a toilet", timeout=timeout_ms)',
        status="ok",
        detail="Executed Python snippet. Active page index is 0.",
    )
    assert page._fake_prompt_field_values == {"#composer": "I need to fix a toilet"}
    assert '<div id="composer" role="textbox">Message</div>' in fake_model.calls[0][
        "messages"
    ][1]["content"][0]["text"]
    assert '"status": "rejected"' in fake_model.calls[1]["messages"][-1]["content"][0]["text"]
    assert events == [
        ("prompt_rendered_html", "Chat"),
        ("prompt_screenshot", "Chat"),
        ("prompt_click", "Chat", "#attach", 5000),
        ("prompt_rendered_html", "Chat"),
        ("prompt_screenshot", "Chat"),
        ("prompt_fill", "Chat", "#composer", "I need to fix a toilet", 5000),
        ("prompt_rendered_html", "Chat"),
        ("prompt_screenshot", "Chat"),
        ("prompt_wait_for_load_state", "Chat", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Chat", 500),
        ("prompt_rendered_html", "Chat"),
        ("prompt_screenshot", "Chat"),
    ]
    assert "step 1/15: AI will click selector '#attach'" in log_output
    assert 'page.locator(\\"#attach\\").click(timeout=timeout_ms)' in log_output
    assert "step 1/15: rejected on page 0 'Chat'" in log_output
    assert "AssertionError: No click handler registered for '#attach'" in log_output
    assert (
        "step 2/15: AI will fill selector '#composer' with "
        "'I need to fix a toilet'"
    ) in log_output
    assert (
        'page.locator(\\"#composer\\").fill'
        '(\\"I need to fix a toilet\\", timeout=timeout_ms)'
    ) in log_output


def test_journey_playwright_prompt_writes_and_reuses_named_memory(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Chat",
        url="http://example.test/chat?session=secret#composer",
        context=context,
        events=events,
        elements=[
            _prompt_element("#attach", name="Attach file"),
            _prompt_element(
                "#composer",
                name="Message",
                role="textbox",
                tag_name="div",
                text="",
            ),
        ],
    )
    context.pages.append(page)

    first_model = _FakeLangChainPromptModel(
        [
            _run_code('page.locator("#attach").click(timeout=timeout_ms)'),
            _run_code('page.locator("#composer").fill("hello", timeout=timeout_ms)'),
            "Started the chat.",
        ]
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: first_model,
    )

    page.prompt(
        "say hello",
        model="openai:gpt-4.1-mini",
        memory="chat-start",
    )

    memory_path = tmp_path / "chat-start.memory.json"
    memory_payload = json.loads(memory_path.read_text(encoding="utf-8"))
    serialized = json.dumps(memory_payload)
    assert memory_payload["version"] == 1
    assert "rendered-html" not in serialized
    assert "data:image" not in serialized
    assert "png:Chat" not in serialized
    assert '<div id="composer"' not in serialized
    entry = next(iter(memory_payload["entries"].values()))
    assert entry["final_output"] == "Started the chat."
    assert entry["page_signature"] == (
        '{"title":"Chat","url":"http://example.test/chat"}'
    )
    assert entry["successful_steps"][0]["target"] == (
        'page.locator("#composer").fill("hello", timeout=timeout_ms)'
    )
    assert entry["rejected_steps"][0]["target"] == (
        'page.locator("#attach").click(timeout=timeout_ms)'
    )

    second_model = _FakeLangChainPromptModel(["Done from memory."])
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: second_model,
    )
    page.prompt(
        "say hello",
        model="openai:gpt-4.1-mini",
        memory="chat-start",
    )
    second_prompt_text = second_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Prompt memory JSON:" in second_prompt_text
    assert "#attach" in second_prompt_text
    assert "#composer" in second_prompt_text
    assert "hello" in second_prompt_text

    third_model = _FakeLangChainPromptModel(["Done without memory."])
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model: third_model,
    )
    page.prompt(
        "say goodbye",
        model="openai:gpt-4.1-mini",
        memory="chat-start",
    )
    third_prompt_text = third_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Prompt memory JSON:" not in third_prompt_text


def test_journey_playwright_prompt_respects_execute_no_memory(monkeypatch):
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
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(["Done."]),
    )

    def fail_memory_access(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("prompt memory should be disabled")

    monkeypatch.setattr(
        journey_playwright_prompt,
        "load_prompt_memory_entry",
        fail_memory_access,
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "write_prompt_memory_entry",
        fail_memory_access,
    )

    def run_prompt() -> journey_playwright.JourneyPlaywrightPromptResult:
        return page.prompt("finish", model="openai:gpt-4.1-mini", memory="disabled")

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    journey_sdk.execute(memory_journey, no_memory=True)


def test_journey_playwright_prompt_respects_execute_no_memory_update(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(["Done."])
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    load_calls: list[tuple[Path, str]] = []
    write_calls: list[tuple[object, ...]] = []

    def load_memory(path: Path, key: str) -> dict[str, object]:
        load_calls.append((path, key))
        return {
            "successful_steps": [
                {
                    "target": 'page.locator("#cached").click(timeout=timeout_ms)',
                    "detail": "worked before",
                }
            ]
        }

    def fail_memory_write(*args: object, **kwargs: object) -> object:
        del kwargs
        write_calls.append(args)
        raise AssertionError("prompt memory updates should be disabled")

    monkeypatch.setattr(
        journey_playwright_prompt,
        "load_prompt_memory_entry",
        load_memory,
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "write_prompt_memory_entry",
        fail_memory_write,
    )

    def run_prompt() -> journey_playwright.JourneyPlaywrightPromptResult:
        return page.prompt("finish", model="openai:gpt-4.1-mini", memory="readonly")

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    journey_sdk.execute(memory_journey, no_memory_update=True)

    prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    assert load_calls
    assert not write_calls
    assert "Prompt memory JSON:" in prompt_text
    assert "#cached" in prompt_text


def test_journey_playwright_prompt_enforces_max_steps(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
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
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            [_run_code('page.locator("#sign-in").click(timeout=timeout_ms)')]
        ),
    )

    with pytest.raises(RuntimeError, match="reached max_steps=1"):
        page.prompt("click sign in", model="openai:gpt-4.1-mini", max_steps=1)
    log_output = capsys.readouterr().err

    assert "step 1/1: AI will click selector '#sign-in'" in log_output
    assert "step 1/1: succeeded on page 0 'Login'" in log_output
    assert "prompt stopped: JourneyPlaywrightPage.prompt(...) reached max_steps=1" in log_output


def test_journey_playwright_prompt_retries_invalid_tool_arguments(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel([_run_code("   "), "Recovered."])
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    result = page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert result.output == "Recovered."
    assert result.steps[0] == journey_playwright.JourneyPlaywrightPromptStep(
        index=1,
        page_index=0,
        action="tool",
        target="journey_run_code",
        status="rejected",
        detail=(
            "ValueError: JourneyPlaywrightPage.prompt(...) "
            "journey_run_code expects a non-blank code string."
        ),
    )
    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert '"action": "tool"' in second_prompt_text
    assert '"status": "rejected"' in second_prompt_text


def test_journey_playwright_prompt_retries_invalid_python(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        [_run_code('page.locator("#sign-in"'), "Recovered."]
    )
    monkeypatch.setattr(
        journey_playwright_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    result = page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert result.output == "Recovered."
    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert '"target": "page.locator(\\"#sign-in\\""' in second_prompt_text
    assert '"status": "rejected"' in second_prompt_text
    assert "SyntaxError:" in second_prompt_text


def test_journey_playwright_prompt_rejects_json_as_python_failure(monkeypatch):
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
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            [
                _run_code('{"action":"hover","target":"e1","value":null}'),
                "Recovered.",
            ]
        ),
    )

    result = page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert result.output == "Recovered."


def test_journey_playwright_prompt_rejects_blank_finish_then_recovers(monkeypatch):
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
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel([_run_code('finish("")'), "Done."]),
    )

    result = page.prompt("finish clearly", model="openai:gpt-4.1-mini")

    assert result.output == "Done."
