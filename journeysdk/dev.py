"""Developer-page inspection helpers for ``journey dev``."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from journeysdk.logger import PrettyLine, pretty_line, pretty_row


_ACTIONABLE_SCRIPT = r"""
() => {
  const actionableTags = new Set(["a", "button", "input", "select", "textarea", "summary", "label"]);
  const noisyTags = new Set(["svg", "path", "circle", "rect", "line", "polyline", "polygon", "g", "use"]);
  const actionableRoles = new Set([
    "button", "link", "menuitem", "option", "tab", "checkbox", "radio", "switch",
    "textbox", "combobox", "searchbox", "slider", "spinbutton"
  ]);
  const actionableEvents = [
    "click", "dblclick", "mousedown", "mouseup", "mouseover", "mouseenter",
    "mouseleave", "keydown", "keyup", "input", "change", "submit", "drop",
    "dragover", "pointerdown", "pointerup"
  ];

  function clean(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function visible(element) {
    if (!element || !element.isConnected) return false;
    const style = window.getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none") return false;
    if (Number(style.opacity) === 0) return false;
    return element.getClientRects().length > 0;
  }

  function genericLabel(value) {
    return /^(button|link|input|menu|item|control)$/i.test(clean(value));
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function attrValue(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
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

  function unique(selector) {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch (_error) {
      return false;
    }
  }

  function selectorFor(element) {
    const testid = element.getAttribute("data-testid");
    if (testid) {
      const selector = `[data-testid="${attrValue(testid)}"]`;
      if (unique(selector)) return selector;
    }
    if (element.id) {
      const selector = `#${cssEscape(element.id)}`;
      if (unique(selector)) return selector;
    }
    const name = element.getAttribute("name");
    if (name) {
      const selector = `${element.tagName.toLowerCase()}[name="${attrValue(name)}"]`;
      if (unique(selector)) return selector;
    }
    return cssPath(element);
  }

  function accessibleName(element) {
    const visibleText = clean(element.innerText || element.textContent || "");
    const aria = clean(element.getAttribute("aria-label"));
    if (aria && !(genericLabel(aria) && visibleText)) return aria;
    const labelledBy = clean(element.getAttribute("aria-labelledby"));
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((node) => clean(node.innerText || node.textContent))
        .filter(Boolean)
        .join(" ");
      if (text) return text;
    }
    if (visibleText) return visibleText;
    if (element.labels && element.labels.length) {
      const text = Array.from(element.labels)
        .map((label) => clean(label.innerText || label.textContent))
        .filter(Boolean)
        .join(" ");
      if (text) return text;
    }
    return clean(
      element.value ||
      element.placeholder ||
      element.getAttribute("data-placeholder") ||
      element.getAttribute("title") ||
      element.getAttribute("data-testid") ||
      ""
    );
  }

  function roleFor(element, tag, type) {
    const explicit = clean(element.getAttribute("role")).toLowerCase();
    if (explicit) return explicit;
    if (tag === "button") return "button";
    if (tag === "a" && (element.href || element.getAttribute("href"))) return "link";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      if (!["button", "submit", "reset", "file", "hidden"].includes(type)) return "textbox";
      if (["button", "submit", "reset"].includes(type)) return "button";
    }
    if (element.isContentEditable) return "textbox";
    return "";
  }

  function eventTypesFor(element) {
    const found = [];
    for (const eventName of actionableEvents) {
      if (typeof element[`on${eventName}`] === "function" || element.hasAttribute(`on${eventName}`)) {
        found.push(eventName);
      }
    }
    return found;
  }

  function eventAncestor(element) {
    let current = element.parentElement;
    while (current && current !== document.body) {
      if (eventTypesFor(current).length) return current;
      current = current.parentElement;
    }
    return null;
  }

  function directActionable(element) {
    const tag = element.tagName.toLowerCase();
    const type = clean(element.getAttribute("type")).toLowerCase();
    const role = roleFor(element, tag, type);
    const eventTypes = eventTypesFor(element);
    const style = window.getComputedStyle(element);
    const semantic = actionableTags.has(tag) || actionableRoles.has(role) || element.isContentEditable;
    const pointer = style.cursor === "pointer";
    return semantic || pointer || eventTypes.length || type === "file";
  }

  function hasActionableAncestor(element) {
    let current = element.parentElement;
    while (current && current !== document.body) {
      if (directActionable(current)) return true;
      current = current.parentElement;
    }
    return false;
  }

  function actionTypeFor(element, tag, role, type, eventTypes) {
    if (type === "file") return "upload";
    if (tag === "select") return "select";
    if (tag === "textarea" || element.isContentEditable || role === "textbox" || role === "searchbox") return "fill";
    if (tag === "input" && !["button", "submit", "reset", "checkbox", "radio"].includes(type)) return "fill";
    if (tag === "a" || role === "link") return "navigate";
    if (tag === "form" || eventTypes.includes("submit")) return "submit";
    return "click";
  }

  function sectionFor(element) {
    const start = element.parentElement || element;
    const section = start.closest("aside, nav, main, header, footer, form, section, [aria-label], [role='dialog'], [role='navigation'], [role='main']");
    if (!section) return "";
    const aria = clean(section.getAttribute("aria-label"));
    if (aria) return aria.slice(0, 80);
    const heading = section.querySelector("h1,h2,h3,[role='heading']");
    if (heading) return clean(heading.innerText || heading.textContent).slice(0, 80);
    return section.tagName.toLowerCase();
  }

  function stateNoteFor(element, enabled, visibleNow, type) {
    if (!enabled) return "disabled; requires prerequisite input or state before clicking";
    if (type === "file" && !visibleNow) return "hidden file input; use set_input_files directly or click the visible attachment control first";
    return "";
  }

  const elements = Array.from(document.querySelectorAll("*"));
  const rows = [];
  for (const element of elements) {
    const tag = element.tagName.toLowerCase();
    if (noisyTags.has(tag)) continue;
    const type = clean(element.getAttribute("type")).toLowerCase();
    const visibleNow = visible(element);
    if (!visibleNow && !(tag === "input" && type === "file")) continue;
    if (hasActionableAncestor(element) && !["input", "select", "textarea", "label"].includes(tag) && !element.isContentEditable) continue;
    const role = roleFor(element, tag, type);
    const eventTypes = eventTypesFor(element);
    const style = window.getComputedStyle(element);
    const semantic = actionableTags.has(tag) || actionableRoles.has(role);
    const pointer = style.cursor === "pointer";
    const delegated = !eventTypes.length && eventAncestor(element);
    if (!semantic && !pointer && !eventTypes.length && !delegated && type !== "file" && !element.isContentEditable) continue;
    const rect = element.getBoundingClientRect();
    const selector = selectorFor(element);
    const enabled = !(element.disabled || element.getAttribute("aria-disabled") === "true");
    const label = accessibleName(element).slice(0, 160);
    const actionType = actionTypeFor(element, tag, role, type, eventTypes);
    rows.push({
      tag,
      role,
      label,
      text: clean(element.innerText || element.textContent).slice(0, 240),
      selector,
      href: element.href || element.getAttribute("href") || "",
      type,
      visible: visibleNow,
      enabled,
      eventTypes,
      actionType,
      section: sectionFor(element),
      stateNote: stateNoteFor(element, enabled, visibleNow, type),
      detection: eventTypes.length
        ? "inline_handler"
        : semantic
          ? "semantic_control"
          : delegated
            ? "delegated_ancestor"
            : "heuristic",
      boundingBox: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
    });
  }
  return rows;
}
"""


@dataclass(frozen=True)
class RenderedPage:
    url: str
    title: str
    screenshot_path: str | None
    html_path: str
    text_path: str


@dataclass(frozen=True)
class ActionableElement:
    id: str
    tag: str
    role: str
    label: str
    text: str
    selector: str | None
    event_types: tuple[str, ...]
    detection: str
    enabled: bool
    visible: bool
    bounding_box: dict[str, int]
    suggested_intent: str
    code_hint: str
    action_type: str = "click"
    section: str = ""
    locator_hint: str = ""
    state_note: str = ""
    priority: int = 100


@dataclass(frozen=True)
class CandidateFlow:
    id: str
    title: str
    action_type: str
    priority: int
    element_ids: tuple[str, ...]
    precondition: str
    action_hints: tuple[str, ...]
    code_hint: str


@dataclass(frozen=True)
class DevInspectionContext:
    file: str
    journey: str
    case_id: str
    paused_step: str
    paused_step_result_name: str


@dataclass(frozen=True)
class ExtensionInstruction:
    summary: str
    step_function_template: str
    journey_insertion_template: str
    verification_commands: tuple[str, ...]


@dataclass(frozen=True)
class DevInspectionResult:
    status: str
    context: DevInspectionContext
    artifact_dir: str
    rendered_page: RenderedPage
    actionable_elements: tuple[ActionableElement, ...]
    extension_instructions: ExtensionInstruction
    candidate_flows: tuple[CandidateFlow, ...] = ()
    dev_result_path: str | None = None

    def to_log_fields(self) -> dict[str, object]:
        return {
            "status": self.status,
            "file": self.context.file,
            "journey": self.context.journey,
            "case_id": self.context.case_id,
            "paused_step": self.context.paused_step,
            "artifact_dir": self.artifact_dir,
            "dev_result_path": self.dev_result_path,
            "rendered_page": {
                "url": self.rendered_page.url,
                "title": self.rendered_page.title,
                "screenshot_path": self.rendered_page.screenshot_path,
                "html_path": self.rendered_page.html_path,
                "text_path": self.rendered_page.text_path,
            },
            "candidate_flows": [
                {
                    "id": flow.id,
                    "title": flow.title,
                    "action_type": flow.action_type,
                    "priority": flow.priority,
                    "element_ids": list(flow.element_ids),
                    "precondition": flow.precondition,
                    "action_hints": list(flow.action_hints),
                    "code_hint": flow.code_hint,
                }
                for flow in self.candidate_flows
            ],
            "actionable_elements": [
                {
                    "id": element.id,
                    "tag": element.tag,
                    "role": element.role,
                    "label": element.label,
                    "text": element.text,
                    "selector": element.selector,
                    "event_types": list(element.event_types),
                    "detection": element.detection,
                    "enabled": element.enabled,
                    "visible": element.visible,
                    "bounding_box": element.bounding_box,
                    "suggested_intent": element.suggested_intent,
                    "code_hint": element.code_hint,
                    "action_type": element.action_type,
                    "section": element.section,
                    "locator_hint": element.locator_hint,
                    "state_note": element.state_note,
                    "priority": element.priority,
                }
                for element in self.actionable_elements
            ],
            "extension_instructions": {
                "summary": self.extension_instructions.summary,
                "step_function_template": self.extension_instructions.step_function_template,
                "journey_insertion_template": self.extension_instructions.journey_insertion_template,
                "verification_commands": list(self.extension_instructions.verification_commands),
            },
        }


def inspect_dev_page(
    page: object,
    *,
    context: DevInspectionContext,
    artifact_root: Path,
) -> DevInspectionResult:
    artifact_dir = artifact_root / f"{int(time.time() * 1000)}-{_safe_segment(context.paused_step)}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    url = _safe_attr(page, "url")
    title = _safe_call(page, "title") or ""
    html = _safe_page_content(page)
    text = _safe_visible_text(page)
    screenshot_path = _write_screenshot(page, artifact_dir / "page.png")

    html_path = artifact_dir / "page.html"
    text_path = artifact_dir / "visible-text.txt"
    html_path.write_text(html, encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")

    elements = _actionable_elements(page)
    candidate_flows = _candidate_flows(elements)
    instruction = _extension_instruction(
        context,
        elements=elements,
        candidate_flows=candidate_flows,
    )
    dev_result_path = artifact_dir / "dev_result.json"
    result = DevInspectionResult(
        status="paused",
        context=context,
        artifact_dir=str(artifact_dir),
        dev_result_path=str(dev_result_path),
        rendered_page=RenderedPage(
            url=url,
            title=title,
            screenshot_path=str(screenshot_path) if screenshot_path is not None else None,
            html_path=str(html_path),
            text_path=str(text_path),
        ),
        actionable_elements=tuple(elements),
        candidate_flows=tuple(candidate_flows),
        extension_instructions=instruction,
    )
    dev_result_path.write_text(
        json.dumps(result.to_log_fields(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def page_from_execution_result(
    result: object,
    side_outputs: dict[str, Sequence[object]],
) -> object | None:
    from .touchpoints.browser import JourneyBrowserPage

    if isinstance(result, JourneyBrowserPage):
        return result
    pages: list[object] = []
    for values in side_outputs.values():
        pages.extend(value for value in values if isinstance(value, JourneyBrowserPage))
    return pages[-1] if pages else None


def render_dev_pretty(result: DevInspectionResult) -> list[PrettyLine]:
    lines: list[PrettyLine] = [
        pretty_line("Journey dev paused", style="heading"),
        pretty_row("Page", f"{result.rendered_page.title or '<untitled>'} {result.rendered_page.url}", indent=2),
        pretty_line("Rendered page artifacts", indent=2, style="heading"),
        pretty_row("Directory", result.artifact_dir, indent=2),
    ]
    if result.rendered_page.screenshot_path:
        lines.append(pretty_row("Screenshot", result.rendered_page.screenshot_path, indent=2))
    lines.extend(
        [
            pretty_row("HTML", result.rendered_page.html_path, indent=2),
            pretty_row("Visible text", result.rendered_page.text_path, indent=2),
        ]
    )
    if result.dev_result_path:
        lines.append(pretty_row("Structured result", result.dev_result_path, indent=2))
    lines.append(pretty_line("Candidate flows", indent=2, style="heading"))
    if not result.candidate_flows:
        lines.append(pretty_line("  none found", indent=2, style="muted"))
    for flow in result.candidate_flows[:10]:
        detail = f"{flow.id}: {flow.title} [{flow.action_type}] priority={flow.priority}"
        if flow.precondition:
            detail += f" note={flow.precondition}"
        lines.append(pretty_row("Flow", detail, indent=2))
        if flow.action_hints:
            lines.append(pretty_row("Hint", " ; ".join(flow.action_hints), indent=4))
    lines.append(pretty_line("Actionable controls", indent=2, style="heading"))
    if not result.actionable_elements:
        lines.append(pretty_line("  none found", indent=2, style="muted"))
    for element in result.actionable_elements[:25]:
        label = element.label or element.text or element.selector or element.tag
        detail = f"{element.id}: {label} [{element.action_type} {element.detection}] priority={element.priority}"
        if element.event_types:
            detail += f" events={','.join(element.event_types)}"
        if element.state_note:
            detail += f" note={element.state_note}"
        if element.locator_hint:
            detail += f" locator={element.locator_hint}"
        elif element.selector:
            detail += f" selector={element.selector}"
        lines.append(pretty_row("Control", detail, indent=2))
    lines.extend(
        [
            pretty_line("Extend this journey", indent=2, style="heading"),
            pretty_line(result.extension_instructions.summary, indent=4),
            pretty_line("Step template:", indent=4, style="heading"),
            pretty_line(result.extension_instructions.step_function_template, indent=6, style="code"),
            pretty_line("Journey insertion:", indent=4, style="heading"),
            pretty_line(result.extension_instructions.journey_insertion_template, indent=6, style="code"),
            pretty_line("Next commands:", indent=4, style="heading"),
        ]
    )
    for command in result.extension_instructions.verification_commands:
        lines.append(pretty_line(command, indent=6, style="code"))
    return lines


def _actionable_elements(page: object) -> list[ActionableElement]:
    payload: object
    try:
        evaluate = getattr(page, "evaluate")
        payload = evaluate(_ACTIONABLE_SCRIPT)
    except Exception:
        payload = []
    if not isinstance(payload, list):
        return []

    elements: list[ActionableElement] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            continue
        selector = _optional_string(raw.get("selector"))
        tag = _string(raw.get("tag")) or "element"
        if tag in {"svg", "path", "circle", "rect", "line", "polyline", "polygon", "g", "use"}:
            continue
        role = _string(raw.get("role"))
        text = _string(raw.get("text"))
        label = _best_label(
            label=_string(raw.get("label")),
            text=text,
            selector=selector,
            tag=tag,
            role=role,
        )
        text = _string(raw.get("text"))
        action_type = _action_type(raw, tag=tag, role=role)
        section = _string(raw.get("section"))
        state_note = _string(raw.get("stateNote"))
        locator_hint = _locator_hint(
            selector=selector,
            label=label,
            tag=tag,
            role=role,
            action_type=action_type,
        )
        key = _element_key(
            selector=selector,
            label=label,
            text=text,
            tag=tag,
            role=role,
            action_type=action_type,
        )
        if key in seen:
            continue
        seen.add(key)
        event_types = tuple(
            item for item in raw.get("eventTypes", ()) if isinstance(item, str)
        )
        bounding_box = raw.get("boundingBox")
        if not isinstance(bounding_box, dict):
            bounding_box = {}
        element_id = f"element_{len(elements) + 1}"
        enabled = bool(raw.get("enabled", True))
        visible = bool(raw.get("visible", True))
        priority = _element_priority(
            label=label,
            text=text,
            selector=selector,
            tag=tag,
            role=role,
            action_type=action_type,
            enabled=enabled,
            visible=visible,
        )
        code_hint = _code_hint(
            locator_hint=locator_hint,
            selector=selector,
            label=label,
            tag=tag,
            role=role,
            action_type=action_type,
        )
        elements.append(
            ActionableElement(
                id=element_id,
                tag=tag,
                role=role,
                label=label,
                text=text,
                selector=selector,
                event_types=event_types,
                detection=_string(raw.get("detection")) or "heuristic",
                enabled=enabled,
                visible=visible,
                bounding_box={
                    str(key): int(value)
                    for key, value in bounding_box.items()
                    if isinstance(value, int | float)
                },
                suggested_intent=_suggested_intent(label or text or selector or tag),
                code_hint=code_hint,
                action_type=action_type,
                section=section,
                locator_hint=locator_hint,
                state_note=state_note,
                priority=priority,
            )
        )
    elements.sort(key=lambda element: (element.priority, element.id))
    return _renumber_elements(elements)


def _renumber_elements(elements: Sequence[ActionableElement]) -> list[ActionableElement]:
    return [
        replace(element, id=f"element_{index}")
        for index, element in enumerate(elements, start=1)
    ]


def _candidate_flows(elements: Sequence[ActionableElement]) -> list[CandidateFlow]:
    flows: list[CandidateFlow] = []
    used_signatures: set[str] = set()

    def add_flow(
        *,
        title: str,
        action_type: str,
        priority: int,
        element_ids: Sequence[str],
        action_hints: Sequence[str],
        precondition: str = "",
    ) -> None:
        signature = _normalized_intent(title)
        if not signature or signature in used_signatures:
            return
        used_signatures.add(signature)
        flow_id = f"flow_{len(flows) + 1}"
        hints = tuple(hint for hint in action_hints if hint)
        flows.append(
            CandidateFlow(
                id=flow_id,
                title=title,
                action_type=action_type,
                priority=priority,
                element_ids=tuple(element_ids),
                precondition=precondition,
                action_hints=hints,
                code_hint="\n".join(hints),
            )
        )

    composer = _find_element(elements, _looks_like_composer)
    send = _find_element(elements, _looks_like_send)
    if composer is not None and send is not None:
        add_flow(
            title="Send message through composer",
            action_type="compose_and_submit",
            priority=10,
            element_ids=(composer.id, send.id),
            precondition=send.state_note or "fill the composer before sending",
            action_hints=(
                _fill_hint(composer, "I need a plumber to fix a leaking tap"),
                _click_hint(send),
            ),
        )

    file_input = _find_element(elements, lambda element: element.action_type == "upload")
    attach = _find_element(elements, _looks_like_attach)
    if file_input is not None:
        add_flow(
            title="Upload attachment",
            action_type="upload",
            priority=18,
            element_ids=tuple(
                element.id
                for element in (file_input, attach)
                if element is not None
            ),
            precondition=file_input.state_note,
            action_hints=(_upload_hint(file_input),),
        )
    elif attach is not None:
        add_flow(
            title="Upload attachment",
            action_type="upload",
            priority=20,
            element_ids=(attach.id,),
            precondition="click the attachment control, then set the selected file input",
            action_hints=(_click_hint(attach),),
        )

    for element in elements:
        title = _flow_title_for_element(element)
        if title is None:
            continue
        add_flow(
            title=title,
            action_type=element.action_type,
            priority=_flow_priority(element, title),
            element_ids=(element.id,),
            precondition=element.state_note,
            action_hints=(_action_hint(element),),
        )

    flows.sort(key=lambda flow: (flow.priority, flow.id))
    return [
        replace(flow, id=f"flow_{index}")
        for index, flow in enumerate(flows, start=1)
    ]


def _find_element(
    elements: Sequence[ActionableElement],
    predicate: object,
) -> ActionableElement | None:
    if not callable(predicate):
        return None
    for element in elements:
        if predicate(element):
            return element
    return None


def _looks_like_composer(element: ActionableElement) -> bool:
    value = _searchable_element_text(element)
    return element.action_type == "fill" and (
        "composer" in value
        or "type or tap" in value
        or "message" in value
        or element.role in {"textbox", "searchbox"}
    )


def _looks_like_send(element: ActionableElement) -> bool:
    value = _searchable_element_text(element)
    return "send" in value and ("message" in value or "send-message" in value)


def _looks_like_attach(element: ActionableElement) -> bool:
    value = _searchable_element_text(element)
    return (
        "attach" in value
        or "attachment" in value
        or "file" in value
    ) and element.action_type == "click"


def _flow_title_for_element(element: ActionableElement) -> str | None:
    value = _searchable_element_text(element)
    label = element.label or element.text
    if element.action_type == "upload" or _is_generic_label(label):
        return None
    if _looks_like_attach(element):
        return None
    if "new chat" in value or "start chat" in value:
        return "Start a new chat"
    if re.search(r"\bsign\s*up\b", value):
        return "Sign up"
    if re.search(r"\blog\s*in\b|\bsign\s*in\b", value):
        return "Log in"
    if "premium" in value and "benefit" in value:
        return "See premium benefits"
    if label and element.priority <= 55 and element.action_type != "fill":
        return label[:80]
    return None


def _flow_priority(element: ActionableElement, title: str) -> int:
    normalized = _normalized_intent(title)
    if normalized == "start a new chat":
        return 6
    if normalized in {"sign up", "log in"}:
        return 14
    if "premium" in normalized:
        return 35
    return min(element.priority + 5, 95)


def _action_hint(element: ActionableElement) -> str:
    if element.action_type == "upload":
        return _upload_hint(element)
    if element.action_type == "fill":
        return _fill_hint(element, "example text")
    if element.action_type == "select":
        return _select_hint(element)
    return _click_hint(element)


def _click_hint(element: ActionableElement) -> str:
    target = element.locator_hint or _fallback_locator(element)
    return f"{target}.click(timeout=timeout_ms)"


def _fill_hint(element: ActionableElement, value: str) -> str:
    target = element.locator_hint or _fallback_locator(element)
    return f"{target}.fill({value!r}, timeout=timeout_ms)"


def _select_hint(element: ActionableElement) -> str:
    target = element.locator_hint or _fallback_locator(element)
    return f"{target}.select_option('value')"


def _upload_hint(element: ActionableElement) -> str:
    target = element.locator_hint or _fallback_locator(element)
    return f"{target}.set_input_files('path/to/fixture.txt')"


def _fallback_locator(element: ActionableElement) -> str:
    if element.selector:
        return f"page.locator({element.selector!r})"
    if element.role and element.label and not _is_generic_label(element.label):
        return f"page.get_by_role({element.role!r}, name={element.label!r})"
    if element.label:
        return f"page.get_by_text({element.label!r})"
    return "page.locator('<selector>')"


def _best_label(
    *,
    label: str,
    text: str,
    selector: str | None,
    tag: str,
    role: str,
) -> str:
    if label and not (_is_generic_label(label) and text):
        return label
    if text:
        return text[:160]
    if selector:
        testid = _testid_from_selector(selector)
        if testid:
            return _humanize_identifier(testid)
    return role or tag


def _action_type(raw: dict[str, object], *, tag: str, role: str) -> str:
    value = _string(raw.get("actionType"))
    if value:
        return value
    type_value = _string(raw.get("type"))
    if type_value == "file":
        return "upload"
    if tag == "select":
        return "select"
    if tag in {"textarea", "input"} or role in {"textbox", "searchbox"}:
        return "fill"
    if tag == "a" or role == "link":
        return "navigate"
    return "click"


def _element_key(
    *,
    selector: str | None,
    label: str,
    text: str,
    tag: str,
    role: str,
    action_type: str,
) -> str:
    intent = _normalized_intent(label or text or selector or tag)
    if selector:
        return f"{action_type}:{selector}"
    return f"{action_type}:{tag}:{role}:{intent}"


def _element_priority(
    *,
    label: str,
    text: str,
    selector: str | None,
    tag: str,
    role: str,
    action_type: str,
    enabled: bool,
    visible: bool,
) -> int:
    value = _normalized_intent(" ".join(part for part in (label, text, selector or "") if part))
    priority = 50
    if selector and "data-testid" in selector:
        priority -= 18
    elif selector and selector.startswith("#"):
        priority -= 12
    if role in {"button", "link", "textbox"} or tag in {"button", "a", "input", "textarea"}:
        priority -= 8
    if "new chat" in value or "start chat" in value:
        priority -= 20
    if "sign up" in value or "log in" in value or "sign in" in value:
        priority -= 18
    if "composer" in value or "send message" in value or "file-input" in value or "attach" in value:
        priority -= 16
    if action_type == "upload":
        priority -= 15
    if not enabled:
        priority += 18
    if not visible:
        priority += 10
    if _is_generic_label(label) and not text:
        priority += 20
    return max(1, min(priority, 100))


def _locator_hint(
    *,
    selector: str | None,
    label: str,
    tag: str,
    role: str,
    action_type: str,
) -> str:
    del tag, action_type
    if selector:
        testid = _testid_from_selector(selector)
        if testid:
            return f"page.get_by_test_id({testid!r})"
        if selector.startswith("#") or selector.startswith("[") or ">" in selector:
            if role and label and not _is_generic_label(label) and not selector.startswith("[data-testid="):
                return f"page.get_by_role({role!r}, name={label!r})"
            return f"page.locator({selector!r})"
    if role and label and not _is_generic_label(label):
        return f"page.get_by_role({role!r}, name={label!r})"
    if label:
        return f"page.get_by_text({label!r})"
    return ""


def _searchable_element_text(element: ActionableElement) -> str:
    return _normalized_intent(
        " ".join(
            part
            for part in (
                element.label,
                element.text,
                element.selector or "",
                element.locator_hint,
                element.section,
            )
            if part
        )
    )


def _normalized_intent(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", value.lower())).strip()


def _is_generic_label(value: str) -> bool:
    return _normalized_intent(value) in {"button", "link", "input", "control", "item", "menu item"}


def _testid_from_selector(selector: str) -> str | None:
    match = re.fullmatch(r"""\[data-testid=(["'])(.+)\1\]""", selector)
    return match.group(2) if match else None


def _humanize_identifier(value: str) -> str:
    text = re.sub(r"[_-]+", " ", value).strip()
    return text[:1].upper() + text[1:] if text else value


def _extension_instruction(
    context: DevInspectionContext,
    *,
    elements: Sequence[ActionableElement],
    candidate_flows: Sequence[CandidateFlow],
) -> ExtensionInstruction:
    first_flow = candidate_flows[0] if candidate_flows else None
    first_element = elements[0] if elements else None
    function_name = _function_name_for(first_flow or first_element)
    action_lines = (
        tuple(first_flow.action_hints)
        if first_flow is not None
        else (
            (_action_hint(first_element),)
            if first_element is not None
            else ("# Choose one candidate flow from dev_result.candidate_flows and act on it.",)
        )
    )
    rendered_action_lines = "\n".join(f"    {line}" for line in action_lines)
    step_template = (
        f"def {function_name}(saved_page: JourneyBrowserPage) -> JourneyBrowserPage:\n"
        "    page = open_page(saved_page)\n"
        "    timeout_ms = 30000\n"
        f"{rendered_action_lines}\n"
        "    page.wait_for_load_state(\"load\", timeout=timeout_ms)\n"
        "    return page"
    )
    insertion = (
        f"if branch(replay_from={context.paused_step_result_name}):\n"
        f"    {function_name}_page = step({function_name}, {context.paused_step_result_name})"
    )
    return ExtensionInstruction(
        summary=(
            "Prefer a candidate flow from `dev_result.candidate_flows`, inspect the rendered-page "
            "artifacts when uncertain, then call the new step in a branch immediately after "
            f"`{context.paused_step}` using `replay_from`."
        ),
        step_function_template=step_template,
        journey_insertion_template=insertion,
        verification_commands=(
            f"journey loop {function_name} --file {context.file}"
            + (f" --journey {context.journey}" if context.journey else ""),
            f"journey verify --step {function_name} --file {context.file}"
            + (f" --journey {context.journey}" if context.journey else ""),
            f"journey verify --file {context.file}"
            + (f" --journey {context.journey}" if context.journey else ""),
        ),
    )


def _safe_page_content(page: object) -> str:
    return _safe_call(page, "content") or ""


def _safe_visible_text(page: object) -> str:
    try:
        locator = getattr(page, "locator")("body")
        text = locator.inner_text(timeout=1000)
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


def _write_screenshot(page: object, path: Path) -> Path | None:
    try:
        screenshot = getattr(page, "screenshot")
        screenshot(path=str(path), full_page=True)
        return path if path.exists() else None
    except Exception:
        return None


def _safe_attr(value: object, name: str) -> str:
    try:
        result = getattr(value, name)
    except Exception:
        return ""
    return result if isinstance(result, str) else ""


def _safe_call(value: object, name: str) -> str | None:
    try:
        method = getattr(value, name)
        result = method()
    except Exception:
        return None
    return result if isinstance(result, str) else None


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_string(value: object) -> str | None:
    text = _string(value)
    return text or None


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return cleaned[:80] or "step"


def _suggested_intent(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return "Interact with element"
    return f"Interact with {normalized[:80]}"


def _function_name_for(element: ActionableElement | CandidateFlow | None) -> str:
    raw = "open_next_branch"
    if isinstance(element, CandidateFlow):
        raw = element.title
    elif element is not None:
        raw = element.label or element.text or element.suggested_intent
    value = re.sub(r"[^0-9A-Za-z_]+", "_", raw.strip().lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    if not value:
        value = "open_next_branch"
    if value[0].isdigit():
        value = f"journey_{value}"
    if not value.startswith(("open_", "click_", "submit_", "fill_", "upload_")):
        value = f"open_{value}"
    return value[:80]


def _code_hint(
    *,
    locator_hint: str,
    selector: str | None,
    label: str,
    tag: str,
    role: str,
    action_type: str,
) -> str:
    target = locator_hint
    if not target and selector:
        target = f"page.locator({selector!r})"
    if not target and role and label:
        target = f"page.get_by_role({role!r}, name={label!r})"
    if not target and label:
        target = f"page.get_by_text({label!r})"
    if target:
        if action_type == "upload":
            return f"{target}.set_input_files('path/to/fixture.txt')"
        if action_type == "fill":
            return f"{target}.fill('example text', timeout=timeout_ms)"
        if action_type == "select":
            return f"{target}.select_option('value')"
        return f"{target}.click(timeout=timeout_ms)"
    return f"# Inspect {json.dumps(tag)} and choose a stable selector."
