"""Internal helpers for journey."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import textwrap
from collections.abc import Callable
from typing import Any


def callable_ref(fn: Callable[..., object]) -> str:
    """Create a stable printable reference for a callable."""

    module = getattr(fn, "__module__", "<unknown>")
    qualname = getattr(fn, "__qualname__", repr(fn))
    return f"{module}:{qualname}"


def callable_source_fingerprint(fn: Callable[..., object]) -> str | None:
    """Return a stable fingerprint for inspectable callable source code."""

    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return None

    normalized = textwrap.dedent(source).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def resolve_ref(ref: str) -> Any:
    """Resolve one ``module:qualname`` reference."""

    module_name, separator, qualname = ref.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(f"Invalid reference {ref!r}.")
    module = importlib.import_module(module_name)
    current: Any = module
    for part in qualname.split("."):
        if part == "<locals>":
            raise ValueError(f"Reference {ref!r} points to a non-importable local object.")
        current = getattr(current, part)
    return current
