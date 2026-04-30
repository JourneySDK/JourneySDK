"""Shared prompt-memory helpers for AI-driven Journey tools."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import tempfile
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import InvalidBranchUsageError
from .session import get_session
from .utils import callable_ref

PROMPT_MEMORY_FORMAT_VERSION = 1
PROMPT_MEMORY_SUFFIX = ".memory.json"
MAX_PROMPT_MEMORY_ITEMS = 10
MAX_PROMPT_MEMORY_TEXT_LENGTH = 1000


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


def prompt_memory_key(
    *,
    tool: str,
    instruction: str,
    page_signature: str,
) -> str:
    payload = {
        "tool": tool,
        "instruction": normalize_prompt_instruction(instruction),
        "page_signature": page_signature,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_prompt_instruction(instruction: str) -> str:
    return " ".join(instruction.split())


def load_prompt_memory_entry(path: Path, key: str) -> dict[str, object] | None:
    data = _load_prompt_memory_file(path)
    entries = data["entries"]
    if not isinstance(entries, dict):
        raise RuntimeError(
            f"Prompt memory file '{path}' has invalid entries data."
        )
    entry = entries.get(key)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise RuntimeError(
            f"Prompt memory file '{path}' has an invalid entry for key '{key}'."
        )
    return dict(entry)


def write_prompt_memory_entry(
    path: Path,
    key: str,
    entry: Mapping[str, object],
) -> int:
    data = _load_prompt_memory_file(path)
    entries = data["entries"]
    if not isinstance(entries, dict):
        raise RuntimeError(
            f"Prompt memory file '{path}' has invalid entries data."
        )
    prior = entries.get(key)
    prior_run_count = 0
    if isinstance(prior, dict) and isinstance(prior.get("run_count"), int):
        prior_run_count = prior["run_count"]

    updated = dict(entry)
    run_count = prior_run_count + 1
    updated["run_count"] = run_count
    updated["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    entries[key] = updated
    _write_prompt_memory_file(path, data)
    return run_count


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


def _load_prompt_memory_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "version": PROMPT_MEMORY_FORMAT_VERSION,
            "entries": {},
        }
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read prompt memory file '{path}': {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Prompt memory file '{path}' must contain a JSON object.")
    if loaded.get("version") != PROMPT_MEMORY_FORMAT_VERSION:
        raise RuntimeError(
            f"Prompt memory file '{path}' uses unsupported format version "
            f"{loaded.get('version')!r}."
        )
    if "entries" not in loaded:
        loaded["entries"] = {}
    if not isinstance(loaded["entries"], dict):
        raise RuntimeError(f"Prompt memory file '{path}' has invalid entries data.")
    return loaded


def _write_prompt_memory_file(path: Path, data: Mapping[str, object]) -> None:
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
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write("\n")
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
