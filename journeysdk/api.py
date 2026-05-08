"""Authoring primitives for journey."""

from __future__ import annotations

import inspect
from typing import ParamSpec, TypeGuard, TypeVar, cast

from ._branch_handle import BranchHandle
from .errors import InvalidBranchUsageError
from .models import (
    PlannedValue,
    StepRetryDelay,
    StepRetryFrom,
)
from .session import get_session
from .types import JourneyEntrypoint, JourneyFunction, StepFunction

_JOURNEY_MARKER_ATTR = "__journey_marker__"
_START_FROM_UNSET = object()
P = ParamSpec("P")
R = TypeVar("R")


def journey(fn: JourneyFunction[P, R]) -> JourneyFunction[P, R]:
    """Mark a top-level authoring function so the CLI can discover it.

    Decorate the function that defines one complete QA journey. The decorated
    callable remains unchanged, but the ``journey`` CLI can now discover it as
    an entrypoint.

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


def is_journey_callable(obj: object) -> TypeGuard[JourneyEntrypoint]:
    """Return whether a callable was marked with the journey decorator."""

    return callable(obj) and bool(getattr(obj, _JOURNEY_MARKER_ATTR, False))


def branch(
    *,
    start_from: object = _START_FROM_UNSET,
) -> BranchHandle:
    """Select one inline branch inside an ``if`` / ``elif`` chain.

    ``start_from`` points at an earlier ``step(...)`` result. In a full
    multi-case run, later branch cases can restore to that step's completed
    post-exit boundary instead of rerunning earlier shared setup. Targeted
    execution reports that step as metadata in ``replay_anchor``, but still
    starts from the selected case's required beginning unless state or retry
    replay says otherwise.

    Args:
        start_from: Optional result from an earlier full ``step(...)`` call.
            Omit this argument to start a branch case from scratch.

    Returns:
        A branch handle. When used directly as an ``if`` / ``elif`` condition,
        or assigned to a variable and checked later, the handle's truthiness
        selects the active branch for the current journey case.

    Raises:
        TypeError: If ``start_from`` is not an earlier step result during
            planning or execution.
        InvalidBranchUsageError: If called outside planning or execution.

    Example:
        ```python
        from journeysdk import branch, step

        account = step(create_account)
        if branch():
            step(finish_fast_path)
        elif branch(start_from=account):
            step(finish_review_path, account)

        fast = branch()
        review = branch(start_from=account)
        if fast:
            step(check_fast_path)
        elif review:
            step(check_review_path)
        ```
    """
    session = get_session()
    if session is None:
        raise InvalidBranchUsageError(
            "branch() can only be used while a journey is being planned or executed.",
            hint=(
                "Call branch() directly as the whole condition in an if/elif chain, "
                "or assign it to a variable and check that variable directly."
            ),
        )

    if getattr(session, "mode", None) == "plan":
        if start_from is _START_FROM_UNSET:
            start_from_node_id = None
        elif isinstance(start_from, PlannedValue):
            if start_from.access_path:
                raise InvalidBranchUsageError(
                    "branch(start_from=...) must point to a full earlier step() result.",
                    hint="Pass the earlier step() result itself, not one of its attributes.",
                )
            if start_from.kind != "step":
                raise InvalidBranchUsageError(
                    "branch(start_from=...) must point to the result of an earlier step() call.",
                    hint="Save the earlier step() result in a variable and pass that variable to branch(start_from=...).",
                )
            start_from_node_id = start_from.node_id
        else:
            raise TypeError(
                "branch(start_from=...) accepts an earlier step() result. Omit start_from to start from scratch."
            )
    else:
        if start_from is _START_FROM_UNSET:
            start_from_node_id = None
        else:
            resolver = getattr(session, "step_anchor_for_value", None)
            if not callable(resolver):
                raise TypeError(
                    "branch(start_from=...) accepts an earlier step() result. Omit start_from to start from scratch."
                )
            start_from_node_id = resolver(start_from)

    caller = inspect.currentframe()
    caller_frame = caller.f_back if caller is not None else None
    if caller_frame is None:
        raise InvalidBranchUsageError(
            "branch() could not determine where it was called from.",
            hint="Call branch() directly as the whole condition in an if/elif chain, or assign it to a variable and check that variable directly.",
        )
    return cast(
        BranchHandle,
        session.branch(start_from=start_from_node_id, frame=caller_frame),
    )


def step(
    fn: StepFunction[P, R],
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
    testing logic such as browser checks, API calls, touchpoint-backed validations,
    or polling steps configured with retry settings. Retries apply when a step
    raises an exception and ``retry`` is greater than 0.

    When a run uses retries, ``journey --state ...``, or branches that start
    from an earlier step, journey may need to save and restore step inputs and
    outputs. Any value that may be replayed that way must be
    pickle-serializable or implement the Journey rehydration protocol.
    In CLI runs with ``--state``, first Ctrl-C lets the active step finish and
    resume after it; pressing Ctrl-C again interrupts the dirty step, which
    later restarts from the top with saved inputs.

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
        retry_from: Optional retry anchor. Use an earlier ``step()`` result or
            ``None``. When retries are enabled, ``None`` retries the current
            step. The default is ``None``.
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
