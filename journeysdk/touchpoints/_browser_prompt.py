"""Private helpers for Journey browser prompting."""

from __future__ import annotations

import ast
import base64
import builtins
import json
import re
from dataclasses import dataclass, field
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from types import FrameType
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from langchain_core.tools import tool
from journeysdk.logger import (
    JourneyLogRecord,
    PrettyLine,
    PrettyStyle,
    get_logger,
    make_log_record,
    pretty_line,
    pretty_row,
)
from journeysdk._prompt_memory import (
    PROMPT_MEMORY_AUTO,
    PromptMemoryEntry,
    PromptMemorySpec,
    PromptMemorySection,
    resolve_prompt_memory_path,
)
from journeysdk._prompt_output import (
    PromptOutputSchema,
    PromptOutputSpec,
    normalize_prompt_output_spec,
)
from journeysdk._prompt_engine import (
    PromptEngineSession,
    PromptMemoryCompileContext,
    PromptMemoryDraft,
    PromptMemoryReplayResult,
    PromptObservation,
    PromptActionContext,
    PromptTextSection,
    _create_langchain_agent as _create_prompt_engine_agent,
    _exception_hint,
    _extract_langchain_text,
    _load_langchain_model as _load_prompt_engine_model,
    _runtime_error_with_hint,
    resolve_prompt_model,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

JOURNEY_BROWSER_PROMPT_MODEL_ENV = "JOURNEY_BROWSER_PROMPT_MODEL"
DEFAULT_JOURNEY_BROWSER_PROMPT_MODEL = "anthropic:claude-haiku-4-5"

_PROMPT_RUN_CODE_ACTION_NAME = "journey_run_code"
_PROMPT_SCREENSHOT_ACTION_NAME = "journey_screenshot"
_PROMPT_INSPECT_DOM_ACTION_NAME = "journey_inspect_dom"
_BROWSER_REPLAY_SECTION = "Replay code"
_BROWSER_SUCCESS_CHECK_SECTION = "Success check code"
_BROWSER_NOTES_SECTION = "Notes"
_PROMPT_PRINT_OUTPUT_MAX_LENGTH = 2000
_PROMPT_SAFE_BUILTIN_NAMES = (
    "len",
    "str",
    "repr",
    "int",
    "float",
    "bool",
    "list",
    "tuple",
    "dict",
    "set",
    "range",
    "enumerate",
    "zip",
    "any",
    "all",
    "min",
    "max",
    "sum",
    "sorted",
    "isinstance",
    "abs",
    "round",
    "Exception",
    "AssertionError",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "TimeoutError",
)
_PROMPT_SAFE_BUILTINS: dict[str, object] = {
    name: getattr(builtins, name) for name in _PROMPT_SAFE_BUILTIN_NAMES
}
_PROMPT_SAFE_BUILTINS_TEXT = ", ".join(("print", *_PROMPT_SAFE_BUILTIN_NAMES))
_PROMPT_BLOCKED_BUILTINS_TEXT = (
    "__import__, open, eval, exec, compile, input, globals, locals, vars, dir, "
    "getattr, setattr, delattr, breakpoint"
)

_PROMPT_SYSTEM_MESSAGE = f"""You control a Playwright sync browser page with available actions.

Use actions when the browser needs more work. When no more browser action is needed, return a concise
completion signal describing the current visible result. A structured finalizer will decide whether the
instruction succeeded and will produce the returned output.

Use journey_run_code to execute one Python snippet against the active page.
Use journey_inspect_dom only when the semantic DOM snapshot is insufficient and you need raw DOM for a specific
selector or page area.
Use journey_screenshot only when semantic DOM, visible text, page records, and prior action results are not enough
to understand visual state.

Available names:
- page: the active Playwright sync Page
- pages: a tuple of known Playwright sync Page objects
- timeout_ms: the configured action timeout in milliseconds
- switch_page(index): switch the active page to a known page index and return that Page
- safe builtins: {_PROMPT_SAFE_BUILTINS_TEXT}

Rules:
- Inspect semantic DOM, visible text, page records, and prior action results first, then choose the fewest Playwright
  commands needed.
- Call journey_inspect_dom with a narrow selector when raw DOM is needed for an element or page area.
- Call journey_screenshot only when visual layout, screenshots, canvas, images, clipping, or styling are necessary.
- Prefer robust Playwright locators such as get_by_role, get_by_text, get_by_label, and locator.
- If a semantic DOM control shows visible text but a generic aria-label such as "Button", locate it by visible
  text, for example locator("button").filter(has_text="..."), because get_by_role(..., name=...) uses aria-label.
- Pass timeout=timeout_ms to actions and waits that accept a timeout.
- Use print only for concise diagnostics that should be visible to the next prompt turn; print output is captured in the
  action result.
- Do not use blocked Python builtins or imports such as {_PROMPT_BLOCKED_BUILTINS_TEXT}.
- Use switch_page(index) before acting on a popup or tab listed in known pages.
- Treat all expectation wording in the instruction, such as "Expect ...", "should ...", and "must ...", as required
  success criteria.
- Do not return a completion signal until no more browser action is needed or possible.
- Do not treat a failed sign-in, failed checkout, failed submission, or similar rejected user task as complete just
  because the page displays the final error state.
- If the previous step was rejected, correct the action arguments or Python and try again.
- If prompt memory is provided, use it as a hint from prior successful runs,
  but trust the current semantic DOM, visible text, DOM inspections, and requested screenshots over stale memory.
- The completion signal should be factual and based on semantic DOM, visible text, DOM inspections, requested screenshots, known
  pages, and executed steps.
- Do not mention implementation details, hidden reasoning, or unavailable metadata.
"""

_PROMPT_MEMORY_COMPILER_SYSTEM_MESSAGE = f"""Create replayable Journey browser prompt memory.

Return Markdown with exactly these sections:

## Replay code
```python
<minimal Playwright code to perform the successful path next time>
```

## Success check code
```python
<assertions or waits that prove the original instruction is complete>
```

## Notes
<short notes for fallback prompting>

Rules:
- Use only names available to Journey prompt code: page, pages, timeout_ms, switch_page(index), and safe builtins:
  {_PROMPT_SAFE_BUILTINS_TEXT}.
- Do not use blocked Python builtins or imports such as {_PROMPT_BLOCKED_BUILTINS_TEXT}.
- Do not include diagnostic-only print calls in replay or success check code.
- Keep only the successful path needed for the next run.
- Remove rejected, failed, speculative, redundant, or superseded attempts.
- If a later fallback corrected an earlier value, keep only the corrected value.
- Success check code must assert all required success criteria from the instruction, including "Expect ..." clauses.
- Prefer robust Playwright locators and pass timeout=timeout_ms to waits/actions.
- Do not include screenshots, semantic DOM, raw DOM, hidden reasoning, or prose outside the requested sections.
"""

_RENDERED_HTML_SCRIPT = "() => document.documentElement ? document.documentElement.outerHTML : ''"
_INSPECT_DOM_SCRIPT = (
    """(selector) => {
  const root = selector ? document.querySelector(selector) : document.documentElement;
  return root ? root.outerHTML : null;
}"""
)
_VISIBLE_TEXT_SCRIPT = "() => document.body ? document.body.innerText : ''"
_PROMPT_LOGGER = get_logger("browser-prompt")
_PROMPT_DETAIL_INDENT = 10
_PROMPT_LABEL_WIDTH = 25
_SEMANTIC_DOM_MAX_LINES = 1200
_SEMANTIC_DOM_MAX_TEXT_LENGTH = 240
_SEMANTIC_DOM_MAX_ATTR_LENGTH = 240
_SEMANTIC_DOM_MAX_CLASS_TOKENS = 6
_MEMORY_DRAFT_FENCE_PATTERN = re.compile(
    r"^```(?P<language>[A-Za-z0-9_-]*)\n(?P<body>.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def _prompt_row(label: object, detail: object = "", *, style: PrettyStyle = "accent") -> PrettyLine:
    return pretty_row(
        label,
        detail,
        indent=_PROMPT_DETAIL_INDENT,
        label_width=_PROMPT_LABEL_WIDTH,
        style=style,
    )


def _prompt_continuation(detail: object, *, style: PrettyStyle = "code") -> PrettyLine:
    return pretty_line(
        detail,
        indent=_PROMPT_DETAIL_INDENT + _PROMPT_LABEL_WIDTH + 1,
        style=style,
    )


def _prompt_step_ref(*, step: int | None, max_steps: int | None, step_label: str | None = None) -> str:
    if step is not None and max_steps is not None:
        return f"{step}/{max_steps}"
    if step_label is not None and step_label.startswith("step "):
        return step_label.removeprefix("step ")
    return step_label or "step"


class _PromptSession:
    def __init__(
        self,
        *,
        page: PlaywrightPage,
        instruction: str,
        model: str,
        max_steps: int,
        action_timeout_seconds: float,
        memory_path: Path | None,
        output_schema: PromptOutputSchema | None,
    ) -> None:
        self._original_page = page
        self._instruction = instruction
        self._model = model
        self._max_steps = max_steps
        self._timeout_ms = int(action_timeout_seconds * 1000)
        self._memory_path = memory_path
        self._output_schema = output_schema
        self._pages: list[PlaywrightPage] = [page]
        self._active_page_index = 0

    def run(self) -> str | dict[str, object]:
        self._log_start()
        try:
            return PromptEngineSession(
                component="browser",
                owner="JourneyBrowserPage.prompt(...)",
                instruction=self._instruction,
                model=self._model,
                max_steps=self._max_steps,
                memory_path=self._memory_path,
                output_schema=self._output_schema,
                system_prompt=_PROMPT_SYSTEM_MESSAGE,
                logger=_PROMPT_LOGGER,
                build_observation=self._build_observation,
                build_actions=self._build_agent_actions,
                build_final_observation=self._build_final_observation,
                load_model=_load_langchain_model,
                create_agent=_create_langchain_agent,
                before_final_observation=self._settle_active_page_for_final_output,
                replay_memory=self._replay_memory,
                compile_memory=self._compile_memory,
                format_memory=_format_browser_memory_for_prompt,
            ).run()
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            raise

    def _log_start(self) -> None:
        self._discover_pages()
        active_page = self._prompt_page_payloads()[self._active_page_index]
        timeout_seconds = self._timeout_ms / 1000
        _emit_prompt_log(
            "prompt start: "
            f"instruction={self._instruction!r}; "
            f"model={self._model!r}; "
            f"max_steps={self._max_steps}; "
            f"timeout={timeout_seconds:g}s; "
            f"active={_page_summary(active_page)}",
            event="prompt_start",
            pretty=[
                pretty_row(
                    "AI prompt",
                    (
                        f"model={self._model} "
                        f"max_steps={self._max_steps} "
                        f"timeout={timeout_seconds:g}s"
                    ),
                    indent=8,
                    label_width=27,
                    style="accent",
                ),
                _prompt_row("instruction", self._instruction, style="accent"),
                _prompt_row("page", _page_summary(active_page), style="accent"),
            ],
            instruction=self._instruction,
            model=self._model,
            max_steps=self._max_steps,
            timeout=f"{timeout_seconds:g}s",
            active=_page_summary(active_page),
        )

    def _log_action(
        self,
        *,
        step_index: int,
        code: str,
    ) -> tuple[JourneyLogRecord, ...]:
        normalized_code = code.strip()
        action_description = _describe_prompt_action(normalized_code)
        records = [
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: AI will "
                f"{action_description}",
                event="prompt_action",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} action",
                    action_description,
                    style="accent",
                ),
                step=step_index,
                max_steps=self._max_steps,
                action=action_description,
            )
        ]
        records.extend(
            _emit_prompt_code_log(
                step_label=f"step {step_index}/{self._max_steps}",
                code=normalized_code,
            )
        )
        return tuple(records)

    def _log_new_pages(
        self,
        *,
        previous_page_count: int,
    ) -> tuple[JourneyLogRecord, ...]:
        current_pages = self._prompt_page_payloads()
        records: list[JourneyLogRecord] = []
        for page in current_pages[previous_page_count:]:
            records.append(
                _emit_prompt_log(
                    f"discovered {_page_summary(page)}",
                    event="page_discovered",
                    pretty=_prompt_row(
                        "page discovered",
                        _page_summary(page),
                        style="accent",
                    ),
                    page=page["index"],
                    title=page["title"],
                    url=page["url"],
                )
            )
        return tuple(records)

    def _log_active_page_change(
        self,
        *,
        previous_active_page_index: int,
    ) -> tuple[JourneyLogRecord, ...]:
        if previous_active_page_index == self._active_page_index:
            return ()
        active_page = self._prompt_page_payloads()[self._active_page_index]
        return (
            _emit_prompt_log(
                f"active page changed to {_page_summary(active_page)}",
                event="active_page_change",
                pretty=_prompt_row(
                    "active page",
                    _page_summary(active_page),
                    style="accent",
                ),
                previous_page=previous_active_page_index,
                active_page=self._active_page_index,
                page_summary=_page_summary(active_page),
            ),
        )

    def _build_observation(self) -> PromptObservation:
        self._discover_pages()
        pages = self._prompt_page_payloads()
        active_page = self._pages[self._active_page_index]
        semantic_dom = _semantic_dom_snapshot(active_page)
        visible_text = _visible_text(active_page)
        return PromptObservation(
            signature=_page_memory_signature(pages[self._active_page_index]),
            records=tuple(_page_record(page) for page in pages),
            sections=(
                PromptTextSection(
                    heading="Active page visible text",
                    text=visible_text,
                    tag="visible-text",
                ),
                PromptTextSection(
                    heading="Active page semantic DOM",
                    text=semantic_dom,
                    tag="semantic-dom",
                ),
            ),
            visible_text=visible_text,
        )

    def _build_final_observation(self) -> PromptObservation:
        self._discover_pages()
        pages = self._prompt_page_payloads()
        active_page = self._pages[self._active_page_index]
        visible_text = _visible_text(active_page)
        return PromptObservation(
            signature=_page_memory_signature(pages[self._active_page_index]),
            records=tuple(_page_record(page) for page in pages),
            sections=(
                PromptTextSection(
                    heading="Active page visible text",
                    text=visible_text,
                    tag="visible-text",
                ),
            ),
            visible_text=visible_text,
        )

    def _build_agent_actions(self, context: PromptActionContext) -> list[object]:
        @tool(_PROMPT_RUN_CODE_ACTION_NAME)
        def journey_run_code(code: str) -> list[dict[str, object]]:
            """Execute one Playwright sync Python snippet against the active Journey browser page."""

            return context.run_on_prompt_thread(
                lambda: self._run_code_tool(code, context=context)
            )

        @tool(_PROMPT_SCREENSHOT_ACTION_NAME)
        def journey_screenshot() -> list[dict[str, object]]:
            """Capture one screenshot of the active Journey browser page."""

            return context.run_on_prompt_thread(
                lambda: self._run_screenshot_tool(context=context)
            )

        @tool(_PROMPT_INSPECT_DOM_ACTION_NAME)
        def journey_inspect_dom(selector: str | None = None) -> list[dict[str, object]]:
            """Inspect raw DOM for the active page or a specific CSS selector."""

            return context.run_on_prompt_thread(
                lambda: self._run_inspect_dom_tool(selector, context=context)
            )

        return [journey_run_code, journey_screenshot, journey_inspect_dom]

    def _replay_memory(
        self,
        entry: PromptMemoryEntry,
    ) -> PromptMemoryReplayResult | None:
        _emit_prompt_log(
            f"replaying prompt memory on {_page_summary(self._prompt_page_payloads()[self._active_page_index])}",
            event="prompt_memory_replay_start",
            pretty=_prompt_row(
                "memory replay",
                _page_summary(self._prompt_page_payloads()[self._active_page_index]),
                style="accent",
            ),
        )
        try:
            replay_code = _browser_memory_code(
                entry,
                _BROWSER_REPLAY_SECTION,
            )
            success_check_code = _browser_memory_code(
                entry,
                _BROWSER_SUCCESS_CHECK_SECTION,
            )
            self._execute_python_code(
                replay_code,
                filename="<journey-browser-memory-replay>",
            )
            self._execute_python_code(
                success_check_code,
                filename="<journey-browser-memory-check>",
            )
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            detail = _format_python_error(exc)
            _emit_prompt_log(
                f"prompt memory replay failed: {detail}",
                event="prompt_memory_replay_failed",
                pretty=_prompt_row("memory replay failed", detail, style="warning"),
                detail=detail,
            )
            raise RuntimeError(detail) from exc
        _emit_prompt_log(
            "prompt memory replay succeeded",
            event="prompt_memory_replay_success",
            pretty=_prompt_row("memory replay", "succeeded", style="success"),
        )
        return PromptMemoryReplayResult(final_output=entry.final_output)

    def _compile_memory(
        self,
        context: PromptMemoryCompileContext,
    ) -> PromptMemoryDraft | None:
        try:
            model = _load_langchain_model(self._model)
            messages = [
                {
                    "role": "system",
                    "content": _PROMPT_MEMORY_COMPILER_SYSTEM_MESSAGE,
                },
                {
                    "role": "user",
                    "content": _memory_compile_prompt(context),
                },
            ]
            response = context.invoke_model(
                "memory_compile",
                lambda config: model.invoke(
                    messages,
                    config=config,
                ),
            )
            response_text = _extract_langchain_text(
                response,
                owner="JourneyBrowserPage.prompt(...) memory compiler",
            )
            return _parse_memory_draft(response_text)
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            _emit_prompt_log(
                f"prompt memory compile skipped: {_format_python_error(exc)}",
                event="prompt_memory_compile_failed",
                pretty=_prompt_row(
                    "memory compile",
                    _format_python_error(exc),
                    style="warning",
                ),
                detail=_format_python_error(exc),
            )
            return None

    def _run_code_tool(
        self,
        code: str,
        *,
        context: PromptActionContext,
    ) -> list[dict[str, object]]:
        step_index = context.next_step_index()
        try:
            normalized_code = _strip_code_fences(
                _require_text_value(
                    code,
                    "JourneyBrowserPage.prompt(...) "
                    "journey_run_code expects a non-blank code string.",
                )
            )
            target = _first_code_line(normalized_code)
        except Exception as exc:
            self._append_rejected_action(
                context=context,
                step_index=step_index,
                action_type="tool",
                target=_PROMPT_RUN_CODE_ACTION_NAME,
                detail=_format_python_error(exc),
            )
            return context.observation_or_stop(step_index=step_index)

        previous_page_count = len(self._pages)
        previous_active_page_index = self._active_page_index
        for record in self._log_action(step_index=step_index, code=normalized_code):
            context.record_memory_log(record)
        try:
            step = self._execute_python_step(
                step_index=step_index,
                code=normalized_code,
                target=target,
            )
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            for record in self._log_new_pages(
                previous_page_count=previous_page_count,
            ):
                context.record_memory_log(record)
            for record in self._log_active_page_change(
                previous_active_page_index=previous_active_page_index,
            ):
                context.record_memory_log(record)
            self._append_rejected_action(
                context=context,
                step_index=step_index,
                action_type="python",
                target=target,
                detail=_format_python_error(exc),
            )
            return context.observation_or_stop(step_index=step_index)

        for record in self._log_new_pages(previous_page_count=previous_page_count):
            context.record_memory_log(record)
        for record in self._log_active_page_change(
            previous_active_page_index=previous_active_page_index,
        ):
            context.record_memory_log(record)
        context.record_memory_log(
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: succeeded on "
                f"{_page_summary(self._prompt_page_payloads()[self._active_page_index])}",
                event="prompt_step_success",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} ok",
                    _page_summary(
                        self._prompt_page_payloads()[self._active_page_index]
                    ),
                    style="success",
                ),
                step=step_index,
                max_steps=self._max_steps,
                page=self._active_page_index,
            )
        )
        context.record_action(step)
        return context.observation_or_stop(step_index=step_index)

    def _run_screenshot_tool(
        self,
        *,
        context: PromptActionContext,
    ) -> list[dict[str, object]]:
        step_index = context.next_step_index()
        previous_page_count = len(self._pages)
        previous_active_page_index = self._active_page_index
        context.record_memory_log(
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: AI will capture screenshot",
                event="prompt_action",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} action",
                    "capture screenshot",
                    style="accent",
                ),
                step=step_index,
                max_steps=self._max_steps,
                action="capture screenshot",
            )
        )
        try:
            self._discover_pages()
            active_page = self._pages[self._active_page_index]
            page_summary = _page_summary(
                self._prompt_page_payloads()[self._active_page_index]
            )
            screenshot_data_url = _png_data_url(active_page)
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            for record in self._log_new_pages(
                previous_page_count=previous_page_count,
            ):
                context.record_memory_log(record)
            for record in self._log_active_page_change(
                previous_active_page_index=previous_active_page_index,
            ):
                context.record_memory_log(record)
            self._append_rejected_action(
                context=context,
                step_index=step_index,
                action_type="screenshot",
                target="active page",
                detail=_format_python_error(exc),
            )
            return context.observation_or_stop(step_index=step_index)

        for record in self._log_new_pages(previous_page_count=previous_page_count):
            context.record_memory_log(record)
        for record in self._log_active_page_change(
            previous_active_page_index=previous_active_page_index,
        ):
            context.record_memory_log(record)
        context.record_action(
            _prompt_action_record(
                step_index=step_index,
                max_steps=self._max_steps,
                page_index=self._active_page_index,
                action_type="screenshot",
                target="active page",
                status="ok",
                detail=f"Captured screenshot of {page_summary}.",
            )
        )
        context.record_memory_log(
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: captured screenshot on "
                f"{page_summary}",
                event="prompt_screenshot",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} ok",
                    f"screenshot captured on {page_summary}",
                    style="success",
                ),
                step=step_index,
                max_steps=self._max_steps,
                page=self._active_page_index,
                page_summary=page_summary,
            )
        )
        context.raise_if_step_limit_reached(step_index=step_index)
        return [
            {
                "type": "text",
                "text": f"Screenshot captured for {page_summary}.",
            },
            {
                "type": "image_url",
                "image_url": {"url": screenshot_data_url},
            },
        ]

    def _run_inspect_dom_tool(
        self,
        selector: str | None,
        *,
        context: PromptActionContext,
    ) -> list[dict[str, object]]:
        step_index = context.next_step_index()
        previous_page_count = len(self._pages)
        previous_active_page_index = self._active_page_index
        normalized_selector = _normalize_optional_selector(selector)
        target = normalized_selector or "document"
        context.record_memory_log(
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: AI will inspect DOM for {target}",
                event="prompt_action",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} action",
                    f"inspect DOM for {target}",
                    style="accent",
                ),
                step=step_index,
                max_steps=self._max_steps,
                action=f"inspect DOM for {target}",
            )
        )
        try:
            self._discover_pages()
            active_page = self._pages[self._active_page_index]
            page_summary = _page_summary(
                self._prompt_page_payloads()[self._active_page_index]
            )
            dom = _inspect_dom(active_page, normalized_selector)
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            for record in self._log_new_pages(
                previous_page_count=previous_page_count,
            ):
                context.record_memory_log(record)
            for record in self._log_active_page_change(
                previous_active_page_index=previous_active_page_index,
            ):
                context.record_memory_log(record)
            self._append_rejected_action(
                context=context,
                step_index=step_index,
                action_type="inspect_dom",
                target=target,
                detail=_format_python_error(exc),
            )
            return context.observation_or_stop(step_index=step_index)

        for record in self._log_new_pages(previous_page_count=previous_page_count):
            context.record_memory_log(record)
        for record in self._log_active_page_change(
            previous_active_page_index=previous_active_page_index,
        ):
            context.record_memory_log(record)
        context.record_action(
            _prompt_action_record(
                step_index=step_index,
                max_steps=self._max_steps,
                page_index=self._active_page_index,
                action_type="inspect_dom",
                target=target,
                status="ok",
                detail=f"Inspected DOM for {target} on {page_summary}.",
            )
        )
        context.record_memory_log(
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: inspected DOM for "
                f"{target} on {page_summary}",
                event="prompt_dom_inspection",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} ok",
                    f"DOM inspected for {target}",
                    style="success",
                ),
                step=step_index,
                max_steps=self._max_steps,
                page=self._active_page_index,
                target=target,
                page_summary=page_summary,
            )
        )
        context.raise_if_step_limit_reached(step_index=step_index)
        return [
            {
                "type": "text",
                "text": "\n".join(
                    [
                        f"Raw DOM for {target} on {page_summary}:",
                        "<journey-dom-inspection>",
                        dom,
                        "</journey-dom-inspection>",
                    ]
                ),
            }
        ]

    def _append_rejected_action(
        self,
        *,
        context: PromptActionContext,
        step_index: int,
        action_type: str,
        target: str,
        detail: str,
    ) -> JourneyLogRecord:
        record = _prompt_action_record(
            step_index=step_index,
            max_steps=self._max_steps,
            page_index=self._active_page_index,
            action_type=action_type,
            target=target,
            status="rejected",
            detail=detail,
        )
        context.record_action(record)
        context.record_memory_log(
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: rejected on "
                f"{_page_summary(self._prompt_page_payloads()[self._active_page_index])}: "
                f"{detail}",
                event="prompt_rejected",
                pretty=_prompt_row(
                    f"{step_index}/{self._max_steps} rejected",
                    detail,
                    style="warning",
                ),
                step=step_index,
                max_steps=self._max_steps,
                page=self._active_page_index,
                detail=detail,
            )
        )
        return record

    def _execute_python_step(
        self,
        *,
        step_index: int,
        code: str,
        target: str,
    ) -> JourneyLogRecord:
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError(
                "JourneyBrowserPage.prompt(...) expected the model to return Python code."
            )
        print_output = self._execute_python_code(
            normalized_code,
            filename="<journey-browser-prompt>",
        )
        return _prompt_action_record(
            step_index=step_index,
            max_steps=self._max_steps,
            page_index=self._active_page_index,
            action_type="python",
            target=target,
            status="ok",
            detail=_python_execution_detail(
                active_page_index=self._active_page_index,
                print_output=print_output,
            ),
        )

    def _execute_python_code(self, code: str, *, filename: str) -> str:
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError("JourneyBrowserPage.prompt(...) expected Python code.")
        self._discover_pages()
        print_chunks: list[str] = []
        print_length = 0
        print_truncated = False

        def prompt_print(
            *values: object,
            sep: str | None = " ",
            end: str | None = "\n",
            file: object | None = None,
            flush: bool = False,
        ) -> None:
            if file is not None:
                raise TypeError("Journey prompt print(...) does not accept file=.")
            if sep is None:
                sep = " "
            if end is None:
                end = "\n"
            if not isinstance(sep, str):
                raise TypeError("print() sep must be a string or None.")
            if not isinstance(end, str):
                raise TypeError("print() end must be a string or None.")
            nonlocal print_length, print_truncated
            if print_truncated:
                return
            chunk = sep.join(str(value) for value in values) + end
            remaining = _PROMPT_PRINT_OUTPUT_MAX_LENGTH - print_length
            if len(chunk) <= remaining:
                print_chunks.append(chunk)
                print_length += len(chunk)
                return
            if remaining > 0:
                print_chunks.append(chunk[:remaining].rstrip())
            print_chunks.append("\n... [print output truncated]")
            print_truncated = True

        namespace: dict[str, object] = {
            "__builtins__": {
                **_PROMPT_SAFE_BUILTINS,
                "print": prompt_print,
            },
            "page": self._pages[self._active_page_index],
            "pages": tuple(self._pages),
            "timeout_ms": self._timeout_ms,
        }

        def switch_page(index: object) -> PlaywrightPage:
            self._discover_pages()
            target_index = _parse_page_index(index, page_count=len(self._pages))
            self._active_page_index = target_index
            target_page = self._pages[target_index]
            namespace["page"] = target_page
            namespace["pages"] = tuple(self._pages)
            return target_page

        namespace["switch_page"] = switch_page
        compiled = compile(normalized_code, filename, "exec")
        try:
            exec(compiled, namespace, namespace)
        finally:
            self._discover_pages()
        return _truncate_prompt_print_output("".join(print_chunks))

    def _discover_pages(self) -> None:
        context = _page_context(self._original_page)
        for page in _context_pages(context):
            if page not in self._pages:
                try:
                    page.wait_for_load_state("load", timeout=self._timeout_ms)
                except Exception as exc:
                    self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
                    pass
                self._pages.append(page)

    def _settle_active_page_for_final_output(self) -> None:
        active_page = self._pages[self._active_page_index]
        try:
            active_page.wait_for_load_state(
                "networkidle",
                timeout=min(self._timeout_ms, 2000),
            )
        except Exception as exc:
            self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
            pass
        wait_for_timeout = getattr(active_page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            try:
                wait_for_timeout(500)
            except Exception as exc:
                self._raise_keyboard_interrupt_if_forced_prompt_abort(exc)
                pass

    def _raise_keyboard_interrupt_if_forced_prompt_abort(
        self,
        exc: BaseException,
    ) -> None:
        _raise_keyboard_interrupt_if_forced_prompt_abort(self._original_page, exc)

    def _prompt_page_payloads(self) -> list[dict[str, object]]:
        prompt_pages: list[dict[str, object]] = []
        for index, page in enumerate(self._pages):
            prompt_pages.append(
                {
                    "index": index,
                    "url": page.url,
                    "title": _safe_page_title(page),
                    "is_original": page is self._original_page,
                    "active": index == self._active_page_index,
                }
            )
        return prompt_pages


def _memory_compile_prompt(context: PromptMemoryCompileContext) -> str:
    return "\n".join(
        [
            f"Instruction:\n{context.instruction}",
            "",
            "Initial observation signature:",
            context.observation_signature,
            "",
            "Final output JSON:",
            json.dumps(context.final_output, sort_keys=True, indent=2)
            if isinstance(context.final_output, dict)
            else json.dumps(context.final_output),
            "",
            "Action transcript:",
            _memory_compile_action_transcript(context.log_records),
            "",
            "Final visible text:",
            context.final_observation.visible_text,
        ]
    )


def _memory_compile_action_transcript(
    records: tuple[JourneyLogRecord, ...],
) -> str:
    lines: list[str] = []
    for record in records:
        payload = record.to_dict()
        event = payload.get("event")
        step = payload.get("step") or payload.get("step_label") or "?"
        if event == "prompt_action":
            action = _compact_log_value(payload.get("action"))
            lines.append(f"- step {step}: action: {action}")
        elif event == "prompt_code":
            code = payload.get("code")
            if isinstance(code, str) and code.strip():
                lines.append(f"  code: {code.strip()}")
        elif event == "prompt_rejected":
            detail = _compact_log_value(payload.get("detail"), max_length=600)
            lines.append(f"  rejected: {detail}")
        elif event == "prompt_step_success":
            page = payload.get("page")
            lines.append(f"  ok: active page {page}")
        elif event == "page_discovered":
            title = _compact_log_value(payload.get("title"))
            url = _compact_log_value(payload.get("url"))
            lines.append(f"  discovered page: {title} {url}".rstrip())
        elif event == "active_page_change":
            summary = _compact_log_value(payload.get("page_summary"))
            lines.append(f"  active page: {summary}")
        elif event == "prompt_screenshot":
            summary = _compact_log_value(payload.get("page_summary"))
            lines.append(f"  screenshot: {summary}")
        elif event == "prompt_dom_inspection":
            target = _compact_log_value(payload.get("target"))
            lines.append(f"  DOM inspected: {target}")
        elif event == "action":
            status = _compact_log_value(payload.get("status"))
            action_type = _compact_log_value(payload.get("action_type"))
            target = _compact_log_value(payload.get("target"))
            detail = _compact_log_value(payload.get("detail"), max_length=600)
            lines.append(
                f"- step {step}: {status} {action_type} target={target} detail={detail}"
            )
    return "\n".join(lines) if lines else "- no recorded actions"


def _compact_log_value(value: object, *, max_length: int = 240) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _parse_memory_draft(text: str) -> PromptMemoryDraft:
    replay_code = _memory_draft_code_section(text, "Replay code")
    success_check_code = _memory_draft_code_section(text, "Success check code")
    notes = _memory_draft_text_section(text, "Notes")
    return PromptMemoryDraft(
        sections=_browser_memory_sections(
            replay_code=replay_code,
            success_check_code=success_check_code,
            notes=notes,
        ),
    )


def _browser_memory_sections(
    *,
    replay_code: str,
    success_check_code: str,
    notes: str = "",
) -> tuple[PromptMemorySection, ...]:
    sections = [
        PromptMemorySection(
            heading=_BROWSER_REPLAY_SECTION,
            body=replay_code,
            language="python",
        ),
        PromptMemorySection(
            heading=_BROWSER_SUCCESS_CHECK_SECTION,
            body=success_check_code,
            language="python",
        ),
    ]
    if notes.strip():
        sections.append(
            PromptMemorySection(
                heading=_BROWSER_NOTES_SECTION,
                body=notes.strip(),
            )
        )
    return tuple(sections)


def _browser_memory_code(entry: PromptMemoryEntry, heading: str) -> str:
    section = _browser_memory_section(entry, heading)
    if section.language != "python":
        raise RuntimeError(
            f"Browser prompt memory section {heading!r} must use a python code fence."
        )
    code = section.body.strip()
    if not code:
        raise RuntimeError(
            f"Browser prompt memory section {heading!r} must not be blank."
        )
    return code


def _browser_memory_section(
    entry: PromptMemoryEntry,
    heading: str,
) -> PromptMemorySection:
    for section in entry.sections:
        if section.heading == heading:
            return section
    raise RuntimeError(f"Browser prompt memory is missing section {heading!r}.")


def _format_browser_memory_for_prompt(
    entry: PromptMemoryEntry,
    replay_error: str | None = None,
) -> str:
    lines = [
        "Prompt memory:",
        "Use this prior successful browser fast path as a hint, but trust the current page.",
    ]
    if replay_error:
        lines.extend(["", "Replay failed before fallback:", replay_error.strip()])
    for heading in (
        _BROWSER_REPLAY_SECTION,
        _BROWSER_SUCCESS_CHECK_SECTION,
        _BROWSER_NOTES_SECTION,
    ):
        section = next(
            (item for item in entry.sections if item.heading == heading),
            None,
        )
        if section is None:
            continue
        lines.extend(["", f"{heading}:"])
        if section.language is None:
            lines.append(section.body)
        else:
            lines.extend([f"```{section.language}", section.body, "```"])
    return "\n".join(lines).strip()


def _memory_draft_code_section(text: str, heading: str) -> str:
    section = _memory_draft_text_section(text, heading)
    if not section:
        raise RuntimeError(f"memory compiler response is missing {heading!r}.")
    match = _MEMORY_DRAFT_FENCE_PATTERN.search(section.strip())
    if match is None:
        raise RuntimeError(f"memory compiler response {heading!r} needs a code fence.")
    language = match.group("language")
    if language and language != "python":
        raise RuntimeError(f"memory compiler response {heading!r} must use python.")
    code = match.group("body").strip()
    if not code:
        raise RuntimeError(f"memory compiler response {heading!r} must not be blank.")
    return code


def _memory_draft_text_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == marker:
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


@dataclass
class _SemanticDomNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text_parts: list[str] = field(default_factory=list)
    children: list[_SemanticDomNode] = field(default_factory=list)


class _SemanticDomParser(HTMLParser):
    _SKIP_SUBTREE_TAGS = {"script", "style", "noscript", "template", "svg", "path"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _SemanticDomNode("document")
        self._stack: list[_SemanticDomNode] = [self.root]
        self._skip_depth = 0
        self._hidden_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        if self._skip_depth:
            self._skip_depth += 1
            return
        if normalized_tag in self._SKIP_SUBTREE_TAGS:
            self._skip_depth = 1
            return
        if self._hidden_depth or _is_hidden_raw_attrs(attrs):
            self._hidden_depth += 1
            return
        normalized_attrs = _semantic_attrs(attrs)
        node = _SemanticDomNode(normalized_tag, normalized_attrs)
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if self._hidden_depth:
            self._hidden_depth -= 1
            return
        normalized_tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._hidden_depth:
            return
        text = _normalize_dom_text(data)
        if text:
            self._stack[-1].text_parts.append(text)


_SEMANTIC_ATTRS = {
    "alt",
    "aria-checked",
    "aria-controls",
    "aria-current",
    "aria-describedby",
    "aria-expanded",
    "aria-invalid",
    "aria-label",
    "aria-labelledby",
    "aria-required",
    "aria-selected",
    "for",
    "href",
    "id",
    "name",
    "placeholder",
    "role",
    "title",
    "type",
}
_SEMANTIC_DATA_ATTRS = {"data-testid", "data-test", "data-test-id"}
_SEMANTIC_LANDMARK_TAGS = {
    "article",
    "aside",
    "dialog",
    "fieldset",
    "footer",
    "form",
    "header",
    "main",
    "nav",
    "section",
    "table",
    "tbody",
    "thead",
    "tr",
    "ul",
    "ol",
}
_SEMANTIC_INTERACTIVE_TAGS = {
    "a",
    "button",
    "input",
    "label",
    "option",
    "select",
    "summary",
    "textarea",
}
_SEMANTIC_TEXT_TAGS = {
    "caption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "td",
    "th",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _semantic_dom_snapshot(page: PlaywrightPage) -> str:
    return _semantic_dom_from_html(_rendered_html(page))


def _semantic_dom_from_html(html: str) -> str:
    parser = _SemanticDomParser()
    parser.feed(html)
    parser.close()
    lines = _render_semantic_dom_lines(parser.root)
    if not lines:
        return "<empty semantic DOM>"
    if len(lines) > _SEMANTIC_DOM_MAX_LINES:
        trimmed = lines[:_SEMANTIC_DOM_MAX_LINES]
        trimmed.append("<!-- semantic DOM truncated -->")
        return "\n".join(trimmed)
    return "\n".join(lines)


def _semantic_attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in attrs:
        name = raw_name.lower()
        value = "" if raw_value is None else _normalize_dom_text(raw_value)
        if name == "class":
            class_value = _semantic_class_value(value)
            if class_value:
                normalized[name] = class_value
            continue
        if name in _SEMANTIC_ATTRS or name in _SEMANTIC_DATA_ATTRS:
            normalized[name] = _truncate_dom_value(value, _SEMANTIC_DOM_MAX_ATTR_LENGTH)
    return normalized


def _semantic_class_value(value: str) -> str:
    kept: list[str] = []
    for token in value.split():
        if len(kept) >= _SEMANTIC_DOM_MAX_CLASS_TOKENS:
            break
        if len(token) > 40:
            continue
        if re.search(r"__[A-Za-z0-9_-]{4,}$", token):
            continue
        kept.append(token)
    return " ".join(kept)


def _is_hidden_raw_attrs(attrs: list[tuple[str, str | None]]) -> bool:
    normalized = {
        name.lower(): "" if value is None else value
        for name, value in attrs
    }
    if "hidden" in normalized:
        return True
    if normalized.get("aria-hidden", "").lower() == "true":
        return True
    style = normalized.get("style", "").lower()
    return (
        "display:none" in style.replace(" ", "")
        or "visibility:hidden" in style.replace(" ", "")
    )


def _render_semantic_dom_lines(
    node: _SemanticDomNode,
    *,
    depth: int = 0,
) -> list[str]:
    rendered_children: list[str] = []
    for child in node.children:
        rendered_children.extend(_render_semantic_dom_lines(child, depth=depth + 1))
    if node.tag == "document":
        return rendered_children
    direct_text = _truncate_dom_value(
        " ".join(node.text_parts),
        _SEMANTIC_DOM_MAX_TEXT_LENGTH,
    )
    display_text = direct_text or _semantic_descendant_display_text(
        node,
        child_lines=rendered_children,
    )
    meaningful = _is_semantic_node(
        node,
        display_text=display_text,
        child_lines=rendered_children,
    )
    if not meaningful:
        return rendered_children
    indent = "  " * max(depth - 1, 0)
    attrs = _format_semantic_attrs(node.attrs)
    tag_open = f"<{node.tag}{attrs}>"
    if not rendered_children:
        if node.tag in _VOID_TAGS:
            return [f"{indent}<{node.tag}{attrs}>"]
        if display_text:
            return [f"{indent}{tag_open}{html_escape(display_text)}</{node.tag}>"]
        return [f"{indent}{tag_open}</{node.tag}>"]
    lines = [f"{indent}{tag_open}"]
    if display_text:
        lines.append(f"{indent}  {html_escape(display_text)}")
    lines.extend(rendered_children)
    lines.append(f"{indent}</{node.tag}>")
    return lines


def _semantic_descendant_display_text(
    node: _SemanticDomNode,
    *,
    child_lines: list[str],
) -> str:
    if child_lines:
        return ""
    if (
        node.tag not in _SEMANTIC_INTERACTIVE_TAGS
        and node.tag not in _SEMANTIC_TEXT_TAGS
    ):
        return ""
    return _truncate_dom_value(
        _semantic_node_text(node),
        _SEMANTIC_DOM_MAX_TEXT_LENGTH,
    )


def _semantic_node_text(node: _SemanticDomNode) -> str:
    parts = [*node.text_parts]
    for child in node.children:
        child_text = _semantic_node_text(child)
        if child_text:
            parts.append(child_text)
    return _normalize_dom_text(" ".join(parts))


def _is_semantic_node(
    node: _SemanticDomNode,
    *,
    display_text: str,
    child_lines: list[str],
) -> bool:
    if node.attrs:
        return True
    if node.tag in _SEMANTIC_INTERACTIVE_TAGS or node.tag in _SEMANTIC_LANDMARK_TAGS:
        return True
    if display_text and node.tag in _SEMANTIC_TEXT_TAGS:
        return True
    return bool(child_lines) and node.tag in _SEMANTIC_LANDMARK_TAGS


def _format_semantic_attrs(attrs: dict[str, str]) -> str:
    if not attrs:
        return ""
    parts = [
        f'{name}="{html_escape(value, quote=True)}"'
        for name, value in sorted(attrs.items())
    ]
    return " " + " ".join(parts)


def _normalize_dom_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate_dom_value(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."


def prompt_page(
    page: PlaywrightPage,
    *,
    instruction: str,
    model: str | None,
    max_steps: int,
    action_timeout_seconds: float,
    memory: PromptMemorySpec = PROMPT_MEMORY_AUTO,
    caller_frame: FrameType | None = None,
    output: PromptOutputSpec | None = None,
) -> str | dict[str, object]:
    normalized_instruction = _require_text_value(
        instruction,
        "JourneyBrowserPage.prompt(...) expects a non-blank instruction.",
    )
    resolved_model = _resolve_model(model)
    normalized_max_steps = _validate_max_steps(max_steps)
    normalized_timeout = _validate_timeout(action_timeout_seconds)
    memory_path = resolve_prompt_memory_path(
        memory,
        owner="JourneyBrowserPage.prompt(...)",
        caller_frame=caller_frame,
    )
    output_schema = normalize_prompt_output_spec(
        output,
        owner="JourneyBrowserPage.prompt(...)",
    )
    session = _PromptSession(
        page=page,
        instruction=normalized_instruction,
        model=resolved_model,
        max_steps=normalized_max_steps,
        action_timeout_seconds=normalized_timeout,
        memory_path=memory_path,
        output_schema=output_schema,
    )
    return session.run()


def _page_memory_signature(page: dict[str, object]) -> str:
    title = page.get("title")
    url = page.get("url")
    signature = {
        "title": title.strip() if isinstance(title, str) else "",
        "url": _url_without_query_or_fragment(url if isinstance(url, str) else ""),
    }
    return json.dumps(signature, sort_keys=True, separators=(",", ":"))


def _url_without_query_or_fragment(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
        )
    )


def _emit_prompt_log(
    message: str,
    *,
    event: str = "prompt_log",
    pretty: object = None,
    **fields: object,
) -> JourneyLogRecord:
    record = make_log_record(_PROMPT_LOGGER.component, event, message, **fields)
    _PROMPT_LOGGER.info(event, message, pretty=pretty, **fields)
    return record


def _emit_prompt_code_log(
    *,
    step_label: str,
    code: str,
) -> tuple[JourneyLogRecord, ...]:
    step_ref = _prompt_step_ref(step=None, max_steps=None, step_label=step_label)
    if not code:
        return (
            _emit_prompt_log(
                f"{step_label} code: <blank>",
                event="prompt_code",
                pretty=_prompt_row(f"{step_ref} code", "<blank>", style="code"),
                step_label=step_label,
                code="<blank>",
            ),
        )
    if "\n" not in code:
        return (
            _emit_prompt_log(
                f"{step_label} code: {code}",
                event="prompt_code",
                pretty=_prompt_row(f"{step_ref} code", code, style="code"),
                step_label=step_label,
                code=code,
            ),
        )
    records = [
        _emit_prompt_log(
            f"{step_label} code:",
            event="prompt_code",
            pretty=_prompt_row(f"{step_ref} code", "", style="code"),
            step_label=step_label,
        )
    ]
    for line in code.splitlines():
        records.append(
            _emit_prompt_log(
                f"  {line}",
                event="prompt_code",
                pretty=_prompt_continuation(line, style="code"),
                step_label=step_label,
                code=line,
            )
        )
    return tuple(records)


def _page_summary(page: dict[str, object]) -> str:
    return _format_page_summary(
        index=page.get("index"),
        title=page.get("title"),
        url=page.get("url"),
    )


def _page_record(page: dict[str, object]) -> JourneyLogRecord:
    return make_log_record(
        "browser",
        "page",
        _page_summary(page),
        page_index=page.get("index"),
        title=page.get("title"),
        url=page.get("url"),
        is_original=page.get("is_original"),
        active=page.get("active"),
    )


def _prompt_action_record(
    *,
    step_index: int,
    max_steps: int,
    page_index: int,
    action_type: str,
    target: str,
    status: str,
    detail: str,
) -> JourneyLogRecord:
    return make_log_record(
        "browser",
        "action",
        detail,
        step=step_index,
        max_steps=max_steps,
        page_index=page_index,
        action_type=action_type,
        target=target,
        status=status,
        detail=detail,
    )


def _format_page_summary(*, index: object, title: object, url: object) -> str:
    page_label = f"page {index}" if isinstance(index, int) else "page ?"
    title_text = title.strip() if isinstance(title, str) else ""
    url_text = url.strip() if isinstance(url, str) else ""
    if title_text and url_text:
        return f"{page_label} {title_text!r} at {url_text}"
    if title_text:
        return f"{page_label} {title_text!r}"
    if url_text:
        return f"{page_label} at {url_text}"
    return page_label


def _describe_prompt_action(code: str) -> str:
    if not code:
        return "run an empty Python snippet"
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "run Python snippet"
    call = _first_statement_call(tree)
    if call is None:
        return "run Python snippet"
    direct_description = _describe_direct_function_call(call)
    if direct_description is not None:
        return direct_description
    if isinstance(call.func, ast.Attribute):
        method_description = _describe_method_call(call)
        if method_description is not None:
            return method_description
    return "run Python snippet"


def _first_statement_call(tree: ast.Module) -> ast.Call | None:
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            return statement.value
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            return statement.value
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.value, ast.Call):
            return statement.value
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                return node
    return None


def _describe_direct_function_call(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Name):
        return None
    if call.func.id == "switch_page":
        return f"switch to page {_call_arg_description(call, 0)}"
    return None


def _describe_method_call(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    method = call.func.attr
    target = _target_description(call.func.value)
    if method == "click":
        return f"click {target}"
    if method == "fill":
        return f"fill {target} with {_call_arg_description(call, 0)}"
    if method == "press":
        return f"press {_call_arg_description(call, 0)} on {target}"
    if method == "goto":
        return f"navigate to {_call_arg_description(call, 0)}"
    if method == "wait_for_url":
        return f"wait for URL {_call_arg_description(call, 0)}"
    if method == "wait_for_load_state":
        return f"wait for load state {_call_arg_description(call, 0)}"
    if method == "wait_for":
        return f"wait for {target}"
    return None


def _target_description(node: ast.AST) -> str:
    if isinstance(node, ast.Name) and node.id == "page":
        return "the active page"
    if isinstance(node, ast.Attribute) and node.attr == "first":
        return _target_description(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method == "locator":
            return f"selector {_call_arg_description(node, 0)}"
        if method == "get_by_text":
            return f"text {_call_arg_description(node, 0)}"
        if method == "get_by_label":
            return f"label {_call_arg_description(node, 0)}"
        if method == "get_by_placeholder":
            return f"placeholder {_call_arg_description(node, 0)}"
        if method == "get_by_role":
            role = _call_arg_description(node, 0)
            name = _call_keyword_description(node, "name")
            if name is not None:
                return f"role {role} named {name}"
            return f"role {role}"
    return "the selected element"


def _call_arg_description(call: ast.Call, index: int) -> str:
    if index >= len(call.args):
        return "value"
    return _node_description(call.args[index])


def _call_keyword_description(call: ast.Call, keyword_name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == keyword_name:
            return _node_description(keyword.value)
    return None


def _node_description(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return repr(node.value)
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    return ast.unparse(node)


def _load_langchain_model(model: str) -> object:
    try:
        return _load_prompt_engine_model(model)
    except Exception as exc:
        raise _runtime_error_with_hint(
            "JourneyBrowserPage.prompt(...) failed to initialize LangChain "
            f"model {model!r}: {exc}",
            hint=_exception_hint(exc),
        ) from exc


def _create_langchain_agent(
    model: object,
    *,
    tools: list[object],
    system_prompt: str,
) -> object:
    return _create_prompt_engine_agent(
        model,
        tools=tools,
        system_prompt=system_prompt,
    )


def _resolve_model(model: str | None) -> str:
    return resolve_prompt_model(
        model,
        env_var=JOURNEY_BROWSER_PROMPT_MODEL_ENV,
        owner="JourneyBrowserPage.prompt(...)",
        default_model=DEFAULT_JOURNEY_BROWSER_PROMPT_MODEL,
    )


def _validate_max_steps(max_steps: int) -> int:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError(
            "JourneyBrowserPage.prompt(..., max_steps=...) expects a positive integer."
        )
    return max_steps


def _validate_timeout(action_timeout_seconds: float) -> float:
    if isinstance(action_timeout_seconds, bool) or not isinstance(
        action_timeout_seconds,
        int | float,
    ):
        raise ValueError(
            "JourneyBrowserPage.prompt(..., action_timeout_seconds=...) "
            "expects a positive number."
        )
    normalized = float(action_timeout_seconds)
    if normalized <= 0:
        raise ValueError(
            "JourneyBrowserPage.prompt(..., action_timeout_seconds=...) "
            "expects a positive number."
        )
    return normalized


def _page_context(page: PlaywrightPage) -> object:
    fake_context = getattr(page, "_journey_prompt_context", None)
    if fake_context is not None:
        return fake_context
    return page.context


def _context_pages(context: object) -> list[PlaywrightPage]:
    pages = getattr(context, "pages", None)
    if not isinstance(pages, list):
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) expected Playwright context.pages to be a list."
        )
    return [cast(PlaywrightPage, page) for page in pages]


def _png_data_url(page: PlaywrightPage) -> str:
    png_bytes = page.screenshot(type="png")
    if not isinstance(png_bytes, bytes):
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) expected screenshot(type='png') to return bytes."
        )
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _normalize_optional_selector(selector: object) -> str | None:
    if selector is None:
        return None
    if not isinstance(selector, str):
        raise TypeError(
            "journey_inspect_dom selector must be a CSS selector string or null."
        )
    normalized = selector.strip()
    return normalized or None


def _inspect_dom(page: PlaywrightPage, selector: str | None) -> str:
    if selector is None:
        return _rendered_html(page)
    html = page.evaluate(_INSPECT_DOM_SCRIPT, selector)
    if html is None:
        raise RuntimeError(f"CSS selector {selector!r} did not match any element.")
    if not isinstance(html, str):
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) expected DOM inspection to return a string."
        )
    return html


def _rendered_html(page: PlaywrightPage) -> str:
    html = page.evaluate(_RENDERED_HTML_SCRIPT)
    if not isinstance(html, str):
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) expected rendered HTML to be a string."
        )
    return html


def _visible_text(page: PlaywrightPage) -> str:
    text = page.evaluate(_VISIBLE_TEXT_SCRIPT)
    if not isinstance(text, str):
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) expected visible text to be a string."
        )
    return text


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_code_line(code: str) -> str:
    for line in code.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _format_python_error(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _python_execution_detail(*, active_page_index: int, print_output: str) -> str:
    detail = f"Executed Python snippet. Active page index is {active_page_index}."
    normalized_output = print_output.strip()
    if normalized_output:
        detail += f"\nCaptured print output:\n{normalized_output}"
    return detail


def _truncate_prompt_print_output(output: str) -> str:
    if len(output) <= _PROMPT_PRINT_OUTPUT_MAX_LENGTH:
        return output
    truncated = output[:_PROMPT_PRINT_OUTPUT_MAX_LENGTH].rstrip()
    return f"{truncated}\n... [print output truncated]"


def _raise_keyboard_interrupt_if_forced_prompt_abort(
    page: PlaywrightPage,
    exc: BaseException,
) -> None:
    """Convert only Playwright teardown errors during forced interrupt cleanup."""

    if (
        bool(getattr(page, "_journey_forced_interrupt_cleanup_started", False))
        and isinstance(exc, PlaywrightError)
    ):
        raise KeyboardInterrupt() from exc


def _parse_page_index(raw_index: object, *, page_count: int) -> int:
    if isinstance(raw_index, bool):
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) switch_page index must be an integer."
        )
    if isinstance(raw_index, int):
        parsed = raw_index
    elif isinstance(raw_index, str):
        text = _require_text_value(
            raw_index,
            "JourneyBrowserPage.prompt(...) switch_page index must be a non-empty string or integer.",
        )
        try:
            parsed = int(text)
        except ValueError as exc:
            raise RuntimeError(
                "JourneyBrowserPage.prompt(...) switch_page index must be an integer."
            ) from exc
    else:
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) switch_page index must be an integer."
        )
    if parsed < 0 or parsed >= page_count:
        raise RuntimeError(
            "JourneyBrowserPage.prompt(...) switch_page target "
            f"{parsed} is outside the known page range 0..{page_count - 1}."
        )
    return parsed


def _require_text_value(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()


def _safe_page_title(page: PlaywrightPage) -> str:
    try:
        title = page.title()
    except Exception:
        return ""
    return title.strip()
