"""Shared prompt-memory helpers for AI-driven Journey tools."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import tempfile
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias

from .errors import InvalidBranchUsageError
from .planner import _PlanSession, _register_planning_step_hook
from .session import get_session
from .utils import callable_ref

PROMPT_MEMORY_FORMAT_VERSION = 2
PROMPT_MEMORY_SUFFIX = ".memory.md"
MAX_PROMPT_MEMORY_TEXT_LENGTH = 1000
_PROMPT_MEMORY_TITLE = "# Journey Prompt Memory"
_FENCE_PATTERN = re.compile(
    r"^```(?P<language>[A-Za-z0-9_-]*)\n(?P<body>.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class PromptMemorySection:
    heading: str
    body: str
    language: str | None = None


@dataclass(frozen=True)
class PromptMemoryEntry:
    component: str
    instruction: str
    observation_signature: str
    sections: tuple[PromptMemorySection, ...]
    final_output: str | dict[str, object]
    run_count: int = 0
    updated_at: str = ""


@dataclass(frozen=True)
class PromptMemoryReference:
    name: str
    file_path: str
    line: int
    column: int
    owner: str

    @property
    def identity(self) -> tuple[str, int, int, str]:
        return (self.file_path, self.line, self.column, self.owner)

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.line}:{self.column + 1}"


_PromptMemoryIdentity: TypeAlias = tuple[str, int, int, str]
_PromptMemoryRefsByName: TypeAlias = dict[
    str,
    dict[_PromptMemoryIdentity, PromptMemoryReference],
]
_PROMPT_MEMORY_PLANNING_STATE_KEY = "prompt_memory"


def collect_prompt_memory_references(fn: object) -> tuple[PromptMemoryReference, ...]:
    """Return literal prompt-memory references found in one step callable."""

    try:
        source_lines, source_start_line = inspect.getsourcelines(fn)
    except (OSError, TypeError):
        return ()

    source_file = inspect.getsourcefile(fn) or "<unknown>"
    source = "".join(source_lines)
    source_col_offset = len(source_lines[0]) - len(source_lines[0].lstrip())
    source = textwrap.dedent(source)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    owner = callable_ref(fn)
    references: list[PromptMemoryReference] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_prompt_call(node):
            continue
        memory_keyword = next(
            (keyword for keyword in node.keywords if keyword.arg == "memory"),
            None,
        )
        if memory_keyword is None:
            continue
        value = memory_keyword.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            raise InvalidBranchUsageError(
                "prompt(..., memory=...) must use a non-empty string literal "
                f"in step '{owner}'.",
                hint=(
                    "Use a stable literal such as "
                    "`page.prompt(..., memory=\"sign-in-popup\")` so Journey can "
                    "validate memory names during planning."
                ),
            )
        try:
            name = normalize_prompt_memory_name(value.value, owner="prompt")
        except ValueError as exc:
            raise InvalidBranchUsageError(
                f"prompt(..., memory=...) in step '{owner}' is invalid: {exc}",
                hint="Use a non-empty filename-safe memory name.",
            ) from exc
        references.append(
            PromptMemoryReference(
                name=name,
                file_path=source_file,
                line=source_start_line + value.lineno - 1,
                column=source_col_offset + value.col_offset,
                owner=owner,
            )
        )
    return tuple(references)


def format_duplicate_prompt_memory_error(
    name: str,
    references: tuple[PromptMemoryReference, ...],
) -> str:
    locations = ", ".join(reference.location for reference in references)
    return (
        f"Prompt memory name {name!r} is used by more than one prompt(...) call: "
        f"{locations}."
    )


@dataclass
class _PromptMemoryPlanningState:
    refs_by_name: _PromptMemoryRefsByName = field(default_factory=dict)
    refs_seen_by_session: dict[int, set[_PromptMemoryIdentity]] = field(
        default_factory=dict
    )

    def validate_step(self, fn: object, *, planning_session_id: int) -> None:
        refs_seen = self.refs_seen_by_session.setdefault(planning_session_id, set())
        for reference in collect_prompt_memory_references(fn):
            refs_by_identity = self.refs_by_name.setdefault(
                reference.name,
                {},
            )
            seen_in_session = reference.identity in refs_seen
            refs_seen.add(reference.identity)
            if reference.identity in refs_by_identity:
                if seen_in_session:
                    raise InvalidBranchUsageError(
                        format_duplicate_prompt_memory_error(
                            reference.name,
                            (refs_by_identity[reference.identity], reference),
                        ),
                        hint=(
                            "Use one unique memory name per prompt(...) invocation "
                            "in a compiled journey."
                        ),
                    )
                continue
            if refs_by_identity:
                references = tuple(refs_by_identity.values()) + (reference,)
                raise InvalidBranchUsageError(
                    format_duplicate_prompt_memory_error(reference.name, references),
                    hint=(
                        "Use one unique memory name per prompt(...) call in a "
                        "compiled journey."
                    ),
                )
            refs_by_identity[reference.identity] = reference


def _validate_step_prompt_memory_references(session: _PlanSession, fn: object) -> None:
    state = session.planning_state(
        _PROMPT_MEMORY_PLANNING_STATE_KEY,
        _PromptMemoryPlanningState,
    )
    state.validate_step(fn, planning_session_id=session.planning_session_id)


def normalize_prompt_memory_name(name: object, *, owner: str) -> str:
    if not isinstance(name, str):
        raise ValueError(f"{owner} memory name must be a string.")
    if not name:
        raise ValueError(f"{owner} memory name must be non-empty.")
    if name != name.strip():
        raise ValueError(f"{owner} memory name must not start or end with whitespace.")
    if name in {".", ".."}:
        raise ValueError(f"{owner} memory name must not be {name!r}.")
    if "/" in name or "\\" in name:
        raise ValueError(f"{owner} memory name must not contain path separators.")
    return name


def resolve_prompt_memory_path(
    memory: str | None,
    *,
    owner: str,
) -> Path | None:
    if memory is None:
        return None
    name = normalize_prompt_memory_name(memory, owner=owner)
    session = get_session()
    disabled = getattr(session, "prompt_memory_disabled", None)
    if callable(disabled) and disabled():
        return None

    root: Path | None = None
    root_resolver = getattr(session, "prompt_memory_root", None)
    if callable(root_resolver):
        resolved = root_resolver()
        if resolved is not None:
            root = Path(resolved)
    if root is None:
        root = Path.cwd()
    return root / f"{name}{PROMPT_MEMORY_SUFFIX}"


def prompt_memory_updates_disabled() -> bool:
    session = get_session()
    disabled = getattr(session, "prompt_memory_disabled", None)
    if callable(disabled) and disabled():
        return True
    update_disabled = getattr(session, "prompt_memory_update_disabled", None)
    return bool(callable(update_disabled) and update_disabled())


def normalize_prompt_instruction(instruction: str) -> str:
    return " ".join(instruction.split())


def prompt_memory_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_prompt_memory_entry(
    path: Path,
    *,
    component: str,
    instruction: str,
    observation_signature: str,
) -> PromptMemoryEntry | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read prompt memory file '{path}': {exc}") from exc
    entry = parse_prompt_memory_entry(text, path=path)
    expected_instruction = normalize_prompt_instruction(instruction)
    if entry.component != component:
        return None
    if entry.instruction != expected_instruction:
        return None
    if entry.observation_signature != observation_signature:
        return None
    return entry


def write_prompt_memory_entry(
    path: Path,
    entry: PromptMemoryEntry,
) -> int:
    prior_run_count = 0
    if path.exists():
        try:
            prior = parse_prompt_memory_entry(
                path.read_text(encoding="utf-8"),
                path=path,
            )
        except RuntimeError:
            prior = None
        if prior is not None:
            prior_run_count = prior.run_count
    run_count = prior_run_count + 1
    updated = PromptMemoryEntry(
        component=entry.component,
        instruction=normalize_prompt_instruction(entry.instruction),
        observation_signature=entry.observation_signature,
        sections=tuple(
            PromptMemorySection(
                heading=_normalize_section_heading(section.heading),
                body=section.body.strip(),
                language=_normalize_section_language(section.language),
            )
            for section in entry.sections
        ),
        final_output=entry.final_output,
        run_count=run_count,
        updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    _write_prompt_memory_file(path, render_prompt_memory_entry(updated))
    return run_count


def prompt_memory_entry_from_result(
    *,
    component: str,
    instruction: str,
    observation_signature: str,
    final_output: str | dict[str, object],
    sections: tuple[PromptMemorySection, ...],
) -> PromptMemoryEntry:
    return PromptMemoryEntry(
        component=component,
        instruction=normalize_prompt_instruction(instruction),
        observation_signature=observation_signature,
        sections=sections,
        final_output=final_output,
    )


def parse_prompt_memory_entry(text: str, *, path: Path | None = None) -> PromptMemoryEntry:
    label = f"Prompt memory file '{path}'" if path is not None else "Prompt memory"
    if not text.startswith(_PROMPT_MEMORY_TITLE):
        raise RuntimeError(f"{label} must start with {_PROMPT_MEMORY_TITLE!r}.")
    metadata = _parse_prompt_memory_metadata(text)
    component = _required_metadata(metadata, "component", label=label)
    instruction = _required_metadata(metadata, "instruction", label=label)
    observation_signature = _required_metadata(
        metadata,
        "observation_signature",
        label=label,
    )
    _validate_fingerprint(
        metadata,
        "instruction_sha256",
        instruction,
        label=label,
    )
    _validate_fingerprint(
        metadata,
        "observation_signature_sha256",
        observation_signature,
        label=label,
    )
    sections = _parse_prompt_memory_sections(text, label=label)
    final_output = _parse_final_output_section(text, label=label)
    run_count = _parse_int_metadata(metadata.get("run_count"), label=label)
    updated_at = metadata.get("updated_at", "")
    return PromptMemoryEntry(
        component=component,
        instruction=instruction,
        observation_signature=observation_signature,
        sections=sections,
        final_output=final_output,
        run_count=run_count,
        updated_at=updated_at,
    )


def render_prompt_memory_entry(entry: PromptMemoryEntry) -> str:
    instruction = normalize_prompt_instruction(entry.instruction)
    final_output_language = "json" if isinstance(entry.final_output, dict) else "text"
    if isinstance(entry.final_output, dict):
        final_output = json.dumps(entry.final_output, sort_keys=True, indent=2)
    else:
        final_output = entry.final_output
    rendered_sections: list[str] = []
    for section in entry.sections:
        rendered_sections.extend(_render_prompt_memory_section(section))
    return "\n".join(
        [
            _PROMPT_MEMORY_TITLE,
            "",
            f"version: {PROMPT_MEMORY_FORMAT_VERSION}",
            f"component: {entry.component}",
            f"instruction: {instruction}",
            f"instruction_sha256: {prompt_memory_fingerprint(instruction)}",
            f"observation_signature: {entry.observation_signature}",
            (
                "observation_signature_sha256: "
                f"{prompt_memory_fingerprint(entry.observation_signature)}"
            ),
            f"run_count: {entry.run_count}",
            f"updated_at: {entry.updated_at}",
            *rendered_sections,
            "",
            "## Final output",
            f"```{final_output_language}",
            str(final_output).strip(),
            "```",
            "",
        ]
    )


def _render_prompt_memory_section(section: PromptMemorySection) -> list[str]:
    heading = _normalize_section_heading(section.heading)
    body = section.body.strip()
    language = _normalize_section_language(section.language)
    if language is None:
        return ["", f"## {heading}", body]
    return ["", f"## {heading}", f"```{language}", body, "```"]


def _normalize_section_heading(heading: str) -> str:
    if not isinstance(heading, str):
        raise ValueError("Prompt memory section heading must be a string.")
    normalized = heading.strip()
    if not normalized:
        raise ValueError("Prompt memory section heading must be non-empty.")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("Prompt memory section heading must be one line.")
    if normalized == "Final output":
        raise ValueError("Prompt memory section heading 'Final output' is reserved.")
    return normalized


def _normalize_section_language(language: str | None) -> str | None:
    if language is None:
        return None
    if not isinstance(language, str):
        raise ValueError("Prompt memory section language must be a string or None.")
    normalized = language.strip()
    if not normalized:
        return None
    if "\n" in normalized or "\r" in normalized or "`" in normalized:
        raise ValueError("Prompt memory section language must be a simple fence tag.")
    return normalized


def _parse_prompt_memory_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if line.startswith("## "):
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            metadata[key] = value.strip()
    return metadata


def _required_metadata(
    metadata: Mapping[str, str],
    key: str,
    *,
    label: str,
) -> str:
    value = metadata.get(key, "").strip()
    if not value:
        raise RuntimeError(f"{label} is missing required metadata {key!r}.")
    return value


def _validate_fingerprint(
    metadata: Mapping[str, str],
    key: str,
    value: str,
    *,
    label: str,
) -> None:
    actual = metadata.get(key, "").strip()
    expected = prompt_memory_fingerprint(value)
    if actual != expected:
        raise RuntimeError(f"{label} has an invalid {key!r}.")


def _parse_int_metadata(value: str | None, *, label: str) -> int:
    if value is None or not value.strip():
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{label} has invalid run_count metadata.") from exc
    return max(0, parsed)


def _parse_final_output_section(text: str, *, label: str) -> str | dict[str, object]:
    body, language = _required_fenced_section(text, "Final output", label=label)
    if language == "json":
        try:
            loaded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} has invalid final output JSON.") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"{label} final output JSON must be an object.")
        return loaded
    return body


def _parse_prompt_memory_sections(
    text: str,
    *,
    label: str,
) -> tuple[PromptMemorySection, ...]:
    sections: list[PromptMemorySection] = []
    for heading, body in _iter_sections(text):
        if heading == "Final output":
            continue
        normalized_heading = _normalize_section_heading(heading)
        normalized_body = body.strip()
        language: str | None = None
        fence = _FENCE_PATTERN.fullmatch(normalized_body)
        if fence is not None:
            language = _normalize_section_language(fence.group("language"))
            normalized_body = fence.group("body").strip()
        sections.append(
            PromptMemorySection(
                heading=normalized_heading,
                body=normalized_body,
                language=language,
            )
        )
    return tuple(sections)


def _required_fenced_section(
    text: str,
    heading: str,
    *,
    label: str,
) -> tuple[str, str]:
    section = _section_text(text, heading)
    if section is None:
        raise RuntimeError(f"{label} is missing required section {heading!r}.")
    match = _FENCE_PATTERN.search(section.strip())
    if match is None:
        raise RuntimeError(f"{label} {heading!r} must contain a fenced code block.")
    return match.group("body"), match.group("language")


def _section_text(text: str, heading: str) -> str | None:
    for section_heading, body in _iter_sections(text):
        if section_heading == heading:
            return body.strip()
    return None


def _iter_sections(text: str) -> tuple[tuple[str, str], ...]:
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line.removeprefix("## ").strip()
            current_body = []
            continue
        if current_heading is not None:
            current_body.append(line)
    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_body).strip()))
    return tuple(sections)


def truncate_prompt_memory_text(value: object) -> str:
    text = str(value)
    if len(text) <= MAX_PROMPT_MEMORY_TEXT_LENGTH:
        return text
    return text[: MAX_PROMPT_MEMORY_TEXT_LENGTH - 3] + "..."


def _is_prompt_call(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr == "prompt"
    if isinstance(call.func, ast.Name):
        return call.func.id == "prompt"
    return False


def _write_prompt_memory_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


_register_planning_step_hook(_validate_step_prompt_memory_references)
