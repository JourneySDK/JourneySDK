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


def _register_step_exit_object(value: object) -> None:
    """Register a lifecycle-aware value for the current step attempt.

    Official tools use this internal hook when they allocate live resources,
    such as browsers or local servers, inside a step function. ``value`` must
    implement the standard context-manager ``__exit__(exc_type, exc,
    traceback)`` method. Journey calls that method when the active step exits
    on success, failure, retry, pause, or interruption.

    This helper is intentionally step-scoped. Calling it during planning, at
    module import time, or anywhere outside the body of a function passed to
    ``step(...)`` raises ``InvalidBranchUsageError``.
    """

    session = get_session()
    register = getattr(session, "_register_step_exit_object", None)
    if not callable(register):
        raise InvalidBranchUsageError(
            "Step-exit cleanup can only be registered while a journey step is running.",
            hint=(
                "Call lifecycle-aware tools from inside a function passed to "
                "step(...), not during planning, module import, or between steps."
            ),
        )
    register(value)


@contextmanager
def use_session(session: Any) -> Iterator[None]:
    token = _CURRENT_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(token)
