"""Internal helpers for journey."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def callable_ref(fn: Callable[..., object]) -> str:
    """Create a stable printable reference for a callable."""

    module = getattr(fn, "__module__", "<unknown>")
    qualname = getattr(fn, "__qualname__", repr(fn))
    return f"{module}:{qualname}"


def validate_checkpoint_call(
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    store: Callable[..., object] | None,
    restore: Callable[..., object] | None,
) -> None:
    """Validate the public checkpoint(...) hook contract."""

    if store is None and restore is None:
        if args or kwargs:
            raise TypeError(
                "checkpoint() only accepts positional and keyword arguments when both store=... and restore=... are provided."
            )
        return

    if store is None or restore is None:
        raise TypeError(
            "checkpoint() requires both store=... and restore=... together."
        )

    if not callable(store):
        raise TypeError("checkpoint(store=...) needs a callable.")
    if not callable(restore):
        raise TypeError("checkpoint(restore=...) needs a callable.")
