"""Journey planner/compiler for journey v1."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from .errors import (
    DuplicateBranchKeyError,
    InvalidBranchUsageError,
    UnknownCheckpointError,
)
from .models import (
    BranchCase,
    BranchMarkerNode,
    BranchSelector,
    CasePlan,
    CheckpointNode,
    CheckpointRef,
    JourneyPlan,
    PlannedValue,
    StepNode,
    StepRetryDelay,
    StepRetryFrom,
    StepRetry,
    _duration_to_seconds,
)
from .session import use_session
from .utils import callable_ref
from .validator import validate_journey

BranchEnv = dict[str, str]
P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class _SuspendPlanning(Exception):
    group_id: str
    cases: list[BranchCase]


class _PlanSession:
    mode = "plan"

    def __init__(self, branch_env: BranchEnv) -> None:
        self.branch_env = dict(branch_env)
        self.nodes: list[Any] = []
        self._node_counter = 0
        self._group_counter = 0
        self._checkpoint_counter = 0
        self._checkpoints_seen: set[str] = set()
        self._steps_seen: set[str] = set()
        self._journey_webhook_epoch = 0

    def _next_node_id(self) -> str:
        self._node_counter += 1
        return f"n_{self._node_counter}"

    def _next_group_id(self) -> str:
        self._group_counter += 1
        return f"bg_{self._group_counter}"

    def _next_checkpoint_name(self) -> str:
        self._checkpoint_counter += 1
        return f"cp_{self._checkpoint_counter}"

    def step(
        self,
        fn: Callable[P, R],
        *args: P.args,
        retry: int = 0,
        retry_delay: StepRetryDelay = 5,
        retry_from: StepRetryFrom = None,
        **kwargs: P.kwargs,
    ) -> PlannedValue:
        if not callable(fn):
            raise TypeError(
                "step(...) needs a callable as its first argument."
            )
        node_id = self._next_node_id()
        resolved_retry = _resolve_step_retry(
            retry=retry,
            retry_delay=retry_delay,
            retry_from=retry_from,
            current_node_id=node_id,
            known_step_ids=self._steps_seen,
            known_checkpoint_names=self._checkpoints_seen,
        )
        node = StepNode(
            node_id=node_id,
            label=getattr(fn, "__name__", "<step>"),
            fn_ref=callable_ref(fn),
            args=args,
            kwargs=kwargs,
            retry=resolved_retry,
        )
        self.nodes.append(node)
        self._steps_seen.add(node.node_id)
        return PlannedValue(node_id=node.node_id, kind="step")

    def checkpoint(
        self,
        *,
        branches: list[BranchCase] | None = None,
    ) -> CheckpointRef | BranchSelector:
        name = self._next_checkpoint_name()
        if name in self._checkpoints_seen:
            raise InvalidBranchUsageError(
                f"Checkpoint '{name}' was created more than once in the same journey path.",
                hint="Create each checkpoint only once before referencing it from a branch or retry.",
            )
        self._checkpoints_seen.add(name)

        node = CheckpointNode(node_id=self._next_node_id(), name=name)
        self.nodes.append(node)
        ref = CheckpointRef(name=name)
        if branches is None:
            return ref

        normalized_cases, case_id_to_key = _normalize_cases(branches)
        keys = [case.key for case in normalized_cases]
        if len(set(keys)) != len(keys):
            raise DuplicateBranchKeyError(
                "Each branch inside checkpoint(branches=[...]) must have a unique key.",
                hint="Create a new branch() value for each option instead of reusing the same one.",
            )

        group_id = self._next_group_id()
        if group_id not in self.branch_env:
            raise _SuspendPlanning(group_id=group_id, cases=normalized_cases)

        active_key = self.branch_env[group_id]
        by_key = {case.key: case for case in normalized_cases}
        if active_key not in by_key:
            raise InvalidBranchUsageError(
                f"The planner selected branch key '{active_key}', but that option does not exist in branch group '{group_id}'.",
                hint="Re-run planning from scratch if the branch options changed.",
            )

        selected_case = by_key[active_key]
        if selected_case.start_from is not None and selected_case.start_from not in self._checkpoints_seen:
            raise UnknownCheckpointError(
                f"Branch '{selected_case.key}' starts from checkpoint '{selected_case.start_from}', but that checkpoint was never created earlier in the journey.",
                hint="Create the checkpoint with checkpoint() before using it in branch(start_from=...).",
            )

        marker = BranchMarkerNode(
            node_id=self._next_node_id(),
            group_id=group_id,
            active_key=active_key,
            start_from=selected_case.start_from,
        )
        self.nodes.append(marker)

        return BranchSelector(
            group_id=group_id,
            active_key=active_key,
            case_id_to_key=case_id_to_key,
        )


def _normalize_cases(
    cases: list[BranchCase],
) -> tuple[list[BranchCase], dict[int, str]]:
    normalized: list[BranchCase] = []
    case_id_to_key: dict[int, str] = {}
    for index, item in enumerate(cases, start=1):
        if isinstance(item, BranchCase):
            key = item.key or f"branch_{index}"
            normalized_case = BranchCase(key=key, start_from=item.start_from)
            normalized.append(normalized_case)
            case_id_to_key[id(item)] = key
            continue
        raise TypeError(
            "checkpoint(branches=[...]) accepts only values returned by branch()."
        )
    return normalized, case_id_to_key


def _normalize_retry_count(retry: int) -> int:
    if isinstance(retry, bool) or not isinstance(retry, int):
        raise TypeError(
            "step(..., retry=...) expects a non-negative integer."
        )
    if retry < 0:
        raise ValueError("step(..., retry=...) expects a non-negative integer.")
    return retry


def _resolve_step_retry(
    *,
    retry: int,
    retry_delay: StepRetryDelay,
    retry_from: StepRetryFrom,
    current_node_id: str,
    known_step_ids: set[str],
    known_checkpoint_names: set[str],
) -> StepRetry | None:
    resolved_retry = _normalize_retry_count(retry)
    delay_seconds = _duration_to_seconds(retry_delay, field_name="retry_delay")
    from_node_id: str | None = None
    from_checkpoint: str | None = None

    if retry_from is None:
        from_node_id = current_node_id

    elif isinstance(retry_from, PlannedValue):
        if retry_from.access_path:
            raise InvalidBranchUsageError(
                "step(..., retry_from=...) must point to a full earlier step() result.",
                hint="Pass the earlier step() result itself to `retry_from=...`, not one of its attributes.",
            )
        if retry_from.kind != "step" or retry_from.node_id not in known_step_ids:
            raise InvalidBranchUsageError(
                "step(..., retry_from=...) must point to the result of an earlier step() call.",
                hint="Save the earlier step() result in a variable and pass that variable to `retry_from=...`.",
            )
        from_node_id = retry_from.node_id

    elif isinstance(retry_from, CheckpointRef):
        if retry_from.name not in known_checkpoint_names:
            raise UnknownCheckpointError(
                f"step(..., retry_from=...) references checkpoint '{retry_from.name}', but that checkpoint was never created earlier in the journey.",
                hint="Create the checkpoint with checkpoint() before using it in `retry_from=...`.",
            )
        from_checkpoint = retry_from.name

    else:
        raise TypeError(
            "step(..., retry_from=...) accepts an earlier step() result, a checkpoint() result, or None."
        )

    if resolved_retry == 0:
        return None

    return StepRetry(
        retries=resolved_retry,
        delay_seconds=delay_seconds,
        from_node_id=from_node_id,
        from_checkpoint=from_checkpoint,
    )


def compile_journey(journey_fn: Callable[..., Any]) -> JourneyPlan:
    """Compile one journey function into linear case plans."""

    if not callable(journey_fn):
        raise TypeError("compile_journey() expects a callable journey function.")

    validate_journey(journey_fn)

    queue: deque[BranchEnv] = deque([{}])
    case_plans: list[CasePlan] = []

    while queue:
        env = queue.popleft()
        session = _PlanSession(env)

        try:
            with use_session(session):
                journey_fn()
        except _SuspendPlanning as suspended:
            for case in suspended.cases:
                expanded = dict(env)
                expanded[suspended.group_id] = case.key
                queue.append(expanded)
            continue

        case_id = f"case_{len(case_plans) + 1}"
        case_plans.append(
            CasePlan(
                case_id=case_id,
                branch_env=dict(env),
                nodes=list(session.nodes),
            )
        )

    return JourneyPlan(
        journey_id=journey_fn.__name__,
        function_ref=callable_ref(journey_fn),
        case_plans=case_plans,
    )
