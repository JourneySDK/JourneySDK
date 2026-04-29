"""Journey planner/compiler for journey v1."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from types import FrameType
from typing import ParamSpec, TypeVar

from .errors import (
    InvalidBranchUsageError,
)
from .models import (
    BranchCase,
    BranchMarkerNode,
    CasePlan,
    JourneyPlan,
    PlanNode,
    PlannedValue,
    StepNode,
    StepRetryDelay,
    StepRetryFrom,
    StepRetry,
    _duration_to_seconds,
)
from .session import use_session
from .types import JourneyEntrypoint, StepFunction
from .utils import callable_ref, callable_source_fingerprint
from .validator import JourneyValidation, resolve_branch_call_site, validate_journey

BranchEnv = dict[str, str]
P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class _SuspendPlanning(Exception):
    group_id: str
    cases: list[BranchCase]


@dataclass
class _ActiveBranchChain:
    group_id: str
    cases: list[BranchCase]


class _PlanSession:
    mode = "plan"

    def __init__(self, branch_env: BranchEnv, *, validation: JourneyValidation) -> None:
        self.branch_env = dict(branch_env)
        self.validation = validation
        self.nodes: list[PlanNode] = []
        self._node_counter = 0
        self._group_counter = 0
        self._active_branch_chains: dict[tuple[int, int], _ActiveBranchChain] = {}
        self._steps_seen: set[str] = set()
        self._journey_webhook_epoch = 0

    def _next_node_id(self) -> str:
        self._node_counter += 1
        return f"n_{self._node_counter}"

    def _next_group_id(self) -> str:
        self._group_counter += 1
        return f"bg_{self._group_counter}"

    def step(
        self,
        fn: StepFunction[P, R],
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
        )
        node = StepNode(
            node_id=node_id,
            label=getattr(fn, "__name__", "<step>"),
            fn_ref=callable_ref(fn),
            args=args,
            kwargs=kwargs,
            retry=resolved_retry,
            source_fingerprint=callable_source_fingerprint(fn),
        )
        self.nodes.append(node)
        self._steps_seen.add(node.node_id)
        return PlannedValue(node_id=node.node_id, kind="step")

    def branch(self, *, start_from: str | None, frame: FrameType) -> bool:
        site = resolve_branch_call_site(frame)
        spec = self.validation.branch_conditions.get(site)
        if spec is None:
            raise InvalidBranchUsageError(
                "journey.branch(...) is only valid as a direct if/elif condition.",
                hint="Use journey.branch(...) directly as `if journey.branch(...):` or `elif journey.branch(...):`.",
            )

        if start_from is not None and start_from not in self._steps_seen:
            raise InvalidBranchUsageError(
                f"Branch '{spec.branch_key}' starts from step '{start_from}', but that step was never created earlier in the journey.",
                hint="Pass the result of an earlier step(...) call to branch(start_from=...).",
            )

        state = self._active_branch_chains.get(spec.template_key)
        if spec.condition_index == 1:
            if state is not None:
                raise InvalidBranchUsageError(
                    "journey.branch(...) re-entered the same if/elif chain before the prior branch selection finished.",
                    hint="Keep journey.branch(...) in one direct if/elif chain without reusing it in helper callbacks.",
                )
            state = _ActiveBranchChain(
                group_id=self._next_group_id(),
                cases=[],
            )
            self._active_branch_chains[spec.template_key] = state
        elif state is None:
            raise InvalidBranchUsageError(
                "journey.branch(...) did not follow the expected if/elif chain.",
                hint="Use journey.branch(...) only in one direct if/elif chain.",
            )

        selected_case = BranchCase(key=spec.branch_key, start_from=start_from)
        state.cases.append(selected_case)

        active_key = self.branch_env.get(state.group_id)
        if active_key is None:
            if spec.condition_index == spec.total_conditions:
                self._active_branch_chains.pop(spec.template_key, None)
                raise _SuspendPlanning(group_id=state.group_id, cases=list(state.cases))
            return False

        if active_key != spec.branch_key:
            if spec.condition_index == spec.total_conditions:
                self._active_branch_chains.pop(spec.template_key, None)
                raise InvalidBranchUsageError(
                    f"The planner selected branch key '{active_key}', but that option does not exist in branch group '{state.group_id}'.",
                    hint="Re-run planning from scratch if the branch options changed.",
                )
            return False

        marker = BranchMarkerNode(
            node_id=self._next_node_id(),
            group_id=state.group_id,
            active_key=active_key,
            start_from=start_from,
        )
        self.nodes.append(marker)
        self._active_branch_chains.pop(spec.template_key, None)
        return True


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
) -> StepRetry | None:
    resolved_retry = _normalize_retry_count(retry)
    delay_seconds = _duration_to_seconds(retry_delay, field_name="retry_delay")
    from_node_id: str | None = None

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

    else:
        raise TypeError(
            "step(..., retry_from=...) accepts an earlier step() result or None."
        )

    if resolved_retry == 0:
        return None

    return StepRetry(
        retries=resolved_retry,
        delay_seconds=delay_seconds,
        from_node_id=from_node_id,
    )


def compile_journey(journey_fn: JourneyEntrypoint) -> JourneyPlan:
    """Compile one journey function into linear case plans."""

    if not callable(journey_fn):
        raise TypeError("compile_journey() expects a callable journey function.")

    validation = validate_journey(journey_fn)

    queue: deque[BranchEnv] = deque([{}])
    case_plans: list[CasePlan] = []

    while queue:
        env = queue.popleft()
        session = _PlanSession(env, validation=validation)

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
