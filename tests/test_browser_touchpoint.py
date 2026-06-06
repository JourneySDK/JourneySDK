from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import MethodType

import journeysdk as journey_sdk
import pytest
from journeysdk.errors import CallableExecutionError, InvalidBranchUsageError

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

from journeysdk._prompt_memory import (
    PromptMemoryEntry,
    PromptMemorySection,
    write_prompt_memory_entry,
)
from journeysdk import executor as journey_executor
from journeysdk import _prompt_engine as journey_prompt_engine
from journeysdk.logger import configure_logging
from journeysdk.touchpoints import _browser_prompt as journey_browser_prompt
from journeysdk.touchpoints import browser as journey_browser


def _prompt_memory_path(root: Path, name: str) -> Path:
    return root / f"{name}.memory.md"


def _browser_recording_root() -> Path:
    return Path(__file__).parent / ".journey" / "logs"


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
    def __init__(self, page: journey_browser.JourneyBrowserPage) -> None:
        self._page = page

    @property
    def url(self) -> str:
        return self._page._fake_url


class _FakeTracing:
    def __init__(self, events: list[object], *, record_events: bool) -> None:
        self._events = events
        self._record_events = record_events
        self.started = False

    def start(
        self,
        *,
        title: str,
        screenshots: bool,
        snapshots: bool,
        sources: bool,
    ) -> None:
        self.started = True
        if self._record_events:
            self._events.append(
                ("trace_start", title, screenshots, snapshots, sources)
            )

    def stop(self, *, path: Path) -> None:
        if self._record_events:
            self._events.append(("trace_stop", Path(path).name))
        Path(path).write_text("fake trace", encoding="utf-8")


class _FakeVideo:
    def __init__(self, events: list[object], *, record_events: bool) -> None:
        self._events = events
        self._record_events = record_events

    def save_as(self, path: Path) -> None:
        if self._record_events:
            self._events.append(("video_save", Path(path).name))
        Path(path).write_text("fake video", encoding="utf-8")


def _attach_fake_live_page(
    page: journey_browser.JourneyBrowserPage,
    native_page: object,
    *,
    events: list[object],
    fallback_snapshot: object,
    initial_url: str = "about:blank",
    goto_errors: list[BaseException] | None = None,
    reload_errors: list[BaseException] | None = None,
    fail_goto: BaseException | None = None,
    force_cleanup_before_goto_failure: bool = False,
) -> None:
    context = getattr(native_page, "context")
    pending_goto_errors = list(goto_errors or [])
    pending_reload_errors = list(reload_errors or [])
    page._journey_snapshot = fallback_snapshot
    page._journey_step_closed = False
    page._fake_context = context
    page._fake_url = initial_url
    page._fake_local_storage = {}
    page._impl_obj = _FakePageImpl(page)
    page._journey_test_video = getattr(context, "video", None)

    def goto(self, url: str, *, wait_until: str) -> None:
        if fail_goto is not None:
            events.append(("goto", url, wait_until))
            if force_cleanup_before_goto_failure:
                self._force_close_after_forced_interrupt()
            raise fail_goto
        events.append(("goto", url, wait_until))
        if pending_goto_errors:
            raise pending_goto_errors.pop(0)
        self._fake_url = url

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
        if pending_reload_errors:
            raise pending_reload_errors.pop(0)

    def snapshot_for_storage(self):
        if not self._is_live and self._journey_snapshot is not None:
            return self._journey_snapshot
        events.append(("capture_state", self._fake_url))
        self._journey_snapshot = journey_browser._PageSnapshot.from_payload(
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
        fail_close: BaseException | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._fail_close = fail_close
        self.page = _FakeNativePage(self)
        self._cookies: list[dict[str, object]] = []
        self.tracing = _FakeTracing(
            events,
            record_events=record_recording_events,
        )
        self.video: _FakeVideo | None = None
        self._record_recording_events = record_recording_events

    def configure_recording(self, *, record_video_dir: object | None = None) -> None:
        if record_video_dir is None:
            self.video = None
            return
        if self._record_recording_events:
            self._events.append(("record_video_dir", Path(record_video_dir).name))
        self.video = _FakeVideo(
            self._events,
            record_events=self._record_recording_events,
        )

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
        if self._fail_close is not None:
            raise self._fail_close


class _FakeBrowser:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_close: BaseException | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_close = fail_close
        self.context = _FakeContext(
            events,
            fail_new_page=fail_new_page,
            fail_close=fail_context_close,
            record_recording_events=record_recording_events,
        )

    def new_context(self, **kwargs: object) -> _FakeContext:
        self._events.append("new_context")
        self.context.configure_recording(
            record_video_dir=kwargs.get("record_video_dir"),
        )
        return self.context

    def close(self) -> None:
        self._events.append("browser_close")
        if self._fail_close is not None:
            raise self._fail_close


class _FakeBrowserType:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_browser_close: BaseException | None = None,
        executable_path: str | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._fail_context_close = fail_context_close
        self._fail_browser_close = fail_browser_close
        self.executable_path = executable_path
        self._record_recording_events = record_recording_events

    def launch(self, *, headless: bool, handle_sigint: bool) -> _FakeBrowser:
        self._events.append(("launch", headless, handle_sigint))
        return _FakeBrowser(
            self._events,
            fail_new_page=self._fail_new_page,
            fail_context_close=self._fail_context_close,
            fail_close=self._fail_browser_close,
            record_recording_events=self._record_recording_events,
        )


class _FakePlaywright:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_browser_close: BaseException | None = None,
        executable_path: str | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self.chromium = _FakeBrowserType(
            events,
            fail_new_page=fail_new_page,
            fail_context_close=fail_context_close,
            fail_browser_close=fail_browser_close,
            executable_path=executable_path,
            record_recording_events=record_recording_events,
        )


class _FakeManager:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_browser_close: BaseException | None = None,
        executable_path: str | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._fail_context_close = fail_context_close
        self._fail_browser_close = fail_browser_close
        self._executable_path = executable_path
        self._record_recording_events = record_recording_events

    def __enter__(self) -> _FakePlaywright:
        self._events.append("playwright_enter")
        return _FakePlaywright(
            self._events,
            fail_new_page=self._fail_new_page,
            fail_context_close=self._fail_context_close,
            fail_browser_close=self._fail_browser_close,
            executable_path=self._executable_path,
            record_recording_events=self._record_recording_events,
        )

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._events.append("playwright_exit")
        return False


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    *,
    fail_new_page: bool = False,
    fail_goto: BaseException | None = None,
    force_cleanup_before_goto_failure: bool = False,
    fail_context_close: BaseException | None = None,
    fail_browser_close: BaseException | None = None,
    executable_path: str | None = None,
    goto_errors: list[BaseException] | None = None,
    reload_errors: list[BaseException] | None = None,
    record_recording_events: bool = False,
) -> None:
    def attach_live_page(
        self: journey_browser.JourneyBrowserPage,
        native_page: object,
        *,
        fallback_snapshot: object,
    ) -> None:
        _attach_fake_live_page(
            self,
            native_page,
            events=events,
            fallback_snapshot=fallback_snapshot,
            goto_errors=goto_errors,
            reload_errors=reload_errors,
            fail_goto=fail_goto,
            force_cleanup_before_goto_failure=force_cleanup_before_goto_failure,
        )

    monkeypatch.setattr(
        journey_browser,
        "sync_playwright",
        lambda: _FakeManager(
            events,
            fail_new_page=fail_new_page,
            fail_context_close=fail_context_close,
            fail_browser_close=fail_browser_close,
            executable_path=executable_path,
            record_recording_events=record_recording_events,
        ),
    )
    monkeypatch.setattr(
        journey_browser.JourneyBrowserPage,
        "_attach_live_page",
        attach_live_page,
    )


class _FakePromptContext:
    def __init__(self) -> None:
        self.pages: list[journey_browser.JourneyBrowserPage] = []


class _FakePromptLocator:
    def __init__(
        self,
        page: journey_browser.JourneyBrowserPage,
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
        usage_metadata: dict[str, object] | None = None,
        response_metadata: dict[str, object] | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.invalid_tool_calls = invalid_tool_calls or []
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


class _FakeStructuredResponse(dict):
    def __init__(
        self,
        payload: dict[str, object],
        *,
        usage_metadata: dict[str, object],
        model_name: str,
    ) -> None:
        super().__init__(payload)
        self.usage_metadata = usage_metadata
        self.response_metadata = {"model_name": model_name}


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

    def invoke(
        self,
        messages: list[object],
        *,
        config: dict[str, object] | None = None,
    ) -> object:
        self._prompt_model.structured_calls.append(
            {
                "messages": list(messages),
                "schema": self._schema,
                "method": self._method,
            }
        )
        if not self._prompt_model._structured_responses:
            raise AssertionError("No fake structured LLM responses remaining.")
        response = self._prompt_model._structured_responses.pop(0)
        usage_metadata = self._prompt_model.next_structured_usage_metadata()
        if usage_metadata is None:
            return response
        assert isinstance(response, dict)
        return _FakeStructuredResponse(
            response,
            usage_metadata=usage_metadata,
            model_name=self._prompt_model.model_name,
        )


class _FakeLangChainPromptModel:
    def __init__(
        self,
        responses: list[str | dict[str, object]],
        *,
        structured_responses: list[object] | None = None,
        usage_metadata: list[dict[str, object] | None] | None = None,
        structured_usage_metadata: list[dict[str, object] | None] | None = None,
        direct_usage_metadata: list[dict[str, object] | None] | None = None,
        model_name: str = "fake:model",
    ) -> None:
        self._responses = list(responses)
        self._structured_responses = list(structured_responses or [])
        self._usage_metadata = list(usage_metadata or [])
        self._structured_usage_metadata = list(structured_usage_metadata or [])
        self._direct_usage_metadata = list(direct_usage_metadata or [])
        self.model_name = model_name
        self.calls: list[dict[str, object]] = []
        self.direct_calls: list[dict[str, object]] = []
        self.structured_calls: list[dict[str, object]] = []
        self._response_index = 0

    def with_structured_output(
        self,
        schema: dict[str, object],
        *,
        method: str | None = None,
    ) -> _FakeStructuredLangChainModel:
        return _FakeStructuredLangChainModel(self, schema, method)

    def add_structured_responses(self, responses: list[object]) -> None:
        self._structured_responses.extend(responses)

    def next_usage_metadata(self) -> dict[str, object] | None:
        if not self._usage_metadata:
            return None
        return self._usage_metadata.pop(0)

    def next_structured_usage_metadata(self) -> dict[str, object] | None:
        if not self._structured_usage_metadata:
            return None
        return self._structured_usage_metadata.pop(0)

    def next_direct_usage_metadata(self) -> dict[str, object] | None:
        if not self._direct_usage_metadata:
            return None
        return self._direct_usage_metadata.pop(0)

    def invoke(
        self,
        messages: list[object],
        *,
        config: dict[str, object] | None = None,
    ) -> _FakeAIMessage:
        self.direct_calls.append({"messages": list(messages)})
        if not self._responses:
            raise AssertionError("No fake direct LLM responses remaining.")
        self._response_index += 1
        response = self._responses.pop(0)
        usage_metadata = self.next_direct_usage_metadata()
        response_metadata = (
            {"model_name": self.model_name} if usage_metadata is not None else None
        )
        if isinstance(response, str):
            return _FakeAIMessage(
                content=response,
                usage_metadata=usage_metadata,
                response_metadata=response_metadata,
            )
        return _FakeAIMessage(
            content=response.get("content", ""),
            usage_metadata=usage_metadata,
            response_metadata=response_metadata,
        )


class _FakeLangChainAgent:
    def __init__(
        self,
        prompt_model: _FakeLangChainPromptModel,
        *,
        tools: list[object],
        system_prompt: str,
    ) -> None:
        self._prompt_model = prompt_model
        self._tools = {getattr(tool, "name"): tool for tool in tools}
        self._system_prompt = system_prompt

    def invoke(
        self,
        payload: dict[str, object],
        *,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raw_messages = payload.get("messages")
        assert isinstance(raw_messages, list)
        messages: list[object] = [
            {"role": "system", "content": self._system_prompt},
            *raw_messages,
        ]
        while True:
            ai_message = self._next_ai_message(messages)
            messages.append(ai_message)
            if not ai_message.tool_calls:
                return {"messages": messages}
            for tool_call in ai_message.tool_calls:
                tool_name = tool_call["name"]
                tool = self._tools.get(tool_name)
                if tool is None:
                    raise AssertionError(f"Unknown fake action call {tool_name!r}.")
                tool_message = tool.invoke(tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "content": tool_message.content,
                        "tool_call_id": getattr(tool_message, "tool_call_id", ""),
                    }
                )

    def _next_ai_message(self, messages: list[object]) -> _FakeAIMessage:
        self._prompt_model.calls.append({"messages": list(messages)})
        if not self._prompt_model._responses:
            raise AssertionError("No fake LLM responses remaining.")
        self._prompt_model._response_index += 1
        response = self._prompt_model._responses.pop(0)
        usage_metadata = self._prompt_model.next_usage_metadata()
        response_metadata = (
            {"model_name": self._prompt_model.model_name}
            if usage_metadata is not None
            else None
        )
        if isinstance(response, str):
            return _FakeAIMessage(
                content=response,
                usage_metadata=usage_metadata,
                response_metadata=response_metadata,
            )
        else:
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
                usage_metadata=usage_metadata,
                response_metadata=response_metadata,
            )


def _fake_create_langchain_agent(
    model: object,
    *,
    tools: list[object],
    system_prompt: str,
) -> _FakeLangChainAgent:
    assert isinstance(model, _FakeLangChainPromptModel)
    return _FakeLangChainAgent(
        model,
        tools=tools,
        system_prompt=system_prompt,
    )


def _prompt_action_call(
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
    return _prompt_action_call(
        "journey_run_code",
        {"code": code},
    )


def _take_screenshot() -> dict[str, object]:
    return _prompt_action_call("journey_screenshot", {})


def _inspect_dom(selector: str | None = None) -> dict[str, object]:
    arguments: dict[str, object] = {}
    if selector is not None:
        arguments["selector"] = selector
    return _prompt_action_call("journey_inspect_dom", arguments)


def _content_has_image(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "image_url"
        for item in content
    )


def _message_has_image(message: object) -> bool:
    if isinstance(message, dict):
        return _content_has_image(message.get("content"))
    return _content_has_image(getattr(message, "content", None))


def _messages_have_image(messages: list[object]) -> bool:
    return any(_message_has_image(message) for message in messages)


def _finalization(
    output: str | dict[str, object],
    *,
    success: bool = True,
    reason: str = "",
) -> dict[str, object]:
    return {
        "success_criteria_met": success,
        "failure_reason": reason,
        "output": output,
    }


@pytest.fixture(autouse=True)
def _install_fake_langchain_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        journey_browser_prompt,
        "_create_langchain_agent",
        _fake_create_langchain_agent,
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


def _prompt_html_for_selector(html: str, selector: str) -> str | None:
    if selector == "html":
        return html
    if selector == "body":
        body_start = html.find("<body")
        body_end = html.find("</body>")
        if body_start == -1 or body_end == -1:
            return None
        return html[body_start : body_end + len("</body>")]
    if not selector.startswith("#"):
        return None
    target_id = selector.removeprefix("#")
    marker = f'id="{target_id}"'
    marker_index = html.find(marker)
    if marker_index == -1:
        return None
    start = html.rfind("<", 0, marker_index)
    if start == -1:
        return None
    tag_end = html.find(">", start)
    if tag_end == -1:
        return None
    tag_name = html[start + 1 : tag_end].split()[0].removesuffix("/")
    close_marker = f"</{tag_name}>"
    close_index = html.find(close_marker, tag_end)
    if close_index == -1:
        return html[start : tag_end + 1]
    return html[start : close_index + len(close_marker)]


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
) -> journey_browser.JourneyBrowserPage:
    page = journey_browser.JourneyBrowserPage._from_snapshot(
        journey_browser._PageSnapshot.from_payload(_state_payload(url=url))
    )
    page._journey_snapshot = journey_browser._PageSnapshot.from_payload(_state_payload(url=url))
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
        if "document.querySelector(selector)" in script:
            assert items is None or isinstance(items, str)
            events.append(("prompt_inspect_dom", self._fake_prompt_title, items))
            if items is None:
                return self._fake_prompt_html
            return _prompt_html_for_selector(self._fake_prompt_html, items)
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


def test_semantic_dom_snapshot_keeps_actionable_dom_and_drops_noise():
    semantic_dom = journey_browser_prompt._semantic_dom_from_html(
        """
        <html>
          <head>
            <style>.hidden { display: none; }</style>
            <script>window.__APP_STATE__ = {"huge": true}</script>
          </head>
          <body>
            <main id="app" class="Layout_shell__abc123 cl-root">
              <button id="submit" data-testid="submit-button" class="cl-formButtonPrimary Button_text__rYg_f">
                <span class="Button_text__rYg_f">Continue</span>
                <svg><path d="M0 0 L1000 1000"></path></svg>
              </button>
              <button aria-label="Button">
                <span class="Button_text__rYg_f">Log in</span>
              </button>
              <a href="/login"><span>Sign in link</span></a>
              <label for="email">Email address</label>
              <input id="email" name="email" type="email" placeholder="Enter email" aria-invalid="false" />
              <a href="/help?token=secret" role="link">Help</a>
              <p hidden>Invisible text</p>
            </main>
          </body>
        </html>
        """
    )

    assert '<main class="cl-root" id="app">' in semantic_dom
    assert 'data-testid="submit-button"' in semantic_dom
    assert 'class="cl-formButtonPrimary"' in semantic_dom
    assert ">Continue<" in semantic_dom
    assert '<button aria-label="Button">Log in</button>' in semantic_dom
    assert '<a href="/login">Sign in link</a>' in semantic_dom
    assert 'for="email"' in semantic_dom
    assert 'name="email"' in semantic_dom
    assert 'placeholder="Enter email"' in semantic_dom
    assert 'href="/help?token=secret"' in semantic_dom
    assert "window.__APP_STATE__" not in semantic_dom
    assert "display: none" not in semantic_dom
    assert "Button_text__rYg_f" not in semantic_dom
    assert "<path" not in semantic_dom
    assert "Invisible text" not in semantic_dom


def test_journey_browser_page_round_trips_rehydration_payload(tmp_path: Path):
    assert issubclass(journey_browser.JourneyBrowserPage, PlaywrightPage)

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
    page = journey_browser.JourneyBrowserPage.__restore__(
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
    restored = journey_browser.JourneyBrowserPage.__restore__(
        payload,
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    assert isinstance(payload, dict)
    assert isinstance(restored, journey_browser.JourneyBrowserPage)
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

    def open_login() -> journey_browser.JourneyBrowserPage:
        page = journey_browser.open_page("http://example.test/login")
        assert isinstance(page, journey_browser.JourneyBrowserPage)
        events.append("inside")
        return page

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "inside",
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]
    log_output = capsys.readouterr().out
    assert "Browser" in log_output
    assert "opening chromium http://example.test/login" in log_output
    assert "opened chromium http://example.test/login" in log_output


def test_open_page_cleans_unreturned_page_at_step_exit(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def assert_login() -> bool:
        page = journey_browser.open_page("http://example.test/login")
        assert isinstance(page, journey_browser.JourneyBrowserPage)
        events.append("inside")
        return True

    def journey():
        journey_sdk.step(assert_login)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "inside",
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_cleans_unreturned_page_when_step_fails(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def assert_login() -> bool:
        journey_browser.open_page("http://example.test/login")
        raise RuntimeError("assertion failed")

    def journey():
        journey_sdk.step(assert_login)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "assertion failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_retries_transient_initial_navigation_failure(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        goto_errors=[
            RuntimeError("JourneyBrowserPage.goto: net::ERR_CONNECTION_RESET"),
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        ("sleep", journey_browser._NAVIGATION_RETRY_DELAY_SECONDS),
        ("goto", "http://example.test/login", "load"),
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_does_not_retry_non_transient_navigation_failure(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        goto_errors=[
            RuntimeError("JourneyBrowserPage.goto: certificate verify failed"),
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "certificate verify failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_repeated_transient_navigation_failure_skips_snapshot_cleanup(
    monkeypatch,
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        goto_errors=[
            RuntimeError("JourneyBrowserPage.goto: net::ERR_CONNECTION_RESET")
            for _ in range(journey_browser._NAVIGATION_RETRY_ATTEMPTS)
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    message = str(exc_info.value)
    assert "ERR_CONNECTION_RESET" in message
    assert "browser page cleanup failed" not in message
    assert "localStorage" not in message
    assert ("capture_state", "about:blank") not in events
    assert not any(event == ("capture_storage", "about:blank") for event in events)
    assert events.count(("goto", "http://example.test/login", "load")) == (
        journey_browser._NAVIGATION_RETRY_ATTEMPTS
    )
    assert events[-3:] == ["context_close", "browser_close", "playwright_exit"]


def test_open_page_retries_transient_reload_failure_after_local_storage(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        reload_errors=[
            RuntimeError("JourneyBrowserPage.reload: net::ERR_EMPTY_RESPONSE"),
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )
    saved_page = journey_browser.JourneyBrowserPage.__restore__(
        _state_payload(local_storage={"journey_session_token": "demo-token"}),
        journey_sdk.JourneyRestoreContext(
            artifact_root=Path("."),
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    def open_dashboard() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page(saved_page)

    def journey():
        journey_sdk.step(open_dashboard)

    journey_sdk.execute(journey)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/dashboard", "load"),
        ("evaluate", {"journey_session_token": "demo-token"}),
        ("reload", "load"),
        ("sleep", journey_browser._NAVIGATION_RETRY_DELAY_SECONDS),
        ("reload", "load"),
        ("capture_state", "http://example.test/dashboard"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_rehydrates_in_expected_order_and_cleans_nested_page(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    saved_page = journey_browser.JourneyBrowserPage.__restore__(
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
        page = journey_browser.open_page(saved_page, headless=False)
        assert isinstance(page, journey_browser.JourneyBrowserPage)
        events.append("inside")
        return {"page": page}

    def journey():
        journey_sdk.step(open_dashboard)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "playwright_enter",
        ("launch", False, False),
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

    monkeypatch.setattr(journey_browser.subprocess, "run", fake_run)

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True)

    assert install_commands == [
        [sys.executable, "-m", "playwright", "install", "chromium"]
    ]
    assert events == [
        "playwright_enter",
        ("launch", True, False),
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
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "new page failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_records_trace_video_and_manifest_by_default(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    configure_logging("info", output_format="jsonl")
    try:
        journey_sdk.execute(journey, no_state=True)
    finally:
        configure_logging("info", output_format="pretty")

    root = _browser_recording_root()
    manifests = [
        path
        for path in sorted(root.glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    traces = sorted(root.glob("*.trace.zip"))
    videos = sorted(root.glob("*.webm"))

    assert len(manifests) == 1
    assert len(traces) == 1
    assert len(videos) == 1
    assert manifests[0].name.startswith(
        "0001-case_1-open_login-attempt-1-context-1-run-"
    )
    assert traces[0].stem.startswith("0001-case_1-open_login-attempt-1-context-1-run-")
    assert videos[0].stem.startswith("0001-case_1-open_login-attempt-1-context-1-run-")

    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["sequence"] == 1
    assert payload["case_id"] == "case_1"
    assert payload["step_name"] == "open_login"
    assert payload["attempt"] == 1
    assert payload["context_index"] == 1
    assert payload["initial_url"] == "http://example.test/login"
    assert payload["final_url"] == "http://example.test/login"
    assert payload["trace_path"] == str(traces[0])
    assert payload["video_path"] == str(videos[0])
    assert payload["trace_saved"] is True
    assert payload["video_saved"] is True
    assert payload["show_trace"] == f"playwright show-trace {traces[0]}"

    recording_events = [event[0] for event in events if isinstance(event, tuple)]
    assert "trace_start" in recording_events
    assert "trace_stop" in recording_events
    assert "video_save" in recording_events
    video_save_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "video_save"
    )
    assert events.index("context_close") < video_save_index
    assert video_save_index < events.index("browser_close")
    assert video_save_index < events.index("playwright_exit")

    log_records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    recording_logs = [
        record
        for record in log_records
        if record["component"] == "browser"
        and record["event"] == "recording_success"
    ]
    assert len(recording_logs) == 1
    assert recording_logs[0]["recording_sequence"] == 1
    assert recording_logs[0]["recording_key"] == payload["recording_key"]
    assert recording_logs[0]["trace_path"] == str(traces[0])
    assert recording_logs[0]["video_path"] == str(videos[0])
    assert recording_logs[0]["manifest_path"] == str(manifests[0])


def test_execute_cleans_previous_browser_recordings_before_run(monkeypatch):
    root = _browser_recording_root()
    root.mkdir(parents=True)
    old_manifest = root / "0001-case_1-old-attempt-1-context-1-run-old.manifest.json"
    old_manifest.write_text("{}", encoding="utf-8")

    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True)

    manifests = [
        path
        for path in sorted(root.glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert not old_manifest.exists()
    assert len(manifests) == 1
    assert manifests[0].name.startswith(
        "0001-case_1-open_login-attempt-1-context-1-run-"
    )


def test_open_page_records_flat_incrementing_files_for_multiple_contexts(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_two_pages() -> tuple[journey_browser.JourneyBrowserPage, journey_browser.JourneyBrowserPage]:
        first = journey_browser.open_page("http://example.test/one")
        second = journey_browser.open_page("http://example.test/two")
        return first, second

    def journey():
        journey_sdk.step(open_two_pages)

    journey_sdk.execute(journey, no_state=True)

    manifests = [
        path
        for path in sorted(_browser_recording_root().glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert [path.name[:4] for path in manifests] == ["0001", "0002"]
    assert all(path.parent == _browser_recording_root() for path in manifests)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in manifests
    ]
    assert [payload["context_index"] for payload in payloads] == [1, 2]
    assert [payload["sequence"] for payload in payloads] == [1, 2]
    assert all(payload["step_name"] == "open_two_pages" for payload in payloads)
    assert all(payload["status"] == "success" for payload in payloads)


def test_execute_can_disable_browser_recording(monkeypatch):
    root = _browser_recording_root()
    root.mkdir(parents=True)
    (root / "old.manifest.json").write_text("{}", encoding="utf-8")

    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True, no_browser_recording=True)

    recording_manifests = [
        path
        for path in _browser_recording_root().glob("*.manifest.json")
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert recording_manifests == []
    assert not any(
        isinstance(event, tuple) and event[0] in {"trace_start", "trace_stop", "video_save"}
        for event in events
    )


def test_open_page_finalizes_recording_when_step_fails(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def assert_login() -> bool:
        journey_browser.open_page("http://example.test/login")
        raise RuntimeError("assertion failed")

    def journey():
        journey_sdk.step(assert_login)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, no_state=True)

    manifests = [
        path
        for path in sorted(_browser_recording_root().glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["step_name"] == "assert_login"
    assert payload["trace_saved"] is True
    assert payload["video_saved"] is True


def test_open_page_keyboard_interrupt_is_not_reported_as_browser_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=KeyboardInterrupt(),
        fail_context_close=RuntimeError("TargetClosedError: context closed"),
        fail_browser_close=RuntimeError("TargetClosedError: browser closed"),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "Browser failed to open" not in output
    assert "browser page cleanup failed" not in output
    assert "TargetClosedError" not in output
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_playwright_abort_after_forced_interrupt_is_keyboard_interrupt(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "JourneyBrowserPage.goto: net::ERR_ABORTED; maybe frame was detached?"
        ),
        force_cleanup_before_goto_failure=True,
    )

    class ForcedInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            raise KeyboardInterrupt()

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(ForcedInterruptController()):
        with pytest.raises(KeyboardInterrupt):
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "Browser failed to open" not in output
    assert "failed after" not in output
    assert ("capture_state", "about:blank") not in events


def test_open_page_forced_interrupt_without_cleanup_keeps_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "JourneyBrowserPage.goto: net::ERR_ABORTED; maybe frame was detached?"
        ),
    )

    class ForcedInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(ForcedInterruptController()):
        with pytest.raises(CallableExecutionError) as exc_info:
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "net::ERR_ABORTED" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_open_page_pending_interrupt_keeps_real_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "JourneyBrowserPage.goto: net::ERR_ABORTED; maybe frame was detached?"
        ),
    )

    class PendingInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return True

        def is_step_forced_interrupt_requested(self) -> bool:
            return False

        def raise_if_interrupted_after_step(self) -> None:
            raise KeyboardInterrupt()

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(PendingInterruptController()):
        with pytest.raises(CallableExecutionError) as exc_info:
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "net::ERR_ABORTED" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_open_page_no_interrupt_keeps_target_closed_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "TargetClosedError: Target page, context or browser has been closed"
        ),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "TargetClosedError" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_open_page_forced_interrupt_keeps_non_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=RuntimeError(
            "TargetClosedError: application-level failure that is not Playwright"
        ),
        force_cleanup_before_goto_failure=True,
    )

    class ForcedInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(ForcedInterruptController()):
        with pytest.raises(CallableExecutionError) as exc_info:
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "application-level failure" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_browser_interrupt_cleanup_marks_playwright_callback_futures_observed():
    class FakeFuture:
        def __init__(self) -> None:
            self.callbacks: list[object] = []
            self.retrieved = False

        def add_done_callback(self, callback: object) -> None:
            self.callbacks.append(callback)

        def exception(self) -> RuntimeError:
            self.retrieved = True
            return RuntimeError("TargetClosedError: browser closed")

    class FakeCallback:
        def __init__(self, future: FakeFuture) -> None:
            self.future = future

    class FakeConnection:
        def __init__(self, future: FakeFuture) -> None:
            self._callbacks = {"pending": FakeCallback(future)}

    class FakeManager:
        def __init__(self, future: FakeFuture) -> None:
            self._connection = FakeConnection(future)

    future = FakeFuture()
    journey_browser._suppress_playwright_callback_future_noise(FakeManager(future))

    assert len(future.callbacks) == 1
    callback = future.callbacks[0]
    assert callable(callback)
    callback(future)
    assert future.retrieved


def test_browser_registers_forced_interrupt_cleanup_that_stops_playwright_driver(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeController:
        def __init__(self) -> None:
            self.callbacks: list[Callable[[], None]] = []

        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

        def register_forced_interrupt_callback(
            self,
            name: str,
            callback: Callable[[], None],
        ) -> Callable[[], None]:
            self.callbacks.append(callback)
            return lambda: None

    class FakeProcess:
        pid = 12345
        returncode = None

    class FakeTransport:
        _proc = FakeProcess()

    class FakeConnection:
        _transport = FakeTransport()
        _callbacks: dict[str, object] = {}

    class FakeManager:
        _connection = FakeConnection()

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        journey_browser.os,
        "kill",
        lambda pid, sig: killed.append((pid, sig)),
    )

    controller = FakeController()
    with journey_executor._use_step_interrupt_controller(controller):
        page = journey_browser.JourneyBrowserPage._from_snapshot(
            journey_browser._PageSnapshot.from_url("https://example.test/")
        )
        page._set_step_resources(manager=FakeManager())

    assert len(controller.callbacks) == 1
    controller.callbacks[0]()
    assert killed == [(12345, journey_browser.signal.SIGTERM)]
    assert page._is_forced_interrupt_cleanup_started()


def test_browser_prompt_forced_cleanup_playwright_error_is_keyboard_interrupt():
    page = journey_browser.JourneyBrowserPage._from_snapshot(
        journey_browser._PageSnapshot.from_url("https://example.test/")
    )
    page._journey_forced_interrupt_cleanup_started = True

    with pytest.raises(KeyboardInterrupt):
        journey_browser_prompt._raise_keyboard_interrupt_if_forced_prompt_abort(
            page,
            PlaywrightError("TargetClosedError: Target page has been closed"),
        )


def test_browser_prompt_forced_interrupt_without_cleanup_keeps_playwright_error():
    class FakeController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

    page = journey_browser.JourneyBrowserPage._from_snapshot(
        journey_browser._PageSnapshot.from_url("https://example.test/")
    )

    with journey_executor._use_step_interrupt_controller(FakeController()):
        journey_browser_prompt._raise_keyboard_interrupt_if_forced_prompt_abort(
            page,
            PlaywrightError("TargetClosedError: Target page has been closed"),
        )


def test_browser_prompt_forced_cleanup_keeps_non_playwright_error():
    page = journey_browser.JourneyBrowserPage._from_snapshot(
        journey_browser._PageSnapshot.from_url("https://example.test/")
    )
    page._journey_forced_interrupt_cleanup_started = True

    journey_browser_prompt._raise_keyboard_interrupt_if_forced_prompt_abort(
        page,
        RuntimeError("ordinary prompt failure"),
    )
    journey_browser_prompt._raise_keyboard_interrupt_if_forced_prompt_abort(
        page,
        RuntimeError("TargetClosedError: application-level failure"),
    )


def test_open_page_real_navigation_failure_remains_browser_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=RuntimeError("navigation failed"),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "navigation failed" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output
    assert ("capture_state", "about:blank") not in events


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

    monkeypatch.setattr(journey_browser.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="could not automatically install"):
        journey_browser.ensure_browser_installed()

    assert events == [
        "playwright_enter",
        "playwright_exit",
    ]


def test_open_page_rejects_outside_step():
    with pytest.raises(InvalidBranchUsageError):
        journey_browser.open_page("http://example.test/login")


def test_open_page_rejects_unsupported_input_type():
    with pytest.raises(TypeError, match="URL string or JourneyBrowserPage"):
        journey_browser.open_page(object())


def test_journey_browser_page_rejects_legacy_json_payload(tmp_path: Path):
    with pytest.raises(TypeError, match="dictionary payload"):
        journey_browser.JourneyBrowserPage.__restore__(
            '{"url":"http://example.test"}',
            journey_sdk.JourneyRestoreContext(
                artifact_root=tmp_path,
                boundary_kind="binding",
                boundary_id="step:n_1",
            ),
        )


def test_execute_resume_rehydrates_saved_journey_browser_page(tmp_path, monkeypatch):
    state_file = tmp_path / "journey.state"
    attempts = {"count": 0}
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def login() -> journey_browser.JourneyBrowserPage:
        page = journey_browser.open_page("http://example.test/login")
        page.goto("http://example.test/dashboard", wait_until="load")
        return page

    def continue_from_page(
        saved_page: journey_browser.JourneyBrowserPage,
    ) -> journey_browser.JourneyBrowserPage:
        attempts["count"] += 1
        events.append(f"continue:{attempts['count']}:{saved_page.url}")
        if attempts["count"] == 1:
            raise KeyboardInterrupt()
        return journey_browser.open_page(saved_page)

    def assert_page(
        saved_page: journey_browser.JourneyBrowserPage,
    ) -> journey_browser.JourneyBrowserPage:
        page = journey_browser.open_page(saved_page)
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


def test_journey_browser_prompt_rejects_blank_instruction(monkeypatch):
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(["done"]),
    )

    with pytest.raises(ValueError, match="non-blank instruction"):
        page.prompt("   ", model="openai:gpt-4.1-mini")


def test_journey_browser_prompt_rejects_saved_page(tmp_path: Path):
    saved_page = journey_browser.JourneyBrowserPage.__restore__(
        _state_payload(url="http://example.test/login"),
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    with pytest.raises(RuntimeError, match="Call open_page\\(saved_page\\) first"):
        saved_page.prompt("click sign in", model="openai:gpt-4.1-mini")


def test_journey_browser_prompt_defaults_model_when_model_and_env_are_missing(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    monkeypatch.delenv(journey_browser_prompt.JOURNEY_BROWSER_PROMPT_MODEL_ENV, raising=False)
    loaded_models: list[str] = []

    def load_model(model: str) -> _FakeLangChainPromptModel:
        loaded_models.append(model)
        return _FakeLangChainPromptModel(
            ["done"],
            structured_responses=[_finalization("done")],
        )

    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        load_model,
    )

    assert page.prompt("click sign in") == "done"
    assert loaded_models == [journey_browser_prompt.DEFAULT_JOURNEY_BROWSER_PROMPT_MODEL]


def test_journey_browser_prompt_reports_langchain_model_initialization_failure(
    monkeypatch,
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)

    def fail_init_chat_model(*args: object, **kwargs: object) -> object:
        raise RuntimeError("missing provider package")

    monkeypatch.setattr(
        journey_prompt_engine,
        "init_chat_model",
        fail_init_chat_model,
    )

    with pytest.raises(RuntimeError, match="failed to initialize LangChain model") as exc_info:
        page.prompt("click sign in", model="openai:gpt-4.1-mini")
    assert "missing provider package" in str(exc_info.value)


def test_journey_browser_prompt_model_initialization_auth_failure_has_hint(
    monkeypatch,
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)

    def fail_init_chat_model(*args: object, **kwargs: object) -> object:
        raise RuntimeError(
            "Could not resolve authentication method. Expected one of api_key, "
            "auth_token, or credentials to be set."
        )

    monkeypatch.setattr(
        journey_prompt_engine,
        "init_chat_model",
        fail_init_chat_model,
    )

    with pytest.raises(RuntimeError, match="failed to initialize LangChain model") as exc_info:
        page.prompt("click sign in", model="anthropic:claude-haiku-4-5")
    hint = getattr(exc_info.value, "hint", "")
    assert "ANTHROPIC_API_KEY" in hint
    assert "JOURNEY_BROWSER_PROMPT_MODEL=anthropic:claude-haiku-4-5" in hint


def test_journey_browser_prompt_delegates_action_execution_to_langchain_agent():
    source = Path(journey_browser_prompt.__file__).read_text(encoding="utf-8")
    engine_source = Path(journey_prompt_engine.__file__).read_text(encoding="utf-8")

    assert "PromptEngineSession" in source
    assert "from langchain.chat_models import init_chat_model" in engine_source
    assert "importlib.import_module" not in source
    assert "journey_fail_session" not in source
    assert "create_agent" in engine_source
    assert "_build_agent_middleware" not in source
    assert "wrap_model_call" not in source
    assert "bind_tools" not in source
    assert ".invoke(tool_call" not in source
    assert "ToolMessage" not in source
    assert "_extract_langchain_tool_calls" not in source
    assert "_tool_result_message" not in source


def test_journey_browser_prompt_clicks_popup_and_returns_text(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    popup_page: journey_browser.JourneyBrowserPage | None = None

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
        ],
        structured_responses=[
            _finalization("The opened popup title is Welcome popup."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt(
        'click on a "Sign in" button and get the title of the opened popup',
        model="anthropic:claude-haiku-4-5",
    )
    log_output = capsys.readouterr().out

    assert result == "The opened popup title is Welcome popup."
    assert len(fake_model.calls) == 3
    assert all(
        "Return the finalization object using this schema"
        not in call["messages"][-1]["content"][0]["text"]
        for call in fake_model.calls
    )
    assert len(fake_model.structured_calls) == 1
    assert not any(
        _messages_have_image(call["messages"]) for call in fake_model.calls
    )
    assert not _messages_have_image(fake_model.structured_calls[0]["messages"])
    assert not hasattr(journey_browser_prompt, "_COLLECT_ELEMENTS_SCRIPT")
    first_prompt_text = fake_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "<journey-semantic-dom>" in first_prompt_text
    assert "Observation records JSON:" in first_prompt_text
    assert "Known pages JSON:" not in first_prompt_text
    assert '"event":"page"' in first_prompt_text
    assert '"is_original":true' in first_prompt_text
    second_prompt_text = fake_model.calls[1]["messages"][-1]["content"][0]["text"]
    assert "Executed steps JSON:" not in second_prompt_text
    assert '"event":"action"' in second_prompt_text
    assert 'page.locator(\\"#sign-in\\").click(timeout=timeout_ms)' in second_prompt_text
    assert '<button id="sign-in" role="button">Sign in</button>' in first_prompt_text
    assert '"actions": [' not in first_prompt_text
    assert '"id": "e1"' not in first_prompt_text
    assert (
        fake_model.calls[1]["messages"][-1]["content"][0]["text"].find('"title":"Welcome popup"')
        != -1
    )
    final_prompt_text = fake_model.calls[2]["messages"][-1]["content"][0]["text"]
    assert "Observation records JSON:" in final_prompt_text
    assert "Executed steps JSON:" not in final_prompt_text
    assert '"event":"action"' in final_prompt_text
    assert '"action_type":"python"' in final_prompt_text
    assert "Final return value requested: plain text" in final_prompt_text
    finalization_prompt_text = fake_model.structured_calls[0]["messages"][1]["content"][0]["text"]
    assert "Return the finalization object using this schema" in finalization_prompt_text
    assert "success_criteria_met" in finalization_prompt_text
    assert "<journey-semantic-dom>" not in finalization_prompt_text
    assert events == [
        ("prompt_rendered_html", "Login page"),
        ("prompt_click", "Login page", "#sign-in", 5000),
        ("prompt_wait_for_load_state", "Welcome popup", "load", 5000),
        ("prompt_rendered_html", "Login page"),
        ("prompt_rendered_html", "Welcome popup"),
        ("prompt_wait_for_load_state", "Welcome popup", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Welcome popup", 500),
    ]
    assert "[journey]" not in log_output
    assert "AI prompt" in log_output
    assert 'click on a "Sign in" button and get the title' in log_output
    assert "model=anthropic:claude-haiku-4-5" in log_output
    assert "page 0 'Login page' at http://example.test/login" in log_output
    assert "1/15 action               click selector '#sign-in'" in log_output
    assert "1/15 code" in log_output
    assert 'page.locator("#sign-in").click(timeout=timeout_ms)' in log_output
    assert "page discovered" in log_output
    assert "page 1 'Welcome popup' at http://example.test/sign-in-popup" in log_output
    assert "active page" in log_output
    assert (
        "3/15 finish               The opened popup title is Welcome popup."
        in log_output
    )


def test_journey_browser_prompt_does_not_attach_screenshots_to_observations(
    monkeypatch,
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Dashboard",
        url="http://example.test/dashboard",
        context=context,
        events=events,
        visible_texts={"Ready"},
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        ["Done."],
        structured_responses=[_finalization("Done.")],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    assert page.prompt("finish", model="openai:gpt-4.1-mini") == "Done."

    assert not _messages_have_image(fake_model.calls[0]["messages"])
    assert not _messages_have_image(fake_model.structured_calls[0]["messages"])
    assert events == [
        ("prompt_rendered_html", "Dashboard"),
        ("prompt_wait_for_load_state", "Dashboard", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Dashboard", 500),
    ]


def test_journey_browser_prompt_screenshot_tool_returns_image_when_requested(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Visual page",
        url="http://example.test/visual",
        context=context,
        events=events,
        visible_texts={"Visual ready"},
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        [
            _take_screenshot(),
            "The screenshot shows the visual page.",
        ],
        structured_responses=[_finalization("The screenshot shows the visual page.")],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    assert (
        page.prompt("inspect visual layout", model="openai:gpt-4.1-mini")
        == "The screenshot shows the visual page."
    )
    log_output = capsys.readouterr().out

    assert len(fake_model.calls) == 2
    assert not _messages_have_image(fake_model.calls[0]["messages"])
    screenshot_message = fake_model.calls[1]["messages"][-1]
    assert _message_has_image(screenshot_message)
    assert (
        "Screenshot captured for page 0 'Visual page' at http://example.test/visual."
        in screenshot_message["content"][0]["text"]
    )
    assert not _messages_have_image(fake_model.structured_calls[0]["messages"])
    assert events == [
        ("prompt_rendered_html", "Visual page"),
        ("prompt_screenshot", "Visual page"),
        ("prompt_wait_for_load_state", "Visual page", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Visual page", 500),
    ]
    assert "1/15 action               capture screenshot" in log_output
    assert "1/15 ok                   screenshot captured on page 0" in log_output


def test_journey_browser_prompt_screenshot_tool_uses_active_page(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login page",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    popup_page = _make_prompt_page(
        title="Welcome popup",
        url="http://example.test/sign-in-popup",
        context=context,
        events=events,
        visible_texts={"Welcome popup"},
    )
    context.pages.extend([page, popup_page])
    fake_model = _FakeLangChainPromptModel(
        [
            _run_code("switch_page(1)"),
            _take_screenshot(),
            "The popup screenshot was captured.",
        ],
        structured_responses=[_finalization("The popup screenshot was captured.")],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    assert (
        page.prompt("inspect the popup", model="openai:gpt-4.1-mini")
        == "The popup screenshot was captured."
    )

    screenshot_message = fake_model.calls[2]["messages"][-1]
    assert _message_has_image(screenshot_message)
    assert "page 1 'Welcome popup'" in screenshot_message["content"][0]["text"]
    assert ("prompt_screenshot", "Welcome popup") in events
    assert ("prompt_screenshot", "Login page") not in events


def test_journey_browser_prompt_screenshot_tool_counts_toward_max_steps(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Visual page",
        url="http://example.test/visual",
        context=context,
        events=events,
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel([_take_screenshot()])
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    with pytest.raises(RuntimeError, match="reached max_steps=1"):
        page.prompt(
            "inspect visual layout",
            model="openai:gpt-4.1-mini",
            max_steps=1,
        )
    log_output = capsys.readouterr().out

    assert events == [
        ("prompt_rendered_html", "Visual page"),
        ("prompt_screenshot", "Visual page"),
    ]
    assert "1/1 action" in log_output
    assert "capture screenshot" in log_output
    assert "Last action was ok: screenshot" in log_output


def test_journey_browser_prompt_inspect_dom_tool_returns_scoped_raw_dom(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Details",
        url="http://example.test/details",
        context=context,
        events=events,
        html=(
            "<html><body>"
            '<main id="app"><div id="details"><span>Raw child</span></div></main>'
            "</body></html>"
        ),
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        [
            _inspect_dom("#details"),
            "The details DOM is available.",
        ],
        structured_responses=[_finalization("The details DOM is available.")],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    assert (
        page.prompt("inspect details DOM", model="openai:gpt-4.1-mini")
        == "The details DOM is available."
    )
    log_output = capsys.readouterr().out

    inspect_message = fake_model.calls[1]["messages"][-1]
    assert not _message_has_image(inspect_message)
    inspect_text = inspect_message["content"][0]["text"]
    assert "Raw DOM for #details" in inspect_text
    assert '<div id="details"><span>Raw child</span></div>' in inspect_text
    assert events == [
        ("prompt_rendered_html", "Details"),
        ("prompt_inspect_dom", "Details", "#details"),
        ("prompt_wait_for_load_state", "Details", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Details", 500),
    ]
    assert "1/15 action               inspect DOM for #details" in log_output
    assert "1/15 ok                   DOM inspected for #details" in log_output


def test_journey_browser_prompt_runs_action_work_on_prompt_thread(monkeypatch):
    events: list[object] = []
    context = _FakePromptContext()
    prompt_thread_id = threading.get_ident()
    click_thread_ids: list[int] = []

    def record_click_thread() -> None:
        click_thread_ids.append(threading.get_ident())

    page = _make_prompt_page(
        title="Login page",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[
            _prompt_element("#sign-in", name="Sign in"),
        ],
        click_handlers={"#sign-in": record_click_thread},
    )
    context.pages.append(page)

    fake_model = _FakeLangChainPromptModel(
        [
            _run_code('page.locator("#sign-in").click(timeout=timeout_ms)'),
            "Done.",
        ],
        structured_responses=[
            _finalization("Done."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert click_thread_ids == [prompt_thread_id]


def test_journey_browser_prompt_logs_langchain_usage_metadata(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login page",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        ["Done."],
        structured_responses=[_finalization("Done.")],
        usage_metadata=[
            {
                "input_tokens": 30,
                "output_tokens": 8,
                "total_tokens": 38,
                "total_cost_usd": 0.00011,
            }
        ],
        structured_usage_metadata=[
            {
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "total_cost_usd": 0.00005,
            }
        ],
        model_name="fake-browser-model",
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "finish",
        model="openai:gpt-4.1-mini",
        memory=None,
    ) == "Done."

    log_output = capsys.readouterr().out
    assert (
        "action_loop model=fake-browser-model "
        "tokens=input:30 output:8 total:38"
    ) in log_output
    assert (
        "finalization model=fake-browser-model "
        "tokens=input:12 output:4 total:16"
    ) in log_output
    assert "Done. tokens=input:42 output:12 total:54" in log_output
    assert "cost=" not in log_output


def test_journey_browser_prompt_returns_structured_output(monkeypatch):
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
            _finalization(
                {"popup_title": "Welcome popup", "has_welcome_text": True}
            ),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
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

    assert result == {
        "popup_title": "Welcome popup",
        "has_welcome_text": True,
    }
    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    agent_call = fake_model.calls[0]
    assert fake_model.structured_calls[0]["method"] == "json_schema"
    assert fake_model.structured_calls[0]["schema"] == {
        "title": "journey_prompt_finalization",
        "description": "Structured finalization for a Journey prompt task.",
        "type": "object",
        "properties": {
            "success_criteria_met": {
                "type": "boolean",
                "description": "Whether the original instruction and all success criteria are satisfied.",
            },
            "failure_reason": {
                "type": "string",
                "description": (
                    "Visible blocking message or concise expected-vs-observed "
                    "explanation when success_criteria_met is false; otherwise an empty string."
                ),
            },
            "output": {
                "type": "object",
                "description": "Output object to return when all success criteria are met.",
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
            },
        },
        "required": ["success_criteria_met", "failure_reason", "output"],
        "additionalProperties": False,
    }
    final_prompt_text = agent_call["messages"][1]["content"][0]["text"]
    assert "Final return value requested: JSON object with these output fields" in final_prompt_text
    assert "popup_title" in final_prompt_text
    assert "has_welcome_text" in final_prompt_text


def test_journey_browser_prompt_final_output_includes_visible_error_text(monkeypatch):
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
            _finalization({"error": ""}),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt(
        "report the visible sign-in error",
        model="openai:gpt-4.1-mini",
        output={"error": "An error message if found."},
    )

    assert result == {
        "error": "Password is incorrect. Try again, or use another method.",
    }
    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    final_prompt_text = fake_model.structured_calls[0]["messages"][1]["content"][0]["text"]
    assert "<journey-visible-text>" in final_prompt_text
    assert "Password is incorrect. Try again, or use another method." in final_prompt_text
    assert (
        "copy the visible message exactly when present"
        in fake_model.structured_calls[0]["messages"][0]["content"]
    )
    assert ("prompt_wait_for_load_state", "Login", "networkidle", 2000) in events
    assert ("prompt_wait_for_timeout", "Login", 500) in events


def test_journey_browser_prompt_finalization_with_blocking_error_raises(
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
        ["The sign-in attempt is blocked by a visible account lock message."],
        structured_responses=[
            _finalization("", success=False, reason=reason),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    with pytest.raises(RuntimeError, match="Your account is locked"):
        page.prompt(
            'Sign in as e2etest@heyalfie.com using password "1111"',
            model="openai:gpt-4.1-mini",
            memory="sign-in",
        )
    log_output = capsys.readouterr().out

    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    prompt_text = fake_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "<journey-visible-text>" in prompt_text
    assert reason in prompt_text
    finalization_prompt_text = fake_model.structured_calls[0]["messages"][1]["content"][0]["text"]
    assert "success_criteria_met" in finalization_prompt_text
    assert reason in finalization_prompt_text
    assert "AI prompt" in log_output
    assert "failed" in log_output
    assert "finish" not in log_output
    assert not _prompt_memory_path(tmp_path, "sign-in").exists()


def test_journey_browser_prompt_failed_finalization_raises_without_memory(
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
    fake_model = _FakeLangChainPromptModel(
        ["The page still shows a blocking sign-in state."],
        structured_responses=[
            _finalization("", success=False, reason=reason),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    with pytest.raises(RuntimeError, match="Your account is locked"):
        page.prompt(
            "sign in",
            model="openai:gpt-4.1-mini",
            memory="sign-in",
        )
    log_output = capsys.readouterr().out

    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    assert "AI prompt" in log_output
    assert "failed" in log_output
    assert not _prompt_memory_path(tmp_path, "sign-in").exists()


def test_journey_browser_prompt_fails_when_expected_state_is_missing(
    monkeypatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)
    reason = "Expected two sidebar chats, but only one chat is visible."
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Hey Alfie",
        url="http://example.test/chats/roof",
        context=context,
        events=events,
        visible_texts={
            "RECENT CHATS",
            "repair a leaking roof",
        },
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        [
            (
                "The chat has been successfully started. However, the task "
                "expects two chats and I only see one chat in the sidebar."
            ),
        ],
        structured_responses=[
            _finalization("", success=False, reason=reason),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    with pytest.raises(RuntimeError, match="Expected two sidebar chats"):
        page.prompt(
            (
                "Start chatting with Alfie - say you need to 'repair a leaking roof'. "
                "Expect there are two chats in the 'Active chats' section in the sidebar."
            ),
            model="openai:gpt-4.1-mini",
            memory="roof-chat",
        )
    log_output = capsys.readouterr().out

    assert len(fake_model.calls) == 1
    assert len(fake_model.structured_calls) == 1
    finalization_prompt_text = fake_model.structured_calls[0]["messages"][1]["content"][0]["text"]
    assert "Expect there are two chats" in finalization_prompt_text
    assert "I only see one chat" in finalization_prompt_text
    assert "success_criteria_met" in finalization_prompt_text
    assert "finish" not in log_output
    assert not (tmp_path / "roof-chat.memory.md").exists()


def test_journey_browser_prompt_rejects_invalid_output_specs(monkeypatch):
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
        journey_browser_prompt,
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


def test_journey_browser_prompt_rejects_malformed_structured_output(monkeypatch):
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            ["The popup summary is ready."],
            structured_responses=[
                _finalization({"popup_title": "Welcome", "extra": True}),
            ],
        ),
    )

    with pytest.raises(RuntimeError, match="unexpected fields"):
        page.prompt(
            "summarize",
            model="openai:gpt-4.1-mini",
            output={"popup_title": "The popup title."},
        )


def test_journey_browser_prompt_rejects_malformed_finalization_output(monkeypatch):
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            ["The popup summary is ready."],
            structured_responses=[{"completed": True, "output": "Welcome"}],
        ),
    )

    with pytest.raises(RuntimeError, match="missing fields"):
        page.prompt("summarize the popup", model="openai:gpt-4.1-mini")


def test_journey_browser_prompt_retries_rejected_python(
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
        ],
        structured_responses=[
            _finalization("Started the chat."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    result = page.prompt("say you need to fix a toilet", model="openai:gpt-4.1-mini")
    log_output = capsys.readouterr().out

    assert result == "Started the chat."
    assert page._fake_prompt_field_values == {"#composer": "I need to fix a toilet"}
    assert '<div id="composer" role="textbox">Message</div>' in fake_model.calls[0][
        "messages"
    ][1]["content"][0]["text"]
    assert '"status":"rejected"' in fake_model.calls[1]["messages"][-1]["content"][0]["text"]
    assert events == [
        ("prompt_rendered_html", "Chat"),
        ("prompt_click", "Chat", "#attach", 5000),
        ("prompt_rendered_html", "Chat"),
        ("prompt_fill", "Chat", "#composer", "I need to fix a toilet", 5000),
        ("prompt_rendered_html", "Chat"),
        ("prompt_wait_for_load_state", "Chat", "networkidle", 2000),
        ("prompt_wait_for_timeout", "Chat", 500),
    ]
    assert "1/15 action               click selector '#attach'" in log_output
    assert 'page.locator("#attach").click(timeout=timeout_ms)' in log_output
    assert "1/15 rejected" in log_output
    assert "AssertionError: No click handler registered for '#attach'" in log_output
    assert (
        "2/15 action               fill selector '#composer' with "
        "'I need to fix a toilet'"
    ) in log_output
    assert (
        'page.locator("#composer").fill'
        '("I need to fix a toilet", timeout=timeout_ms)'
    ) in log_output


def test_journey_browser_prompt_compiles_and_replays_named_memory(
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
    page._fake_prompt_elements.extend(
        [
            _prompt_element(
                "#password-field",
                name="Password",
                role="textbox",
                tag_name="input",
            ),
            _prompt_element("#submit", name="Continue"),
        ]
    )

    def submit_login() -> None:
        if page._fake_prompt_field_values.get("#password-field") == "1111":
            page._fake_prompt_visible_texts.add("Welcome")

    page._fake_prompt_click_handlers["#submit"] = submit_login

    first_model = _FakeLangChainPromptModel(
        [
            _take_screenshot(),
            _run_code(
                "\n".join(
                    [
                        'page.locator("#password-field").fill("1212", timeout=timeout_ms)',
                        'page.locator("#submit").click(timeout=timeout_ms)',
                    ]
                )
            ),
            _run_code(
                "\n".join(
                    [
                        'page.locator("#password-field").fill("1111", timeout=timeout_ms)',
                        'page.locator("#submit").click(timeout=timeout_ms)',
                    ]
                )
            ),
            "Signed in.",
            "\n".join(
                [
                    "## Replay code",
                    "```python",
                    'page.locator("#password-field").fill("1111", timeout=timeout_ms)',
                    'page.locator("#submit").click(timeout=timeout_ms)',
                    "```",
                    "",
                    "## Success check code",
                    "```python",
                    'page.locator("text=Welcome").wait_for(state="visible", timeout=timeout_ms)',
                    "```",
                    "",
                    "## Notes",
                    "Use the known-good password directly.",
                ]
            ),
        ],
        structured_responses=[
            _finalization("Signed in."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: first_model,
    )

    assert page.prompt(
        "sign in",
        model="openai:gpt-4.1-mini",
        memory="chat-start",
    ) == "Signed in."

    memory_path = _prompt_memory_path(tmp_path, "chat-start")
    memory_text = memory_path.read_text(encoding="utf-8")
    assert 'page.locator("#password-field").fill("1111", timeout=timeout_ms)' in memory_text
    assert '"1212"' not in memory_text
    assert "rendered-html" not in memory_text
    assert "semantic-dom" not in memory_text
    assert "data:image" not in memory_text
    assert "png:Chat" not in memory_text
    assert '<div id="composer"' not in memory_text
    assert first_model.direct_calls

    replay_events: list[object] = []
    replay_context = _FakePromptContext()
    replay_page = _make_prompt_page(
        title="Chat",
        url="http://example.test/chat?session=secret#composer",
        context=replay_context,
        events=replay_events,
        elements=list(page._fake_prompt_elements),
    )
    replay_context.pages.append(replay_page)

    def submit_replay_login() -> None:
        if replay_page._fake_prompt_field_values.get("#password-field") == "1111":
            replay_page._fake_prompt_visible_texts.add("Welcome")

    replay_page._fake_prompt_click_handlers["#submit"] = submit_replay_login

    def fail_model_load(model: str) -> object:
        raise AssertionError("replay should not load or call a model")

    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        fail_model_load,
    )
    assert replay_page.prompt(
        "sign in",
        model="openai:gpt-4.1-mini",
        memory="chat-start",
    ) == "Signed in."
    assert (
        "prompt_fill",
        "Chat",
        "#password-field",
        "1111",
        5000,
    ) in replay_events
    assert all("1212" not in repr(event) for event in replay_events)

    third_model = _FakeLangChainPromptModel(
        ["Done without memory."],
        structured_responses=[
            _finalization("Done without memory."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: third_model,
    )
    page.prompt(
        "sign out",
        model="openai:gpt-4.1-mini",
        memory="chat-start",
    )
    third_prompt_text = third_model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Prompt memory:" not in third_prompt_text


def test_journey_browser_prompt_falls_back_when_memory_replay_fails(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    write_prompt_memory_entry(
        _prompt_memory_path(tmp_path, "sign-in"),
        PromptMemoryEntry(
            component="browser",
            instruction="sign in",
            observation_signature='{"title":"Login","url":"http://example.test/login"}',
            sections=(
                PromptMemorySection(
                    heading="Replay code",
                    body='page.locator("#missing").click(timeout=timeout_ms)',
                    language="python",
                ),
                PromptMemorySection(
                    heading="Success check code",
                    body='page.locator("text=Welcome").wait_for(state="visible", timeout=timeout_ms)',
                    language="python",
                ),
                PromptMemorySection(
                    heading="Notes",
                    body="This stale selector used to work.",
                ),
            ),
            final_output="Signed in from memory.",
        ),
    )
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[
            _prompt_element("#password-field", name="Password", role="textbox", tag_name="input"),
        ],
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        ["Recovered after fallback."],
        structured_responses=[
            _finalization("Recovered after fallback."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "sign in",
        model="openai:gpt-4.1-mini",
        memory="sign-in",
    ) == "Recovered after fallback."

    prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Prompt memory:" in prompt_text
    assert "Replay failed before fallback:" in prompt_text
    assert "#missing" in prompt_text


@pytest.mark.parametrize(
    ("replay_code", "expected_detail"),
    [
        ("await page.locator('#submit').click(timeout=timeout_ms)", "async/await"),
        ("import time\npage.wait_for_timeout(1)", "uses imports"),
        ("re.compile('^Archive$')", "unsupported Python name 're'"),
        ("page.locator('#submit').click()", "timeout=timeout_ms"),
        ("page.wait_for_timeout(60000)", "exceeds the 5000ms"),
    ],
)
def test_journey_browser_prompt_skips_invalid_memory_before_replay(
    monkeypatch,
    tmp_path: Path,
    replay_code: str,
    expected_detail: str,
):
    monkeypatch.chdir(tmp_path)
    write_prompt_memory_entry(
        _prompt_memory_path(tmp_path, "sign-in"),
        PromptMemoryEntry(
            component="browser",
            instruction="sign in",
            observation_signature='{"title":"Login","url":"http://example.test/login"}',
            sections=(
                PromptMemorySection(
                    heading="Replay code",
                    body=replay_code,
                    language="python",
                ),
                PromptMemorySection(
                    heading="Success check code",
                    body="assert True",
                    language="python",
                ),
            ),
            final_output="Signed in from memory.",
        ),
    )
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[_prompt_element("#submit", name="Submit")],
        click_handlers={
            "#submit": lambda: (_ for _ in ()).throw(
                AssertionError("invalid memory should not click")
            )
        },
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        ["Recovered after invalid memory."],
        structured_responses=[
            _finalization("Recovered after invalid memory."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "sign in",
        model="openai:gpt-4.1-mini",
        memory="sign-in",
    ) == "Recovered after invalid memory."

    prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Replay failed before fallback:" in prompt_text
    assert expected_detail in prompt_text
    assert not any(
        event[0] == "prompt_click" for event in events if isinstance(event, tuple)
    )


def test_journey_browser_prompt_rebuilds_observation_after_failed_memory_replay(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    write_prompt_memory_entry(
        _prompt_memory_path(tmp_path, "sign-in"),
        PromptMemoryEntry(
            component="browser",
            instruction="sign in",
            observation_signature='{"title":"Login","url":"http://example.test/login"}',
            sections=(
                PromptMemorySection(
                    heading="Replay code",
                    body='page.locator("#continue").click(timeout=timeout_ms)',
                    language="python",
                ),
                PromptMemorySection(
                    heading="Success check code",
                    body='raise AssertionError("stale dashboard label")',
                    language="python",
                ),
            ),
            final_output="Signed in from memory.",
        ),
    )
    events: list[object] = []
    context = _FakePromptContext()

    def continue_to_dashboard() -> None:
        page._fake_url = "http://example.test/dashboard"
        page._fake_prompt_title = "Dashboard"
        page._fake_prompt_visible_texts = {"Dashboard Ready"}
        page._fake_prompt_elements = [_prompt_element("#done", name="Done")]
        page._fake_prompt_html = _prompt_html(
            title="Dashboard",
            elements=page._fake_prompt_elements,
            visible_texts=page._fake_prompt_visible_texts,
        )

    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[_prompt_element("#continue", name="Continue")],
        click_handlers={"#continue": continue_to_dashboard},
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        ["Recovered from live page."],
        structured_responses=[
            _finalization("Recovered from live page."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "sign in",
        model="openai:gpt-4.1-mini",
        memory="sign-in",
    ) == "Recovered from live page."

    prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Replay failed before fallback:" in prompt_text
    assert "Dashboard Ready" in prompt_text
    assert "http://example.test/dashboard" in prompt_text
    assert ("prompt_click", "Login", "#continue", 5000) in events


@pytest.mark.parametrize(
    ("sections", "expected_detail"),
    [
        (
            (
                PromptMemorySection(
                    heading="Success check code",
                    body="assert True",
                    language="python",
                ),
            ),
            "Replay code",
        ),
        (
            (
                PromptMemorySection(
                    heading="Replay code",
                    body='page.locator("#cached").click(timeout=timeout_ms)',
                    language="text",
                ),
                PromptMemorySection(
                    heading="Success check code",
                    body="assert True",
                    language="python",
                ),
            ),
            "Replay code",
        ),
    ],
)
def test_journey_browser_prompt_validates_memory_sections_at_browser_boundary(
    monkeypatch,
    tmp_path: Path,
    sections: tuple[PromptMemorySection, ...],
    expected_detail: str,
):
    monkeypatch.chdir(tmp_path)
    write_prompt_memory_entry(
        _prompt_memory_path(tmp_path, "sign-in"),
        PromptMemoryEntry(
            component="browser",
            instruction="sign in",
            observation_signature='{"title":"Login","url":"http://example.test/login"}',
            sections=sections,
            final_output="Signed in from memory.",
        ),
    )
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
        ["Recovered after invalid memory."],
        structured_responses=[
            _finalization("Recovered after invalid memory."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "sign in",
        model="openai:gpt-4.1-mini",
        memory="sign-in",
    ) == "Recovered after invalid memory."

    prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Replay failed before fallback:" in prompt_text
    assert expected_detail in prompt_text


def test_journey_browser_prompt_does_not_reuse_legacy_memory_shape(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    memory_path = tmp_path / "legacy.memory.json"
    memory_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "legacy-key": {
                        "tool": "playwright",
                        "page_signature": '{"title":"Chat","url":"http://example.test/chat"}',
                        "successful_steps": [
                            {
                                "target": 'page.locator("#legacy").click(timeout=timeout_ms)',
                                "detail": "worked before",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Chat",
        url="http://example.test/chat",
        context=context,
        events=events,
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        ["Done."],
        structured_responses=[
            _finalization("Done."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    page.prompt("say hello", model="openai:gpt-4.1-mini", memory="legacy")

    prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    assert "Prompt memory:" not in prompt_text
    assert "#legacy" not in prompt_text


def test_journey_browser_prompt_execute_uses_state_dir_parent_as_memory_root(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / "flows" / ".journey" / "state.json"
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
        [
            "Done.",
            "\n".join(
                [
                    "## Replay code",
                    "```python",
                    (
                        'page.locator("text=Done").wait_for'
                        '(state="visible", timeout=timeout_ms)'
                    ),
                    "```",
                    "",
                    "## Success check code",
                    "```python",
                    "assert True",
                    "```",
                    "",
                    "## Notes",
                    "No actions are needed.",
                ]
            ),
        ],
        structured_responses=[
            _finalization("Done."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )
    write_paths: list[Path] = []

    def capture_memory_write(path: Path, entry: PromptMemoryEntry) -> int:
        assert entry.component == "browser"
        write_paths.append(path)
        return 1

    monkeypatch.setattr(
        journey_prompt_engine,
        "write_prompt_memory_entry",
        capture_memory_write,
    )

    def run_prompt() -> str | dict[str, object]:
        return page.prompt("finish", model="openai:gpt-4.1-mini", memory="state-root")

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    journey_sdk.execute(memory_journey, state=state_path, no_logs=True)

    assert write_paths == [tmp_path.resolve() / "flows" / "state-root.memory.md"]


def test_journey_browser_prompt_respects_execute_no_memory(monkeypatch):
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            ["Done."],
            structured_responses=[
                _finalization("Done."),
            ],
        ),
    )

    def fail_memory_access(*args: object, **kwargs: object) -> object:
        raise AssertionError("prompt memory should be disabled")

    monkeypatch.setattr(
        journey_prompt_engine,
        "load_prompt_memory_entry",
        fail_memory_access,
    )
    monkeypatch.setattr(
        journey_prompt_engine,
        "write_prompt_memory_entry",
        fail_memory_access,
    )

    def run_prompt() -> str | dict[str, object]:
        return page.prompt("finish", model="openai:gpt-4.1-mini", memory="disabled")

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    journey_sdk.execute(memory_journey, no_memory=True)


def test_journey_browser_prompt_uses_generated_callsite_memory_by_default(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
    )
    context.pages.append(page)
    fake_model = _FakeLangChainPromptModel(
        [
            "Done.",
            "\n".join(
                [
                    "## Replay code",
                    "```python",
                    "page.wait_for_timeout(1)",
                    "```",
                    "",
                    "## Success check code",
                    "```python",
                    "page.wait_for_timeout(1)",
                    "```",
                    "",
                    "## Notes",
                    "No-op prompt memory for test.",
                ]
            ),
        ],
        structured_responses=[_finalization("Done.")],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: fake_model,
    )

    def run_prompt() -> str | dict[str, object]:
        return page.prompt("finish", model="openai:gpt-4.1-mini")

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    plan = journey_sdk.compile_journey(memory_journey)
    journey_executor._execute_plan(
        memory_journey,
        plan=plan,
        no_state=True,
        prompt_memory_root=tmp_path,
    )

    assert _prompt_memory_path(tmp_path, "run-prompt-prompt-1").exists()


def test_journey_browser_prompt_memory_none_disables_callsite_memory(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            ["Done."],
            structured_responses=[_finalization("Done.")],
        ),
    )

    def fail_memory_access(*args: object, **kwargs: object) -> object:
        raise AssertionError("prompt memory should be disabled")

    monkeypatch.setattr(
        journey_prompt_engine,
        "load_prompt_memory_entry",
        fail_memory_access,
    )
    monkeypatch.setattr(
        journey_prompt_engine,
        "write_prompt_memory_entry",
        fail_memory_access,
    )

    def run_prompt() -> str | dict[str, object]:
        return page.prompt("finish", model="openai:gpt-4.1-mini", memory=None)

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    plan = journey_sdk.compile_journey(memory_journey)
    journey_executor._execute_plan(
        memory_journey,
        plan=plan,
        no_state=True,
        prompt_memory_root=tmp_path,
    )

    assert not list(tmp_path.glob("*.memory.md"))


def test_journey_browser_prompt_respects_execute_no_memory_update(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[_prompt_element("#cached", name="Cached")],
    )
    context.pages.append(page)

    def mark_done() -> None:
        page._fake_prompt_visible_texts.add("Done")

    page._fake_prompt_click_handlers["#cached"] = mark_done

    load_calls: list[tuple[Path, str, str, str]] = []
    write_calls: list[tuple[object, ...]] = []

    def load_memory(
        path: Path,
        *,
        component: str,
        instruction: str,
        observation_signature: str,
    ) -> PromptMemoryEntry:
        load_calls.append((path, component, instruction, observation_signature))
        return PromptMemoryEntry(
            component=component,
            instruction=instruction,
            observation_signature=observation_signature,
            sections=(
                PromptMemorySection(
                    heading="Replay code",
                    body='page.locator("#cached").click(timeout=timeout_ms)',
                    language="python",
                ),
                PromptMemorySection(
                    heading="Success check code",
                    body=(
                        'page.locator("text=Done").wait_for'
                        '(state="visible", timeout=timeout_ms)'
                    ),
                    language="python",
                ),
            ),
            final_output="Done from readonly memory.",
        )

    def fail_memory_write(*args: object, **kwargs: object) -> object:
        write_calls.append(args)
        raise AssertionError("prompt memory updates should be disabled")

    monkeypatch.setattr(journey_prompt_engine, "load_prompt_memory_entry", load_memory)
    monkeypatch.setattr(
        journey_prompt_engine,
        "write_prompt_memory_entry",
        fail_memory_write,
    )

    def fail_model_load(model_name: str) -> object:
        raise AssertionError("readonly replay should not require a model")

    monkeypatch.setattr(journey_browser_prompt, "_load_langchain_model", fail_model_load)

    def run_prompt() -> str | dict[str, object]:
        return page.prompt("finish", model="openai:gpt-4.1-mini", memory="readonly")

    def memory_journey() -> None:
        journey_sdk.step(run_prompt)

    journey_sdk.execute(memory_journey, no_memory_update=True)

    assert load_calls
    assert not write_calls
    assert ("prompt_click", "Login", "#cached", 5000) in events


def test_journey_browser_prompt_enforces_max_steps(
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            [_run_code('page.locator("#sign-in").click(timeout=timeout_ms)')]
        ),
    )

    with pytest.raises(RuntimeError, match="reached max_steps=1"):
        page.prompt("click sign in", model="openai:gpt-4.1-mini", max_steps=1)
    log_output = capsys.readouterr().out

    assert "1/1 action" in log_output
    assert "click selector '#sign-in'" in log_output
    assert "1/1 ok" in log_output
    assert "page 0 'Login'" in log_output
    assert "AI prompt                   JourneyBrowserPage.prompt(...) reached max_steps=1" in log_output


def test_journey_browser_prompt_retries_invalid_action_arguments(monkeypatch):
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
        [_run_code("   "), "Recovered."],
        structured_responses=[
            _finalization("Recovered."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    result = page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert result == "Recovered."
    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert '"action_type":"tool"' in second_prompt_text
    assert '"status":"rejected"' in second_prompt_text


def test_journey_browser_prompt_retries_invalid_python(monkeypatch):
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
        [_run_code('page.locator("#sign-in"'), "Recovered."],
        structured_responses=[
            _finalization("Recovered."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    result = page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert result == "Recovered."
    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert '"target":"page.locator(\\"#sign-in\\""' in second_prompt_text
    assert '"status":"rejected"' in second_prompt_text
    assert "not valid Python" in second_prompt_text


def test_journey_browser_prompt_allows_safe_builtins_and_captures_print(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Dashboard",
        url="http://example.test/dashboard",
        context=context,
        events=events,
        visible_texts={"Ready"},
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        [
            _run_code(
                "\n".join(
                    [
                        "values = sorted([3, 1, 2])",
                        'print("count=", len(values), " values=", values, sep="")',
                        "try:",
                        '    raise ValueError("expected")',
                        "except Exception as exc:",
                        '    print("caught ", str(exc), sep="")',
                        "assert isinstance(values, list)",
                        "assert values == [1, 2, 3]",
                    ]
                )
            ),
            "Diagnostics collected.",
        ],
        structured_responses=[
            _finalization("Diagnostics collected."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt("inspect state", model="openai:gpt-4.1-mini") == "Diagnostics collected."
    log_output = capsys.readouterr().out

    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert "Captured print output" in second_prompt_text
    assert "count=3 values=[1, 2, 3]" in second_prompt_text
    assert "caught expected" in second_prompt_text
    assert "count=3 values=[1, 2, 3]" not in log_output
    assert "caught expected" not in log_output


@pytest.mark.parametrize(
    ("code", "blocked_name"),
    [
        ('__import__("os")', "__import__"),
        ('open("secret.txt")', "open"),
    ],
)
def test_journey_browser_prompt_rejects_blocked_python_builtins(
    monkeypatch,
    code: str,
    blocked_name: str,
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Dashboard",
        url="http://example.test/dashboard",
        context=context,
        events=events,
        visible_texts={"Ready"},
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        [_run_code(code), "Recovered."],
        structured_responses=[
            _finalization("Recovered."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt("inspect state", model="openai:gpt-4.1-mini") == "Recovered."

    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert '"status":"rejected"' in second_prompt_text
    assert "blocked Python name" in second_prompt_text
    assert blocked_name in second_prompt_text


def test_journey_browser_prompt_rejects_missing_timeout_before_playwright_default(
    monkeypatch,
):
    events: list[object] = []
    context = _FakePromptContext()
    page = _make_prompt_page(
        title="Login",
        url="http://example.test/login",
        context=context,
        events=events,
        elements=[_prompt_element("#submit", name="Submit")],
    )
    context.pages.append(page)
    model = _FakeLangChainPromptModel(
        [
            _run_code('page.locator("#submit").click()'),
            "Recovered after rejected action.",
        ],
        structured_responses=[
            _finalization("Recovered after rejected action."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "click submit",
        model="openai:gpt-4.1-mini",
    ) == "Recovered after rejected action."

    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert '"status":"rejected"' in second_prompt_text
    assert "timeout=timeout_ms" in second_prompt_text
    assert not any(
        event[0] == "prompt_click" for event in events if isinstance(event, tuple)
    )


@pytest.mark.parametrize(
    ("memory_code", "expected_detail"),
    [
        ("await page.locator('#submit').click(timeout=timeout_ms)", "async/await"),
        ("import time\npage.wait_for_timeout(1)", "uses imports"),
        ("re.compile('^Archive$')", "unsupported Python name 're'"),
        ("page.locator('#submit').click()", "timeout=timeout_ms"),
        ("page.wait_for_timeout(60000)", "exceeds the 5000ms"),
    ],
)
def test_journey_browser_prompt_skips_invalid_compiled_memory(
    monkeypatch,
    tmp_path: Path,
    memory_code: str,
    expected_detail: str,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)
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
        [
            "Done.",
            "\n".join(
                [
                    "## Replay code",
                    "```python",
                    memory_code,
                    "```",
                    "",
                    "## Success check code",
                    "```python",
                    "assert True",
                    "```",
                    "",
                    "## Notes",
                    "Invalid memory should not be saved.",
                ]
            ),
        ],
        structured_responses=[
            _finalization("Done."),
        ],
    )
    monkeypatch.setattr(
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model_name: model,
    )

    assert page.prompt(
        "finish",
        model="openai:gpt-4.1-mini",
        memory="invalid-compile",
    ) == "Done."

    assert not _prompt_memory_path(tmp_path, "invalid-compile").exists()
    log_output = capsys.readouterr().out
    assert "memory compile" in log_output
    assert expected_detail in log_output


def test_journey_browser_prompt_python_environment_prompts_document_allowlist():
    for prompt_text in (
        journey_browser_prompt._PROMPT_SYSTEM_MESSAGE,
        journey_browser_prompt._PROMPT_MEMORY_COMPILER_SYSTEM_MESSAGE,
    ):
        assert "safe builtins" in prompt_text
        assert "print, len, str" in prompt_text
        assert "isinstance" in prompt_text
        assert "Exception" in prompt_text
        assert "__import__" in prompt_text
        assert "open, eval, exec, compile" in prompt_text
        assert "sync Playwright" in prompt_text
        assert "async/await" in prompt_text
        assert "timeout=timeout_ms" in prompt_text
    assert "Do not include diagnostic-only print" in (
        journey_browser_prompt._PROMPT_MEMORY_COMPILER_SYSTEM_MESSAGE
    )
    assert "hard sleeps longer than" in (
        journey_browser_prompt._PROMPT_MEMORY_COMPILER_SYSTEM_MESSAGE
    )
    assert "current page state as" in journey_browser_prompt._PROMPT_SYSTEM_MESSAGE


def test_journey_browser_prompt_rejects_json_as_python_failure(monkeypatch):
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            [
                _run_code('{"action":"hover","target":"e1","value":null}'),
                "Recovered.",
            ],
            structured_responses=[
                _finalization("Recovered."),
            ],
        ),
    )

    result = page.prompt("click sign in", model="openai:gpt-4.1-mini")

    assert result == "Recovered."


def test_journey_browser_prompt_rejects_blank_finish_then_recovers(monkeypatch):
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
        journey_browser_prompt,
        "_load_langchain_model",
        lambda model: _FakeLangChainPromptModel(
            [_run_code('finish("")'), "Done."],
            structured_responses=[
                _finalization("Done."),
            ],
        ),
    )

    result = page.prompt("finish clearly", model="openai:gpt-4.1-mini")

    assert result == "Done."
