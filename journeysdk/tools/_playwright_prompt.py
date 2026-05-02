"""Private helpers for Journey Playwright prompting."""

from __future__ import annotations

import ast
import base64
from dataclasses import dataclass
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from journeysdk.logger import get_logger
from journeysdk._prompt_memory import (
    MAX_PROMPT_MEMORY_ITEMS,
    load_prompt_memory_entry,
    normalize_prompt_instruction,
    prompt_memory_key,
    prompt_memory_updates_disabled,
    resolve_prompt_memory_path,
    truncate_prompt_memory_text,
    write_prompt_memory_entry,
)
from journeysdk._prompt_output import (
    PromptOutputSchema,
    PromptOutputSpec,
    normalize_prompt_output_spec,
    parse_prompt_structured_output,
)
from playwright.sync_api import Page as PlaywrightPage

JOURNEY_PLAYWRIGHT_PROMPT_MODEL_ENV = "JOURNEY_PLAYWRIGHT_PROMPT_MODEL"

_PROMPT_RUN_CODE_TOOL_NAME = "journey_run_code"
_PROMPT_FAIL_SESSION_TOOL_NAME = "journey_fail_session"

_PROMPT_SYSTEM_MESSAGE = """You control a Playwright sync browser page with tools.

Use tools when the browser needs more work. When the requested browser task is complete, do not call a tool; return the
final answer directly.

Use journey_run_code to execute one Python snippet against the active page.
Use journey_fail_session when a visible page state prevents the requested browser task from completing.

Available names:
- page: the active Playwright sync Page
- pages: a tuple of known Playwright sync Page objects
- timeout_ms: the configured action timeout in milliseconds
- switch_page(index): switch the active page to a known page index and return that Page

Rules:
- Inspect the rendered HTML and screenshot, then choose the fewest Playwright commands needed.
- Prefer robust Playwright locators such as get_by_role, get_by_text, get_by_label, and locator.
- Pass timeout=timeout_ms to actions and waits that accept a timeout.
- Use switch_page(index) before acting on a popup or tab listed in known pages.
- Return the final answer directly only when the instruction is complete in the browser.
- If the page shows a blocking app error or status, such as a locked account, invalid password, authorization failure,
  or unavailable action, call journey_fail_session with the visible message instead of returning a final answer.
- Do not treat a failed sign-in, failed checkout, failed submission, or similar rejected user task as complete just
  because the page displays the final error state.
- If the previous step was rejected, correct the tool call or Python and try again.
- If prompt memory is provided, use it as a hint from prior successful runs,
  but trust the current rendered HTML and screenshot over stale memory.
- Use the rendered HTML, visible text, screenshot, known pages, and executed steps to answer the original instruction.
- Base the final output on the current visible page state.
If a requested output field asks for an error, validation message, warning, status, or problem, copy the visible
message exactly when present. Do not return an empty string for such a field when the current visible page text or
screenshot contains a matching message.
- Do not mention implementation details, hidden reasoning, or unavailable metadata.
"""

_RENDERED_HTML_SCRIPT = "() => document.documentElement ? document.documentElement.outerHTML : ''"
_VISIBLE_TEXT_SCRIPT = "() => document.body ? document.body.innerText : ''"
_PROMPT_LOGGER = get_logger("playwright-prompt")
_PROMPT_AGENT_TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": _PROMPT_RUN_CODE_TOOL_NAME,
            "description": (
                "Execute one Playwright sync Python snippet against the active "
                "Journey browser page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Executable Python code using page, pages, timeout_ms, "
                            "and switch_page(index)."
                        ),
                    }
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": _PROMPT_FAIL_SESSION_TOOL_NAME,
            "description": (
                "Stop the prompt because a visible page state blocks the "
                "requested browser task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "The visible blocking message or concise reason the "
                            "browser task cannot be completed."
                        ),
                    }
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True)
class JourneyPlaywrightPromptPage:
    index: int
    url: str
    title: str
    is_original: bool


@dataclass(frozen=True)
class JourneyPlaywrightPromptStep:
    index: int
    page_index: int
    action: str
    target: str
    status: str
    detail: str


@dataclass(frozen=True)
class JourneyPlaywrightPromptResult:
    output: str | dict[str, object]
    model: str
    active_page_index: int
    pages: tuple[JourneyPlaywrightPromptPage, ...]
    steps: tuple[JourneyPlaywrightPromptStep, ...]


@dataclass(frozen=True)
class _PromptToolCall:
    id: str
    name: str
    arguments: object
    raw: dict[str, object]


class _CompletionCallable(Protocol):
    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        response_format: dict[str, object] | None = None,
        enable_json_schema_validation: bool | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> object:
        ...


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
        self._memory_key: str | None = None
        self._memory_page_signature: str | None = None
        self._memory_entry: dict[str, object] | None = None
        self._memory_loaded = False
        self._completion = _load_litellm_completion()
        self._pages: list[PlaywrightPage] = [page]
        self._active_page_index = 0
        self._steps: list[JourneyPlaywrightPromptStep] = []

    def run(self) -> JourneyPlaywrightPromptResult:
        self._log_start()
        observation = self._build_observation()
        messages = self._build_agent_messages(observation=observation)
        for step_index in range(1, self._max_steps + 1):
            self._log_inspection(step_index=step_index, observation=observation)
            response = self._request_agent_response(messages=messages)
            try:
                tool_calls = _extract_completion_tool_calls(response)
            except RuntimeError as exc:
                detail = _format_python_error(exc)
                self._append_rejected_step(
                    step_index=step_index,
                    action="tool",
                    target="",
                    detail=detail,
                )
                observation = self._build_observation()
                messages.append(
                    self._build_observation_message(
                        observation=observation,
                        include_memory=False,
                    )
                )
                continue
            if not tool_calls:
                return self._finish_from_response(
                    step_index=step_index,
                    response=response,
                )

            messages.append(_assistant_message_from_response(response, tool_calls))
            if len(tool_calls) != 1:
                detail = _format_python_error(
                    RuntimeError(
                        "JourneyPlaywrightPage.prompt(...) expected the model "
                        f"to call exactly one tool, but received {len(tool_calls)}."
                    )
                )
                self._append_rejected_step(
                    step_index=step_index,
                    action="tool",
                    target=_tool_calls_target(tool_calls),
                    detail=detail,
                )
                for tool_call in tool_calls:
                    messages.append(
                        _tool_result_message(
                            tool_call=tool_call,
                            payload={"status": "rejected", "detail": detail},
                        )
                    )
                observation = self._build_observation()
                messages.append(
                    self._build_observation_message(
                        observation=observation,
                        include_memory=False,
                    )
                )
                continue

            tool_call = tool_calls[0]
            if tool_call.name == _PROMPT_FAIL_SESSION_TOOL_NAME:
                try:
                    reason = _tool_call_text_argument(
                        tool_call,
                        "reason",
                        "JourneyPlaywrightPage.prompt(...) "
                        "journey_fail_session expects a non-blank reason string.",
                    )
                except Exception as exc:
                    detail = _format_python_error(exc)
                    self._append_rejected_step(
                        step_index=step_index,
                        action="tool",
                        target=tool_call.name,
                        detail=detail,
                    )
                    messages.append(
                        _tool_result_message(
                            tool_call=tool_call,
                            payload={"status": "rejected", "detail": detail},
                        )
                    )
                    observation = self._build_observation()
                    messages.append(
                        self._build_observation_message(
                            observation=observation,
                            include_memory=False,
                        )
                    )
                    continue

                fail_step = JourneyPlaywrightPromptStep(
                    index=step_index,
                    page_index=self._active_page_index,
                    action="fail",
                    target=reason,
                    status="failed",
                    detail=reason,
                )
                self._steps.append(fail_step)
                self._raise_prompt_failed(
                    step_index=step_index,
                    reason=reason,
                )

            if tool_call.name != _PROMPT_RUN_CODE_TOOL_NAME:
                detail = _format_python_error(
                    RuntimeError(
                        "JourneyPlaywrightPage.prompt(...) received unknown "
                        f"tool call {tool_call.name!r}."
                    )
                )
                self._append_rejected_step(
                    step_index=step_index,
                    action="tool",
                    target=tool_call.name,
                    detail=detail,
                )
                messages.append(
                    _tool_result_message(
                        tool_call=tool_call,
                        payload={"status": "rejected", "detail": detail},
                    )
                )
                observation = self._build_observation()
                messages.append(
                    self._build_observation_message(
                        observation=observation,
                        include_memory=False,
                    )
                )
                continue

            try:
                code = _strip_code_fences(
                    _tool_call_text_argument(
                        tool_call,
                        "code",
                        "JourneyPlaywrightPage.prompt(...) "
                        "journey_run_code expects a non-blank code string.",
                    )
                )
                target = _first_code_line(code)
            except Exception as exc:
                detail = _format_python_error(exc)
                self._append_rejected_step(
                    step_index=step_index,
                    action="tool",
                    target=tool_call.name,
                    detail=detail,
                )
                messages.append(
                    _tool_result_message(
                        tool_call=tool_call,
                        payload={"status": "rejected", "detail": detail},
                    )
                )
                observation = self._build_observation()
                messages.append(
                    self._build_observation_message(
                        observation=observation,
                        include_memory=False,
                    )
                )
                continue

            previous_page_count = len(self._pages)
            previous_active_page_index = self._active_page_index
            self._log_action(step_index=step_index, code=code)
            try:
                step = self._execute_python_step(
                    step_index=step_index,
                    code=code,
                    target=target,
                )
            except Exception as exc:
                self._log_new_pages(previous_page_count=previous_page_count)
                self._log_active_page_change(
                    previous_active_page_index=previous_active_page_index,
                )
                step = self._append_rejected_step(
                    step_index=step_index,
                    action="python",
                    target=target,
                    detail=_format_python_error(exc),
                )
                messages.append(
                    _tool_result_message(
                        tool_call=tool_call,
                        payload={"status": "rejected", "detail": step.detail},
                    )
                )
                observation = self._build_observation()
                messages.append(
                    self._build_observation_message(
                        observation=observation,
                        include_memory=False,
                    )
                )
                continue
            self._log_new_pages(previous_page_count=previous_page_count)
            self._log_active_page_change(
                previous_active_page_index=previous_active_page_index,
            )
            _emit_prompt_log(
                f"step {step_index}/{self._max_steps}: succeeded on "
                f"{_page_summary(self._prompt_pages()[self._active_page_index])}",
                event="prompt_step_success",
                step=step_index,
                max_steps=self._max_steps,
                page=self._active_page_index,
            )
            self._steps.append(step)
            messages.append(
                _tool_result_message(
                    tool_call=tool_call,
                    payload={
                        "status": "ok",
                        "detail": step.detail,
                        "active_page_index": self._active_page_index,
                    },
                )
            )
            observation = self._build_observation()
            messages.append(
                self._build_observation_message(
                    observation=observation,
                    include_memory=False,
                )
            )

        message = (
            "JourneyPlaywrightPage.prompt(...) reached "
            f"max_steps={self._max_steps} without a final response."
        )
        if self._steps:
            last_step = self._steps[-1]
            message += (
                f" Last step was {last_step.status}: "
                f"{last_step.action} {last_step.target!r} ({last_step.detail})."
            )
        _emit_prompt_log(
            f"prompt stopped: {message}",
            event="prompt_stopped",
            max_steps=self._max_steps,
        )
        raise RuntimeError(message)

    def _log_start(self) -> None:
        self._discover_pages()
        active_page = self._prompt_pages()[self._active_page_index]
        timeout_seconds = self._timeout_ms / 1000
        _emit_prompt_log(
            "prompt start: "
            f"instruction={self._instruction!r}; "
            f"model={self._model!r}; "
            f"max_steps={self._max_steps}; "
            f"timeout={timeout_seconds:g}s; "
            f"active={_page_summary(active_page)}",
            event="prompt_start",
            instruction=self._instruction,
            model=self._model,
            max_steps=self._max_steps,
            timeout=f"{timeout_seconds:g}s",
            active=_page_summary(active_page),
        )

    def _log_inspection(
        self,
        *,
        step_index: int,
        observation: dict[str, object],
    ) -> None:
        active_page_index = cast(int, observation["active_page_index"])
        pages = cast(list[dict[str, object]], observation["pages"])
        active_page = pages[active_page_index]
        _emit_prompt_log(
            f"step {step_index}/{self._max_steps}: inspecting "
            f"{_page_dict_summary(active_page)}",
            event="prompt_inspect",
            step=step_index,
            max_steps=self._max_steps,
            page=active_page_index,
            page_summary=_page_dict_summary(active_page),
        )

    def _log_action(self, *, step_index: int, code: str) -> None:
        normalized_code = code.strip()
        action_description = _describe_prompt_action(normalized_code)
        _emit_prompt_log(
            f"step {step_index}/{self._max_steps}: AI will "
            f"{action_description}",
            event="prompt_action",
            step=step_index,
            max_steps=self._max_steps,
            action=action_description,
        )
        _emit_prompt_code_log(
            step_label=f"step {step_index}/{self._max_steps}",
            code=normalized_code,
        )

    def _log_new_pages(self, *, previous_page_count: int) -> None:
        current_pages = self._prompt_pages()
        for page in current_pages[previous_page_count:]:
            _emit_prompt_log(
                f"discovered {_page_summary(page)}",
                event="page_discovered",
                page=page.index,
                title=page.title,
                url=page.url,
            )

    def _log_active_page_change(self, *, previous_active_page_index: int) -> None:
        if previous_active_page_index == self._active_page_index:
            return
        active_page = self._prompt_pages()[self._active_page_index]
        _emit_prompt_log(
            f"active page changed to {_page_summary(active_page)}",
            event="active_page_change",
            previous_page=previous_active_page_index,
            active_page=self._active_page_index,
            page_summary=_page_summary(active_page),
        )

    def _build_observation(self) -> dict[str, object]:
        self._discover_pages()
        pages = self._prompt_pages()
        active_page = self._pages[self._active_page_index]
        rendered_html = _rendered_html(active_page)
        screenshot_data_url = _png_data_url(active_page)
        return {
            "active_page_index": self._active_page_index,
            "pages": [
                {
                    "index": page.index,
                    "url": page.url,
                    "title": page.title,
                    "is_original": page.is_original,
                }
                for page in pages
            ],
            "rendered_html": rendered_html,
            "visible_text": _visible_text(active_page),
            "steps": _steps_observation(tuple(self._steps)),
            "screenshot_data_url": screenshot_data_url,
        }

    def _build_agent_messages(
        self,
        *,
        observation: dict[str, object],
    ) -> list[dict[str, object]]:
        return [
            {
                "role": "system",
                "content": _PROMPT_SYSTEM_MESSAGE,
            },
            self._build_observation_message(
                observation=observation,
                include_memory=True,
            ),
        ]

    def _build_observation_message(
        self,
        *,
        observation: dict[str, object],
        include_memory: bool,
    ) -> dict[str, object]:
        memory_entry = (
            self._memory_for_observation(observation) if include_memory else None
        )
        memory_section: list[str] = []
        if memory_entry is not None:
            memory_section = [
                "",
                "Prompt memory JSON:",
                json.dumps(memory_entry, sort_keys=True, indent=2),
            ]
        output_section: list[str]
        if self._output_schema is None:
            output_section = [
                "",
                "When the browser task is complete, return the final answer as plain text.",
            ]
        else:
            output_section = [
                "",
                "When the browser task is complete, return the final answer using these output fields JSON:",
                self._output_schema.prompt_text,
            ]
        prompt_text = "\n".join(
            [
                f"Instruction:\n{self._instruction}",
                *memory_section,
                "",
                "Known pages JSON:",
                json.dumps(
                    observation["pages"],
                    sort_keys=True,
                    indent=2,
                ),
                "",
                f"Active page index: {observation['active_page_index']}",
                "",
                "Executed steps JSON:",
                json.dumps(observation["steps"], sort_keys=True, indent=2),
                "",
                "Active page visible text:",
                "<journey-visible-text>",
                cast(str, observation["visible_text"]),
                "</journey-visible-text>",
                "",
                "Active page rendered HTML:",
                "<journey-rendered-html>",
                cast(str, observation["rendered_html"]),
                "</journey-rendered-html>",
                *output_section,
                "",
                (
                    "Call journey_run_code to continue browser work, call "
                    "journey_fail_session for a blocking visible page state, "
                    "or return the final answer directly when complete."
                ),
            ]
        )
        screenshot_data_url = cast(str, observation["screenshot_data_url"])
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt_text,
                },
                {
                    "type": "image_url",
                    "image_url": {"url": screenshot_data_url},
                },
            ],
        }

    def _request_agent_response(
        self,
        *,
        messages: list[dict[str, object]],
    ) -> object:
        try:
            kwargs: dict[str, object] = {
                "tools": _PROMPT_AGENT_TOOLS,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            }
            if self._output_schema is not None:
                kwargs["response_format"] = self._output_schema.response_format
                kwargs["enable_json_schema_validation"] = True
            response = self._completion(
                model=self._model,
                messages=messages,
                max_tokens=1000,
                temperature=0.0,
                **kwargs,
            )
        except Exception as exc:
            raise RuntimeError(
                "JourneyPlaywrightPage.prompt(...) failed to call "
                f"model {self._model!r}: {exc}"
            ) from exc
        return response

    def _finish_from_response(
        self,
        *,
        step_index: int,
        response: object,
    ) -> JourneyPlaywrightPromptResult:
        response_text = _extract_completion_text(response).strip()
        finish_step = JourneyPlaywrightPromptStep(
            index=step_index,
            page_index=self._active_page_index,
            action="finish",
            target="",
            status="ok",
            detail="Prompt marked complete.",
        )
        self._steps.append(finish_step)
        self._settle_active_page_for_final_output()
        final_observation = self._build_observation()
        final_output = self._parse_final_output(
            response_text,
            observation=final_observation,
        )
        _emit_prompt_log(
            f"step {step_index}/{self._max_steps}: finished with output: "
            f"{_prompt_output_summary(final_output)}",
            event="prompt_finish",
            step=step_index,
            max_steps=self._max_steps,
            output=_prompt_output_summary(final_output),
        )
        self._write_memory(final_output=final_output)
        return JourneyPlaywrightPromptResult(
            output=final_output,
            model=self._model,
            active_page_index=self._active_page_index,
            pages=tuple(self._prompt_pages()),
            steps=tuple(self._steps),
        )

    def _parse_final_output(
        self,
        response_text: str,
        *,
        observation: dict[str, object],
    ) -> str | dict[str, object]:
        if self._output_schema is None:
            return response_text
        structured_output = parse_prompt_structured_output(
            response_text,
            self._output_schema,
            owner="JourneyPlaywrightPage.prompt(...)",
        )
        return _fill_visible_message_fields(
            structured_output,
            schema=self._output_schema,
            visible_text=cast(str, observation["visible_text"]),
        )

    def _append_rejected_step(
        self,
        *,
        step_index: int,
        action: str,
        target: str,
        detail: str,
    ) -> JourneyPlaywrightPromptStep:
        step = JourneyPlaywrightPromptStep(
            index=step_index,
            page_index=self._active_page_index,
            action=action,
            target=target,
            status="rejected",
            detail=detail,
        )
        self._steps.append(step)
        _emit_prompt_log(
            f"step {step_index}/{self._max_steps}: rejected on "
            f"{_page_summary(self._prompt_pages()[self._active_page_index])}: "
            f"{step.detail}",
            event="prompt_rejected",
            step=step_index,
            max_steps=self._max_steps,
            page=self._active_page_index,
            detail=step.detail,
        )
        return step

    def _raise_prompt_failed(self, *, step_index: int, reason: str) -> None:
        normalized_reason = reason.strip() or (
            "The requested browser task could not be completed."
        )
        message = (
            "JourneyPlaywrightPage.prompt(...) could not complete instruction: "
            f"{normalized_reason}"
        )
        _emit_prompt_log(
            f"step {step_index}/{self._max_steps}: prompt failed: "
            f"{normalized_reason}",
            event="prompt_failed",
            step=step_index,
            max_steps=self._max_steps,
            page=self._active_page_index,
            reason=normalized_reason,
        )
        raise RuntimeError(message)

    def _memory_for_observation(
        self,
        observation: dict[str, object],
    ) -> dict[str, object] | None:
        if self._memory_path is None:
            return None
        if not self._memory_loaded:
            self._memory_loaded = True
            self._memory_page_signature = _page_memory_signature(observation)
            self._memory_key = prompt_memory_key(
                tool="playwright",
                instruction=self._instruction,
                page_signature=self._memory_page_signature,
            )
            self._memory_entry = load_prompt_memory_entry(
                self._memory_path,
                self._memory_key,
            )
            if self._memory_entry is not None:
                _emit_prompt_log(
                    f"loaded prompt memory from {self._memory_path}",
                    event="prompt_memory_loaded",
                    path=str(self._memory_path),
                    key=self._memory_key,
                )
        return self._memory_entry

    def _write_memory(self, *, final_output: str | dict[str, object]) -> None:
        if (
            self._memory_path is None
            or self._memory_key is None
            or self._memory_page_signature is None
            or prompt_memory_updates_disabled()
        ):
            return
        entry = _memory_entry_from_result(
            instruction=self._instruction,
            page_signature=self._memory_page_signature,
            final_output=final_output,
            pages=tuple(self._prompt_pages()),
            steps=tuple(self._steps),
        )
        if self._memory_entry is not None:
            entry["successful_steps"] = _merge_memory_steps(
                self._memory_entry.get("successful_steps"),
                entry["successful_steps"],
            )
            entry["rejected_steps"] = _merge_memory_steps(
                self._memory_entry.get("rejected_steps"),
                entry["rejected_steps"],
            )
        run_count = write_prompt_memory_entry(
            self._memory_path,
            self._memory_key,
            entry,
        )
        _emit_prompt_log(
            f"wrote prompt memory to {self._memory_path}",
            event="prompt_memory_saved",
            path=str(self._memory_path),
            key=self._memory_key,
            run_count=run_count,
        )

    def _execute_python_step(
        self,
        *,
        step_index: int,
        code: str,
        target: str,
    ) -> JourneyPlaywrightPromptStep:
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError(
                "JourneyPlaywrightPage.prompt(...) expected the model to return Python code."
            )
        self._discover_pages()
        namespace: dict[str, object] = {
            "__builtins__": {},
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
        compiled = compile(normalized_code, "<journey-playwright-prompt>", "exec")
        try:
            exec(compiled, namespace, namespace)
        finally:
            self._discover_pages()
        return JourneyPlaywrightPromptStep(
            index=step_index,
            page_index=self._active_page_index,
            action="python",
            target=target,
            status="ok",
            detail=f"Executed Python snippet. Active page index is {self._active_page_index}.",
        )

    def _discover_pages(self) -> None:
        context = _page_context(self._original_page)
        for page in _context_pages(context):
            if page not in self._pages:
                try:
                    page.wait_for_load_state("load", timeout=self._timeout_ms)
                except Exception:
                    pass
                self._pages.append(page)

    def _settle_active_page_for_final_output(self) -> None:
        active_page = self._pages[self._active_page_index]
        try:
            active_page.wait_for_load_state(
                "networkidle",
                timeout=min(self._timeout_ms, 2000),
            )
        except Exception:
            pass
        wait_for_timeout = getattr(active_page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            try:
                wait_for_timeout(500)
            except Exception:
                pass

    def _prompt_pages(self) -> list[JourneyPlaywrightPromptPage]:
        prompt_pages: list[JourneyPlaywrightPromptPage] = []
        for index, page in enumerate(self._pages):
            prompt_pages.append(
                JourneyPlaywrightPromptPage(
                    index=index,
                    url=page.url,
                    title=_safe_page_title(page),
                    is_original=page is self._original_page,
                )
            )
        return prompt_pages


def prompt_page(
    page: PlaywrightPage,
    *,
    instruction: str,
    model: str | None,
    max_steps: int,
    action_timeout_seconds: float,
    memory: str | None = None,
    output: PromptOutputSpec | None = None,
) -> JourneyPlaywrightPromptResult:
    normalized_instruction = _require_text_value(
        instruction,
        "JourneyPlaywrightPage.prompt(...) expects a non-blank instruction.",
    )
    resolved_model = _resolve_model(model)
    normalized_max_steps = _validate_max_steps(max_steps)
    normalized_timeout = _validate_timeout(action_timeout_seconds)
    memory_path = resolve_prompt_memory_path(
        memory,
        owner="JourneyPlaywrightPage.prompt(...)",
    )
    output_schema = normalize_prompt_output_spec(
        output,
        owner="JourneyPlaywrightPage.prompt(...)",
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


def _page_memory_signature(observation: dict[str, object]) -> str:
    active_page_index = cast(int, observation["active_page_index"])
    pages = cast(list[dict[str, object]], observation["pages"])
    active_page = pages[active_page_index]
    title = active_page.get("title")
    url = active_page.get("url")
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


def _memory_entry_from_result(
    *,
    instruction: str,
    page_signature: str,
    final_output: str | dict[str, object],
    pages: tuple[JourneyPlaywrightPromptPage, ...],
    steps: tuple[JourneyPlaywrightPromptStep, ...],
) -> dict[str, object]:
    return {
        "tool": "playwright",
        "instruction": normalize_prompt_instruction(instruction),
        "page_signature": page_signature,
        "final_output": _truncate_memory_value(final_output),
        "pages": [
            {
                "index": page.index,
                "url": truncate_prompt_memory_text(page.url),
                "title": truncate_prompt_memory_text(page.title),
                "is_original": page.is_original,
            }
            for page in pages
        ],
        "successful_steps": _memory_steps(steps, status="ok"),
        "rejected_steps": _memory_steps(steps, status="rejected"),
    }


def _memory_steps(
    steps: tuple[JourneyPlaywrightPromptStep, ...],
    *,
    status: str,
) -> list[dict[str, object]]:
    selected = [
        step
        for step in steps
        if step.action == "python" and step.status == status
    ]
    return [
        {
            "page_index": step.page_index,
            "target": truncate_prompt_memory_text(step.target),
            "detail": truncate_prompt_memory_text(step.detail),
        }
        for step in selected[-MAX_PROMPT_MEMORY_ITEMS:]
    ]


def _steps_observation(
    steps: tuple[JourneyPlaywrightPromptStep, ...],
) -> list[dict[str, object]]:
    return [
        {
            "index": step.index,
            "page_index": step.page_index,
            "action": step.action,
            "target": step.target,
            "status": step.status,
            "detail": step.detail,
        }
        for step in steps
    ]


def _truncate_memory_value(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return truncate_prompt_memory_text(value)
    if isinstance(value, list):
        return [
            _truncate_memory_value(item)
            for item in value[-MAX_PROMPT_MEMORY_ITEMS:]
        ]
    if isinstance(value, dict):
        return {
            truncate_prompt_memory_text(key): _truncate_memory_value(item)
            for key, item in list(value.items())[-MAX_PROMPT_MEMORY_ITEMS:]
            if isinstance(key, str)
        }
    return truncate_prompt_memory_text(value)


def _merge_memory_steps(
    existing: object,
    current: object,
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for collection in (existing, current):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            normalized = {
                "page_index": item.get("page_index"),
                "target": truncate_prompt_memory_text(item.get("target", "")),
                "detail": truncate_prompt_memory_text(item.get("detail", "")),
            }
            identity = (
                normalized["page_index"],
                normalized["target"],
                normalized["detail"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(normalized)
    return merged[-MAX_PROMPT_MEMORY_ITEMS:]


def _emit_prompt_log(
    message: str,
    *,
    event: str = "prompt_log",
    **fields: object,
) -> None:
    _PROMPT_LOGGER.info(event, message, **fields)


def _emit_prompt_code_log(*, step_label: str, code: str) -> None:
    if not code:
        _emit_prompt_log(
            f"{step_label} code: <blank>",
            event="prompt_code",
            step_label=step_label,
            code="<blank>",
        )
        return
    if "\n" not in code:
        _emit_prompt_log(
            f"{step_label} code: {code}",
            event="prompt_code",
            step_label=step_label,
            code=code,
        )
        return
    _emit_prompt_log(
        f"{step_label} code:",
        event="prompt_code",
        step_label=step_label,
    )
    for line in code.splitlines():
        _emit_prompt_log(
            f"  {line}",
            event="prompt_code",
            step_label=step_label,
            code=line,
        )


def _prompt_output_summary(value: str | dict[str, object]) -> str:
    if isinstance(value, str):
        return truncate_prompt_memory_text(value)
    return truncate_prompt_memory_text(json.dumps(value, sort_keys=True))


def _object_field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _completion_message(response: object) -> object:
    choices = _object_field(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected the model response to include choices."
        )
    first_choice = choices[0]
    message = _object_field(first_choice, "message")
    if message is None:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected the model response to include a message."
        )
    return message


def _extract_completion_tool_calls(response: object) -> tuple[_PromptToolCall, ...]:
    message = _completion_message(response)
    raw_tool_calls = _object_field(message, "tool_calls")
    if raw_tool_calls is None:
        return ()
    if not isinstance(raw_tool_calls, list):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected model tool_calls to be a list."
        )
    tool_calls: list[_PromptToolCall] = []
    for index, raw_tool_call in enumerate(raw_tool_calls, start=1):
        call_id = _object_field(raw_tool_call, "id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"journey_tool_call_{index}"
        function = _object_field(raw_tool_call, "function")
        name = _object_field(function, "name") if function is not None else None
        if not isinstance(name, str):
            name = ""
        arguments = (
            _object_field(function, "arguments") if function is not None else None
        )
        if isinstance(arguments, str):
            raw_arguments = arguments
        else:
            try:
                raw_arguments = json.dumps(arguments)
            except TypeError:
                raw_arguments = str(arguments)
        tool_calls.append(
            _PromptToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                raw={
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": raw_arguments,
                    },
                },
            )
        )
    return tuple(tool_calls)


def _assistant_message_from_response(
    response: object,
    tool_calls: tuple[_PromptToolCall, ...],
) -> dict[str, object]:
    message = _completion_message(response)
    assistant_message: dict[str, object] = {
        "role": "assistant",
        "tool_calls": [tool_call.raw for tool_call in tool_calls],
    }
    content = _object_field(message, "content")
    if content is not None:
        assistant_message["content"] = content
    return assistant_message


def _tool_result_message(
    *,
    tool_call: _PromptToolCall,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps(payload, sort_keys=True),
    }


def _tool_calls_target(tool_calls: tuple[_PromptToolCall, ...]) -> str:
    names = [tool_call.name or "<unnamed>" for tool_call in tool_calls]
    return ", ".join(names)[:200]


def _tool_call_text_argument(
    tool_call: _PromptToolCall,
    name: str,
    message: str,
) -> str:
    arguments = _parse_tool_call_arguments(tool_call)
    return _require_text_value(arguments.get(name), message)


def _parse_tool_call_arguments(tool_call: _PromptToolCall) -> dict[str, object]:
    raw_arguments = tool_call.arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "JourneyPlaywrightPage.prompt(...) expected tool arguments "
                "to be a JSON object."
            ) from exc
    else:
        parsed = raw_arguments
    if not isinstance(parsed, dict):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected tool arguments "
            "to be a JSON object."
        )
    return parsed


_VISIBLE_MESSAGE_FIELD_TERMS = (
    "error",
    "validation",
    "warning",
    "problem",
    "issue",
    "failure",
    "failed",
    "invalid",
    "incorrect",
)
_VISIBLE_MESSAGE_TEXT_TERMS = (
    "cannot",
    "denied",
    "error",
    "expired",
    "failed",
    "failure",
    "incorrect",
    "invalid",
    "not found",
    "required",
    "try again",
    "unable",
    "wrong",
)


def _fill_visible_message_fields(
    output: dict[str, object],
    *,
    schema: PromptOutputSchema,
    visible_text: str,
) -> dict[str, object]:
    fallback_message = _extract_visible_message(visible_text)
    if not fallback_message:
        return output
    repaired = dict(output)
    for field_name in schema.fields:
        if repaired.get(field_name) != "":
            continue
        field_schema = schema.properties.get(field_name)
        description = ""
        if isinstance(field_schema, dict):
            raw_description = field_schema.get("description")
            if isinstance(raw_description, str):
                description = raw_description
        field_hint = f"{field_name} {description}".lower()
        if any(term in field_hint for term in _VISIBLE_MESSAGE_FIELD_TERMS):
            repaired[field_name] = fallback_message
    return repaired


def _extract_visible_message(visible_text: str) -> str:
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    for window_size in (1, 2, 3):
        for index in range(0, max(len(lines) - window_size + 1, 0)):
            candidate = " ".join(lines[index : index + window_size])
            normalized = candidate.lower()
            if any(term in normalized for term in _VISIBLE_MESSAGE_TEXT_TERMS):
                return candidate
    return ""


def _page_summary(page: JourneyPlaywrightPromptPage) -> str:
    return _format_page_summary(index=page.index, title=page.title, url=page.url)


def _page_dict_summary(page: dict[str, object]) -> str:
    return _format_page_summary(
        index=page.get("index"),
        title=page.get("title"),
        url=page.get("url"),
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
    if call.func.id == "finish":
        return "finish the browser action loop"
    if call.func.id == "fail":
        return f"fail the browser prompt with {_call_arg_description(call, 0)}"
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


def _load_litellm_completion() -> _CompletionCallable:
    try:
        module = importlib.import_module("litellm")
    except ImportError as exc:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) could not import `litellm`. "
            f"The active Python interpreter is {sys.executable!r}; run Journey "
            "through the project environment or reinstall/sync this interpreter "
            "so it includes Journey SDK runtime dependencies."
        ) from exc
    completion = getattr(module, "completion", None)
    if not callable(completion):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) could not find litellm.completion."
        )
    return cast(_CompletionCallable, completion)


def _resolve_model(model: str | None) -> str:
    if model is not None and model.strip():
        return model.strip()
    env_model = os.environ.get(JOURNEY_PLAYWRIGHT_PROMPT_MODEL_ENV, "").strip()
    if env_model:
        return env_model
    raise RuntimeError(
        "JourneyPlaywrightPage.prompt(...) requires model=... or the "
        f"{JOURNEY_PLAYWRIGHT_PROMPT_MODEL_ENV} environment variable."
    )


def _validate_max_steps(max_steps: int) -> int:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError(
            "JourneyPlaywrightPage.prompt(..., max_steps=...) expects a positive integer."
        )
    return max_steps


def _validate_timeout(action_timeout_seconds: float) -> float:
    if isinstance(action_timeout_seconds, bool) or not isinstance(
        action_timeout_seconds,
        int | float,
    ):
        raise ValueError(
            "JourneyPlaywrightPage.prompt(..., action_timeout_seconds=...) "
            "expects a positive number."
        )
    normalized = float(action_timeout_seconds)
    if normalized <= 0:
        raise ValueError(
            "JourneyPlaywrightPage.prompt(..., action_timeout_seconds=...) "
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
            "JourneyPlaywrightPage.prompt(...) expected Playwright context.pages to be a list."
        )
    return [cast(PlaywrightPage, page) for page in pages]


def _png_data_url(page: PlaywrightPage) -> str:
    png_bytes = page.screenshot(type="png")
    if not isinstance(png_bytes, bytes):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected screenshot(type='png') to return bytes."
        )
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _rendered_html(page: PlaywrightPage) -> str:
    html = page.evaluate(_RENDERED_HTML_SCRIPT)
    if not isinstance(html, str):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected rendered HTML to be a string."
        )
    return html


def _visible_text(page: PlaywrightPage) -> str:
    text = page.evaluate(_VISIBLE_TEXT_SCRIPT)
    if not isinstance(text, str):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected visible text to be a string."
        )
    return text


def _extract_completion_text(response: object) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected the model response to include choices."
        )
    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None and isinstance(first_choice, dict):
        message = first_choice.get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        joined = "".join(text_parts).strip()
        if joined:
            return joined
    raise RuntimeError(
        "JourneyPlaywrightPage.prompt(...) expected the model response to include text content."
    )


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


def _parse_page_index(raw_index: object, *, page_count: int) -> int:
    if isinstance(raw_index, bool):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) switch_page index must be an integer."
        )
    if isinstance(raw_index, int):
        parsed = raw_index
    elif isinstance(raw_index, str):
        text = _require_text_value(
            raw_index,
            "JourneyPlaywrightPage.prompt(...) switch_page index must be a non-empty string or integer.",
        )
        try:
            parsed = int(text)
        except ValueError as exc:
            raise RuntimeError(
                "JourneyPlaywrightPage.prompt(...) switch_page index must be an integer."
            ) from exc
    else:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) switch_page index must be an integer."
        )
    if parsed < 0 or parsed >= page_count:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) switch_page target "
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
