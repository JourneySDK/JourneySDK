"""Internal browser explorer for generating Journey specs."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import tempfile
import textwrap
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
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
from journeysdk.touchpoints.browser import ensure_browser_installed


JOURNEY_EXPLORE_MODEL_ENV = "JOURNEY_BROWSER_PROMPT_MODEL"
DEFAULT_JOURNEY_EXPLORE_MODEL = "anthropic:claude-haiku-4-5"
_SUPPORTED_BROWSERS = {"chromium", "firefox", "webkit"}
_LOGGER = get_logger("explore")
_MAX_OBSERVATION_TEXT_LENGTH = 8000
_DEFAULT_CANDIDATES_PER_STATE = 5


@dataclass(frozen=True)
class ExploreOptions:
    urls: tuple[str, ...]
    output_file: Path = Path("journeys/explored_journey.py")
    journey_name: str = "explored_journey"
    depth: int = 4
    max_actions: int = 30
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    model: str | None = None
    allow_external: bool = False
    force: bool = False
    action_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str
    visible_text: str
    semantic_dom: str
    signature: str


@dataclass(frozen=True)
class CandidateAction:
    name: str
    description: str
    code: str


@dataclass
class ExploredNode:
    node_id: str
    start_url: str
    snapshot: PageSnapshot
    depth: int
    path: tuple["ExploredEdge", ...] = ()
    edges: list["ExploredEdge"] = field(default_factory=list)


@dataclass
class ExploredEdge:
    edge_id: str
    parent: ExploredNode
    action: CandidateAction
    snapshot: PageSnapshot
    child: ExploredNode | None = None
    function_name: str = ""


@dataclass(frozen=True)
class ExploreResult:
    output_file: Path
    journey_name: str
    roots: tuple[ExploredNode, ...]
    source: str
    actions: int
    branches: int
    omitted_actions: int
    model: str


class ActionProvider(Protocol):
    def propose_actions(
        self,
        snapshot: PageSnapshot,
        *,
        path: tuple[ExploredEdge, ...],
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
        path: tuple[ExploredEdge, ...],
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
            {"role": "system", "content": _EXPLORE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = self._usage_tracker.call(
            operation="explore_actions",
            configured_model=self._model_name,
            logger=_LOGGER,
            callback=lambda config: self._model.invoke(messages, config=config),
        )
        response_text = _extract_langchain_text(
            response,
            owner="journey explore",
        )
        return parse_candidate_actions(response_text)[
            : min(self._candidates_per_state, remaining_actions)
        ]


def explore(
    options: ExploreOptions,
    *,
    action_provider: ActionProvider | None = None,
) -> ExploreResult:
    normalized = _normalize_options(options)
    if normalized.output_file.exists() and not normalized.force:
        raise FileExistsError(
            f"Journey explore output already exists: {normalized.output_file}. "
            "Pass --force to replace it."
        )

    model = _resolve_explore_model(normalized.model)
    provider = action_provider or ModelActionProvider(model=model)
    ensure_browser_installed(normalized.browser)

    roots: list[ExploredNode] = []
    omitted_actions = 0
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, normalized.browser)
        browser = browser_type.launch(
            headless=normalized.headless,
            handle_sigint=False,
        )
        try:
            for index, start_url in enumerate(normalized.urls, start=1):
                root, omitted = _explore_start_url(
                    browser,
                    start_url=start_url,
                    options=normalized,
                    provider=provider,
                    node_prefix=f"start_{index}_",
                )
                roots.append(root)
                omitted_actions += omitted
        finally:
            browser.close()

    source = render_journey_source(
        tuple(roots),
        journey_name=normalized.journey_name,
    )
    validate_generated_source(source, journey_name=normalized.journey_name)
    normalized.output_file.parent.mkdir(parents=True, exist_ok=True)
    normalized.output_file.write_text(source, encoding="utf-8")

    actions = sum(_iter_edge_count(root) for root in roots)
    branches = sum(1 for node in _iter_nodes(roots) if len(node.edges) > 1)
    result = ExploreResult(
        output_file=normalized.output_file,
        journey_name=normalized.journey_name,
        roots=tuple(roots),
        source=source,
        actions=actions,
        branches=branches,
        omitted_actions=omitted_actions,
        model=model,
    )
    _LOGGER.info(
        "explore_success",
        "journey explore generated a Journey spec",
        pretty=pretty_row(
            "Explore",
            f"wrote {result.output_file} actions={actions} branches={branches}",
            indent=8,
            label_width=27,
            style="success",
        ),
        output_file=str(result.output_file),
        journey_name=result.journey_name,
        actions=actions,
        branches=branches,
        omitted_actions=omitted_actions,
        model=model,
    )
    return result


def parse_candidate_actions(text: str) -> list[CandidateAction]:
    payload = _extract_json_payload(text)
    raw_actions = payload.get("actions") if isinstance(payload, dict) else payload
    if not isinstance(raw_actions, list):
        raise RuntimeError("journey explore expected model JSON with an actions list.")

    actions: list[CandidateAction] = []
    for index, item in enumerate(raw_actions, start=1):
        if not isinstance(item, dict):
            continue
        code = _strip_code_fences(str(item.get("code") or "")).strip()
        if not code:
            continue
        name = _clean_action_text(str(item.get("name") or f"action_{index}"))
        description = _clean_action_text(str(item.get("description") or name))
        _validate_explore_python_code(code)
        actions.append(CandidateAction(name=name, description=description, code=code))
    return actions


def render_journey_source(
    roots: tuple[ExploredNode, ...],
    *,
    journey_name: str,
) -> str:
    if not roots:
        raise ValueError("render_journey_source(...) needs at least one explored root.")
    allocator = _NameAllocator()
    journey_name = _sanitize_identifier(journey_name, default="explored_journey")
    root_functions = {
        root.node_id: allocator.allocate(f"open_{_state_slug(root.snapshot)}")
        for root in roots
    }
    for edge in _iter_edges(roots):
        edge.function_name = allocator.allocate(edge.action.name)

    lines: list[str] = [
        '"""Generated by `journey explore`. Review before committing."""',
        "",
        "from __future__ import annotations",
        "",
        "from urllib.parse import urlsplit",
        "from uuid import uuid4",
        "",
        "from journeysdk import branch, journey, step",
        "from journeysdk.touchpoints.browser import JourneyBrowserPage, open_page",
        "",
        "",
        "def _unique_email(prefix: str) -> str:",
        "    return f\"{prefix}-{uuid4().hex[:8]}@example.test\"",
        "",
        "",
        "def _assert_page_state(",
        "    page: JourneyBrowserPage,",
        "    *,",
        "    expected_path: str | None = None,",
        "    expected_title: str | None = None,",
        ") -> None:",
        "    if expected_path is not None:",
        "        actual_path = urlsplit(page.url).path or \"/\"",
        "        if actual_path != expected_path:",
        "            raise AssertionError(f\"Expected path {expected_path!r}, got {actual_path!r} from {page.url!r}.\")",
        "    if expected_title is not None:",
        "        actual_title = page.title()",
        "        if actual_title != expected_title:",
        "            raise AssertionError(f\"Expected title {expected_title!r}, got {actual_title!r}.\")",
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


def validate_generated_source(source: str, *, journey_name: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError(f"journey explore generated invalid Python: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="journey-explore-") as temp_dir:
        path = Path(temp_dir) / "generated_journey.py"
        path.write_text(source, encoding="utf-8")
        module = _import_generated_module(path)
        journey_fn = getattr(module, journey_name, None)
        if not is_journey_callable(journey_fn):
            raise RuntimeError(
                f"journey explore generated source without {journey_name!r} Journey entrypoint."
            )
        compile_journey(journey_fn)


def _explore_start_url(
    browser: object,
    *,
    start_url: str,
    options: ExploreOptions,
    provider: ActionProvider,
    node_prefix: str,
) -> tuple[ExploredNode, int]:
    root_snapshot = _snapshot_for_path(browser, start_url, ())
    root = ExploredNode(
        node_id=f"{node_prefix}node_1",
        start_url=start_url,
        snapshot=root_snapshot,
        depth=0,
    )
    queue: deque[ExploredNode] = deque([root])
    seen_signatures = {root_snapshot.signature}
    node_sequence = 1
    edge_sequence = 0
    action_count = 0
    omitted_actions = 0
    allowed_origins = {_origin(url) for url in options.urls}

    while queue and action_count < options.max_actions:
        node = queue.popleft()
        if node.depth >= options.depth:
            continue
        remaining = options.max_actions - action_count
        candidates = provider.propose_actions(
            node.snapshot,
            path=node.path,
            remaining_actions=remaining,
            depth_remaining=options.depth - node.depth,
        )
        if not candidates:
            continue
        for candidate in candidates:
            if action_count >= options.max_actions:
                break
            try:
                snapshot = _snapshot_after_action(
                    browser,
                    start_url=node.start_url,
                    path=node.path,
                    action=candidate,
                    timeout_seconds=options.action_timeout_seconds,
                )
            except Exception as exc:
                omitted_actions += 1
                _LOGGER.warning(
                    "explore_action_omitted",
                    "journey explore omitted a failed action",
                    pretty=pretty_row(
                        "Explore",
                        f"omitted {candidate.name}: {_format_exception(exc)}",
                        indent=8,
                        label_width=27,
                        style="warning",
                    ),
                    action=candidate.name,
                    error=_format_exception(exc),
                )
                continue
            if not options.allow_external and _origin(snapshot.url) not in allowed_origins:
                omitted_actions += 1
                _LOGGER.warning(
                    "explore_external_omitted",
                    "journey explore omitted an external navigation",
                    pretty=pretty_row(
                        "Explore",
                        f"omitted external {snapshot.url}",
                        indent=8,
                        label_width=27,
                        style="warning",
                    ),
                    action=candidate.name,
                    url=snapshot.url,
                )
                continue

            edge_sequence += 1
            action_count += 1
            edge = ExploredEdge(
                edge_id=f"{node_prefix}edge_{edge_sequence}",
                parent=node,
                action=candidate,
                snapshot=snapshot,
            )
            node.edges.append(edge)

            if snapshot.signature not in seen_signatures:
                seen_signatures.add(snapshot.signature)
                node_sequence += 1
                child = ExploredNode(
                    node_id=f"{node_prefix}node_{node_sequence}",
                    start_url=node.start_url,
                    snapshot=snapshot,
                    depth=node.depth + 1,
                    path=(*node.path, edge),
                )
                edge.child = child
                queue.append(child)
    return root, omitted_actions


def _snapshot_for_path(
    browser: object,
    start_url: str,
    path: tuple[ExploredEdge, ...],
) -> PageSnapshot:
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(start_url, wait_until="load", timeout=30_000)
        _settle_page(page)
        active_page = page
        for edge in path:
            active_page = _execute_explore_code(
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
    start_url: str,
    path: tuple[ExploredEdge, ...],
    action: CandidateAction,
    timeout_seconds: float,
) -> PageSnapshot:
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(start_url, wait_until="load", timeout=int(timeout_seconds * 1000))
        _settle_page(page)
        active_page = page
        for edge in path:
            active_page = _execute_explore_code(
                active_page,
                edge.action.code,
                timeout_seconds=timeout_seconds,
            )
            _settle_page(active_page)
        active_page = _execute_explore_code(
            active_page,
            action.code,
            timeout_seconds=timeout_seconds,
        )
        _settle_page(active_page)
        return _snapshot_page(active_page)
    finally:
        context.close()


def _execute_explore_code(
    page: PlaywrightPage,
    code: str,
    *,
    timeout_seconds: float,
) -> PlaywrightPage:
    normalized = _strip_code_fences(code).strip()
    _validate_explore_python_code(normalized)
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
            raise RuntimeError("journey explore switch_page index must be an integer.")
        if index < 0 or index >= len(pages):
            raise RuntimeError(
                f"journey explore switch_page index {index} is outside 0..{len(pages) - 1}."
            )
        active_page = pages[index]
        namespace["page"] = active_page
        namespace["pages"] = tuple(pages)
        return active_page

    namespace["switch_page"] = switch_page
    exec(compile(normalized, "<journey-explore>", "exec"), namespace, namespace)
    candidate_page = namespace.get("page")
    if isinstance(candidate_page, PlaywrightPage):
        active_page = candidate_page
    return active_page


def _validate_explore_python_code(code: str) -> None:
    _validate_prompt_python_code(
        code,
        owner="Journey explore Python snippet",
        extra_allowed_names={"unique_email"},
    )
    tree = ast.parse(code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "switch_page":
            raise RuntimeError(
                "Journey explore Python snippet cannot use switch_page(...) because "
                "generated Journey steps must return a replayable JourneyBrowserPage."
            )


def _snapshot_page(page: PlaywrightPage) -> PageSnapshot:
    url = _safe_page_url(page)
    title = _safe_page_title(page)
    visible_text = _truncate(_safe_visible_text(page), _MAX_OBSERVATION_TEXT_LENGTH)
    semantic_dom = _truncate(_safe_semantic_dom(page), _MAX_OBSERVATION_TEXT_LENGTH)
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


def _candidate_prompt(
    snapshot: PageSnapshot,
    *,
    path: tuple[ExploredEdge, ...],
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
    return textwrap.dedent(
        f"""
        Explore this browser page and return up to {max_candidates} next user actions.

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

        Semantic DOM:
        <semantic-dom>
        {snapshot.semantic_dom}
        </semantic-dom>

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
        - Execute one meaningful user path segment from the current page.
        - Use page, timeout_ms, and unique_email(prefix).
        - Prefer data-testid selectors and accessible selectors.
        - Pass timeout=timeout_ms to Playwright actions and waits.
        - Include enough waits/assertions that replay proves the resulting page.
        - Do not import modules, read files, spawn processes, or use eval/exec/open.
        - Use unique_email("organizer") or unique_email("attendee") for email fields.
        """
    ).strip()


_EXPLORE_SYSTEM_PROMPT = """You are Journey Explore, an autonomous browser test author.
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
        raise RuntimeError("journey explore model response did not contain JSON.")
    start = min(candidates)
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    if end < start:
        raise RuntimeError("journey explore model response JSON was incomplete.")
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"journey explore model response was not valid JSON: {exc}") from exc


def _render_root_function(root: ExploredNode, *, function_name: str) -> list[str]:
    expected_path = _path_for_url(root.snapshot.url)
    expected_title = root.snapshot.title or None
    return [
        f"def {function_name}() -> JourneyBrowserPage:",
        f"    page = open_page({root.start_url!r})",
        "    timeout_ms = 30000",
        "    page.wait_for_load_state(\"load\", timeout=timeout_ms)",
        (
            "    _assert_page_state("
            f"page, expected_path={expected_path!r}, expected_title={expected_title!r})"
        ),
        "    return page",
    ]


def _render_edge_function(edge: ExploredEdge) -> list[str]:
    expected_path = _path_for_url(edge.snapshot.url)
    expected_title = edge.snapshot.title or None
    lines = [
        f"def {edge.function_name}(saved_page: JourneyBrowserPage) -> JourneyBrowserPage:",
        f"    \"\"\"{_docstring_text(edge.action.description)}\"\"\"",
        "    page = open_page(saved_page)",
        "    timeout_ms = 30000",
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
            (
                "    _assert_page_state("
                f"page, expected_path={expected_path!r}, expected_title={expected_title!r})"
            ),
            "    return page",
        ]
    )
    return lines


def _render_journey_function(
    roots: tuple[ExploredNode, ...],
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


def _render_node_body(node: ExploredNode, *, current_var: str, indent: str) -> list[str]:
    if not node.edges:
        return []
    if len(node.edges) == 1:
        edge = node.edges[0]
        edge_var = _var_name(edge.function_name)
        lines = [f"{indent}{edge_var} = step({edge.function_name}, {current_var})"]
        if edge.child is not None:
            lines.extend(_render_node_body(edge.child, current_var=edge_var, indent=indent))
        return lines

    lines: list[str] = []
    for index, edge in enumerate(node.edges):
        prefix = "if" if index == 0 else "elif"
        edge_var = _var_name(edge.function_name)
        lines.append(f"{indent}{prefix} branch(replay_from={current_var}):")
        lines.append(f"{indent}    {edge_var} = step({edge.function_name}, {current_var})")
        if edge.child is not None:
            lines.extend(
                _render_node_body(
                    edge.child,
                    current_var=edge_var,
                    indent=f"{indent}    ",
                )
            )
    return lines


def _import_generated_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_journey_explore_generated", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import generated Journey file {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iter_nodes(roots: Sequence[ExploredNode]) -> list[ExploredNode]:
    nodes: list[ExploredNode] = []
    seen: set[str] = set()

    def visit(node: ExploredNode) -> None:
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


def _iter_edges(roots: Sequence[ExploredNode]) -> list[ExploredEdge]:
    edges: list[ExploredEdge] = []
    for node in _iter_nodes(roots):
        edges.extend(node.edges)
    return edges


def _iter_edge_count(root: ExploredNode) -> int:
    return len([edge for edge in _iter_edges((root,))])


def _normalize_options(options: ExploreOptions) -> ExploreOptions:
    if not options.urls:
        raise ValueError("journey explore requires at least one URL.")
    urls = tuple(_normalize_url(url) for url in options.urls)
    if options.depth <= 0:
        raise ValueError("journey explore --depth must be a positive integer.")
    if options.max_actions <= 0:
        raise ValueError("journey explore --max-actions must be a positive integer.")
    if options.browser not in _SUPPORTED_BROWSERS:
        raise ValueError("journey explore --browser must be chromium, firefox, or webkit.")
    return ExploreOptions(
        urls=urls,
        output_file=options.output_file,
        journey_name=_sanitize_identifier(options.journey_name, default="explored_journey"),
        depth=options.depth,
        max_actions=options.max_actions,
        browser=options.browser,
        headless=options.headless,
        model=options.model,
        allow_external=options.allow_external,
        force=options.force,
        action_timeout_seconds=options.action_timeout_seconds,
    )


def _resolve_explore_model(model: str | None) -> str:
    return resolve_prompt_model(
        model,
        env_var=JOURNEY_EXPLORE_MODEL_ENV,
        owner="journey explore",
        default_model=DEFAULT_JOURNEY_EXPLORE_MODEL,
    )


def _normalize_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        raise ValueError("journey explore URL values must be non-blank.")
    parsed = urlsplit(normalized)
    if not parsed.scheme:
        normalized = f"http://{normalized}"
        parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https", "file"}:
        raise ValueError(
            "journey explore supports http, https, and file URLs. "
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


class _NameAllocator:
    def __init__(self) -> None:
        self._used: set[str] = set()

    def allocate(self, raw: str) -> str:
        base = _sanitize_identifier(raw, default="explore_action")
        candidate = base
        suffix = 2
        while candidate in self._used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        self._used.add(candidate)
        return candidate
