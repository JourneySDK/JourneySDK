"""Private helpers for Journey Playwright prompting."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import importlib
import json
import os
import sys
from typing import Protocol, cast

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
"""

_RENDERED_HTML_SCRIPT = "() => document.documentElement ? document.documentElement.outerHTML : ''"


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
    ) -> None:
        self._original_page = page
        self._instruction = instruction
        self._model = model
        self._max_steps = max_steps
        self._timeout_ms = int(action_timeout_seconds * 1000)
        self._completion = _load_litellm_completion()
        self._pages: list[PlaywrightPage] = [page]
        self._active_page_index = 0
        self._steps: list[JourneyPlaywrightPromptStep] = []

    def run(self) -> JourneyPlaywrightPromptResult:
        for step_index in range(1, self._max_steps + 1):
            observation = self._build_observation()
            code = self._request_code(observation=observation)
            target = _first_code_line(code)
            try:
                step = self._execute_python_step(
                    step_index=step_index,
                    code=code,
                    target=target,
                )
            except _PromptFinished as finished:
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
                pages = tuple(self._prompt_pages())
                return JourneyPlaywrightPromptResult(
                    text=finished.text,
                    model=self._model,
                    active_page_index=self._active_page_index,
                    pages=pages,
                    steps=tuple(self._steps),
                )
            except Exception as exc:
                step = JourneyPlaywrightPromptStep(
                    index=step_index,
                    page_index=self._active_page_index,
                    action="python",
                    target=target,
                    status="rejected",
                    detail=_format_python_error(exc),
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
        raise RuntimeError(message)

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
        prompt_text = "\n".join(
            [
                f"Instruction:\n{self._instruction}",
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
) -> JourneyPlaywrightPromptResult:
    normalized_instruction = _require_text_value(
        instruction,
        "JourneyPlaywrightPage.prompt(...) expects a non-blank instruction.",
    )
    resolved_model = _resolve_model(model)
    normalized_max_steps = _validate_max_steps(max_steps)
    normalized_timeout = _validate_timeout(action_timeout_seconds)
    session = _PromptSession(
        page=page,
        instruction=normalized_instruction,
        model=resolved_model,
        max_steps=normalized_max_steps,
        action_timeout_seconds=normalized_timeout,
    )
    return session.run()


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
