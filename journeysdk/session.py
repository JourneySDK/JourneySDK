"""Session context used by journey API primitives."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .errors import InvalidBranchUsageError

_CURRENT_SESSION: ContextVar[Any | None] = ContextVar("journey_current_session", default=None)


def get_session() -> Any | None:
    return _CURRENT_SESSION.get()


def register_step_exit_callback(callback: Callable[[], None]) -> None:
    """Register cleanup for the currently executing step.

    Official tools use this hook when they open live resources, such as
    browsers or local servers, inside a step function. The callback is tied to
    the active step attempt: it runs when that step exits on success, failure,
    retry, pause, or interruption. Tool authors should keep callbacks
    idempotent and close only resources owned by that tool call.

    This helper is intentionally step-scoped. Calling it during planning, at
    module import time, or anywhere outside the body of a function passed to
    ``step(...)`` raises ``InvalidBranchUsageError``.
    """

    if not callable(callback):
        raise TypeError("register_step_exit_callback(...) expects a callable.")

    session = get_session()
    register = getattr(session, "register_step_exit_callback", None)
    if not callable(register):
        raise InvalidBranchUsageError(
            "Step-exit cleanup can only be registered while a journey step is running.",
            hint=(
                "Call lifecycle-aware tools from inside a function passed to "
                "step(...), not during planning, module import, or between steps."
            ),
        )
    register(callback)


@contextmanager
def use_session(session: Any) -> Iterator[None]:
    token = _CURRENT_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(token)
