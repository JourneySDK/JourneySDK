"""Discovery helpers for CLI journey selection."""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import linecache
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .api import is_journey_callable
from .errors import (
    AmbiguousJourneySelectionError,
    JourneyDiscoveryError,
    JourneySelectionError,
)
from .types import JourneyEntrypoint

_SKIP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "dist",
    "output",
}

_SUPPORTED_IMPORT_MODULES = {"journey", "journeysdk"}


@dataclass(frozen=True)
class DiscoveredJourney:
    file_path: Path
    journey_name: str
    function: JourneyEntrypoint


def discover_journeys(
    root: Path,
    *,
    file_path: str | None = None,
    journey_name: str | None = None,
    fail_fast: bool = False,
) -> tuple[list[DiscoveredJourney], list[JourneyDiscoveryError]]:
    """Discover decorated module-level journeys rooted at ``root``."""

    candidates = [_resolve_file(root, file_path)] if file_path is not None else list(
        _iter_python_files(root)
    )
    discovered: list[DiscoveredJourney] = []
    errors: list[JourneyDiscoveryError] = []

    for path in candidates:
        try:
            candidate_names = _candidate_journey_names(path)
        except JourneyDiscoveryError as error:
            errors.append(error)
            if fail_fast:
                break
            continue

        if journey_name is not None:
            candidate_names = [name for name in candidate_names if name == journey_name]
        if not candidate_names:
            continue

        try:
            module = _load_file_module(path)
        except Exception as exc:
            errors.append(JourneyDiscoveryError(str(path), str(exc)))
            if fail_fast:
                break
            continue

        for candidate_name in candidate_names:
            obj = getattr(module, candidate_name, None)
            if not is_journey_callable(obj):
                continue
            discovered.append(
                DiscoveredJourney(
                    file_path=path,
                    journey_name=candidate_name,
                    function=obj,
                )
            )

    if journey_name is not None and len(discovered) > 1:
        raise AmbiguousJourneySelectionError(
            journey_name,
            matches=[str(item.file_path) for item in discovered],
        )

    discovered.sort(key=lambda item: (str(item.file_path), item.journey_name))
    return discovered, errors


def _resolve_file(root: Path, file_path: str) -> Path:
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()

    if not path.exists():
        raise JourneySelectionError(
            f"Python file '{file_path}' was not found.",
            hint="Check the path or run the command from the directory that contains the file.",
        )
    if not path.is_file():
        raise JourneySelectionError(
            f"'{file_path}' is not a file.",
            hint="Pass the path to a Python file that defines a journey.",
        )
    if path.suffix != ".py":
        raise JourneySelectionError(
            f"'{file_path}' is not a Python file.",
            hint="Pass a file that ends with `.py`.",
        )
    return path


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and name not in _SKIP_DIR_NAMES
        )
        for filename in sorted(filenames):
            if filename.startswith(".") or not filename.endswith(".py"):
                continue
            files.append(Path(current_root, filename).resolve())
    return files


def _candidate_journey_names(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JourneyDiscoveryError(str(path), str(exc)) from exc

    if "journey" not in source:
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise JourneyDiscoveryError(str(path), str(exc)) from exc

    module_aliases: set[str] = set()
    decorator_aliases: set[str] = set()
    names: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SUPPORTED_IMPORT_MODULES:
                    module_aliases.add(alias.asname or alias.name)
            continue

        if isinstance(node, ast.ImportFrom):
            if node.module not in _SUPPORTED_IMPORT_MODULES:
                continue
            for alias in node.names:
                if alias.name == "journey":
                    decorator_aliases.add(alias.asname or alias.name)
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _has_journey_decorator(
            node,
            module_aliases=module_aliases,
            decorator_aliases=decorator_aliases,
        ):
            names.append(node.name)

    return sorted(names)


def _has_journey_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    module_aliases: set[str],
    decorator_aliases: set[str],
) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id in decorator_aliases:
            return True
        if not isinstance(decorator, ast.Attribute):
            continue
        if decorator.attr != "journey":
            continue
        if isinstance(decorator.value, ast.Name) and decorator.value.id in module_aliases:
            return True
    return False


def _load_file_module(path: Path) -> ModuleType:
    importlib.invalidate_caches()
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    module_name = f"_journey_file_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Python could not create a module spec for '{path}'")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    source = path.read_text(encoding="utf-8")
    linecache.cache[str(path)] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        str(path),
    )
    code = compile(source, str(path), "exec")
    exec(code, module.__dict__)
    return module
