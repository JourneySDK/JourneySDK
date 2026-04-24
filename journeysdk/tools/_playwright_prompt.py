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

_PROMPT_SYSTEM_MESSAGE = """You control a Playwright browser page through a strict JSON action protocol.

Return exactly one JSON object and nothing else.

JSON schema:
{
  "action": "click" | "fill" | "press" | "switch_page" | "wait_for_url" | "wait_for_text" | "finish",
  "target": "string",
  "value": "string or null"
}

Rules:
- For click/fill/press, target must be an element id from observation.elements[].id.
- Elements include an actions list; only choose an action that appears in that list.
- For switch_page, target must be a page index rendered as a string.
- For wait_for_url, target must be the expected URL or Playwright URL pattern.
- For wait_for_text, target must be the visible text to wait for.
- For finish, set value to the final user-facing answer and target to "".
- Never return markdown fences, explanations, or extra keys.
- Never invent element ids or page indexes.
- Use the fewest actions needed to satisfy the instruction.
"""

_COLLECT_ELEMENTS_SCRIPT = r"""
() => {
    const MAX_ELEMENTS = 50;
    const SELECTOR = [
        'a[href]',
        'button',
        'input:not([type="hidden"])',
        'textarea',
        'select',
        '[contenteditable]:not([contenteditable="false"])',
        '[role="button"]',
        '[role="link"]',
        '[role="checkbox"]',
        '[role="radio"]',
        '[role="tab"]',
        '[role="textbox"]',
        '[role="searchbox"]',
        '[role="combobox"]',
        '[role="spinbutton"]',
        '[tabindex]',
    ].join(',');
    const NON_FILLABLE_INPUT_TYPES = new Set([
        'button',
        'checkbox',
        'color',
        'file',
        'hidden',
        'image',
        'radio',
        'range',
        'reset',
        'submit',
    ]);
    const FILLABLE_ROLES = new Set([
        'combobox',
        'searchbox',
        'spinbutton',
        'textbox',
    ]);

    const escapeCss = (value) => {
        if (window.CSS && typeof window.CSS.escape === 'function') {
            return window.CSS.escape(value);
        }
        return String(value).replace(/["\\#.:>+~\[\]()]/g, '\\$&');
    };

    const normalizeText = (value) =>
        String(value || '')
            .replace(/\s+/g, ' ')
            .trim()
            .slice(0, 120);

    const isVisible = (element) => {
        if (!(element instanceof Element)) {
            return false;
        }
        const style = window.getComputedStyle(element);
        if (
            style.display === 'none' ||
            style.visibility === 'hidden' ||
            style.opacity === '0'
        ) {
            return false;
        }
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const inferredRole = (element) => {
        const explicit = element.getAttribute('role');
        if (explicit) {
            return explicit.toLowerCase();
        }
        const tag = element.tagName.toLowerCase();
        if (tag === 'button') {
            return 'button';
        }
        if (tag === 'a' && element.hasAttribute('href')) {
            return 'link';
        }
        if (tag === 'input') {
            const type = (element.getAttribute('type') || '').toLowerCase();
            if (type === 'checkbox') {
                return 'checkbox';
            }
            if (type === 'radio') {
                return 'radio';
            }
            if (type === 'button' || type === 'submit') {
                return 'button';
            }
        }
        return '';
    };

    const isDisabled = (element) =>
        element.hasAttribute('disabled') ||
        (element.getAttribute('aria-disabled') || '').toLowerCase() === 'true';

    const isReadonly = (element) =>
        element.hasAttribute('readonly') ||
        (element.getAttribute('aria-readonly') || '').toLowerCase() === 'true';

    const canFill = (element, role) => {
        if (isDisabled(element) || isReadonly(element)) {
            return false;
        }
        const tag = element.tagName.toLowerCase();
        if (tag === 'textarea' || tag === 'select') {
            return true;
        }
        if (tag === 'input') {
            const type = (element.getAttribute('type') || 'text').toLowerCase();
            return !NON_FILLABLE_INPUT_TYPES.has(type);
        }
        if (element.isContentEditable) {
            return true;
        }
        return FILLABLE_ROLES.has(role);
    };

    const actionsFor = (element, role) => {
        if (isDisabled(element)) {
            return [];
        }
        const actions = ['click'];
        if (canFill(element, role)) {
            actions.push('fill');
        }
        actions.push('press');
        return actions;
    };

    const buildSelector = (element) => {
        if (!(element instanceof Element)) {
            return '';
        }
        if (element.id) {
            return `#${escapeCss(element.id)}`;
        }
        const parts = [];
        let current = element;
        while (current && current instanceof Element) {
            let part = current.tagName.toLowerCase();
            if (current.id) {
                part += `#${escapeCss(current.id)}`;
                parts.unshift(part);
                break;
            }
            let index = 1;
            let sibling = current.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === current.tagName) {
                    index += 1;
                }
                sibling = sibling.previousElementSibling;
            }
            part += `:nth-of-type(${index})`;
            parts.unshift(part);
            current = current.parentElement;
        }
        return parts.join(' > ');
    };

    const seen = new Set();
    const candidates = [];
    for (const element of document.querySelectorAll(SELECTOR)) {
        if (!isVisible(element)) {
            continue;
        }
        const selector = buildSelector(element);
        if (!selector || seen.has(selector)) {
            continue;
        }
        seen.add(selector);
        const tagName = element.tagName.toLowerCase();
        const type = element.getAttribute('type') || '';
        const placeholder = element.getAttribute('placeholder') || '';
        const text = normalizeText(element.innerText || element.textContent || '');
        const value =
            'value' in element && typeof element.value === 'string'
                ? normalizeText(element.value)
                : '';
        const ariaLabel = normalizeText(element.getAttribute('aria-label') || '');
        const role = inferredRole(element);
        const actions = actionsFor(element, role);
        if (actions.length === 0) {
            continue;
        }
        const rect = element.getBoundingClientRect();
        candidates.push({
            selector,
            tag_name: tagName,
            type,
            role,
            name: ariaLabel || text || value || normalizeText(placeholder),
            text,
            placeholder,
            actions,
            _journey_sort_top: rect.top,
            _journey_sort_left: rect.left,
        });
    }
    candidates.sort((left, right) => {
        const leftFillable = left.actions.includes('fill') ? 0 : 1;
        const rightFillable = right.actions.includes('fill') ? 0 : 1;
        if (leftFillable !== rightFillable) {
            return leftFillable - rightFillable;
        }
        if (left._journey_sort_top !== right._journey_sort_top) {
            return left._journey_sort_top - right._journey_sort_top;
        }
        return left._journey_sort_left - right._journey_sort_left;
    });
    return candidates.slice(0, MAX_ELEMENTS).map((item) => {
        delete item._journey_sort_top;
        delete item._journey_sort_left;
        return item;
    });
}
"""


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


@dataclass(frozen=True)
class _PromptAction:
    action: str
    target: str
    value: str | None


@dataclass(frozen=True)
class _ObservedElement:
    element_id: str
    page_index: int
    selector: str
    actions: tuple[str, ...]
    role: str
    name: str
    text: str
    tag_name: str
    input_type: str
    placeholder: str


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


class _PromptActionRejected(RuntimeError):
    """A model-selected action was invalid for the current observation."""


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
        self._element_ids: dict[tuple[int, str], str] = {}
        self._next_element_id = 1

    def run(self) -> JourneyPlaywrightPromptResult:
        for step_index in range(1, self._max_steps + 1):
            observation = self._build_observation()
            action = self._request_action(observation=observation)
            if action.action == "finish":
                final_text = _require_text_value(
                    action.value,
                    "JourneyPlaywrightPage.prompt(...) finish action requires a non-empty value.",
                )
                self._steps.append(
                    JourneyPlaywrightPromptStep(
                        index=step_index,
                        page_index=self._active_page_index,
                        action=action.action,
                        target=action.target,
                        status="ok",
                        detail=final_text,
                    )
                )
                pages = tuple(self._prompt_pages())
                return JourneyPlaywrightPromptResult(
                    text=final_text,
                    model=self._model,
                    active_page_index=self._active_page_index,
                    pages=pages,
                    steps=tuple(self._steps),
                )
            try:
                step = self._execute_action(step_index=step_index, action=action)
            except _PromptActionRejected as exc:
                step = JourneyPlaywrightPromptStep(
                    index=step_index,
                    page_index=self._active_page_index,
                    action=action.action,
                    target=action.target,
                    status="rejected",
                    detail=str(exc),
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
        elements = self._collect_elements()
        active_page = self._pages[self._active_page_index]
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
            "elements": [
                {
                    "id": element.element_id,
                    "page_index": element.page_index,
                    "role": element.role,
                    "name": element.name,
                    "text": element.text,
                    "tag_name": element.tag_name,
                    "type": element.input_type,
                    "placeholder": element.placeholder,
                    "actions": list(element.actions),
                    "locator_hint": element.selector,
                }
                for element in elements
            ],
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

    def _collect_elements(self) -> list[_ObservedElement]:
        observed: list[_ObservedElement] = []
        for page_index, page in enumerate(self._pages):
            raw_elements = page.evaluate(_COLLECT_ELEMENTS_SCRIPT)
            if not isinstance(raw_elements, list):
                raise RuntimeError(
                    "JourneyPlaywrightPage.prompt(...) expected a list of page elements."
                )
            for raw_element in raw_elements:
                if not isinstance(raw_element, dict):
                    raise RuntimeError(
                        "JourneyPlaywrightPage.prompt(...) expected element snapshots to be dictionaries."
                    )
                selector = _require_text_value(
                    raw_element.get("selector"),
                    "JourneyPlaywrightPage.prompt(...) expected each element to include a selector.",
                )
                element_key = (page_index, selector)
                element_id = self._element_ids.get(element_key)
                if element_id is None:
                    element_id = f"e{self._next_element_id}"
                    self._next_element_id += 1
                    self._element_ids[element_key] = element_id
                role = _normalize_optional_text(raw_element.get("role"))
                tag_name = _normalize_optional_text(raw_element.get("tag_name"))
                input_type = _normalize_optional_text(raw_element.get("type"))
                actions = _normalize_actions(raw_element.get("actions"))
                if not actions:
                    actions = _infer_element_actions(
                        role=role,
                        tag_name=tag_name,
                        input_type=input_type,
                    )
                observed.append(
                    _ObservedElement(
                        element_id=element_id,
                        page_index=page_index,
                        selector=selector,
                        actions=actions,
                        role=role,
                        name=_normalize_optional_text(raw_element.get("name")),
                        text=_normalize_optional_text(raw_element.get("text")),
                        tag_name=tag_name,
                        input_type=input_type,
                        placeholder=_normalize_optional_text(raw_element.get("placeholder")),
                    )
                )
        return observed

    def _request_action(self, *, observation: dict[str, object]) -> _PromptAction:
        prompt_text = "\n".join(
            [
                f"Instruction:\n{self._instruction}",
                "",
                "Observation JSON:",
                json.dumps(
                    {
                        "active_page_index": observation["active_page_index"],
                        "pages": observation["pages"],
                        "elements": observation["elements"],
                        "steps": observation["steps"],
                    },
                    sort_keys=True,
                    indent=2,
                ),
                "",
                "Choose the single best next action.",
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
                max_tokens=300,
                temperature=0.0,
            )
        except Exception as exc:
            raise RuntimeError(
                "JourneyPlaywrightPage.prompt(...) failed to call "
                f"model {self._model!r}: {exc}"
            ) from exc
        response_text = _extract_completion_text(response)
        return _parse_action_response(response_text, model=self._model)

    def _execute_action(
        self,
        *,
        step_index: int,
        action: _PromptAction,
    ) -> JourneyPlaywrightPromptStep:
        self._discover_pages()
        active_page = self._pages[self._active_page_index]
        if action.action == "switch_page":
            target_index = _parse_page_index(action.target, page_count=len(self._pages))
            self._active_page_index = target_index
            target_page = self._pages[target_index]
            return JourneyPlaywrightPromptStep(
                index=step_index,
                page_index=target_index,
                action=action.action,
                target=action.target,
                status="ok",
                detail=(
                    f"Switched to page {target_index} "
                    f"({ _safe_page_title(target_page) or target_page.url })."
                ),
            )
        if action.action == "wait_for_url":
            target = _require_text_value(
                action.target,
                "JourneyPlaywrightPage.prompt(...) wait_for_url action requires a non-empty target.",
            )
            active_page.wait_for_url(target, timeout=self._timeout_ms)
            self._discover_pages()
            return JourneyPlaywrightPromptStep(
                index=step_index,
                page_index=self._active_page_index,
                action=action.action,
                target=target,
                status="ok",
                detail=f"Waited for URL {target!r}.",
            )
        if action.action == "wait_for_text":
            target = _require_text_value(
                action.target,
                "JourneyPlaywrightPage.prompt(...) wait_for_text action requires a non-empty target.",
            )
            active_page.get_by_text(target).first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
            self._discover_pages()
            return JourneyPlaywrightPromptStep(
                index=step_index,
                page_index=self._active_page_index,
                action=action.action,
                target=target,
                status="ok",
                detail=f"Waited for visible text {target!r}.",
            )

        element = self._find_target_element(action.target)
        if element.page_index != self._active_page_index:
            raise _PromptActionRejected(
                "Must switch to page "
                f"{element.page_index} before targeting element {element.element_id!r}."
            )
        if action.action == "click":
            _require_element_action(element, "click")
            locator = active_page.locator(element.selector).first
            locator.click(timeout=self._timeout_ms)
            self._discover_pages()
            return JourneyPlaywrightPromptStep(
                index=step_index,
                page_index=self._active_page_index,
                action=action.action,
                target=element.element_id,
                status="ok",
                detail=f"Clicked { _element_label(element) }.",
            )
        if action.action == "fill":
            value = _require_text_value(
                action.value,
                "JourneyPlaywrightPage.prompt(...) fill action requires a non-empty value.",
            )
            _require_element_action(element, "fill")
            locator = active_page.locator(element.selector).first
            locator.fill(value, timeout=self._timeout_ms)
            self._discover_pages()
            return JourneyPlaywrightPromptStep(
                index=step_index,
                page_index=self._active_page_index,
                action=action.action,
                target=element.element_id,
                status="ok",
                detail=f"Filled { _element_label(element) } with {value!r}.",
            )
        if action.action == "press":
            value = _require_text_value(
                action.value,
                "JourneyPlaywrightPage.prompt(...) press action requires a non-empty value.",
            )
            _require_element_action(element, "press")
            locator = active_page.locator(element.selector).first
            locator.press(value, timeout=self._timeout_ms)
            self._discover_pages()
            return JourneyPlaywrightPromptStep(
                index=step_index,
                page_index=self._active_page_index,
                action=action.action,
                target=element.element_id,
                status="ok",
                detail=f"Pressed {value!r} on { _element_label(element) }.",
            )
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) received unsupported action "
            f"{action.action!r}."
        )

    def _find_target_element(self, element_id: str) -> _ObservedElement:
        normalized_id = _require_text_value(
            element_id,
            "JourneyPlaywrightPage.prompt(...) action target must be a non-empty string.",
        )
        for element in self._collect_elements():
            if element.element_id == normalized_id:
                return element
        raise _PromptActionRejected(
            "The action referenced unknown element "
            f"{normalized_id!r}."
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


def _parse_action_response(text: str, *, model: str) -> _PromptAction:
    normalized = _strip_code_fences(text)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected model "
            f"{model!r} to return one JSON action."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) expected the model to return one JSON object."
        )
    action = _require_text_value(
        payload.get("action"),
        "JourneyPlaywrightPage.prompt(...) model action must include a non-empty 'action' field.",
    )
    allowed_actions = {
        "click",
        "fill",
        "press",
        "switch_page",
        "wait_for_url",
        "wait_for_text",
        "finish",
    }
    if action not in allowed_actions:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) model returned unsupported action "
            f"{action!r}."
        )
    target = _normalize_optional_text(payload.get("target"))
    value = payload.get("value")
    if value is not None and not isinstance(value, str):
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) model action 'value' must be a string or null."
        )
    return _PromptAction(action=action, target=target, value=value)


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


def _parse_page_index(raw_index: str, *, page_count: int) -> int:
    text = _require_text_value(
        raw_index,
        "JourneyPlaywrightPage.prompt(...) switch_page action requires a page index target.",
    )
    try:
        parsed = int(text)
    except ValueError as exc:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) switch_page target must be an integer string."
        ) from exc
    if parsed < 0 or parsed >= page_count:
        raise RuntimeError(
            "JourneyPlaywrightPage.prompt(...) switch_page target "
            f"{parsed} is outside the known page range 0..{page_count - 1}."
        )
    return parsed


def _normalize_optional_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalize_actions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    actions: list[str] = []
    for item in value:
        if item in {"click", "fill", "press"} and item not in actions:
            actions.append(item)
    return tuple(actions)


def _infer_element_actions(
    *,
    role: str,
    tag_name: str,
    input_type: str,
) -> tuple[str, ...]:
    fillable_roles = {"combobox", "searchbox", "spinbutton", "textbox"}
    non_fillable_input_types = {
        "button",
        "checkbox",
        "color",
        "file",
        "hidden",
        "image",
        "radio",
        "range",
        "reset",
        "submit",
    }
    actions = ["click"]
    if (
        tag_name in {"select", "textarea"}
        or role in fillable_roles
        or (tag_name == "input" and input_type not in non_fillable_input_types)
    ):
        actions.append("fill")
    actions.append("press")
    return tuple(actions)


def _require_text_value(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()


def _require_element_action(element: _ObservedElement, action: str) -> None:
    if action in element.actions:
        return
    available = ", ".join(element.actions) or "none"
    raise _PromptActionRejected(
        f"Element { _element_label(element) } does not allow {action!r}; "
        f"available actions: {available}."
    )


def _safe_page_title(page: PlaywrightPage) -> str:
    try:
        title = page.title()
    except Exception:
        return ""
    return title.strip()


def _element_label(element: _ObservedElement) -> str:
    parts = [element.element_id]
    if element.name:
        parts.append(element.name)
    elif element.text:
        parts.append(element.text)
    elif element.selector:
        parts.append(element.selector)
    return " ".join(parts)
