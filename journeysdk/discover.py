"""Internal browser discovery for generating Journey specs."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, is_dataclass, fields
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from playwright.sync_api import Page as PlaywrightPage
from playwright.sync_api import sync_playwright

from journeysdk._prompt_engine import (
    _PromptUsageTracker,
    _extract_langchain_text,
    _load_langchain_model,
    resolve_prompt_model,
)
from journeysdk.api import is_journey_callable
from journeysdk.logger import get_logger, pretty_row
from journeysdk.planner import compile_journey
from journeysdk.touchpoints._browser_prompt import (
    _PROMPT_SAFE_BUILTINS,
    _semantic_dom_snapshot,
    _strip_code_fences,
    _validate_prompt_python_code,
    _visible_text,
)
from journeysdk.touchpoints.browser import JourneyBrowserPage, ensure_browser_installed
from journeysdk.touchpoints.webhook import CloudWebhookEndpoint
from journeysdk.touchpoints._webhook_cloud import fetch_next_request


JOURNEY_DISCOVER_MODEL_ENV = "JOURNEY_BROWSER_PROMPT_MODEL"
DEFAULT_JOURNEY_DISCOVER_MODEL = "anthropic:claude-haiku-4-5"
_SUPPORTED_BROWSERS = {"chromium", "firefox", "webkit"}
_LOGGER = get_logger("discover")
_MAX_OBSERVATION_TEXT_LENGTH = 8000
_DEFAULT_CANDIDATES_PER_STATE = 5
_DEFAULT_MAX_MODEL_CALLS = 8
_DEFAULT_DISCOVER_ACTION_TIMEOUT_SECONDS = 8.0
_GENERATED_REPLAY_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_VARIANTS_PER_CONTROL = 3
_JSON_STATE_PATHS = (
    "/api/state",
    "/api/status",
    "/api/registration",
    "/api/registrations",
    "/api/order",
    "/api/orders",
    "/api/events",
    "/state",
)
_IDENTIFIER_QUERY_KEYS = (
    "confirmation_code",
    "code",
    "id",
    "reference",
    "token",
)
_EMAIL_EVIDENCE_URL_ENV = (
    "JOURNEY_DISCOVER_EMAIL_EVIDENCE_URL",
    "MAILPIT_URL",
    "MAILPIT_HTTP_URL",
    "MAILHOG_URL",
)
_WEBHOOK_EVIDENCE_URL_ENV = (
    "JOURNEY_DISCOVER_WEBHOOK_EVIDENCE_URL",
    "WEBHOOK_RECEIVER_URL",
    "WEBHOOK_EVIDENCE_URL",
)
_EMAIL_EVIDENCE_URLS = (
    "http://127.0.0.1:8025",
    "http://localhost:8025",
)
_WEBHOOK_EVIDENCE_URLS: tuple[str, ...] = ()
_OPTIONAL_EVIDENCE_FIELD_TERMS = (
    "accessibility",
    "accommodation",
    "accommodations",
    "needs",
    "requirement",
    "requirements",
    "preference",
    "preferences",
    "comment",
    "comments",
    "instruction",
    "instructions",
    "note",
    "notes",
    "dietary",
)
_OPTIONAL_EVIDENCE_FIELD_VALUE = "Wheelchair access near the front row"

_AFFORDANCE_SCRIPT = r"""
() => {
  const cssEscape = window.CSS && CSS.escape
    ? (value) => CSS.escape(String(value))
    : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");

  function attrValue(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function cleanText(value) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, 180);
  }

  function isVisible(element) {
    if (!element || !element.isConnected) return false;
    const style = window.getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none") return false;
    if (Number(style.opacity) === 0) return false;
    return element.getClientRects().length > 0;
  }

  function cssPath(element) {
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
      const tag = current.tagName.toLowerCase();
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName.toLowerCase() === tag) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${tag}:nth-of-type(${index})`);
      current = current.parentElement;
    }
    return parts.length ? `body > ${parts.join(" > ")}` : null;
  }

  function uniqueSelector(selector) {
    if (!selector) return false;
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch (_error) {
      return false;
    }
  }

  function genericAriaLabel(value) {
    const normalized = cleanText(value).toLowerCase();
    return ["", "button", "link", "menu", "close", "icon"].includes(normalized);
  }

  function selectorFor(element) {
    const testid = element.getAttribute("data-testid");
    if (testid) {
      const selector = `[data-testid="${attrValue(testid)}"]`;
      if (uniqueSelector(selector)) return { selector, kind: "testid", unique: true };
    }
    if (element.id) {
      const selector = `#${cssEscape(element.id)}`;
      if (uniqueSelector(selector)) return { selector, kind: "id", unique: true };
    }
    const name = element.getAttribute("name");
    if (name) {
      const selector = `${element.tagName.toLowerCase()}[name="${attrValue(name)}"]`;
      if (uniqueSelector(selector)) return { selector, kind: "name", unique: true };
    }
    const aria = element.getAttribute("aria-label");
    if (aria && !genericAriaLabel(aria)) {
      const selector = `${element.tagName.toLowerCase()}[aria-label="${attrValue(aria)}"]`;
      if (uniqueSelector(selector)) return { selector, kind: "aria", unique: true };
    }
    const selector = cssPath(element);
    return { selector, kind: selector ? "css_path" : "", unique: Boolean(selector) };
  }

  function labelFor(element) {
    if (element.labels && element.labels.length) {
      return cleanText(Array.from(element.labels).map((label) => label.innerText || label.textContent).join(" "));
    }
    const id = element.getAttribute("id");
    if (id) {
      const label = document.querySelector(`label[for="${attrValue(id)}"]`);
      if (label) return cleanText(label.innerText || label.textContent);
    }
    return cleanText(element.getAttribute("aria-label") || element.getAttribute("placeholder") || "");
  }

  function fieldInfo(element) {
    const tag = element.tagName.toLowerCase();
    const type = tag === "input" ? String(element.getAttribute("type") || "text").toLowerCase() : tag;
    if (["hidden", "submit", "button", "image", "reset", "file"].includes(type)) return null;
    const selector = selectorFor(element);
    const info = {
      tag,
      type,
      selector: selector.selector,
      selectorKind: selector.kind,
      selectorUnique: selector.unique,
      id: element.getAttribute("id") || "",
      name: element.getAttribute("name") || "",
      testid: element.getAttribute("data-testid") || "",
      label: labelFor(element),
      placeholder: element.getAttribute("placeholder") || "",
      value: "value" in element ? String(element.value || "") : "",
      required: Boolean(element.required),
      disabled: Boolean(element.disabled) || element.getAttribute("aria-disabled") === "true",
      readonly: Boolean(element.readOnly),
      checked: Boolean(element.checked),
      visible: isVisible(element),
    };
    if (tag === "select") {
      info.options = Array.from(element.options || []).map((option) => ({
        value: option.value,
        text: cleanText(option.text),
        selected: Boolean(option.selected),
        disabled: Boolean(option.disabled),
      }));
    }
    return info;
  }

  function controlInfo(element) {
    const tag = element.tagName.toLowerCase();
    const text = cleanText(element.innerText || element.textContent || element.value || element.getAttribute("aria-label") || "");
    const selector = selectorFor(element);
    const matchingTextControls = text
      ? Array.from(document.querySelectorAll(tag))
          .filter((candidate) => isVisible(candidate))
          .filter((candidate) => cleanText(candidate.innerText || candidate.textContent || candidate.value || candidate.getAttribute("aria-label") || "") === text)
          .length
      : 0;
    return {
      tag,
      selector: selector.selector,
      selectorKind: selector.kind,
      selectorUnique: selector.unique,
      testid: element.getAttribute("data-testid") || "",
      id: element.getAttribute("id") || "",
      name: element.getAttribute("name") || "",
      role: element.getAttribute("role") || "",
      aria: element.getAttribute("aria-label") || "",
      text,
      textUnique: Boolean(text && matchingTextControls === 1),
      href: element.href || element.getAttribute("href") || "",
      type: String(element.getAttribute("type") || "").toLowerCase(),
      disabled: Boolean(element.disabled) || element.getAttribute("aria-disabled") === "true",
      visible: isVisible(element),
    };
  }

  const forms = Array.from(document.querySelectorAll("form")).map((form) => {
    const fields = Array.from(form.querySelectorAll("input, textarea, select"))
      .map(fieldInfo)
      .filter((field) => field && field.selector && !field.disabled);
    const submitElement = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
    const selector = selectorFor(form);
    return {
      selector: selector.selector,
      action: form.action || form.getAttribute("action") || "",
      method: String(form.method || "get").toLowerCase(),
      fields,
      submit: submitElement ? controlInfo(submitElement) : null,
    };
  });

  const links = Array.from(document.querySelectorAll("a[href]"))
    .filter(isVisible)
    .map(controlInfo)
    .filter((link) => link.selector && link.href);

  const buttons = Array.from(document.querySelectorAll("button, [role='button']"))
    .filter((button) => isVisible(button) && !button.closest("form"))
    .map(controlInfo)
    .filter((button) => button.selector && !button.disabled);

  return {
    route: window.location.pathname || "/",
    title: document.title || "",
    forms,
    links,
    buttons,
  };
}
"""


@dataclass(frozen=True)
class DiscoverOptions:
    urls: tuple[str, ...] = ()
    start_page_state: "BrowserStartState | None" = None
    anchor_step: str | None = None
    journey_name: str = "discovered_journey"
    depth: int = 4
    max_actions: int = 30
    max_model_calls: int = _DEFAULT_MAX_MODEL_CALLS
    max_variants_per_control: int = _DEFAULT_MAX_VARIANTS_PER_CONTROL
    side_effect_probes: Literal["auto", "off"] = "auto"
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    model: str | None = None
    allow_external: bool = False
    action_timeout_seconds: float = _DEFAULT_DISCOVER_ACTION_TIMEOUT_SECONDS
    email_evidence_urls: tuple[str, ...] = ()
    webhook_evidence_urls: tuple[str, ...] = ()
    cloud_webhook_endpoints: tuple[object, ...] = ()


@dataclass(frozen=True)
class BrowserStartState:
    url: str
    cookies: tuple[dict[str, object], ...] = ()
    local_storage: tuple[tuple[str, str], ...] = ()

    def local_storage_dict(self) -> dict[str, str]:
        return dict(self.local_storage)


@dataclass(frozen=True)
class AnchorEvidenceContext:
    email_evidence_urls: tuple[str, ...] = ()
    webhook_evidence_urls: tuple[str, ...] = ()
    cloud_webhook_endpoints: tuple[object, ...] = ()


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str
    visible_text: str
    semantic_dom: str
    signature: str
    affordances: str = ""


@dataclass(frozen=True)
class CandidateAction:
    name: str
    description: str
    code: str
    captures: tuple["ActionCapture", ...] = ()
    variant_group: str | None = None
    variant_value: str | None = None
    variant_label: str | None = None


@dataclass(frozen=True)
class ActionCapture:
    variable: str
    value: str
    kind: str
    label: str


@dataclass(frozen=True)
class ProbeSpec:
    kind: Literal[
        "json_state",
        "email_evidence",
        "webhook_evidence",
        "cloud_webhook_evidence",
    ]
    url: str
    description: str


@dataclass
class DiscoveredNode:
    node_id: str
    start_url: str
    snapshot: PageSnapshot
    depth: int
    path: tuple["DiscoveredEdge", ...] = ()
    edges: list["DiscoveredEdge"] = field(default_factory=list)
    stop_reasons: set[str] = field(default_factory=set)
    prefetched_candidates: tuple[CandidateAction, ...] | None = None


@dataclass
class DiscoveredEdge:
    edge_id: str
    parent: DiscoveredNode
    action: CandidateAction
    snapshot: PageSnapshot
    child: DiscoveredNode | None = None
    function_name: str = ""
    probes: tuple[ProbeSpec, ...] = ()
    visible_assertions: tuple[str, ...] = ()


@dataclass
class _CrawlBudget:
    max_model_calls: int
    model_calls: int = 0


@dataclass(frozen=True)
class _ActionResult:
    snapshot: PageSnapshot
    prefetched_candidates: tuple[CandidateAction, ...]
    probes: tuple[ProbeSpec, ...] = ()


@dataclass(frozen=True)
class DiscoverResult:
    mode: Literal["url", "step"]
    journey_name: str
    extension_name: str | None
    roots: tuple[DiscoveredNode, ...]
    source: str
    actions: int
    branches: int
    omitted_actions: int
    model_calls: int
    model: str
    stop_reason: str


@dataclass(frozen=True)
class SourceMergeResult:
    source: str
    model: str



class ActionProvider(Protocol):
    def propose_actions(
        self,
        snapshot: PageSnapshot,
        *,
        path: tuple[DiscoveredEdge, ...],
        remaining_actions: int,
        depth_remaining: int,
    ) -> list[CandidateAction]:
        """Return candidate snippets to try from the current page state."""


class ModelActionProvider:
    def __init__(self, *, model: str, candidates_per_state: int = _DEFAULT_CANDIDATES_PER_STATE) -> None:
        self._model_name = model
        self._model: object | None = None
        self._candidates_per_state = candidates_per_state
        self._usage_tracker = _PromptUsageTracker()

    def propose_actions(
        self,
        snapshot: PageSnapshot,
        *,
        path: tuple[DiscoveredEdge, ...],
        remaining_actions: int,
        depth_remaining: int,
    ) -> list[CandidateAction]:
        if self._model is None:
            self._model = _load_langchain_model(self._model_name)
        prompt = _candidate_prompt(
            snapshot,
            path=path,
            remaining_actions=remaining_actions,
            depth_remaining=depth_remaining,
            max_candidates=min(self._candidates_per_state, remaining_actions),
        )
        messages = [
            {"role": "system", "content": _DISCOVER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = self._usage_tracker.call(
            operation="discover_actions",
            configured_model=self._model_name,
            logger=_LOGGER,
            callback=lambda config: self._model.invoke(messages, config=config),
        )
        response_text = _extract_langchain_text(
            response,
            owner="journey discover",
        )
        return parse_candidate_actions(
            response_text,
            strict=False,
            forbidden_literals=_stable_identifiers(snapshot.visible_text),
        )[
            : min(self._candidates_per_state, remaining_actions)
        ]


def browser_state_from_journey_page(
    page: object,
    *,
    step_label: str | None = None,
) -> BrowserStartState:
    """Extract the saved browser state needed to continue discovery from a step."""

    if not isinstance(page, JourneyBrowserPage):
        actual_type = type(page).__name__ if page is not None else "None"
        step_text = f" step {step_label!r}" if step_label else " step"
        raise TypeError(
            f"journey discover step mode requires{step_text} to return JourneyBrowserPage, got {actual_type}."
        )
    snapshot = page._snapshot_for_storage()
    return BrowserStartState(
        url=snapshot.url,
        cookies=tuple(dict(cookie) for cookie in snapshot.cookies),
        local_storage=tuple(snapshot.local_storage),
    )


def browser_state_from_step_anchor(
    step_result: object,
    *,
    side_outputs: dict[str, tuple[object, ...]] | None = None,
    step_label: str | None = None,
) -> BrowserStartState:
    """Extract browser state from a returned page or browser side output."""

    if isinstance(step_result, JourneyBrowserPage):
        return browser_state_from_journey_page(step_result, step_label=step_label)

    candidate_pages: list[JourneyBrowserPage] = []
    for values in (side_outputs or {}).values():
        candidate_pages.extend(
            value for value in values if isinstance(value, JourneyBrowserPage)
        )
    if candidate_pages:
        if len(candidate_pages) > 1:
            _LOGGER.info(
                "discover_anchor_pages_found",
                "journey discover found multiple browser pages from the anchor step; using the last one",
                step=step_label,
                pages=[page.url for page in candidate_pages],
                selected_url=candidate_pages[-1].url,
            )
        return browser_state_from_journey_page(
            candidate_pages[-1],
            step_label=step_label,
        )

    actual_type = type(step_result).__name__ if step_result is not None else "None"
    step_text = f" step {step_label!r}" if step_label else " step"
    raise TypeError(
        f"journey discover step mode requires{step_text} to return JourneyBrowserPage "
        f"or open one with open_page(...); got {actual_type} and no browser side output."
    )


def evidence_context_from_step_anchor(step_result: object) -> AnchorEvidenceContext:
    """Extract generic local evidence sources from an anchor step result."""

    email_urls: list[str] = []
    webhook_urls: list[str] = []
    cloud_webhook_endpoints: list[object] = []

    for value in _walk_anchor_objects(step_result):
        if isinstance(value, CloudWebhookEndpoint):
            cloud_webhook_endpoints.append(value)
            continue
        service_url = getattr(value, "service_url", None)
        if not callable(service_url):
            continue
        available_services = _available_service_names(value)
        email_urls.extend(
            _service_url_candidates(
                service_url,
                service_names=_service_name_candidates(
                    ("mailpit", "mailhog", "mail", "email", "smtp"),
                    available=available_services,
                ),
                ports=(8025,),
            )
        )
        webhook_urls.extend(
            _service_url_candidates(
                service_url,
                service_names=_service_name_candidates(
                    ("webhook", "webhooks", "receiver", "callback"),
                    available=available_services,
                ),
                ports=(9000, 8080, 8000),
            )
        )

    return AnchorEvidenceContext(
        email_evidence_urls=_dedupe_strings(email_urls),
        webhook_evidence_urls=_dedupe_strings(webhook_urls),
        cloud_webhook_endpoints=tuple(cloud_webhook_endpoints),
    )


def _walk_anchor_objects(value: object, *, limit: int = 200) -> tuple[object, ...]:
    values: list[object] = []
    seen: set[int] = set()

    def visit(item: object) -> None:
        if len(values) >= limit:
            return
        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            return
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        values.append(item)
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)
            return
        if is_dataclass(item) and not isinstance(item, type):
            for field_info in fields(item):
                try:
                    visit(getattr(item, field_info.name))
                except Exception:
                    continue
            return
        item_dict = getattr(item, "__dict__", None)
        if isinstance(item_dict, dict):
            for child in item_dict.values():
                visit(child)

    visit(value)
    return tuple(values)


def _service_url_candidates(
    service_url: object,
    *,
    service_names: Sequence[str],
    ports: Sequence[int],
) -> list[str]:
    candidates: list[str] = []
    for service_name in service_names:
        for port in ports:
            try:
                url = service_url(service_name, port)
            except Exception:
                continue
            if isinstance(url, str) and url:
                candidates.append(url.rstrip("/"))
                break
    return candidates


def _available_service_names(value: object) -> tuple[str, ...] | None:
    try:
        statuses = getattr(value, "statuses")
    except Exception:
        return None
    if not isinstance(statuses, dict):
        return None
    names = tuple(name for name in statuses if isinstance(name, str) and name)
    return names or None


def _service_name_candidates(
    candidates: Sequence[str],
    *,
    available: Sequence[str] | None,
) -> tuple[str, ...]:
    if available is None:
        return tuple(candidates)
    available_set = set(available)
    matched = tuple(candidate for candidate in candidates if candidate in available_set)
    return matched


def _dedupe_strings(values: Sequence[str]) -> tuple[str, ...]:
    deduped: list[str] = []
    for value in values:
        stripped = value.strip().rstrip("/")
        if stripped and stripped not in deduped:
            deduped.append(stripped)
    return tuple(deduped)


def discover(
    options: DiscoverOptions,
    *,
    action_provider: ActionProvider | None = None,
) -> DiscoverResult:
    normalized = _normalize_options(options)

    model = _resolve_discover_model(normalized.model)
    provider = action_provider or ModelActionProvider(model=model)
    ensure_browser_installed(normalized.browser)

    roots: list[DiscoveredNode] = []
    omitted_actions = 0
    omitted_by_reason: dict[str, int] = {}
    stop_reasons: set[str] = set()
    model_cache: dict[str, list[CandidateAction]] = {}
    budget = _CrawlBudget(max_model_calls=normalized.max_model_calls)
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, normalized.browser)
        browser = browser_type.launch(
            headless=normalized.headless,
            handle_sigint=False,
        )
        try:
            for index, start_state in enumerate(_start_states_for_options(normalized), start=1):
                _LOGGER.info(
                    "discover_start_url",
                    "journey discover crawling start URL",
                    pretty=pretty_row(
                        "Discover",
                        f"start {index}: {start_state.url}",
                        indent=8,
                        label_width=27,
                    ),
                    index=index,
                    url=start_state.url,
                )
                root, omitted, start_omitted_by_reason = _discover_start_url(
                    browser,
                    start_state=start_state,
                    options=normalized,
                    provider=provider,
                    model_cache=model_cache,
                    budget=budget,
                    node_prefix=f"start_{index}_",
                )
                roots.append(root)
                omitted_actions += omitted
                _merge_counts(omitted_by_reason, start_omitted_by_reason)
                stop_reasons.update(root.stop_reasons)
        finally:
            browser.close()

    mode: Literal["url", "step"] = "step" if normalized.start_page_state is not None else "url"
    extension_name = (
        f"discover_after_{_sanitize_identifier(normalized.anchor_step or 'anchor', default='anchor')}"
        if mode == "step"
        else None
    )
    if mode == "step":
        source = render_extension_source(
            tuple(roots),
            anchor_step=normalized.anchor_step or "anchor",
            extension_name=extension_name or "discover_after_anchor",
        )
        validate_generated_extension_source(
            source,
            extension_name=extension_name or "discover_after_anchor",
        )
    else:
        source = render_journey_source(
            tuple(roots),
            journey_name=normalized.journey_name,
        )
        validate_generated_source(source, journey_name=normalized.journey_name)

    actions = sum(_iter_edge_count(root) for root in roots)
    branches = sum(1 for node in _iter_nodes(roots) if len(node.edges) > 1)
    result = DiscoverResult(
        mode=mode,
        journey_name=normalized.journey_name,
        extension_name=extension_name,
        roots=tuple(roots),
        source=source,
        actions=actions,
        branches=branches,
        omitted_actions=omitted_actions,
        model_calls=budget.model_calls,
        model=model,
        stop_reason=", ".join(sorted(stop_reasons)) or "frontier_exhausted",
    )
    _LOGGER.info(
        "discover_success",
        "journey discover generated Python source",
        pretty=pretty_row(
            "Discover",
            f"generated {mode} source actions={actions} branches={branches}",
            indent=8,
            label_width=27,
            style="success",
        ),
        mode=mode,
        journey_name=result.journey_name,
        extension_name=result.extension_name,
        actions=actions,
        branches=branches,
        omitted_actions=omitted_actions,
        model_calls=budget.model_calls,
        model=model,
        stop_reason=result.stop_reason,
    )
    _emit_discover_omission_summary(omitted_by_reason)
    return result


def parse_candidate_actions(
    text: str,
    *,
    strict: bool = True,
    forbidden_literals: Sequence[str] = (),
) -> list[CandidateAction]:
    payload = _extract_json_payload(text)
    raw_actions = payload.get("actions") if isinstance(payload, dict) else payload
    if not isinstance(raw_actions, list):
        raise RuntimeError("journey discover expected model JSON with an actions list.")

    actions: list[CandidateAction] = []
    for index, item in enumerate(raw_actions, start=1):
        if not isinstance(item, dict):
            continue
        code = _strip_code_fences(str(item.get("code") or "")).strip()
        if not code:
            continue
        name = _clean_action_text(str(item.get("name") or f"action_{index}"))
        description = _clean_action_text(str(item.get("description") or name))
        forbidden = next((literal for literal in forbidden_literals if literal in code), None)
        if forbidden is not None:
            if strict:
                raise RuntimeError(
                    "journey discover model snippet embeds a stable identifier from the observed page."
                )
            _LOGGER.warning(
                "discover_action_omitted",
                "journey discover omitted a model action with a hard-coded page identifier",
                action=name,
                reason="hard_coded_identifier",
            )
            continue
        try:
            _validate_discover_python_code(code)
        except RuntimeError as exc:
            if strict:
                raise
            _LOGGER.warning(
                "discover_action_omitted",
                "journey discover omitted an invalid model action",
                action=name,
                reason="invalid_model_snippet",
                error=_format_exception(exc),
            )
            continue
        actions.append(CandidateAction(name=name, description=description, code=code))
    return actions


def render_journey_source(
    roots: tuple[DiscoveredNode, ...],
    *,
    journey_name: str,
) -> str:
    if not roots:
        raise ValueError("render_journey_source(...) needs at least one discovered root.")
    allocator = _NameAllocator()
    journey_name = _sanitize_identifier(journey_name, default="discovered_journey")
    root_functions = {
        root.node_id: allocator.allocate(f"open_{_state_slug(root.snapshot)}")
        for root in roots
    }
    for edge in _iter_edges(roots):
        edge.function_name = allocator.allocate(edge.action.name)

    lines: list[str] = [
        '"""Generated by `journey discover`. Review before committing."""',
        "",
        "from __future__ import annotations",
        "",
        "import json",
        "import re",
        "import time",
        "from dataclasses import fields, is_dataclass",
        "from urllib.parse import quote, urlsplit",
        "from uuid import uuid4",
        "",
        "from journeysdk import branch, journey, step",
        "from journeysdk.touchpoints.browser import JourneyBrowserPage, browser_page_from_step_result, open_page",
        "from journeysdk.touchpoints.http import http_request",
        "from journeysdk.touchpoints.webhook import CloudWebhookEndpoint, wait_for_webhook_request",
        "",
        "",
        "def _unique_email(prefix: str) -> str:",
        "    return f\"{prefix}-{uuid4().hex[:8]}@example.test\"",
        "",
        "",
        "def _json_contains_value(payload: object, expected: object) -> bool:",
        "    expected_text = str(expected)",
        "    if isinstance(payload, dict):",
        "        return any(_json_contains_value(value, expected_text) for value in payload.values())",
        "    if isinstance(payload, (list, tuple)):",
        "        return any(_json_contains_value(value, expected_text) for value in payload)",
        "    if payload is None:",
        "        return False",
        "    return str(payload) == expected_text or expected_text in str(payload)",
        "",
        "",
        "def _dedupe_expected_values(values: list[object]) -> list[str]:",
        "    deduped: list[str] = []",
        "    for value in values:",
        "        if value is None:",
        "            continue",
        "        text = str(value).strip()",
        "        if text and text not in deduped:",
        "            deduped.append(text)",
        "    return deduped",
        "",
        "",
        "def _http_json(url: str, *, timeout: float = 10.0) -> object | None:",
        "    try:",
        "        response = http_request(url, timeout=timeout)",
        "    except Exception:",
        "        return None",
        "    if response.status < 200 or response.status > 299:",
        "        return None",
        "    try:",
        "        return response.json()",
        "    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):",
        "        return None",
        "",
        "",
        "def _missing_expected_values(payload: object, expected_values: list[str]) -> list[str]:",
        "    return [value for value in expected_values if not _json_contains_value(payload, value)]",
        "",
        "",
        "def _wait_for_http_json(",
        "    url: str,",
        "    expected_values: list[str],",
        "    *,",
        "    label: str,",
        "    timeout_seconds: float = 45.0,",
        "    poll_interval: float = 0.5,",
        ") -> object:",
        "    deadline = time.monotonic() + timeout_seconds",
        "    last_payload: object | None = None",
        "    while True:",
        "        payload = _http_json(url, timeout=min(10.0, max(0.1, poll_interval)))",
        "        if payload is not None:",
        "            last_payload = payload",
        "            missing = _missing_expected_values(payload, expected_values)",
        "            if not missing:",
        "                return payload",
        "        if time.monotonic() >= deadline:",
        "            raise AssertionError(",
        "                f\"Timed out waiting for {label} at {url!r} to contain {expected_values!r}; \"",
        "                f\"last payload: {last_payload!r}.\"",
        "            )",
        "        time.sleep(poll_interval)",
        "",
        "",
        "def _mail_messages(payload: object) -> list[dict[str, object]]:",
        "    if isinstance(payload, dict):",
        "        for key in (\"messages\", \"Messages\", \"items\", \"data\"):",
        "            value = payload.get(key)",
        "            if isinstance(value, list):",
        "                return [item for item in value if isinstance(item, dict)]",
        "    if isinstance(payload, list):",
        "        return [item for item in payload if isinstance(item, dict)]",
        "    return []",
        "",
        "",
        "def _mail_message_id(message: dict[str, object]) -> str | None:",
        "    for key in (\"ID\", \"Id\", \"id\", \"message_id\", \"MessageID\"):",
        "        value = message.get(key)",
        "        if value:",
        "            return str(value)",
        "    return None",
        "",
        "",
        "def _wait_for_email_evidence(",
        "    base_url: str,",
        "    identifier: str,",
        "    expected_values: list[str],",
        "    *,",
        "    timeout_seconds: float = 45.0,",
        "    poll_interval: float = 0.5,",
        ") -> object:",
        "    deadline = time.monotonic() + timeout_seconds",
        "    messages_url = f\"{base_url.rstrip('/')}/api/v1/messages\"",
        "    last_payload: object | None = None",
        "    while True:",
        "        payload = _http_json(messages_url, timeout=min(10.0, max(0.1, poll_interval)))",
        "        if payload is not None:",
        "            last_payload = payload",
        "            for message in _mail_messages(payload):",
        "                detail: object = message",
        "                message_id = _mail_message_id(message)",
        "                if message_id:",
        "                    detail_url = f\"{base_url.rstrip('/')}/api/v1/message/{quote(message_id)}\"",
        "                    detail = _http_json(detail_url, timeout=10.0) or message",
        "                evidence = {\"message\": message, \"detail\": detail}",
        "                if not _json_contains_value(evidence, identifier):",
        "                    continue",
        "                missing = _missing_expected_values(evidence, expected_values)",
        "                if not missing:",
        "                    return evidence",
        "        if time.monotonic() >= deadline:",
        "            raise AssertionError(",
        "                f\"Timed out waiting for email evidence containing {expected_values!r}; \"",
        "                f\"last payload: {last_payload!r}.\"",
        "            )",
        "        time.sleep(poll_interval)",
        "",
        "",
        "def _wait_for_webhook_evidence(",
        "    base_url: str,",
        "    identifier: str,",
        "    expected_values: list[str],",
        "    *,",
        "    timeout_seconds: float = 45.0,",
        "    poll_interval: float = 0.5,",
        ") -> object:",
        "    url = f\"{base_url.rstrip('/')}/requests/latest?confirmation_code={quote(identifier)}\"",
        "    return _wait_for_http_json(",
        "        url,",
        "        expected_values,",
        "        label=\"webhook evidence\",",
        "        timeout_seconds=timeout_seconds,",
        "        poll_interval=poll_interval,",
        "    )",
        "",
        "",
        "def _anchor_values(value: object, *, limit: int = 200) -> list[object]:",
        "    values: list[object] = []",
        "    seen: set[int] = set()",
        "",
        "    def visit(item: object) -> None:",
        "        if len(values) >= limit:",
        "            return",
        "        if item is None or isinstance(item, (str, bytes, int, float, bool)):",
        "            return",
        "        identity = id(item)",
        "        if identity in seen:",
        "            return",
        "        seen.add(identity)",
        "        values.append(item)",
        "        if isinstance(item, dict):",
        "            for child in item.values():",
        "                visit(child)",
        "            return",
        "        if isinstance(item, (list, tuple, set, frozenset)):",
        "            for child in item:",
        "                visit(child)",
        "            return",
        "        if is_dataclass(item) and not isinstance(item, type):",
        "            for field_info in fields(item):",
        "                try:",
        "                    visit(getattr(item, field_info.name))",
        "                except Exception:",
        "                    continue",
        "            return",
        "        item_dict = getattr(item, \"__dict__\", None)",
        "        if isinstance(item_dict, dict):",
        "            for child in item_dict.values():",
        "                visit(child)",
        "",
        "    visit(value)",
        "    return values",
        "",
        "",
        "def _cloud_webhook_endpoints_from_anchor(anchor_result: object) -> list[CloudWebhookEndpoint]:",
        "    return [",
        "        value",
        "        for value in _anchor_values(anchor_result)",
        "        if isinstance(value, CloudWebhookEndpoint)",
        "    ]",
        "",
        "",
        "def _wait_for_cloud_webhook_evidence(",
        "    anchor_result: object,",
        "    identifier: str,",
        "    expected_values: list[str],",
        "    *,",
        "    timeout_seconds: float = 45.0,",
        "    poll_interval: float = 0.5,",
        ") -> object:",
        "    endpoints = _cloud_webhook_endpoints_from_anchor(anchor_result)",
        "    if not endpoints:",
        "        raise AssertionError(\"Expected anchor result to contain a CloudWebhookEndpoint.\")",
        "    deadline = time.monotonic() + timeout_seconds",
        "    last_payload: object | None = None",
        "    while True:",
        "        for endpoint in endpoints:",
        "            try:",
        "                payload = wait_for_webhook_request(",
        "                    endpoint,",
        "                    timeout=0,",
        "                    poll_interval=poll_interval,",
        "                )",
        "            except TimeoutError:",
        "                payload = None",
        "            if payload is None:",
        "                continue",
        "            last_payload = payload",
        "            if not _json_contains_value(payload, identifier):",
        "                continue",
        "            missing = _missing_expected_values(payload, expected_values)",
        "            if not missing:",
        "                return payload",
        "        if time.monotonic() >= deadline:",
        "            raise AssertionError(",
        "                f\"Timed out waiting for Journey Cloud webhook evidence containing {expected_values!r}; \"",
        "                f\"last payload: {last_payload!r}.\"",
        "            )",
        "        time.sleep(poll_interval)",
        "",
        "",
        "def _first_visible_identifier(page: JourneyBrowserPage, *, timeout_ms: int) -> str:",
        "    body_text = page.locator(\"body\").inner_text(timeout=timeout_ms)",
        "    patterns = (",
        "        (r\"\\b[A-Z]{2,}-[A-Z0-9][A-Z0-9-]{3,}\\b\", 0),",
        "        (r\"\\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\b\", re.IGNORECASE),",
        "        (r\"\\b[A-Z0-9]{8,}\\b\", 0),",
        "        (r\"\\b[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}\\b\", re.IGNORECASE),",
        "    )",
        "    for pattern, flags in patterns:",
        "        match = re.search(pattern, body_text, flags=flags)",
        "        if match:",
        "            return match.group(0)",
        "    raise AssertionError(\"Expected a stable visible identifier after the discovered transition.\")",
        "",
        "",
        "def _cookie_consent_visible(page: JourneyBrowserPage) -> bool:",
        "    try:",
        "        return bool(",
        "            page.evaluate(",
        "                r\"\"\"",
        "                () => {",
        "                  function isVisible(element) {",
        "                    if (!element || !element.isConnected) return false;",
        "                    const style = window.getComputedStyle(element);",
        "                    if (style.visibility === \"hidden\" || style.display === \"none\") return false;",
        "                    if (Number(style.opacity) === 0) return false;",
        "                    return element.getClientRects().length > 0;",
        "                  }",
        "                  const pattern = /(cookie|cookies|consent|privacy|tracking)/i;",
        "                  const candidates = Array.from(document.querySelectorAll(\"[role='dialog'], [aria-modal='true'], [id], [class], [data-testid], [aria-label]\"));",
        "                  return candidates.some((element) => {",
        "                    if (!isVisible(element)) return false;",
        "                    const idClass = `${element.id || \"\"} ${element.className || \"\"} ${element.getAttribute(\"data-testid\") || \"\"} ${element.getAttribute(\"aria-label\") || \"\"}`;",
        "                    const modalLike = element.getAttribute(\"role\") === \"dialog\" || element.getAttribute(\"aria-modal\") === \"true\";",
        "                    const consentNamed = pattern.test(idClass);",
        "                    if (!modalLike && !consentNamed) return false;",
        "                    const text = element.innerText || element.textContent || \"\";",
        "                    return consentNamed || pattern.test(text);",
        "                  });",
        "                }",
        "                \"\"\"",
        "            )",
        "        )",
        "    except Exception:",
        "        return False",
        "",
        "",
        "def _click_first_visible_control(locator: object) -> bool:",
        "    try:",
        "        count = min(int(locator.count()), 5)  # type: ignore[attr-defined]",
        "    except Exception:",
        "        count = 1",
        "    for index in range(count):",
        "        try:",
        "            control = locator.nth(index) if hasattr(locator, \"nth\") else locator",
        "            if hasattr(control, \"is_visible\") and not control.is_visible(timeout=250):",
        "                continue",
        "            control.click(timeout=1000)",
        "            return True",
        "        except Exception:",
        "            continue",
        "    return False",
        "",
        "",
        "def _dismiss_cookie_consent(page: JourneyBrowserPage) -> bool:",
        "    if not _cookie_consent_visible(page):",
        "        return False",
        "    patterns = (",
        "        r\"^(accept all|allow all|accept|agree|i accept|ok)(?: cookies?)?$\",",
        "        r\"^(reject all|decline all|decline|deny all|no thanks)(?: cookies?)?$\",",
        "        r\"^(close|dismiss)(?: cookies?)?$\",",
        "    )",
        "    for pattern in patterns:",
        "        if _click_first_visible_control(page.get_by_role(\"button\", name=re.compile(pattern, re.IGNORECASE))):",
        "            return True",
        "    return False",
        "",
        "",
        "def _settle_replay_page(page: JourneyBrowserPage, *, timeout_ms: int) -> None:",
        "    deadline = time.monotonic() + max(timeout_ms, 1) / 1000",
        "    for _ in range(2):",
        "        for state, cap_ms in ((\"load\", 5000), (\"networkidle\", 1500)):",
        "            remaining_ms = int((deadline - time.monotonic()) * 1000)",
        "            if remaining_ms <= 0:",
        "                return",
        "            try:",
        "                page.wait_for_load_state(state, timeout=max(1, min(cap_ms, remaining_ms)))",
        "            except Exception:",
        "                pass",
        "        _dismiss_cookie_consent(page)",
        "    try:",
        "        page.wait_for_timeout(250)",
        "    except Exception:",
        "        pass",
        "",
        "",
        "def _assert_page_state(",
        "    page: JourneyBrowserPage,",
        "    *,",
        "    expected_path: str | None = None,",
        "    expected_title: str | None = None,",
        "    expected_text: str | None = None,",
        f"    timeout_ms: int = {int(_GENERATED_REPLAY_TIMEOUT_SECONDS * 1000)},",
        ") -> None:",
        "    deadline = time.monotonic() + max(timeout_ms, 1) / 1000",
        "    last_path = None",
        "    last_title = None",
        "    last_text = \"\"",
        "    while True:",
        "        remaining_ms = int((deadline - time.monotonic()) * 1000)",
        "        if remaining_ms <= 0:",
        "            break",
        "        _settle_replay_page(page, timeout_ms=max(1, min(remaining_ms, 3000)))",
        "        ok = True",
        "        if expected_path is not None:",
        "            last_path = urlsplit(page.url).path or \"/\"",
        "            ok = ok and last_path == expected_path",
        "        if expected_title is not None:",
        "            try:",
        "                last_title = page.title()",
        "            except Exception:",
        "                last_title = None",
        "            ok = ok and last_title == expected_title",
        "        if expected_text is not None:",
        "            try:",
        "                text_timeout = max(1, min(1000, int((deadline - time.monotonic()) * 1000)))",
        "                last_text = page.locator(\"body\").inner_text(timeout=text_timeout)",
        "            except Exception:",
        "                last_text = \"\"",
        "            ok = ok and expected_text in last_text",
        "        if ok:",
        "            return",
        "        time.sleep(0.25)",
        "    raise AssertionError(",
        "        \"Expected page state was not reached: \"",
        "        f\"path={expected_path!r} actual={last_path!r}; \"",
        "        f\"title={expected_title!r} actual={last_title!r}; \"",
        "        f\"text={expected_text!r} visible={last_text[:500]!r}.\"",
        "    )",
        "",
        "",
    ]

    for root in roots:
        lines.extend(
            _render_root_function(
                root,
                function_name=root_functions[root.node_id],
            )
        )
        lines.append("")
        lines.append("")

    for edge in _iter_edges(roots):
        lines.extend(_render_edge_function(edge))
        lines.append("")
        lines.append("")

    lines.extend(_render_journey_function(roots, root_functions, journey_name=journey_name))
    return "\n".join(lines).rstrip() + "\n"


def render_extension_source(
    roots: tuple[DiscoveredNode, ...],
    *,
    anchor_step: str,
    extension_name: str,
) -> str:
    if not roots:
        raise ValueError("render_extension_source(...) needs at least one discovered root.")
    extension_name = _sanitize_identifier(extension_name, default="discover_after_anchor")
    full_source = render_journey_source(
        roots,
        journey_name="_journey_discover_unused",
    )
    root_start = full_source.find("\ndef open_")
    journey_start = full_source.rfind("\n@journey")
    if root_start < 0 or journey_start < 0 or journey_start <= root_start:
        raise RuntimeError("journey discover could not render a valid extension snippet.")

    first_edge = next(iter(_iter_edges(roots)), None)
    if first_edge is None:
        prelude = full_source[: root_start + 1].rstrip()
        edge_definitions = ""
    else:
        edge_start = full_source.find(f"\ndef {first_edge.function_name}(", root_start)
        if edge_start < 0 or edge_start >= journey_start:
            raise RuntimeError("journey discover could not find generated transition steps.")
        prelude = full_source[: root_start + 1].rstrip()
        edge_definitions = full_source[edge_start + 1 : journey_start].rstrip()

    sanitized_step = _sanitize_identifier(anchor_step, default="anchor")
    anchor_var = _var_name(sanitized_step)
    lines = [
        prelude,
        "",
    ]
    if edge_definitions:
        lines.extend([edge_definitions, ""])
    lines.extend(
        [
            f"# Paste near the existing anchor step:",
            f"# {anchor_var} = step({sanitized_step})",
            f"# {extension_name}({anchor_var})",
            "",
            f"def _recover_{extension_name}_page(anchor_result: object) -> JourneyBrowserPage:",
            "    if isinstance(anchor_result, JourneyBrowserPage):",
            "        return anchor_result",
            "    return browser_page_from_step_result(anchor_result)",
            "",
            f"def {extension_name}(anchor_result: object) -> None:",
            f"    \"\"\"Generated by `journey discover` after step {anchor_step!r}.\"\"\"",
        ]
    )
    if len(roots) == 1:
        lines.append(f"    anchor_page = step(_recover_{extension_name}_page, anchor_result)")
        body = _render_node_body(
            roots[0],
            current_var="anchor_page",
            indent="    ",
            replay_var="anchor_result",
            anchor_var="anchor_result",
        )
        lines.extend(body or ["    pass"])
    else:
        for index, root in enumerate(roots):
            prefix = "if" if index == 0 else "elif"
            lines.append(f"    {prefix} branch(replay_from=anchor_result):")
            lines.append(f"        anchor_page = step(_recover_{extension_name}_page, anchor_result)")
            body = _render_node_body(
                root,
                current_var="anchor_page",
                indent="        ",
                anchor_var="anchor_result",
            )
            lines.extend(body or ["        pass"])
    return "\n".join(lines).rstrip() + "\n"


def validate_generated_source(source: str, *, journey_name: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError(f"journey discover generated invalid Python: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="journey-discover-") as temp_dir:
        path = Path(temp_dir) / "generated_journey.py"
        path.write_text(source, encoding="utf-8")
        module = _import_generated_module(path)
        journey_fn = getattr(module, journey_name, None)
        if not is_journey_callable(journey_fn):
            raise RuntimeError(
                f"journey discover generated source without {journey_name!r} Journey entrypoint."
            )
        compile_journey(journey_fn)


def validate_generated_extension_source(source: str, *, extension_name: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError(f"journey discover generated invalid Python: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="journey-discover-") as temp_dir:
        path = Path(temp_dir) / "generated_extension.py"
        path.write_text(source, encoding="utf-8")
        module = _import_generated_module(path)
        extension_fn = getattr(module, extension_name, None)
        if not callable(extension_fn):
            raise RuntimeError(
                f"journey discover generated source without {extension_name!r} extension function."
            )

        from journeysdk import journey, step

        def _dummy_anchor() -> None:
            return None

        @journey
        def _journey_discover_validation() -> None:
            anchor_result = step(_dummy_anchor)
            extension_fn(anchor_result)

        compile_journey(_journey_discover_validation)


def merge_extension_source_with_model(
    *,
    original_source: str,
    generated_extension_source: str,
    journey_name: str,
    anchor_step: str,
    model: str | None = None,
) -> SourceMergeResult:
    resolved_model = _resolve_discover_model(model)
    model_obj = _load_langchain_model(resolved_model, max_tokens=32000)
    prompt = _merge_extension_prompt(
        original_source=original_source,
        generated_extension_source=generated_extension_source,
        journey_name=journey_name,
        anchor_step=anchor_step,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You merge Journey SDK Python source files. Return complete, valid Python "
                "source only. Do not include markdown, explanations, or code fences."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    response = _PromptUsageTracker().call(
        operation="discover_merge_source",
        configured_model=resolved_model,
        logger=_LOGGER,
        callback=lambda config: model_obj.invoke(messages, config=config),
    )
    merged_source = _strip_code_fences(
        _extract_langchain_text(response, owner="journey discover merge")
    ).strip()
    if not merged_source:
        raise RuntimeError("journey discover merge returned empty source.")
    try:
        ast.parse(merged_source)
    except SyntaxError as exc:
        raise RuntimeError(f"journey discover merge returned invalid Python: {exc}") from exc
    return SourceMergeResult(source=merged_source.rstrip() + "\n", model=resolved_model)


def _merge_extension_prompt(
    *,
    original_source: str,
    generated_extension_source: str,
    journey_name: str,
    anchor_step: str,
) -> str:
    return textwrap.dedent(
        f"""
        Merge generated Journey discovery code into an existing Journey file.

        Requirements:
        - Return one complete Python source file and nothing else.
        - Preserve unrelated existing code, comments, imports, fixtures, and Journey functions.
        - Target Journey function: {journey_name!r}.
        - Anchor step label/function: {anchor_step!r}.
        - Add the generated helper and step functions from the extension source.
        - In the target Journey, call the generated extension immediately after the existing anchor step result is assigned.
        - Preserve existing branch structure and only add the discovered coverage below the anchor step.
        - Ensure the merged source can be imported, compiled by Journey SDK, and executed.

        Existing source:
        ```python
        {original_source}
        ```

        Generated extension source:
        ```python
        {generated_extension_source}
        ```
        """
    ).strip()


def _discover_start_url(
    browser: object,
    *,
    start_state: BrowserStartState,
    options: DiscoverOptions,
    provider: ActionProvider,
    model_cache: dict[str, list[CandidateAction]],
    budget: _CrawlBudget,
    node_prefix: str,
) -> tuple[DiscoveredNode, int, dict[str, int]]:
    root_snapshot = _snapshot_for_path(browser, start_state, ())
    root = DiscoveredNode(
        node_id=f"{node_prefix}node_1",
        start_url=start_state.url,
        snapshot=root_snapshot,
        depth=0,
    )
    queue: deque[DiscoveredNode] = deque([root])
    seen_signatures = {root_snapshot.signature}
    node_sequence = 1
    edge_sequence = 0
    action_count = 0
    omitted_actions = 0
    omitted_by_reason: dict[str, int] = {}
    allowed_origins = _allowed_origins(options)

    while queue and action_count < options.max_actions:
        node = queue.popleft()
        if node.depth >= options.depth:
            node.stop_reasons.add("max_depth")
            continue
        remaining = options.max_actions - action_count
        if node.prefetched_candidates is not None:
            deterministic_candidates = list(node.prefetched_candidates)
        else:
            deterministic_candidates = _deterministic_candidates_for_path(
                browser,
                start_state=start_state,
                path=node.path,
                max_variants_per_control=options.max_variants_per_control,
                timeout_seconds=options.action_timeout_seconds,
            )
        candidates = _dedupe_candidates(deterministic_candidates)
        if not candidates:
            cached = model_cache.get(node.snapshot.signature)
            if cached is None and budget.model_calls < budget.max_model_calls:
                _LOGGER.info(
                    "discover_model_fallback_start",
                    "journey discover asking model for candidate actions",
                    pretty=pretty_row(
                        "Discover",
                        (
                            f"model fallback {budget.model_calls + 1}/{budget.max_model_calls} "
                            f"depth={node.depth} url={node.snapshot.url}"
                        ),
                        indent=8,
                        label_width=27,
                    ),
                    url=node.snapshot.url,
                    depth=node.depth,
                    remaining_actions=remaining,
                    depth_remaining=options.depth - node.depth,
                    model_call=budget.model_calls + 1,
                    max_model_calls=budget.max_model_calls,
                )
                budget.model_calls += 1
                cached = provider.propose_actions(
                    node.snapshot,
                    path=node.path,
                    remaining_actions=remaining,
                    depth_remaining=options.depth - node.depth,
                )
                model_cache[node.snapshot.signature] = cached
            elif cached is None:
                node.stop_reasons.add("max_model_calls")
                cached = []
            candidates = _dedupe_candidates(cached)
        if not candidates:
            node.stop_reasons.add("frontier_exhausted")
            continue

        successful_transition = False
        for candidate in candidates:
            if action_count >= options.max_actions:
                node.stop_reasons.add("max_actions")
                break
            try:
                action_result = _snapshot_after_action(
                    browser,
                    start_state=start_state,
                    path=node.path,
                    action=candidate,
                    side_effect_probes=options.side_effect_probes,
                    email_evidence_urls=options.email_evidence_urls,
                    webhook_evidence_urls=options.webhook_evidence_urls,
                    cloud_webhook_endpoints=options.cloud_webhook_endpoints,
                    max_variants_per_control=options.max_variants_per_control,
                    timeout_seconds=options.action_timeout_seconds,
                )
                snapshot = action_result.snapshot
            except Exception as exc:
                omitted_actions += 1
                reason = _omission_reason_for_exception(exc)
                _increment_count(omitted_by_reason, reason)
                error = _short_exception(exc)
                _LOGGER.warning(
                    "discover_action_omitted",
                    "journey discover omitted a failed action",
                    pretty=pretty_row(
                        "Discover",
                        f"omitted {candidate.name}: {reason} ({error})",
                        indent=8,
                        label_width=27,
                        style="warning",
                    ),
                    action=candidate.name,
                    reason=reason,
                    error=error,
                )
                continue
            if not options.allow_external and _origin(snapshot.url) not in allowed_origins:
                omitted_actions += 1
                _increment_count(omitted_by_reason, "external_navigation")
                _LOGGER.warning(
                    "discover_external_omitted",
                    "journey discover omitted an external navigation",
                    pretty=pretty_row(
                        "Discover",
                        f"omitted external {snapshot.url}",
                        indent=8,
                        label_width=27,
                        style="warning",
                    ),
                    action=candidate.name,
                    url=snapshot.url,
                )
                continue
            if snapshot.signature == node.snapshot.signature:
                omitted_actions += 1
                _increment_count(omitted_by_reason, "unchanged_state")
                _LOGGER.debug(
                    "discover_action_omitted",
                    "journey discover omitted a non-transition action",
                    action=candidate.name,
                    reason="state_unchanged",
                )
                continue

            edge_sequence += 1
            action_count += 1
            successful_transition = True
            _LOGGER.info(
                "discover_transition_found",
                "journey discover found a transition",
                pretty=pretty_row(
                    "Discover",
                    (
                        f"transition {action_count}/{options.max_actions}: "
                        f"{candidate.name} -> {snapshot.url}"
                    ),
                    indent=8,
                    label_width=27,
                    style="success",
                ),
                action=candidate.name,
                from_url=node.snapshot.url,
                to_url=snapshot.url,
                depth=node.depth,
                actions=action_count,
                max_actions=options.max_actions,
            )
            edge = DiscoveredEdge(
                edge_id=f"{node_prefix}edge_{edge_sequence}",
                parent=node,
                action=candidate,
                snapshot=snapshot,
                probes=action_result.probes,
                visible_assertions=_visible_capture_assertions(
                    (*node.path,),
                    candidate,
                    snapshot,
                ),
            )
            node.edges.append(edge)

            if snapshot.signature not in seen_signatures:
                seen_signatures.add(snapshot.signature)
                node_sequence += 1
                child = DiscoveredNode(
                    node_id=f"{node_prefix}node_{node_sequence}",
                    start_url=node.start_url,
                    snapshot=snapshot,
                    depth=node.depth + 1,
                    path=(*node.path, edge),
                    prefetched_candidates=action_result.prefetched_candidates,
                )
                edge.child = child
                queue.append(child)
        if not successful_transition:
            node.stop_reasons.add("frontier_exhausted")
    if action_count >= options.max_actions:
        root.stop_reasons.add("max_actions")
    for discovered_node in _iter_nodes((root,)):
        root.stop_reasons.update(discovered_node.stop_reasons)
    if not root.stop_reasons:
        root.stop_reasons.add("frontier_exhausted")
    return root, omitted_actions, omitted_by_reason


def _snapshot_for_path(
    browser: object,
    start_state: BrowserStartState,
    path: tuple[DiscoveredEdge, ...],
) -> PageSnapshot:
    context = browser.new_context()
    try:
        active_page = _new_page_for_start_state(
            context,
            start_state,
            timeout_seconds=30.0,
        )
        for edge in path:
            active_page = _execute_discover_code(
                active_page,
                edge.action.code,
                timeout_seconds=30.0,
            )
            _settle_page(active_page)
        return _snapshot_page(active_page)
    finally:
        context.close()


def _snapshot_after_action(
    browser: object,
    *,
    start_state: BrowserStartState,
    path: tuple[DiscoveredEdge, ...],
    action: CandidateAction,
    side_effect_probes: Literal["auto", "off"],
    email_evidence_urls: tuple[str, ...],
    webhook_evidence_urls: tuple[str, ...],
    cloud_webhook_endpoints: tuple[object, ...],
    max_variants_per_control: int,
    timeout_seconds: float,
) -> _ActionResult:
    context = browser.new_context()
    try:
        active_page = _new_page_for_start_state(
            context,
            start_state,
            timeout_seconds=timeout_seconds,
        )
        for edge in path:
            _dismiss_cookie_consent(active_page)
            active_page = _execute_discover_code(
                active_page,
                edge.action.code,
                timeout_seconds=timeout_seconds,
            )
            _settle_page(active_page)
        _dismiss_cookie_consent(active_page)
        active_page = _execute_discover_code(
            active_page,
            action.code,
            timeout_seconds=timeout_seconds,
        )
        _settle_page(active_page)
        snapshot = _snapshot_page(active_page)
        probes = ()
        if side_effect_probes == "auto":
            probes = tuple(
                _discover_probes_for_page(
                    active_page,
                    action=action,
                    email_evidence_urls=email_evidence_urls,
                    webhook_evidence_urls=webhook_evidence_urls,
                    cloud_webhook_endpoints=cloud_webhook_endpoints,
                )
            )
        return _ActionResult(
            snapshot=snapshot,
            prefetched_candidates=tuple(
                _deterministic_candidates_for_page(
                    active_page,
                    max_variants_per_control=max_variants_per_control,
                )
            ),
            probes=probes,
        )
    finally:
        context.close()


def _deterministic_candidates_for_path(
    browser: object,
    *,
    start_state: BrowserStartState,
    path: tuple[DiscoveredEdge, ...],
    max_variants_per_control: int,
    timeout_seconds: float,
) -> list[CandidateAction]:
    context = browser.new_context()
    try:
        active_page = _new_page_for_start_state(
            context,
            start_state,
            timeout_seconds=timeout_seconds,
        )
        for edge in path:
            _dismiss_cookie_consent(active_page)
            active_page = _execute_discover_code(
                active_page,
                edge.action.code,
                timeout_seconds=timeout_seconds,
            )
            _settle_page(active_page)
        _dismiss_cookie_consent(active_page)
        return _deterministic_candidates_for_page(
            active_page,
            max_variants_per_control=max_variants_per_control,
        )
    finally:
        context.close()


def _new_page_for_start_state(
    context: object,
    start_state: BrowserStartState,
    *,
    timeout_seconds: float,
) -> PlaywrightPage:
    if start_state.cookies:
        context.add_cookies([dict(cookie) for cookie in start_state.cookies])
    page = context.new_page()
    timeout_ms = int(timeout_seconds * 1000)
    page.goto(start_state.url, wait_until="load", timeout=timeout_ms)
    local_storage = start_state.local_storage_dict()
    if local_storage:
        page.evaluate(
            """
            (storage) => {
              for (const [key, value] of Object.entries(storage)) {
                window.localStorage.setItem(key, value);
              }
            }
            """,
            local_storage,
        )
        page.reload(wait_until="load", timeout=timeout_ms)
    _settle_page(page)
    _dismiss_cookie_consent(page)
    return cast(PlaywrightPage, page)


def _dismiss_cookie_consent(page: PlaywrightPage) -> bool:
    if not _cookie_consent_visible(page):
        return False

    patterns = (
        r"^(accept all|allow all|accept|agree|i accept|ok)(?: cookies?)?$",
        r"^(reject all|decline all|decline|deny all|no thanks)(?: cookies?)?$",
        r"^(close|dismiss)(?: cookies?)?$",
    )
    for pattern in patterns:
        matcher = re.compile(pattern, re.IGNORECASE)
        if _click_first_visible_control(page.get_by_role("button", name=matcher)):
            _LOGGER.info(
                "discover_cookie_consent_dismissed",
                "journey discover dismissed a cookie consent overlay",
                pretty=pretty_row(
                    "Discover",
                    "dismissed cookie consent overlay",
                    indent=8,
                    label_width=27,
                ),
            )
            _settle_page(page)
            return True
    return False


def _cookie_consent_visible(page: PlaywrightPage) -> bool:
    try:
        return bool(
            page.evaluate(
                r"""
                () => {
                  function isVisible(element) {
                    if (!element || !element.isConnected) return false;
                    const style = window.getComputedStyle(element);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    if (Number(style.opacity) === 0) return false;
                    return element.getClientRects().length > 0;
                  }
                  const pattern = /(cookie|cookies|consent|privacy|tracking)/i;
                  const candidates = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true'], [id], [class], [data-testid], [aria-label]"));
                  return candidates.some((element) => {
                    if (!isVisible(element)) return false;
                    const idClass = `${element.id || ""} ${element.className || ""} ${element.getAttribute("data-testid") || ""} ${element.getAttribute("aria-label") || ""}`;
                    const modalLike = element.getAttribute("role") === "dialog" || element.getAttribute("aria-modal") === "true";
                    const consentNamed = pattern.test(idClass);
                    if (!modalLike && !consentNamed) return false;
                    const text = element.innerText || element.textContent || "";
                    return consentNamed || pattern.test(text);
                  });
                }
                """
            )
        )
    except Exception:
        return False


def _click_first_visible_control(locator: object) -> bool:
    try:
        count = min(int(locator.count()), 5)  # type: ignore[attr-defined]
    except Exception:
        count = 1
    for index in range(count):
        try:
            control = locator.nth(index) if hasattr(locator, "nth") else locator
            if hasattr(control, "is_visible") and not control.is_visible(timeout=250):
                continue
            control.click(timeout=1000)
            return True
        except Exception:
            continue
    return False


def _deterministic_candidates_for_page(
    page: PlaywrightPage,
    *,
    max_variants_per_control: int = _DEFAULT_MAX_VARIANTS_PER_CONTROL,
) -> list[CandidateAction]:
    _dismiss_cookie_consent(page)
    affordances = _extract_affordances(page)
    candidates: list[CandidateAction] = []
    for form in _as_list(affordances.get("forms")):
        candidates.extend(
            _form_submit_candidates(
                form,
                max_variants_per_control=max_variants_per_control,
            )
        )
    for link in _as_list(affordances.get("links")):
        candidate = _click_candidate(link, kind="link")
        if candidate is not None:
            candidates.append(candidate)
    for button in _as_list(affordances.get("buttons")):
        candidate = _click_candidate(button, kind="button")
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _form_submit_candidate(form: dict[str, object]) -> CandidateAction | None:
    candidates = _form_submit_candidates(
        form,
        max_variants_per_control=1,
    )
    return candidates[0] if candidates else None


def _form_submit_candidates(
    form: dict[str, object],
    *,
    max_variants_per_control: int,
) -> list[CandidateAction]:
    submit = form.get("submit")
    if not isinstance(submit, dict):
        return []
    if _control_is_disabled(submit):
        return []
    submit_selector = _string_value(submit.get("selector"))
    if not submit_selector:
        return []
    finite_controls = _finite_control_variants(
        form,
        max_variants_per_control=max_variants_per_control,
    )
    if not finite_controls:
        finite_controls = ((),)
    candidates: list[CandidateAction] = []
    for control_choices in finite_controls:
        candidate = _form_submit_candidate_for_choices(
            form,
            submit=submit,
            submit_selector=submit_selector,
            control_choices=control_choices,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _form_submit_candidate_for_choices(
    form: dict[str, object],
    *,
    submit: dict[str, object],
    submit_selector: str,
    control_choices: tuple[dict[str, object], ...],
) -> CandidateAction | None:
    lines: list[str] = []
    captures: list[ActionCapture] = []
    choice_by_selector = {
        _string_value(choice.get("selector")): choice
        for choice in control_choices
        if _string_value(choice.get("selector"))
    }
    has_choice_variants = bool(control_choices)
    used_variables: set[str] = set()
    for raw_field in _as_list(form.get("fields")):
        if not isinstance(raw_field, dict):
            continue
        field = cast(dict[str, object], raw_field)
        selector = _string_value(field.get("selector"))
        if not selector or field.get("disabled") or field.get("readonly"):
            continue
        field_tag = _string_value(field.get("tag")).lower()
        field_type = _string_value(field.get("type")).lower()
        if field_tag == "select":
            choice = choice_by_selector.get(selector)
            option_value = (
                _string_value(choice.get("value"))
                if choice is not None
                else _first_select_option_value(field)
            )
            if option_value is not None:
                variable = _allocate_variable_name(
                    _capture_variable_base(field, default="selected_option"),
                    used_variables,
                )
                label = _string_value(choice.get("label")) if choice is not None else ""
                lines.append(f"{variable} = {option_value!r}")
                lines.append(
                    f"page.locator({selector!r}).select_option({variable}, timeout=timeout_ms)"
                )
                captures.append(
                    ActionCapture(
                        variable=variable,
                        value=option_value,
                        kind="option",
                        label=label or _string_value(field.get("label")) or option_value,
                    )
                )
            continue
        if field_type in {"checkbox", "radio"}:
            choice = choice_by_selector.get(selector)
            should_check = (
                choice is not None
                if has_choice_variants
                else field_type == "checkbox" and bool(field.get("required")) and not bool(field.get("checked"))
            )
            if should_check:
                lines.append(f"page.locator({selector!r}).check(timeout=timeout_ms)")
                value = (
                    _string_value(choice.get("value"))
                    if choice is not None
                    else _string_value(field.get("value")) or "on"
                )
                captures.append(
                    ActionCapture(
                        variable="",
                        value=value,
                        kind=field_type,
                        label=(
                            _string_value(choice.get("label"))
                            if choice is not None
                            else _string_value(field.get("label")) or value
                        ),
                    )
                )
            continue
        if field_type in {"hidden", "submit", "button", "image", "reset", "file"}:
            continue
        value_expr = _field_value_expression(field)
        if value_expr is None:
            continue
        variable = _allocate_variable_name(
            _capture_variable_base(field, default="field_value"),
            used_variables,
        )
        lines.append(f"{variable} = {value_expr}")
        lines.append(f"page.locator({selector!r}).fill({variable}, timeout=timeout_ms)")
        literal_value = _literal_string_expression(value_expr)
        is_evidence_field = _is_optional_evidence_text_field(field)
        captures.append(
            ActionCapture(
                variable=variable,
                value=literal_value or "",
                kind=(
                    "email"
                    if "email" in variable
                    else "evidence_field"
                    if is_evidence_field
                    else "field"
                ),
                label=_string_value(field.get("label")) or _string_value(field.get("name")) or variable,
            )
        )
    lines.append(f"page.locator({submit_selector!r}).click(timeout=timeout_ms)")
    lines.append('page.wait_for_load_state("load", timeout=timeout_ms)')
    action_name = _transition_name(
        submit,
        fallback=_string_value(form.get("action")) or "submit form",
    )
    variant_group = None
    variant_value = None
    variant_label = None
    if control_choices:
        first_choice = control_choices[0]
        variant_group = _string_value(first_choice.get("group"))
        variant_value = _string_value(first_choice.get("value"))
        variant_label = _string_value(first_choice.get("label"))
        suffix = variant_label or variant_value
        if suffix:
            action_name = f"{action_name}_{_sanitize_identifier(suffix, default='variant')}"
    description = f"Complete and submit the {action_name.replace('_', ' ')} form."
    return CandidateAction(
        name=action_name,
        description=description,
        code="\n".join(lines),
        captures=tuple(captures),
        variant_group=variant_group,
        variant_value=variant_value,
        variant_label=variant_label,
    )


def _click_candidate(item: dict[str, object], *, kind: str) -> CandidateAction | None:
    if _control_is_disabled(item):
        return None
    if not _control_has_actionable_identity(item):
        return None
    locator = _click_locator_expression(item, kind=kind)
    if locator is None:
        return None
    lines = [
        f"{locator}.click(timeout=timeout_ms)",
        'page.wait_for_load_state("load", timeout=timeout_ms)',
    ]
    action_name = _transition_name(item, fallback=f"open {kind}")
    description = f"Click {kind} {action_name.replace('_', ' ')}."
    return CandidateAction(name=action_name, description=description, code="\n".join(lines))


def _control_is_disabled(item: dict[str, object]) -> bool:
    return bool(item.get("disabled")) or _string_value(item.get("ariaDisabled")).lower() == "true"


def _control_has_actionable_identity(item: dict[str, object]) -> bool:
    if any(
        _string_value(item.get(key))
        for key in ("testid", "id", "text", "name", "href")
    ):
        return True
    aria = _string_value(item.get("aria"))
    return bool(aria and not _is_generic_aria_label(aria))


def _is_generic_aria_label(value: str) -> bool:
    return value.strip().lower() in {"", "button", "link", "menu", "close", "icon"}


def _click_locator_expression(item: dict[str, object], *, kind: str) -> str | None:
    selector = _string_value(item.get("selector"))
    text = _string_value(item.get("text"))
    selector_kind = _string_value(item.get("selectorKind"))
    selector_unique = item.get("selectorUnique")
    text_unique = bool(item.get("textUnique"))
    if selector_unique is False and not text_unique:
        return None
    if text and text_unique and (selector_unique is False or selector_kind in {"css_path", "aria", ""}):
        tag = _string_value(item.get("tag")) or ("a" if kind == "link" else "button")
        role = _string_value(item.get("role"))
        base_selector = (
            f"[role={role!r}]"
            if role and tag not in {"button", "a"}
            else tag
        )
        return f"page.locator({base_selector!r}).filter(has_text={text!r})"
    if selector:
        return f"page.locator({selector!r})"
    return None


def _transition_name(item: dict[str, object], *, fallback: str) -> str:
    for key in ("testid", "text", "label", "name", "href"):
        value = _string_value(item.get(key))
        if value:
            return _sanitize_identifier(value, default=fallback)
    return _sanitize_identifier(fallback, default="discover_transition")


def _finite_control_variants(
    form: dict[str, object],
    *,
    max_variants_per_control: int,
) -> tuple[tuple[dict[str, object], ...], ...]:
    if max_variants_per_control <= 1:
        return ()
    variants: list[tuple[dict[str, object], ...]] = []
    for raw_field in _as_list(form.get("fields")):
        if not isinstance(raw_field, dict):
            continue
        field = cast(dict[str, object], raw_field)
        selector = _string_value(field.get("selector"))
        if not selector or field.get("disabled") or field.get("readonly"):
            continue
        field_tag = _string_value(field.get("tag")).lower()
        field_type = _string_value(field.get("type")).lower()
        if field_tag == "select":
            choices = _select_variants(field, max_variants_per_control=max_variants_per_control)
        elif field_type == "radio":
            choices = _radio_variants(field, max_variants_per_control=max_variants_per_control)
        elif field_type == "checkbox":
            choices = _checkbox_variants(field, max_variants_per_control=max_variants_per_control)
        else:
            choices = ()
        variants.extend((choice,) for choice in choices)
    return tuple(variants)


def _select_variants(
    field: dict[str, object],
    *,
    max_variants_per_control: int,
) -> tuple[dict[str, object], ...]:
    selector = _string_value(field.get("selector"))
    group = _string_value(field.get("name")) or _string_value(field.get("testid")) or selector
    choices: list[dict[str, object]] = []
    for raw_option in _as_list(field.get("options")):
        if not isinstance(raw_option, dict):
            continue
        option = cast(dict[str, object], raw_option)
        if option.get("disabled"):
            continue
        value = _string_value(option.get("value")) or _string_value(option.get("text"))
        label = _string_value(option.get("text")) or value
        if not value or _is_placeholder_option(value=value, label=label):
            continue
        choices.append(
            {
                "selector": selector,
                "group": group,
                "value": value,
                "label": label,
                "selected": bool(option.get("selected")),
            }
        )
    return tuple(choices[:max_variants_per_control])


def _radio_variants(
    field: dict[str, object],
    *,
    max_variants_per_control: int,
) -> tuple[dict[str, object], ...]:
    selector = _string_value(field.get("selector"))
    value = _string_value(field.get("value")) or "on"
    label = _string_value(field.get("label")) or value
    if not selector or _is_placeholder_option(value=value, label=label):
        return ()
    return (
        {
            "selector": selector,
            "group": _string_value(field.get("name")) or selector,
            "value": value,
            "label": label,
        },
    )[:max_variants_per_control]


def _checkbox_variants(
    field: dict[str, object],
    *,
    max_variants_per_control: int,
) -> tuple[dict[str, object], ...]:
    if max_variants_per_control <= 0:
        return ()
    selector = _string_value(field.get("selector"))
    value = _string_value(field.get("value")) or "on"
    label = _string_value(field.get("label")) or value
    if not selector or _is_placeholder_option(value=value, label=label):
        return ()
    return (
        {
            "selector": selector,
            "group": _string_value(field.get("name")) or selector,
            "value": value,
            "label": label,
        },
    )[:max_variants_per_control]


def _is_placeholder_option(*, value: str, label: str) -> bool:
    normalized = f"{value} {label}".strip().lower()
    if not normalized:
        return True
    return normalized in {"select", "choose", "none", "n/a"} or normalized.startswith(
        ("select ", "choose ")
    )


def _capture_variable_base(field: dict[str, object], *, default: str) -> str:
    for key in ("name", "testid", "id", "label", "placeholder"):
        value = _string_value(field.get(key))
        if value:
            return value
    return default


def _allocate_variable_name(raw: str, used: set[str]) -> str:
    base = _sanitize_identifier(raw, default="value")
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _field_value_expression(field: dict[str, object]) -> str | None:
    field_type = _string_value(field.get("type")).lower()
    current_value = _string_value(field.get("value"))
    identity = _field_identity(field)
    if field_type == "email" or "email" in identity:
        prefix = re.sub(r"[^a-z0-9]+", "-", identity).strip("-") or "user"
        return f"unique_email({prefix[:32]!r})"
    if current_value:
        return repr(current_value)
    if _is_optional_evidence_text_field(field):
        return repr(_OPTIONAL_EVIDENCE_FIELD_VALUE)
    if "name" in identity:
        return repr("Test User")
    if field_type in {"number", "range"}:
        return repr("1")
    if field_type in {"tel", "phone"}:
        return repr("5550100")
    if field_type == "url":
        return repr("https://example.test")
    if field.get("required"):
        return repr("test value")
    return None


def _field_identity(field: dict[str, object]) -> str:
    return " ".join(
        _string_value(field.get(key)).lower()
        for key in ("testid", "name", "label", "placeholder", "id")
    )


def _is_optional_evidence_text_field(field: dict[str, object]) -> bool:
    if field.get("required"):
        return False
    field_type = _string_value(field.get("type")).lower()
    field_tag = _string_value(field.get("tag")).lower()
    if field_type not in {"text", "search", "textarea"} and field_tag != "textarea":
        return False
    identity = _field_identity(field)
    return any(term in identity for term in _OPTIONAL_EVIDENCE_FIELD_TERMS)


def _literal_string_expression(expression: str) -> str | None:
    try:
        value = ast.literal_eval(expression)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _first_select_option_value(field: dict[str, object]) -> str | None:
    selected_value: str | None = None
    fallback_value: str | None = None
    for raw_option in _as_list(field.get("options")):
        if not isinstance(raw_option, dict):
            continue
        option = cast(dict[str, object], raw_option)
        if option.get("disabled"):
            continue
        value = _string_value(option.get("value"))
        if not value:
            value = _string_value(option.get("text"))
        if not value:
            continue
        if fallback_value is None:
            fallback_value = value
        if option.get("selected"):
            selected_value = value
            break
    return selected_value or fallback_value


def _dedupe_candidates(candidates: Sequence[CandidateAction]) -> list[CandidateAction]:
    deduped: list[CandidateAction] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _sanitize_identifier(candidate.name, default="discover_action")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _discover_probes_for_page(
    page: PlaywrightPage,
    *,
    action: CandidateAction,
    email_evidence_urls: tuple[str, ...],
    webhook_evidence_urls: tuple[str, ...],
    cloud_webhook_endpoints: tuple[object, ...],
) -> list[ProbeSpec]:
    visible_text = _safe_visible_text(page)
    identifier = _first_stable_identifier(visible_text)
    if identifier is None:
        _LOGGER.debug(
            "discover_probe_omitted",
            "journey discover found no stable identifier for side-effect probes",
            reason="missing_identifier",
            action=action.name,
        )
        return []

    probes: list[ProbeSpec] = []
    state_url = _discover_json_state_url(page.url, identifier)
    if state_url is not None:
        probes.append(
            ProbeSpec(
                kind="json_state",
                url=state_url,
                description="same-origin JSON state contains the visible identifier",
            )
        )
        _LOGGER.info(
            "discover_probe_found",
            "journey discover found a JSON state probe",
            action=action.name,
            kind="json_state",
            url=state_url,
        )
    else:
        _LOGGER.debug(
            "discover_probe_omitted",
            "journey discover did not find a matching JSON state probe",
            action=action.name,
            kind="json_state",
            reason="not_reachable_or_no_identifier_match",
        )

    if _mentions_email_evidence(visible_text):
        email_url = _discover_email_evidence_url(
            identifier,
            extra_urls=email_evidence_urls,
        )
        if email_url is not None:
            probes.append(
                ProbeSpec(
                    kind="email_evidence",
                    url=email_url,
                    description="local email evidence contains the visible identifier",
                )
            )
            _LOGGER.info(
                "discover_probe_found",
                "journey discover found an email evidence probe",
                action=action.name,
                kind="email_evidence",
                url=email_url,
            )
        else:
            _LOGGER.debug(
                "discover_probe_omitted",
                "journey discover did not find reachable email evidence",
                action=action.name,
                kind="email_evidence",
                reason="not_reachable_or_no_identifier_match",
            )

    if _mentions_webhook_evidence(visible_text):
        webhook_url = _discover_webhook_evidence_url(
            identifier,
            extra_urls=webhook_evidence_urls,
        )
        if webhook_url is not None:
            probes.append(
                ProbeSpec(
                    kind="webhook_evidence",
                    url=webhook_url,
                    description="local webhook evidence contains the visible identifier",
                )
            )
            _LOGGER.info(
                "discover_probe_found",
                "journey discover found a webhook evidence probe",
                action=action.name,
                kind="webhook_evidence",
                url=webhook_url,
            )
        else:
            _LOGGER.debug(
                "discover_probe_omitted",
                "journey discover did not find reachable webhook evidence",
                action=action.name,
                kind="webhook_evidence",
                reason="not_reachable_or_no_identifier_match",
            )
        if _discover_cloud_webhook_evidence(identifier, cloud_webhook_endpoints):
            probes.append(
                ProbeSpec(
                    kind="cloud_webhook_evidence",
                    url="",
                    description="Journey Cloud webhook evidence contains the visible identifier",
                )
            )
            _LOGGER.info(
                "discover_probe_found",
                "journey discover found a Journey Cloud webhook evidence probe",
                action=action.name,
                kind="cloud_webhook_evidence",
            )
        elif cloud_webhook_endpoints:
            _LOGGER.debug(
                "discover_probe_omitted",
                "journey discover did not find matching Journey Cloud webhook evidence",
                action=action.name,
                kind="cloud_webhook_evidence",
                reason="not_reachable_or_no_identifier_match",
            )
    return probes


def _visible_capture_assertions(
    path: tuple[DiscoveredEdge, ...],
    action: CandidateAction,
    snapshot: PageSnapshot,
) -> tuple[str, ...]:
    visible_text = snapshot.visible_text
    values: list[str] = []
    for edge in path:
        values.extend(_static_capture_values(edge.action))
    values.extend(_static_capture_values(action))

    assertions: list[str] = []
    for value in values:
        text = " ".join(value.split())
        if not 3 <= len(text) <= 160:
            continue
        if _weak_assertion_text(text):
            continue
        if text in visible_text and text not in assertions:
            assertions.append(text)
    return tuple(assertions[:5])


def _static_capture_values(action: CandidateAction) -> list[str]:
    values: list[str] = []
    for capture in action.captures:
        if capture.value:
            values.append(capture.value)
        if capture.kind == "option" and capture.label and capture.label != capture.value:
            values.append(capture.label)
    return values


def _first_stable_identifier(text: str) -> str | None:
    identifiers = _stable_identifiers(text)
    return identifiers[0] if identifiers else None


def _stable_identifiers(text: str) -> tuple[str, ...]:
    patterns = (
        (r"\b[A-Z]{2,}-[A-Z0-9][A-Z0-9-]{3,}\b", 0),
        (
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        (r"\b[A-Z0-9]{8,}\b", 0),
        (r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.IGNORECASE),
    )
    identifiers: list[str] = []
    for pattern, flags in patterns:
        for match in re.finditer(pattern, text, flags=flags):
            identifier = match.group(0)
            if identifier not in identifiers:
                identifiers.append(identifier)
    return tuple(identifiers)


def _discover_json_state_url(page_url: str, identifier: str) -> str | None:
    origin = _origin(page_url)
    if not origin.startswith(("http://", "https://")):
        return None
    for path in _JSON_STATE_PATHS:
        for query_key in _IDENTIFIER_QUERY_KEYS:
            url = f"{origin}{path}?{query_key}={quote(identifier)}"
            payload = _fetch_json_if_ok(url, timeout=1.5)
            if payload is not None and _json_contains_value(payload, identifier):
                return url.replace(quote(identifier), "{identifier}")
    return None


def _discover_email_evidence_url(
    identifier: str,
    *,
    extra_urls: Sequence[str] = (),
) -> str | None:
    for base_url in _candidate_service_urls(
        _EMAIL_EVIDENCE_URL_ENV,
        _EMAIL_EVIDENCE_URLS,
        extra_urls=extra_urls,
    ):
        messages = _fetch_json_if_ok(f"{base_url}/api/v1/messages", timeout=1.5)
        if messages is None or not _json_contains_value(messages, identifier):
            continue
        return base_url
    return None


def _discover_webhook_evidence_url(
    identifier: str,
    *,
    extra_urls: Sequence[str] = (),
) -> str | None:
    for base_url in _candidate_service_urls(
        _WEBHOOK_EVIDENCE_URL_ENV,
        _WEBHOOK_EVIDENCE_URLS,
        extra_urls=extra_urls,
    ):
        url = f"{base_url}/requests/latest?confirmation_code={quote(identifier)}"
        payload = _fetch_json_if_ok(url, timeout=1.5)
        if payload is not None and _json_contains_value(payload, identifier):
            return base_url
    return None


def _discover_cloud_webhook_evidence(
    identifier: str,
    endpoints: Sequence[object],
) -> bool:
    for endpoint in endpoints:
        if not isinstance(endpoint, CloudWebhookEndpoint):
            continue
        try:
            payload = fetch_next_request(
                endpoint_id=endpoint.endpoint_id,
                api_base_url=endpoint.api_base_url,
            )
        except Exception:
            continue
        if payload is not None and _json_contains_value(payload, identifier):
            return True
    return False


def _candidate_service_urls(
    env_names: Sequence[str],
    defaults: Sequence[str],
    *,
    extra_urls: Sequence[str] = (),
) -> tuple[str, ...]:
    values: list[str] = []
    values.extend(extra_urls)
    for name in env_names:
        value = os.environ.get(name)
        if value:
            values.append(value)
    values.extend(defaults)
    normalized: list[str] = []
    for value in values:
        stripped = value.strip().rstrip("/")
        if stripped and stripped not in normalized:
            normalized.append(stripped)
    return tuple(normalized)


def _fetch_json_if_ok(url: str, *, timeout: float) -> object | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", getattr(response, "code", 200))
            if int(status) < 200 or int(status) > 299:
                return None
            content_type = str(response.headers.get("Content-Type", ""))
            body = response.read()
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    if "json" not in content_type.lower() and not body.strip().startswith((b"{", b"[")):
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _json_contains_value(payload: object, expected: object) -> bool:
    expected_text = str(expected)
    if isinstance(payload, dict):
        return any(_json_contains_value(value, expected_text) for value in payload.values())
    if isinstance(payload, list | tuple):
        return any(_json_contains_value(value, expected_text) for value in payload)
    if payload is None:
        return False
    return str(payload) == expected_text or expected_text in str(payload)


def _mentions_email_evidence(text: str) -> bool:
    lowered = text.lower()
    return "email" in lowered or "mail" in lowered


def _mentions_webhook_evidence(text: str) -> bool:
    lowered = text.lower()
    return "webhook" in lowered or "callback" in lowered


def _execute_discover_code(
    page: PlaywrightPage,
    code: str,
    *,
    timeout_seconds: float,
) -> PlaywrightPage:
    normalized = _strip_code_fences(code).strip()
    _validate_discover_python_code(normalized)
    context = page.context
    pages = list(context.pages)
    active_page = page
    timeout_ms = int(timeout_seconds * 1000)

    def unique_email(prefix: str) -> str:
        safe_prefix = re.sub(r"[^a-z0-9]+", "-", prefix.lower()).strip("-") or "user"
        return f"{safe_prefix}-{uuid4().hex[:8]}@example.test"

    namespace: dict[str, object] = {
        "__builtins__": {
            **_PROMPT_SAFE_BUILTINS,
            "print": lambda *args, **kwargs: None,
        },
        "page": active_page,
        "pages": tuple(pages),
        "timeout_ms": timeout_ms,
        "unique_email": unique_email,
    }

    def switch_page(index: object) -> PlaywrightPage:
        nonlocal active_page, pages
        pages = list(context.pages)
        if isinstance(index, bool) or not isinstance(index, int):
            raise RuntimeError("journey discover switch_page index must be an integer.")
        if index < 0 or index >= len(pages):
            raise RuntimeError(
                f"journey discover switch_page index {index} is outside 0..{len(pages) - 1}."
            )
        active_page = pages[index]
        namespace["page"] = active_page
        namespace["pages"] = tuple(pages)
        return active_page

    namespace["switch_page"] = switch_page
    exec(compile(normalized, "<journey-discover>", "exec"), namespace, namespace)
    candidate_page = namespace.get("page")
    if isinstance(candidate_page, PlaywrightPage):
        active_page = candidate_page
    return active_page


def _validate_discover_python_code(code: str) -> None:
    _validate_prompt_python_code(
        code,
        owner="Journey discover Python snippet",
        extra_allowed_names={"unique_email"},
    )
    tree = ast.parse(code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "switch_page":
            raise RuntimeError(
                "Journey discover Python snippet cannot use switch_page(...) because "
                "generated Journey steps must return a replayable JourneyBrowserPage."
            )


def _snapshot_page(page: PlaywrightPage) -> PageSnapshot:
    url = _safe_page_url(page)
    title = _safe_page_title(page)
    visible_text = _truncate(_safe_visible_text(page), _MAX_OBSERVATION_TEXT_LENGTH)
    semantic_dom = _truncate(_safe_semantic_dom(page), _MAX_OBSERVATION_TEXT_LENGTH)
    affordances = _truncate(_safe_affordance_inventory(page), _MAX_OBSERVATION_TEXT_LENGTH)
    signature = json.dumps(
        {
            "url": _url_signature(url),
            "title": title,
            "text": _text_signature(visible_text),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return PageSnapshot(
        url=url,
        title=title,
        visible_text=visible_text,
        semantic_dom=semantic_dom,
        signature=signature,
        affordances=affordances,
    )


def _settle_page(page: PlaywrightPage) -> None:
    for state, timeout in (("load", 5000), ("networkidle", 1500)):
        try:
            page.wait_for_load_state(state, timeout=timeout)
        except Exception:
            pass


def _safe_page_url(page: PlaywrightPage) -> str:
    try:
        return page.url
    except Exception:
        return ""


def _safe_page_title(page: PlaywrightPage) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _safe_visible_text(page: PlaywrightPage) -> str:
    try:
        return _visible_text(page)
    except Exception:
        try:
            body = page.locator("body")
            text = body.inner_text(timeout=1000)
            return text if isinstance(text, str) else ""
        except Exception:
            return ""


def _safe_semantic_dom(page: PlaywrightPage) -> str:
    try:
        return _semantic_dom_snapshot(page)
    except Exception:
        return ""


def _safe_affordance_inventory(page: PlaywrightPage) -> str:
    try:
        return _render_affordance_inventory(_extract_affordances(page))
    except Exception:
        return ""


def _extract_affordances(page: PlaywrightPage) -> dict[str, object]:
    payload = page.evaluate(_AFFORDANCE_SCRIPT)
    if isinstance(payload, dict):
        return cast(dict[str, object], payload)
    return {"forms": [], "links": [], "buttons": []}


def _render_affordance_inventory(affordances: dict[str, object]) -> str:
    lines: list[str] = []
    for index, raw_form in enumerate(_as_list(affordances.get("forms")), start=1):
        if not isinstance(raw_form, dict):
            continue
        form = cast(dict[str, object], raw_form)
        submit = form.get("submit") if isinstance(form.get("submit"), dict) else {}
        submit_text = _string_value(cast(dict[str, object], submit).get("text")) if submit else ""
        action = _string_value(form.get("action"))
        lines.append(f"- form {index}: action={action or '?'} submit={submit_text or '?'}")
        for raw_field in _as_list(form.get("fields")):
            if not isinstance(raw_field, dict):
                continue
            field = cast(dict[str, object], raw_field)
            label = _string_value(field.get("label")) or _string_value(field.get("name"))
            field_type = _string_value(field.get("type")) or _string_value(field.get("tag"))
            required = " required" if field.get("required") else ""
            testid = _string_value(field.get("testid"))
            testid_text = f" data-testid={testid}" if testid else ""
            lines.append(f"  - {label or '?'} type={field_type or '?'}{required}{testid_text}")
    for raw_link in _as_list(affordances.get("links")):
        if not isinstance(raw_link, dict):
            continue
        link = cast(dict[str, object], raw_link)
        lines.append(
            f"- link: {_string_value(link.get('text')) or '?'} href={_string_value(link.get('href')) or '?'}"
        )
    for raw_button in _as_list(affordances.get("buttons")):
        if not isinstance(raw_button, dict):
            continue
        button = cast(dict[str, object], raw_button)
        lines.append(f"- button: {_string_value(button.get('text')) or '?'}")
    return "\n".join(lines)


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _candidate_prompt(
    snapshot: PageSnapshot,
    *,
    path: tuple[DiscoveredEdge, ...],
    remaining_actions: int,
    depth_remaining: int,
    max_candidates: int,
) -> str:
    prior_actions = "\n".join(
        f"- {edge.action.name}: {edge.action.description} -> {edge.snapshot.url}"
        for edge in path
    )
    if not prior_actions:
        prior_actions = "- none"
    affordance_block = snapshot.affordances or snapshot.semantic_dom
    affordance_label = "Affordances" if snapshot.affordances else "Semantic DOM"
    return textwrap.dedent(
        f"""
        Discover this browser page and return up to {max_candidates} next complete user transitions.

        Current URL: {snapshot.url}
        Current title: {snapshot.title}
        Remaining action budget: {remaining_actions}
        Remaining depth: {depth_remaining}

        Path so far:
        {prior_actions}

        Visible text:
        <visible-text>
        {snapshot.visible_text}
        </visible-text>

        {affordance_label}:
        <affordances>
        {affordance_block}
        </affordances>

        Return JSON only:
        {{
          "actions": [
            {{
              "name": "short snake_case intent",
              "description": "one sentence user action",
              "code": "Playwright sync Python snippet"
            }}
          ]
        }}

        Snippet rules:
        - Execute one complete user transition from the current page.
        - Compose required fills, selects, submits, waits, and assertions together.
        - Do not return standalone field fills unless they reveal a new stable state.
        - Use page, timeout_ms, and unique_email(prefix).
        - Prefer data-testid selectors and accessible selectors.
        - Pass timeout=timeout_ms to Playwright actions and waits.
        - Include enough waits/assertions that replay proves the resulting page.
        - Do not import modules, read files, spawn processes, or use eval/exec/open.
        - Use unique_email("organizer") or unique_email("attendee") for email fields.
        """
    ).strip()


_DISCOVER_SYSTEM_PROMPT = """You are Journey Discover, an autonomous browser test author.
Your job is to discover reachable user paths and produce replayable Playwright snippets.
Return strict JSON only. Do not include markdown unless it is inside a JSON string.
Prefer robust user-facing or data-testid selectors. Avoid repeating actions already listed
in the path unless the page state clearly requires it."""


def _extract_json_payload(text: str) -> object:
    stripped = _strip_code_fences(text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    object_start = stripped.find("{")
    array_start = stripped.find("[")
    candidates = [index for index in (object_start, array_start) if index >= 0]
    if not candidates:
        raise RuntimeError("journey discover model response did not contain JSON.")
    start = min(candidates)
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    if end < start:
        raise RuntimeError("journey discover model response JSON was incomplete.")
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"journey discover model response was not valid JSON: {exc}") from exc


def _render_root_function(root: DiscoveredNode, *, function_name: str) -> list[str]:
    expected_path = _path_for_url(root.snapshot.url)
    expected_title = root.snapshot.title or None
    expected_text = _visible_assertion_text(root.snapshot)
    return [
        f"def {function_name}() -> JourneyBrowserPage:",
        f"    page = open_page({root.start_url!r})",
        f"    timeout_ms = {int(_GENERATED_REPLAY_TIMEOUT_SECONDS * 1000)}",
        "    page.wait_for_load_state(\"load\", timeout=timeout_ms)",
        "    _settle_replay_page(page, timeout_ms=timeout_ms)",
        (
            "    _assert_page_state("
            f"page, expected_path={expected_path!r}, expected_title={expected_title!r}, "
            f"expected_text={expected_text!r}, timeout_ms=timeout_ms)"
        ),
        "    return page",
    ]


def _render_edge_function(edge: DiscoveredEdge) -> list[str]:
    expected_path = _path_for_url(edge.snapshot.url)
    expected_title = edge.snapshot.title or None
    expected_text = _visible_assertion_text(edge.snapshot)
    lines = [
        f"def {edge.function_name}(",
        "    saved_page: JourneyBrowserPage,",
        "    anchor_result: object | None = None,",
        ") -> JourneyBrowserPage:",
        f"    \"\"\"{_docstring_text(edge.action.description)}\"\"\"",
        "    page = open_page(saved_page)",
        f"    timeout_ms = {int(_GENERATED_REPLAY_TIMEOUT_SECONDS * 1000)}",
        "    _settle_replay_page(page, timeout_ms=timeout_ms)",
        "",
        "    def unique_email(prefix: str) -> str:",
        "        return _unique_email(prefix)",
        "",
    ]
    for code_line in edge.action.code.strip().splitlines():
        lines.append(f"    {code_line.rstrip()}")
    lines.extend(
        [
            "    page.wait_for_load_state(\"load\", timeout=timeout_ms)",
            "    _settle_replay_page(page, timeout_ms=timeout_ms)",
            (
                "    _assert_page_state("
                f"page, expected_path={expected_path!r}, expected_title={expected_title!r}, "
                f"expected_text={expected_text!r}, timeout_ms=timeout_ms)"
            ),
        ]
    )
    for assertion in edge.visible_assertions:
        if assertion != expected_text:
            lines.append(
                "    _assert_page_state("
                f"page, expected_text={assertion!r}, timeout_ms=timeout_ms)"
            )
    if edge.probes:
        expected_values = ["_journey_visible_identifier"]
        machine_expected_values = ["_journey_visible_identifier"]
        for capture in edge.action.captures:
            if capture.variable:
                expected_values.append(capture.variable)
                if capture.kind != "field":
                    machine_expected_values.append(capture.variable)
            elif capture.value:
                expected_values.append(repr(capture.value))
                if capture.kind != "field":
                    machine_expected_values.append(repr(capture.value))
        lines.extend(
            [
                "    _journey_visible_identifier = _first_visible_identifier(page, timeout_ms=timeout_ms)",
                (
                    "    _journey_expected_values = _dedupe_expected_values(["
                    + ", ".join(expected_values)
                    + "])"
                ),
                (
                    "    _journey_machine_expected_values = _dedupe_expected_values(["
                    + ", ".join(machine_expected_values)
                    + "])"
                ),
            ]
        )
        for probe in edge.probes:
            if probe.kind == "json_state":
                lines.append(
                    "    _wait_for_http_json("
                    f"{probe.url!r}.format(identifier=quote(_journey_visible_identifier)), "
                    "_journey_expected_values, "
                    f"label={probe.description!r})"
                )
            elif probe.kind == "email_evidence":
                lines.append(
                    "    _wait_for_email_evidence("
                    f"{probe.url!r}, _journey_visible_identifier, _journey_expected_values)"
                )
            elif probe.kind == "webhook_evidence":
                lines.append(
                    "    _wait_for_webhook_evidence("
                    f"{probe.url!r}, _journey_visible_identifier, _journey_machine_expected_values)"
                )
            elif probe.kind == "cloud_webhook_evidence":
                lines.extend(
                    [
                        "    if anchor_result is None:",
                        "        raise AssertionError(\"Expected anchor result for Journey Cloud webhook evidence.\")",
                        "    _wait_for_cloud_webhook_evidence(",
                        "        anchor_result,",
                        "        _journey_visible_identifier,",
                        "        _journey_machine_expected_values,",
                        "    )",
                    ]
                )
    lines.append("    return page")
    return lines


def _render_journey_function(
    roots: tuple[DiscoveredNode, ...],
    root_functions: dict[str, str],
    *,
    journey_name: str,
) -> list[str]:
    lines = [
        "@journey",
        f"def {journey_name}() -> None:",
    ]
    if len(roots) == 1:
        root = roots[0]
        root_var = _var_name(root_functions[root.node_id])
        lines.append(f"    {root_var} = step({root_functions[root.node_id]})")
        lines.extend(_render_node_body(root, current_var=root_var, indent="    "))
    else:
        for index, root in enumerate(roots):
            prefix = "if" if index == 0 else "elif"
            lines.append(f"    {prefix} branch():")
            root_var = _var_name(root_functions[root.node_id])
            lines.append(f"        {root_var} = step({root_functions[root.node_id]})")
            lines.extend(_render_node_body(root, current_var=root_var, indent="        "))
    if len(lines) == 2:
        lines.append("    pass")
    return lines


def _render_node_body(
    node: DiscoveredNode,
    *,
    current_var: str,
    indent: str,
    replay_var: str | None = None,
    anchor_var: str | None = None,
) -> list[str]:
    if not node.edges:
        return []
    if len(node.edges) == 1:
        edge = node.edges[0]
        edge_var = _var_name(edge.function_name)
        step_args = (
            f"{current_var}, {anchor_var}" if anchor_var is not None else current_var
        )
        lines = [f"{indent}{edge_var} = step({edge.function_name}, {step_args})"]
        if edge.child is not None:
            lines.extend(
                _render_node_body(
                    edge.child,
                    current_var=edge_var,
                    indent=indent,
                    anchor_var=anchor_var,
                )
            )
        return lines

    lines: list[str] = []
    for index, edge in enumerate(node.edges):
        prefix = "if" if index == 0 else "elif"
        edge_var = _var_name(edge.function_name)
        branch_replay_var = replay_var or current_var
        lines.append(f"{indent}{prefix} branch(replay_from={branch_replay_var}):")
        step_args = (
            f"{current_var}, {anchor_var}" if anchor_var is not None else current_var
        )
        lines.append(f"{indent}    {edge_var} = step({edge.function_name}, {step_args})")
        if edge.child is not None:
            lines.extend(
                _render_node_body(
                    edge.child,
                    current_var=edge_var,
                    indent=f"{indent}    ",
                    anchor_var=anchor_var,
                )
            )
    return lines


def _import_generated_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_journey_discover_generated", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import generated Journey file {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iter_nodes(roots: Sequence[DiscoveredNode]) -> list[DiscoveredNode]:
    nodes: list[DiscoveredNode] = []
    seen: set[str] = set()

    def visit(node: DiscoveredNode) -> None:
        if node.node_id in seen:
            return
        seen.add(node.node_id)
        nodes.append(node)
        for edge in node.edges:
            if edge.child is not None:
                visit(edge.child)

    for root in roots:
        visit(root)
    return nodes


def _iter_edges(roots: Sequence[DiscoveredNode]) -> list[DiscoveredEdge]:
    edges: list[DiscoveredEdge] = []
    for node in _iter_nodes(roots):
        edges.extend(node.edges)
    return edges


def _iter_edge_count(root: DiscoveredNode) -> int:
    return len([edge for edge in _iter_edges((root,))])


def _normalize_options(options: DiscoverOptions) -> DiscoverOptions:
    has_urls = bool(options.urls)
    has_page_state = options.start_page_state is not None
    if has_urls == has_page_state:
        raise ValueError("journey discover requires either URL values or one anchor page state.")
    urls = tuple(_normalize_url(url) for url in options.urls)
    start_page_state = (
        _normalize_browser_start_state(options.start_page_state)
        if options.start_page_state is not None
        else None
    )
    if options.depth <= 0:
        raise ValueError("journey discover --depth must be a positive integer.")
    if options.max_actions <= 0:
        raise ValueError("journey discover --max-actions must be a positive integer.")
    if options.max_model_calls < 0:
        raise ValueError("journey discover --max-model-calls must be zero or a positive integer.")
    if options.max_variants_per_control <= 0:
        raise ValueError("journey discover --max-variants-per-control must be a positive integer.")
    if options.action_timeout_seconds <= 0:
        raise ValueError("journey discover --action-timeout must be a positive number.")
    if options.side_effect_probes not in {"auto", "off"}:
        raise ValueError("journey discover --side-effect-probes must be auto or off.")
    if options.browser not in _SUPPORTED_BROWSERS:
        raise ValueError("journey discover --browser must be chromium, firefox, or webkit.")
    return DiscoverOptions(
        urls=urls,
        start_page_state=start_page_state,
        anchor_step=options.anchor_step.strip() if options.anchor_step else None,
        journey_name=_sanitize_identifier(options.journey_name, default="discovered_journey"),
        depth=options.depth,
        max_actions=options.max_actions,
        max_model_calls=options.max_model_calls,
        max_variants_per_control=options.max_variants_per_control,
        side_effect_probes=options.side_effect_probes,
        browser=options.browser,
        headless=options.headless,
        model=options.model,
        allow_external=options.allow_external,
        action_timeout_seconds=options.action_timeout_seconds,
        email_evidence_urls=_dedupe_strings(options.email_evidence_urls),
        webhook_evidence_urls=_dedupe_strings(options.webhook_evidence_urls),
        cloud_webhook_endpoints=tuple(options.cloud_webhook_endpoints),
    )


def _normalize_browser_start_state(start_state: BrowserStartState | None) -> BrowserStartState:
    if start_state is None:
        raise ValueError("journey discover requires an anchor page state.")
    url = _normalize_url(start_state.url)
    cookies = tuple(dict(cookie) for cookie in start_state.cookies)
    local_storage = tuple(
        sorted(
            (str(key), str(value))
            for key, value in start_state.local_storage
            if str(key)
        )
    )
    return BrowserStartState(url=url, cookies=cookies, local_storage=local_storage)


def _start_states_for_options(options: DiscoverOptions) -> tuple[BrowserStartState, ...]:
    if options.start_page_state is not None:
        return (options.start_page_state,)
    return tuple(BrowserStartState(url=url) for url in options.urls)


def _allowed_origins(options: DiscoverOptions) -> set[str]:
    return {_origin(start_state.url) for start_state in _start_states_for_options(options)}


def _resolve_discover_model(model: str | None) -> str:
    return resolve_prompt_model(
        model,
        env_var=JOURNEY_DISCOVER_MODEL_ENV,
        owner="journey discover",
        default_model=DEFAULT_JOURNEY_DISCOVER_MODEL,
    )


def _normalize_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        raise ValueError("journey discover URL values must be non-blank.")
    parsed = urlsplit(normalized)
    if not parsed.scheme:
        normalized = f"http://{normalized}"
        parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https", "file"}:
        raise ValueError(
            "journey discover supports http, https, and file URLs. "
            f"Got {parsed.scheme!r}."
        )
    return normalized


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        return "file://"
    return f"{parsed.scheme}://{parsed.netloc}"


def _url_signature(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def _path_for_url(url: str) -> str:
    path = urlsplit(url).path
    return path or "/"


def _text_signature(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9_.@/-]+", text.lower())
    return " ".join(words[:80])


def _state_slug(snapshot: PageSnapshot) -> str:
    parsed = urlsplit(snapshot.url)
    raw = snapshot.title or parsed.path or "page"
    return raw


def _visible_assertion_text(snapshot: PageSnapshot) -> str | None:
    for line in snapshot.visible_text.splitlines():
        text = " ".join(line.strip().split())
        if 3 <= len(text) <= 100 and not _weak_assertion_text(text):
            return text
    words = " ".join(snapshot.visible_text.split())
    if 3 <= len(words) <= 100 and not _weak_assertion_text(words):
        return words
    if len(words) > 100:
        truncated = words[:100].rstrip()
        if not _weak_assertion_text(truncated):
            return truncated
    return None


def _weak_assertion_text(text: str) -> bool:
    normalized = " ".join(text.strip().split())
    lowered = normalized.lower()
    if not normalized:
        return True
    if normalized.upper() == "TEST":
        return True
    if len(normalized.split()) <= 1:
        return True
    if any(token in lowered for token in ("cookie", "cookies", "consent", "privacy", "tracking")):
        return True
    if lowered in {
        "loading",
        "please wait",
        "sign up",
        "log in",
        "home",
        "help",
        "close",
        "dismiss",
        "accept",
        "accept all",
        "reject all",
        "decline all",
    }:
        return True
    return False


def _sanitize_identifier(raw: str, *, default: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", raw.strip().lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    if not value:
        value = default
    if value[0].isdigit():
        value = f"journey_{value}"
    return value


def _clean_action_text(text: str) -> str:
    return " ".join(text.strip().split())[:200]


def _docstring_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _var_name(function_name: str) -> str:
    return f"{function_name}_page"


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _short_exception(exc: BaseException, *, max_length: int = 220) -> str:
    return _truncate(" ".join(_format_exception(exc).split()), max_length)


def _omission_reason_for_exception(exc: BaseException) -> str:
    text = _format_exception(exc).lower()
    if "intercepts pointer events" in text and any(
        token in text for token in ("cookie", "consent", "privacy", "cybot")
    ):
        return "overlay_blocked"
    if "strict mode violation" in text or "resolved to" in text:
        return "non_unique_selector"
    if "element is not enabled" in text or "disabled" in text:
        return "disabled_control"
    if "timeout" in text:
        return "timeout"
    return "execution_error"


def _increment_count(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _emit_discover_omission_summary(counts: dict[str, int]) -> None:
    if not counts:
        return
    summary = ", ".join(
        f"{key}={value}" for key, value in sorted(counts.items())
    )
    _LOGGER.info(
        "discover_omissions_summary",
        "journey discover omitted actions by reason",
        pretty=pretty_row(
            "Discover",
            f"omitted actions: {summary}",
            indent=8,
            label_width=27,
            style="warning",
        ),
        omitted_by_reason=dict(sorted(counts.items())),
    )


class _NameAllocator:
    def __init__(self) -> None:
        self._used: set[str] = set()

    def allocate(self, raw: str) -> str:
        base = _sanitize_identifier(raw, default="discover_action")
        candidate = base
        suffix = 2
        while candidate in self._used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        self._used.add(candidate)
        return candidate
