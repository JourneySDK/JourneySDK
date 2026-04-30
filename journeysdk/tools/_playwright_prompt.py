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
    resolve_prompt_memory_path,
    truncate_prompt_memory_text,
    write_prompt_memory_entry,
)
from playwright.sync_api import Page as PlaywrightPage

JOURNEY_PLAYWRIGHT_PROMPT_MODEL_ENV = "JOURNEY_PLAYWRIGHT_PROMPT_MODEL"

_PROMPT_SYSTEM_MESSAGE = """You control a Playwright sync browser page by returning Python snippets.

Return only executable Python code. Do not return JSON, markdown fences, comments, or explanations.

Available names:
- page: the active Playwright sync Page
- pages: a tuple of known Playwright sync Page objects
- timeout_ms: the configured action timeout in milliseconds
- switch_page(index): switch the active page to a known page index and return that Page
- finish(text): finish the loop with the final user-facing answer

Rules:
- Inspect the rendered HTML and screenshot, then choose the fewest Playwright commands needed.
- Prefer robust Playwright locators such as get_by_role, get_by_text, get_by_label, and locator.
- Pass timeout=timeout_ms to actions and waits that accept a timeout.
- Use switch_page(index) before acting on a popup or tab listed in known pages.
- Call finish("...") only when the instruction is complete.
- If the previous step was rejected, correct the Python and try again.
- If prompt memory is provided, use it as a hint from prior successful runs,
  but trust the current rendered HTML and screenshot over stale memory.
"""

_RENDERED_HTML_SCRIPT = "() => document.documentElement ? document.documentElement.outerHTML : ''"
_PROMPT_LOGGER = get_logger("playwright-prompt")


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
    text: str
    model: str
    active_page_index: int
    pages: tuple[JourneyPlaywrightPromptPage, ...]
    steps: tuple[JourneyPlaywrightPromptStep, ...]


class _CompletionCallable(Protocol):
    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
    ) -> object:
        ...


class _PromptFinished(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


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
    ) -> None:
        self._original_page = page
        self._instruction = instruction
        self._model = model
        self._max_steps = max_steps
        self._timeout_ms = int(action_timeout_seconds * 1000)
        self._memory_path = memory_path
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
        for step_index in range(1, self._max_steps + 1):
            observation = self._build_observation()
            self._log_inspection(step_index=step_index, observation=observation)
            code = self._request_code(observation=observation)
            target = _first_code_line(code)
            previous_page_count = len(self._pages)
            previous_active_page_index = self._active_page_index
            self._log_action(step_index=step_index, code=code)
            try:
                step = self._execute_python_step(
                    step_index=step_index,
                    code=code,
                    target=target,
                )
            except _PromptFinished as finished:
                self._log_new_pages(previous_page_count=previous_page_count)
                self._log_active_page_change(
                    previous_active_page_index=previous_active_page_index,
                )
                self._steps.append(
                    JourneyPlaywrightPromptStep(
                        index=step_index,
                        page_index=self._active_page_index,
                        action="finish",
                        target="",
                        status="ok",
                        detail=finished.text,
                    )
                )
                _emit_prompt_log(
                    f"step {step_index}/{self._max_steps}: finished with "
                    f"answer: {finished.text}",
                    event="prompt_finish",
                    step=step_index,
                    max_steps=self._max_steps,
                    answer=finished.text,
                )
                pages = tuple(self._prompt_pages())
                result = JourneyPlaywrightPromptResult(
                    text=finished.text,
                    model=self._model,
                    active_page_index=self._active_page_index,
                    pages=pages,
                    steps=tuple(self._steps),
                )
                self._write_memory(result)
                return result
            except Exception as exc:
                self._log_new_pages(previous_page_count=previous_page_count)
                self._log_active_page_change(
                    previous_active_page_index=previous_active_page_index,
                )
                step = JourneyPlaywrightPromptStep(
                    index=step_index,
                    page_index=self._active_page_index,
                    action="python",
                    target=target,
                    status="rejected",
                    detail=_format_python_error(exc),
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

        message = (
            "JourneyPlaywrightPage.prompt(...) reached "
            f"max_steps={self._max_steps} without a finish action."
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
            "steps": [
                {
                    "index": step.index,
                    "page_index": step.page_index,
                    "action": step.action,
                    "target": step.target,
                    "status": step.status,
                    "detail": step.detail,
                }
                for step in self._steps
            ],
            "screenshot_data_url": screenshot_data_url,
        }

    def _request_code(self, *, observation: dict[str, object]) -> str:
        memory_entry = self._memory_for_observation(observation)
        memory_section: list[str] = []
        if memory_entry is not None:
            memory_section = [
                "",
                "Prompt memory JSON:",
                json.dumps(memory_entry, sort_keys=True, indent=2),
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
                "Previous steps JSON:",
                json.dumps(observation["steps"], sort_keys=True, indent=2),
                "",
                "Active page rendered HTML:",
                "<journey-rendered-html>",
                cast(str, observation["rendered_html"]),
                "</journey-rendered-html>",
                "",
                "Return only the next Python snippet to execute.",
            ]
        )
        screenshot_data_url = cast(str, observation["screenshot_data_url"])
        try:
            response = self._completion(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": _PROMPT_SYSTEM_MESSAGE,
                    },
                    {
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
                    },
                ],
                max_tokens=800,
                temperature=0.0,
            )
        except Exception as exc:
            raise RuntimeError(
                "JourneyPlaywrightPage.prompt(...) failed to call "
                f"model {self._model!r}: {exc}"
            ) from exc
        response_text = _extract_completion_text(response)
        return _strip_code_fences(response_text)

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

    def _write_memory(self, result: JourneyPlaywrightPromptResult) -> None:
        if (
            self._memory_path is None
            or self._memory_key is None
            or self._memory_page_signature is None
        ):
            return
        entry = _memory_entry_from_result(
            instruction=self._instruction,
            page_signature=self._memory_page_signature,
            result=result,
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

        def finish(text: object) -> None:
            final_text = _require_text_value(
                text,
                "JourneyPlaywrightPage.prompt(...) finish(...) requires a non-empty string.",
            )
            raise _PromptFinished(final_text)

        namespace["switch_page"] = switch_page
        namespace["finish"] = finish
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
    session = _PromptSession(
        page=page,
        instruction=normalized_instruction,
        model=resolved_model,
        max_steps=normalized_max_steps,
        action_timeout_seconds=normalized_timeout,
        memory_path=memory_path,
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
    result: JourneyPlaywrightPromptResult,
) -> dict[str, object]:
    return {
        "tool": "playwright",
        "instruction": normalize_prompt_instruction(instruction),
        "page_signature": page_signature,
        "final_answer": truncate_prompt_memory_text(result.text),
        "pages": [
            {
                "index": page.index,
                "url": truncate_prompt_memory_text(page.url),
                "title": truncate_prompt_memory_text(page.title),
                "is_original": page.is_original,
            }
            for page in result.pages
        ],
        "successful_steps": _memory_steps(result.steps, status="ok"),
        "rejected_steps": _memory_steps(result.steps, status="rejected"),
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
        if call.args:
            return f"finish with answer {_node_description(call.args[0])}"
        return "finish the prompt"
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
