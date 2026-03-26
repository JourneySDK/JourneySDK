"""Internal helpers for journey."""

from __future__ import annotations

from collections.abc import Callable


def callable_ref(fn: Callable[..., object]) -> str:
    """Create a stable printable reference for a callable."""

    module = getattr(fn, "__module__", "<unknown>")
    qualname = getattr(fn, "__qualname__", repr(fn))
    return f"{module}:{qualname}"
