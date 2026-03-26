"""Session context used by journey API primitives."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CURRENT_SESSION: ContextVar[Any | None] = ContextVar("journey_current_session", default=None)


def get_session() -> Any | None:
    return _CURRENT_SESSION.get()


@contextmanager
def use_session(session: Any) -> Iterator[None]:
    token = _CURRENT_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(token)
