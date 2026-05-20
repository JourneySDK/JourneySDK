"""Session context used by journey API primitives."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .errors import InvalidBranchUsageError

_CURRENT_SESSION: ContextVar[Any | None] = ContextVar("journey_current_session", default=None)


def get_session() -> Any | None:
    return _CURRENT_SESSION.get()


def _require_executing_step(owner: str) -> None:
    """Raise unless code is running inside a step function body.

    Official touchpoints use this guard before opening live resources that Journey
    owns for the duration of the active step.
    """

    session = get_session()
    is_step_executing = getattr(session, "_is_step_executing", None)
    if not callable(is_step_executing) or not is_step_executing():
        raise InvalidBranchUsageError(
            f"{owner}(...) can only be called while a journey step is running.",
            hint=(
                "Call lifecycle-aware touchpoints from inside a function passed to "
                "step(...), not during planning, module import, or between steps."
            ),
        )


def _register_step_exit_object(owner: str, value: Any) -> None:
    """Register a live touchpoint resource for step-exit cleanup."""

    _require_executing_step(owner)
    session = get_session()
    register = getattr(session, "_register_step_exit_object", None)
    if not callable(register):
        raise InvalidBranchUsageError(
            f"{owner}(...) could not register its step-exit cleanup.",
            hint=(
                "Call lifecycle-aware touchpoints from inside a function passed to "
                "step(...), not during planning, module import, or between steps."
            ),
        )
    register(value)


def _allocate_browser_recording(owner: str) -> Any | None:
    """Allocate one browser recording slot for the active step, when enabled."""

    _require_executing_step(owner)
    session = get_session()
    allocate = getattr(session, "_allocate_browser_recording", None)
    if not callable(allocate):
        return None
    return allocate()


@contextmanager
def use_session(session: Any) -> Iterator[None]:
    token = _CURRENT_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(token)
