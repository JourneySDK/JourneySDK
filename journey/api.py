"""Public authoring primitives for journey."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

from .errors import InvalidBranchUsageError
from .models import (
    BranchCase,
    BranchSelector,
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
        import journey

        @journey.journey
        def signup_flow() -> None:
            created = journey.step(create_account)
            journey.step(account_is_ready, created)
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
    """Return whether a callable was marked with the public journey decorator."""

    return callable(obj) and bool(getattr(obj, _JOURNEY_MARKER_ATTR, False))


def branch(
    *,
    start_from: CheckpointRef | str | None = None,
) -> BranchCase:
    """Create one branch option for ``checkpoint(branches=[...])``.

    ``branch()`` returns an opaque case descriptor. Store it in a variable,
    pass it to ``checkpoint(branches=[...])``, and compare it with
    ``selector.is_(branch_case)`` inside the matching ``if``/``elif`` chain.

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
        A ``BranchCase`` that can be passed to ``checkpoint(branches=[...])``.

    Raises:
        TypeError: If ``start_from`` is not a ``CheckpointRef``, string, or
            ``None``.

    Example:
        ```python
        after_login = journey.checkpoint()
        fast_path = journey.branch()
        review_path = journey.branch(start_from=after_login)
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

    return BranchCase(key=None, start_from=start_from_name)


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
    pickle-serializable.

    Outside an active planning or execution session, ``step()`` is invalid.

    Args:
        fn: Step callable to plan or execute.
        *args: Positional arguments forwarded to ``fn``. Values that may need
            to be replayed later must be pickle-serializable.
        retry: Optional number of extra retries after the initial attempt.
            Retries run only when this value is greater than 0. The default is
            0 extra retries.
        retry_delay: Optional delay between retry attempts in seconds or as a
            ``datetime.timedelta``. The default is 5 seconds.
        retry_from: Optional retry anchor. Use an earlier ``step()`` result, a
            ``checkpoint()`` reference, or ``None``. When retries are enabled,
            ``None`` retries the current step. The default is ``None``.
        **kwargs: Keyword arguments forwarded to ``fn``. Values that may need
            to be replayed later must be pickle-serializable.

    Returns:
        During planning, a placeholder object that can be passed into later
        ``step()`` calls, used with ``retry_from=...``, or dereferenced through
        plain attribute access such as ``result.url``.

        During execution, the raw Python value returned by ``fn``.

    Raises:
        InvalidBranchUsageError: If called outside planning or execution.

    Example:
        ```python
        created = journey.step(create_subscription)
        journey.step(
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


def checkpoint(
    *,
    branches: list[BranchCase] | None = None,
) -> CheckpointRef | BranchSelector:
    """Mark a checkpoint, optionally creating a branch decision point.

    Use plain ``checkpoint()`` to create a named anchor that later steps or
    branches can refer to. Use ``checkpoint(branches=[...])`` to define one
    branch group and receive a ``BranchSelector`` that supports ``is_(...)``.

    Branch selectors are intentionally constrained in v1:

    - assign the selector to one variable,
    - use it only in a direct ``if``/``elif`` chain,
    - call only ``selector.is_(branch_case)`` in each condition,
    - do not mix that condition with other boolean expressions.

    Args:
        branches: Optional branch options created with ``branch()`` for one
            decision point.

    Returns:
        A ``CheckpointRef`` when ``branches`` is omitted, or a
        ``BranchSelector`` when ``branches`` is provided.

    Raises:
        InvalidBranchUsageError: If called outside planning or execution.

    Example:
        ```python
        after_signup = journey.checkpoint()
        instant = journey.branch()
        manual = journey.branch(start_from=after_signup)
        selected = journey.checkpoint(branches=[instant, manual])

        if selected.is_(instant):
            journey.step(finish_instant)
        elif selected.is_(manual):
            journey.step(finish_manual)
        ```
    """
    session = get_session()
    if session is None:
        raise InvalidBranchUsageError(
            "checkpoint() can only be used while a journey is being planned or executed.",
            hint="Call checkpoint() inside a function decorated with @journey.",
        )
    return session.checkpoint(branches=branches)
