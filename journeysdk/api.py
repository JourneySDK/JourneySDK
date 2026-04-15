"""Authoring primitives for journey."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import ParamSpec, TypeVar, cast

from .errors import InvalidBranchUsageError
from .models import (
    CheckpointRef,
    PlannedValue,
    StepRetryDelay,
    StepRetryFrom,
)
from .session import get_session

_JOURNEY_MARKER_ATTR = "__journey_marker__"
P = ParamSpec("P")
R = TypeVar("R")


def journey(fn: Callable[P, R]) -> Callable[P, R]:
    """Mark a top-level authoring function so the CLI can discover it.

    Decorate the function that defines one complete QA journey. The decorated
    callable remains unchanged, but ``journey plan`` and ``journey execute``
    can now discover it as an entrypoint.

    Args:
        fn: Callable to expose as a journey entrypoint.

    Returns:
        The same callable, marked for discovery.

    Raises:
        TypeError: If ``fn`` is not callable.

    Example:
        ```python
        from journeysdk import journey, step

        @journey
        def signup_flow() -> None:
            created = step(create_account)
            step(account_is_ready, created)
        ```
    """

    if not callable(fn):
        raise TypeError(
            "@journey can only decorate a callable function. "
            "Apply it directly to a top-level function."
        )
    setattr(fn, _JOURNEY_MARKER_ATTR, True)
    return fn


def is_journey_callable(obj: Any) -> bool:
    """Return whether a callable was marked with the journey decorator."""

    return callable(obj) and bool(getattr(obj, _JOURNEY_MARKER_ATTR, False))


def branch(
    *,
    start_from: CheckpointRef | str | None = None,
) -> bool:
    """Select one inline branch inside a direct ``if`` / ``elif`` chain.

    ``start_from`` names the checkpoint where this branch begins for downstream
    single-step execution and for full-case checkpoint replay. When a targeted
    execution reaches a step inside this branch, the execution report exposes
    that checkpoint as the branch replay anchor. When all cases are executed,
    later branches that start from the same checkpoint can restore saved state
    from that checkpoint instead of rerunning earlier shared steps.

    Args:
        start_from: Optional checkpoint reference, checkpoint name, or ``None``.
            Use this when the branch should be associated with an earlier
            ``checkpoint()`` result.

    Returns:
        ``True`` only for the selected branch on the active journey path.

    Raises:
        TypeError: If ``start_from`` is not a ``CheckpointRef``, string, or
            ``None``.
        InvalidBranchUsageError: If called outside planning or execution.

    Example:
        ```python
        from journeysdk import branch, checkpoint, step

        after_login = checkpoint()
        if branch():
            step(finish_fast_path)
        elif branch(start_from=after_login):
            step(finish_review_path)
        ```
    """
    start_from_name: str | None
    if isinstance(start_from, CheckpointRef):
        start_from_name = start_from.name
    elif isinstance(start_from, str) or start_from is None:
        start_from_name = start_from
    else:
        raise TypeError(
            "branch(start_from=...) accepts a checkpoint reference, a checkpoint name, or None."
        )

    session = get_session()
    if session is None:
        raise InvalidBranchUsageError(
            "branch() can only be used while a journey is being planned or executed.",
            hint=(
                "Call branch() directly as the whole condition in an if/elif chain "
                "inside a function decorated with @journey."
            ),
        )

    caller = inspect.currentframe()
    caller_frame = caller.f_back if caller is not None else None
    if caller_frame is None:
        raise InvalidBranchUsageError(
            "branch() could not determine where it was called from.",
            hint="Call branch() directly as the whole condition in an if/elif chain.",
        )
    return cast(bool, session.branch(start_from=start_from_name, frame=caller_frame))


def step(
    fn: Callable[P, R],
    *args: P.args,
    retry: int = 0,
    retry_delay: StepRetryDelay = 5,
    retry_from: StepRetryFrom = None,
    **kwargs: P.kwargs,
) -> R | PlannedValue:
    """Add or execute one step inside a journey.

    ``fn`` is called with exactly the positional and keyword arguments passed
    to ``step()``. Use returned values from earlier ``step()`` calls as
    explicit inputs to later steps. When a prior step returns an object with
    plain Python attributes, later step arguments can also read those
    attributes directly, such as ``endpoint.url``. Use ``step()`` for arbitrary
    testing logic such as browser checks, API calls, tool-backed validations,
    or polling steps configured with retry settings. Retries apply when a step
    raises an exception and ``retry`` is greater than 0.

    When a run uses retries, ``journey execute --state ...``, or branches that
    start from an earlier checkpoint, journey may need to save and restore step
    inputs and outputs. Any value that may be replayed that way must be
    pickle-serializable or implement the Journey rehydration protocol.

    Outside an active planning or execution session, ``step()`` is invalid.

    Args:
        fn: Step callable to plan or execute.
        *args: Positional arguments forwarded to ``fn``. Values that may need
            to be replayed later must be pickle-serializable or implement the
            Journey rehydration protocol.
        retry: Optional number of extra retries after the initial attempt.
            Retries run only when this value is greater than 0. The default is
            0 extra retries.
        retry_delay: Optional delay between retry attempts in seconds or as a
            ``datetime.timedelta``. The default is 5 seconds.
        retry_from: Optional retry anchor. Use an earlier ``step()`` result, a
            ``checkpoint()`` reference, or ``None``. When retries are enabled,
            ``None`` retries the current step. The default is ``None``.
        **kwargs: Keyword arguments forwarded to ``fn``. Values that may need
            to be replayed later must be pickle-serializable or implement the
            Journey rehydration protocol.

    Returns:
        During planning, a placeholder object that can be passed into later
        ``step()`` calls, used with ``retry_from=...``, or dereferenced through
        plain attribute access such as ``result.url``.

        During execution, the raw Python value returned by ``fn``.

    Raises:
        InvalidBranchUsageError: If called outside planning or execution.

    Example:
        ```python
        from journeysdk import step

        created = step(create_subscription)
        step(
            invoice_paid,
            created,
            retry=15,
            retry_delay=2,
            retry_from=created,
        )
        ```
    """
    session = get_session()
    if session is None:
        raise InvalidBranchUsageError(
            "step() can only be used while a journey is being planned or executed.",
            hint="Call step() inside a function decorated with @journey.",
        )
    return cast(
        R | PlannedValue,
        session.step(
            fn,
            *args,
            retry=retry,
            retry_delay=retry_delay,
            retry_from=retry_from,
            **kwargs,
        ),
    )


def checkpoint() -> CheckpointRef:
    """Mark a checkpoint that later steps or branches can refer to.

    Returns:
        A ``CheckpointRef`` anchor that later steps or branches can reuse.

    Raises:
        InvalidBranchUsageError: If called outside planning or execution.

    Example:
        ```python
        from journeysdk import branch, checkpoint, step

        after_signup = checkpoint()
        if branch():
            step(finish_instant)
        elif branch(start_from=after_signup):
            step(finish_manual)
        ```
    """
    session = get_session()
    if session is None:
        raise InvalidBranchUsageError(
            "checkpoint() can only be used while a journey is being planned or executed.",
            hint="Call checkpoint() inside a function decorated with @journey.",
        )
    return cast(CheckpointRef, session.checkpoint())
