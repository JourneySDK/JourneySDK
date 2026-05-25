from __future__ import annotations

import base64
import json
import pickle
from types import TracebackType

import journeysdk.errors as journey_errors
import journeysdk.executor as journey_executor
import journeysdk.models as journey_models
import journeysdk as journey_sdk
import pytest
from journeysdk.errors import (
    AmbiguousStepSelectionError,
    CallableExecutionError,
    CorruptExecutionStateError,
    ExecutionStateMismatchError,
    ExecutionStateSerializationError,
    InvalidBranchUsageError,
    StepNotFoundError,
    UnsupportedControlFlowError,
    UnsupportedLoopError,
)
from journeysdk.models import BranchMarkerNode, StepNode, StepRetry
from journeysdk.state import ExecutionStateEnvelope, SelectedCaseState


def _labels(case_plan):
    labels: list[str] = []
    for node in case_plan.nodes:
        label = getattr(node, "label", None)
        if label is not None:
            labels.append(label)
    return labels


def _record_labels(case_report):
    return [record.label for record in case_report.records if record.label is not None]


class _ReplayValue:
    events: list[str] = []
    fail_store_message: str | None = None
    fail_store_boundary: str | None = None
    fail_restore_message: str | None = None
    fail_restore_boundary: str | None = None

    def __init__(self, seed: str, *, mode: str) -> None:
        self.seed = seed
        self.mode = mode

    @classmethod
    def reset(cls) -> None:
        cls.events = []
        cls.fail_store_message = None
        cls.fail_store_boundary = None
        cls.fail_restore_message = None
        cls.fail_restore_boundary = None

    def __store__(self, context) -> object:
        if (
            self.fail_store_message is not None
            and (
                self.fail_store_boundary is None
                or self.fail_store_boundary == context.boundary_kind
            )
        ):
            raise RuntimeError(self.fail_store_message)
        type(self).events.append(
            f"store_{self.seed}_{self.mode}_{context.boundary_kind}_{context.boundary_id}"
        )
        return {
            "seed": self.seed,
            "mode": self.mode,
        }

    @classmethod
    def __restore__(cls, payload: object, context) -> "_ReplayValue":
        if (
            cls.fail_restore_message is not None
            and (
                cls.fail_restore_boundary is None
                or cls.fail_restore_boundary == context.boundary_kind
            )
        ):
            raise RuntimeError(cls.fail_restore_message)
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        seed = payload["seed"]
        mode = payload["mode"]
        cls.events.append(
            f"restore_{seed}_{mode}_{context.boundary_kind}_{context.boundary_id}"
        )
        return cls(seed, mode=mode)


class _StepExitValue:
    def __init__(
        self,
        events: list[str],
        name: str,
        *,
        fail_message: str | None = None,
    ) -> None:
        self._events = events
        self._name = name
        self._fail_message = fail_message
        self.closed = False

    def __store__(self, context) -> object:
        state = "closed" if self.closed else "open"
        self._events.append(f"{self._name}:store_{state}")
        return {
            "name": self._name,
            "closed": self.closed,
        }

    @classmethod
    def __restore__(cls, payload: object, context) -> "_StepExitValue":
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        value = cls([], str(payload["name"]))
        value.closed = bool(payload["closed"])
        return value

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        suffix = exc_type.__name__ if exc_type is not None else "None"
        self._events.append(f"{self._name}:{suffix}")
        self.closed = True
        if self._fail_message is not None:
            raise RuntimeError(self._fail_message)
        return True


class _CaseExitValue:
    restore_events: list[str] = []

    def __init__(
        self,
        events: list[str],
        name: str,
        *,
        fail_message: str | None = None,
    ) -> None:
        self._events = events
        self._name = name
        self._fail_message = fail_message
        self.closed = False

    def __store__(self, context) -> object:
        return {
            "name": self._name,
            "fail_message": self._fail_message,
        }

    @classmethod
    def __restore__(cls, payload: object, context) -> "_CaseExitValue":
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        name = str(payload["name"])
        cls.restore_events.append(f"{name}:restore_{context.boundary_kind}")
        fail_message = payload.get("fail_message")
        return cls(
            cls.restore_events,
            name,
            fail_message=fail_message if isinstance(fail_message, str) else None,
        )

    def __case_exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.closed:
            return True
        suffix = exc_type.__name__ if exc_type is not None else "None"
        self._events.append(f"{self._name}:case_exit_{suffix}")
        self.closed = True
        if self._fail_message is not None:
            raise RuntimeError(self._fail_message)
        return True


def _without_closed_store_events(events: list[str]) -> list[str]:
    return [event for event in events if not event.endswith(":store_closed")]


class _RecordingObserver(journey_executor._ExecutionObserver):
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def on_journey_start(self, *, plan, selected_cases) -> None:
        self.events.append(
            ("journey_start", [item.case_plan.case_id for item in selected_cases])
        )

    def on_case_start(self, *, case_plan, stop_after_index, replay_anchor) -> None:
        self.events.append(("case_start", case_plan.case_id))

    def on_case_resume(
        self,
        *,
        case_plan,
        stop_after_index,
        replay_anchor,
        replay_from_index,
    ) -> None:
        self.events.append(("case_resume", case_plan.case_id, replay_from_index))

    def on_step_start(self, *, case_plan, node, node_index, attempt) -> None:
        self.events.append(("step_start", node.label, attempt))

    def on_branch(self, *, case_plan, node, node_index) -> None:
        self.events.append(("branch", node.group_id, node.active_key))

    def on_retry(
        self,
        *,
        case_plan,
        node,
        node_index,
        attempt,
        duration_seconds,
        delay_seconds,
        remaining_retries,
        error,
    ) -> None:
        self.events.append(
            (
                "retry",
                node.label,
                attempt,
                remaining_retries,
                delay_seconds,
                type(error).__name__,
                duration_seconds >= 0,
            )
        )

    def on_step_success(
        self,
        *,
        case_plan,
        node,
        node_index,
        attempt,
        duration_seconds,
    ) -> None:
        self.events.append(("step_ok", node.label, attempt, duration_seconds >= 0))

    def on_step_failure(
        self,
        *,
        case_plan,
        node,
        node_index,
        attempt,
        duration_seconds,
        error,
    ) -> None:
        self.events.append(
            (
                "step_failed",
                node.label,
                attempt,
                type(error).__name__,
                duration_seconds >= 0,
            )
        )

    def on_step_interrupted(
        self,
        *,
        case_plan,
        node,
        node_index,
        attempt,
        duration_seconds,
        error,
    ) -> None:
        self.events.append(
            (
                "step_interrupted",
                node.label,
                attempt,
                type(error).__name__,
                duration_seconds >= 0,
            )
        )

    def on_case_complete(self, *, case_plan, report, duration_seconds) -> None:
        self.events.append(
            (
                "case_complete",
                report.case_id,
                report.stopped_at_label,
                report.replay_anchor,
                duration_seconds >= 0,
            )
        )

    def on_journey_complete(self, *, report) -> None:
        self.events.append(
            ("journey_complete", [case.case_id for case in report.case_reports])
        )


def _execute_with_observer(journey, observer, *, step=None, state=None):
    plan = journey_sdk.compile_journey(journey)
    return journey_executor._execute_plan(
        journey,
        plan=plan,
        step=step,
        state=state,
        observer=observer,
    )


def test_legacy_result_objects_and_errors_are_not_public():
    for statement in (
        "from journeysdk.models import EvalResult",
        "from journeysdk.models import WaitResult",
        "from journeysdk.models import RunResult",
        "from journeysdk.models import RunContext",
        "from journeysdk.models import RetryPolicy",
        "from journeysdk import EvalResult",
        "from journeysdk import WaitResult",
        "from journeysdk import RunResult",
        "from journeysdk import RetryPolicy",
        "from journeysdk import retry",
        "from journeysdk.errors import DuplicateBranchKeyError",
        "from journeysdk import DuplicateBranchKeyError",
        "from journeysdk.errors import EvaluationFailedError",
        "from journeysdk.errors import WaitFailedError",
        "from journeysdk import EvaluationFailedError",
        "from journeysdk import WaitFailedError",
    ):
        with pytest.raises(ImportError):
            exec(statement, {})

    for name in ("EvalResult", "WaitResult", "RunResult", "RunContext", "RetryPolicy"):
        assert not hasattr(journey_models, name)
        assert not hasattr(journey_sdk, name)

    assert not hasattr(journey_sdk, "retry")
    assert not hasattr(journey_errors, "DuplicateBranchKeyError")
    assert not hasattr(journey_sdk, "DuplicateBranchKeyError")

    for name in ("EvaluationFailedError", "WaitFailedError"):
        assert not hasattr(journey_errors, name)
        assert not hasattr(journey_sdk, name)


def test_single_branch_group_compiles_to_one_case_per_branch():
    def signup_user():
        return {"user_id": "u-1"}

    def user_buys_something():
        return {"order_id": "o-1"}

    def user_subscribes():
        return {"sub_id": "s-1"}

    def user_signs_out():
        return True

    def assert_ok(target):
        return True

    def temporal_fast_forward(_duration):
        return True

    def journey():
        signup = journey_sdk.step(signup_user)
        after_signup = journey_sdk.step(assert_ok, signup)
        if journey_sdk.branch(start_from=after_signup):
            order = journey_sdk.step(user_buys_something)
            journey_sdk.step(assert_ok, order)
        elif journey_sdk.branch(start_from=after_signup):
            sub = journey_sdk.step(user_subscribes)
            journey_sdk.step(temporal_fast_forward, "30d")
            journey_sdk.step(assert_ok, sub)
        elif journey_sdk.branch():
            out = journey_sdk.step(user_signs_out)
            journey_sdk.step(assert_ok, out)

    plan = journey_sdk.compile_journey(journey)
    assert len(plan.case_plans) == 3

    label_paths = sorted(_labels(case) for case in plan.case_plans)
    assert label_paths == sorted(
        [
            ["signup_user", "assert_ok", "user_buys_something", "assert_ok"],
            [
                "signup_user",
                "assert_ok",
                "user_subscribes",
                "temporal_fast_forward",
                "assert_ok",
            ],
            ["signup_user", "assert_ok", "user_signs_out", "assert_ok"],
        ]
    )


def test_nested_branch_groups_expand_recursively_only_when_reachable():
    def journey():
        if journey_sdk.branch():
            if journey_sdk.branch():
                journey_sdk.step(c)
            elif journey_sdk.branch():
                journey_sdk.step(d)
        elif journey_sdk.branch():
            journey_sdk.step(b)

    def a():
        return True

    def b():
        return True

    def c():
        return True

    def d():
        return True

    plan = journey_sdk.compile_journey(journey)
    assert len(plan.case_plans) == 3
    assert sorted(_labels(case) for case in plan.case_plans) == [["b"], ["c"], ["d"]]


def test_branch_start_from_rejects_non_step_value():
    def path_a():
        return True

    def path_b():
        return True

    def journey():
        if journey_sdk.branch(start_from="missing_step"):
            journey_sdk.step(path_a)
        elif journey_sdk.branch():
            journey_sdk.step(path_b)

    with pytest.raises(TypeError):
        journey_sdk.compile_journey(journey)


def test_branch_rejects_name_argument():
    with pytest.raises(TypeError):
        journey_sdk.branch(name="x")


def test_execute_step_runs_single_matching_flow_and_stops_after_target():
    events: list[str] = []

    def signup_user():
        events.append("signup")
        return "u1"

    def user_subscribes():
        events.append("subscribe")
        return "sub1"

    def user_signs_out():
        events.append("signout")
        return "out"

    def user_buys_something():
        events.append("buy")
        return "order"

    def temporal_fast_forward(days):
        events.append(f"wait_{days}")
        return True

    def assert_ok(target):
        return True

    def stripe_invoice_paid(target):
        events.append("invoice_paid")
        return True

    def after_invoice():
        events.append("after_invoice")
        return True

    def journey():
        signup = journey_sdk.step(signup_user)
        after_signup = journey_sdk.step(assert_ok, signup)
        if journey_sdk.branch(start_from=after_signup):
            order = journey_sdk.step(user_buys_something)
            journey_sdk.step(assert_ok, order)
        elif journey_sdk.branch(start_from=after_signup):
            sub = journey_sdk.step(user_subscribes)
            journey_sdk.step(temporal_fast_forward, "30d")
            journey_sdk.step(stripe_invoice_paid, sub)
            journey_sdk.step(after_invoice)
        elif journey_sdk.branch():
            out = journey_sdk.step(user_signs_out)
            journey_sdk.step(assert_ok, out)

    report = journey_sdk.execute(journey, step="stripe_invoice_paid")

    assert len(report.case_reports) == 1
    case_report = report.case_reports[0]
    assert case_report.stopped_at_label == "stripe_invoice_paid"
    assert case_report.replay_anchor == "assert_ok"

    labels = [record.label for record in case_report.records if record.label is not None]
    assert labels[-1] == "stripe_invoice_paid"
    assert "after_invoice" not in labels

    assert events == ["signup", "subscribe", "wait_30d", "invoice_paid"]


def test_execute_step_raises_ambiguity_when_label_matches_multiple_cases():
    def branch_a():
        return True

    def branch_b():
        return True

    def step():
        return True

    def journey():
        if journey_sdk.branch():
            journey_sdk.step(step)
        elif journey_sdk.branch():
            journey_sdk.step(step)

    with pytest.raises(AmbiguousStepSelectionError):
        journey_sdk.execute(journey, step="step")


def test_execute_step_raises_not_found_for_unknown_label():
    def step():
        return True

    def journey():
        journey_sdk.step(step)

    with pytest.raises(StepNotFoundError):
        journey_sdk.execute(journey, step="unknown")


def test_execute_rejects_removed_only_step_keyword():
    def step():
        return True

    def journey():
        journey_sdk.step(step)

    with pytest.raises(TypeError):
        journey_sdk.execute(journey, only_step="step")


def test_validator_rejects_while_loops():
    def step():
        return True

    def journey():
        i = 0
        while i < 1:
            journey_sdk.step(step)
            i += 1

    with pytest.raises(UnsupportedLoopError):
        journey_sdk.compile_journey(journey)


def test_validator_rejects_mixed_boolean_with_inline_branch():
    flag = True

    def a():
        return True

    def b():
        return True

    def step():
        return True

    def journey():
        if flag and journey_sdk.branch():
            journey_sdk.step(step)
        elif journey_sdk.branch():
            journey_sdk.step(step)

    with pytest.raises(InvalidBranchUsageError):
        journey_sdk.compile_journey(journey)


def test_validator_rejects_non_literal_range_loop():
    def step():
        return True

    def journey():
        count = 2
        for _ in range(count):
            journey_sdk.step(step)

    with pytest.raises(UnsupportedLoopError):
        journey_sdk.compile_journey(journey)


def test_validator_rejects_branching_on_legacy_ok_attribute():
    def step():
        return True

    def branch_step():
        return True

    def journey():
        result = journey_sdk.step(step)
        if result.ok:
            journey_sdk.step(branch_step)

    with pytest.raises(UnsupportedControlFlowError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert str(exc_info.value) == (
        "Branching on prior step result fields is not supported in journey v1."
    )


def test_compile_rejects_duplicate_prompt_memory_names():
    def first_prompt_step():
        page.prompt("first", memory="shared")

    def second_prompt_step():
        page.prompt("second", memory="shared")

    def journey():
        journey_sdk.step(first_prompt_step)
        journey_sdk.step(second_prompt_step)

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "Prompt memory name 'shared' is used by more than one prompt(...) call" in str(
        exc_info.value
    )
    assert "Use one unique memory name" in exc_info.value.hint


def test_compile_rejects_repeated_prompt_memory_invocation():
    def prompt_step():
        page.prompt("first", memory="shared")

    def journey():
        journey_sdk.step(prompt_step)
        journey_sdk.step(prompt_step)

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "Prompt memory name 'shared' is used by more than one prompt(...) call" in str(
        exc_info.value
    )


def test_compile_allows_implicit_prompt_memory_names():
    def first_prompt_step():
        page.prompt("first")

    def second_prompt_step():
        page.prompt("second")

    def journey():
        journey_sdk.step(first_prompt_step)
        journey_sdk.step(second_prompt_step)

    plan = journey_sdk.compile_journey(journey)

    assert _labels(plan.case_plans[0]) == ["first_prompt_step", "second_prompt_step"]


def test_compile_rejects_repeated_implicit_prompt_memory_invocation():
    def prompt_step():
        page.prompt("first")

    def journey():
        journey_sdk.step(prompt_step)
        journey_sdk.step(prompt_step)

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert (
        "Prompt memory name 'prompt-step-prompt-1' is used by more than one "
        "prompt(...) call"
    ) in str(exc_info.value)


def test_compile_treats_prompt_memory_none_as_disabled():
    def prompt_step():
        page.prompt("first", memory=None)

    def journey():
        journey_sdk.step(prompt_step)
        journey_sdk.step(prompt_step)

    plan = journey_sdk.compile_journey(journey)

    assert _labels(plan.case_plans[0]) == ["prompt_step", "prompt_step"]


def test_compile_allows_shared_prompt_memory_before_branch_cases():
    def prompt_step():
        page.prompt("first", memory="shared")

    def branch_a_step():
        return True

    def branch_b_step():
        return True

    def journey():
        journey_sdk.step(prompt_step)
        if journey_sdk.branch():
            journey_sdk.step(branch_a_step)
        elif journey_sdk.branch():
            journey_sdk.step(branch_b_step)

    plan = journey_sdk.compile_journey(journey)

    assert sorted(_labels(case) for case in plan.case_plans) == [
        ["prompt_step", "branch_a_step"],
        ["prompt_step", "branch_b_step"],
    ]


def test_compile_rejects_dynamic_prompt_memory_names():
    def prompt_step():
        memory_name = "shared"
        page.prompt("first", memory=memory_name)

    def journey():
        journey_sdk.step(prompt_step)

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "prompt(..., memory=...) must use a string literal or None" in str(
        exc_info.value
    )


def test_plan_includes_branch_marker_nodes_with_start_from_metadata():
    def a():
        return True

    def b():
        return True

    def step_a():
        return True

    def step_b():
        return True

    def journey():
        anchor = journey_sdk.step(a)
        if journey_sdk.branch(start_from=anchor):
            journey_sdk.step(step_a)
        elif journey_sdk.branch():
            journey_sdk.step(step_b)

    plan = journey_sdk.compile_journey(journey)
    assert len(plan.case_plans) == 2

    for case in plan.case_plans:
        markers = [node for node in case.nodes if isinstance(node, BranchMarkerNode)]
        assert len(markers) == 1
        if _labels(case) == ["a", "step_a"]:
            anchor_node = next(node for node in case.nodes if isinstance(node, StepNode) and node.label == "a")
            assert markers[0].start_from == anchor_node.node_id
        else:
            assert _labels(case) == ["a", "step_b"]
            assert markers[0].start_from is None


def test_branch_call_outside_inline_if_chain_is_rejected():
    def journey():
        journey_sdk.branch()

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "direct if/elif condition" in str(exc_info.value)
    assert exc_info.value.hint is not None


def test_assigned_branch_handles_can_be_reused_without_new_cases():
    events: list[str] = []

    def step_1():
        events.append("step_1")

    def step_2():
        events.append("step_2")

    def step_3():
        events.append("step_3")

    def step_4():
        events.append("step_4")

    def step_5():
        events.append("step_5")

    def step_6():
        events.append("step_6")

    def step_7():
        events.append("step_7")

    def step_8():
        events.append("step_8")

    def journey():
        s1 = journey_sdk.step(step_1)
        s2 = journey_sdk.step(step_2)

        b1 = journey_sdk.branch(start_from=s1)
        b2 = journey_sdk.branch(start_from=s2)

        if b1:
            journey_sdk.step(step_3)
        elif b2:
            journey_sdk.step(step_4)

        s5 = journey_sdk.step(step_5)
        s6 = journey_sdk.step(step_6)

        b3 = journey_sdk.branch(start_from=s5)
        b4 = journey_sdk.branch(start_from=s6)

        if b1:
            journey_sdk.step(step_7)
        elif b2:
            journey_sdk.step(step_8)

    report = journey_sdk.execute(journey)

    assert events == [
        "step_1",
        "step_2",
        "step_3",
        "step_5",
        "step_6",
        "step_7",
        "step_4",
        "step_5",
        "step_6",
        "step_8",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert [_record_labels(case) for case in report.case_reports] == [
        ["step_1", "step_2", "step_3", "step_5", "step_6", "step_7"],
        ["step_1", "step_2", "step_4", "step_5", "step_6", "step_8"],
    ]


def test_labels_default_to_function_names_when_missing():
    def run_step():
        return "ok"

    def evaluate_step(_target):
        return True

    def wait_step():
        return True

    def journey():
        value = journey_sdk.step(run_step)
        journey_sdk.step(evaluate_step, value)
        journey_sdk.step(wait_step)

    plan = journey_sdk.compile_journey(journey)
    assert len(plan.case_plans) == 1
    assert _labels(plan.case_plans[0]) == ["run_step", "evaluate_step", "wait_step"]


def test_branches_accept_branch_variables_without_callables():
    def prepare_anchor():
        return "anchor"

    def branch_a_step():
        return "a"

    def branch_b_step():
        return "b"

    def journey():
        anchor = journey_sdk.step(prepare_anchor)
        if journey_sdk.branch(start_from=anchor):
            journey_sdk.step(branch_a_step)
        elif journey_sdk.branch():
            journey_sdk.step(branch_b_step)

    plan = journey_sdk.compile_journey(journey)
    assert sorted(_labels(case) for case in plan.case_plans) == [
        ["prepare_anchor", "branch_a_step"],
        ["prepare_anchor", "branch_b_step"],
    ]

    branch_a_case = next(
        case for case in plan.case_plans if _labels(case) == ["prepare_anchor", "branch_a_step"]
    )
    anchor_node = next(node for node in branch_a_case.nodes if isinstance(node, StepNode) and node.label == "prepare_anchor")
    marker = next(node for node in branch_a_case.nodes if isinstance(node, BranchMarkerNode))
    assert marker.start_from == anchor_node.node_id

    report = journey_sdk.execute(journey, step="branch_a_step")
    assert len(report.case_reports) == 1
    assert report.case_reports[0].replay_anchor == "prepare_anchor"


def test_step_rejects_calls_outside_planning_or_execution():
    called = False

    def outside_step():
        nonlocal called
        called = True
        return False

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.step(outside_step)

    assert called is False
    assert str(exc_info.value) == (
        "step() can only be used while a journey is being planned or executed."
    )
    assert exc_info.value.hint == "Call step() inside a function decorated with @journey."


def test_plan_includes_retry_metadata_for_step_anchors():
    def prepare():
        return "prepared"

    def poll_from_step():
        return True

    def poll_from_prepare():
        return True

    def journey():
        prepared = journey_sdk.step(prepare)
        journey_sdk.step(
            poll_from_step,
            retry=6,
            retry_delay=5,
            retry_from=prepared,
        )
        journey_sdk.step(
            poll_from_prepare,
            retry=10,
            retry_delay=2,
            retry_from=prepared,
        )

    plan = journey_sdk.compile_journey(journey)
    step_nodes = [node for node in plan.case_plans[0].nodes if isinstance(node, StepNode)]

    assert step_nodes[0].retry is None
    assert step_nodes[1].retry == StepRetry(
        retries=6,
        delay_seconds=5.0,
        from_node_id=step_nodes[0].node_id,
    )
    assert step_nodes[2].retry == StepRetry(
        retries=10,
        delay_seconds=2.0,
        from_node_id=step_nodes[0].node_id,
    )


def test_plan_requires_positive_retry_to_emit_retry_metadata():
    def same_step_retry():
        return True

    def anchored_retry():
        return True

    def delay_only():
        return True

    def anchor_only():
        return True

    def disabled():
        return True

    def anchor_step():
        return True

    def journey():
        anchor = journey_sdk.step(anchor_step)
        journey_sdk.step(same_step_retry, retry=2, retry_delay=1.5)
        journey_sdk.step(anchored_retry, retry=1, retry_from=anchor)
        journey_sdk.step(delay_only, retry_delay=1.5)
        journey_sdk.step(anchor_only, retry_from=anchor)
        journey_sdk.step(disabled, retry=0, retry_delay=0, retry_from=anchor)

    plan = journey_sdk.compile_journey(journey)
    step_nodes = [node for node in plan.case_plans[0].nodes if isinstance(node, StepNode)]

    assert step_nodes[0].retry is None
    assert step_nodes[1].retry == StepRetry(
        retries=2,
        delay_seconds=1.5,
        from_node_id=step_nodes[1].node_id,
    )
    assert step_nodes[2].retry == StepRetry(
        retries=1,
        delay_seconds=5.0,
        from_node_id=step_nodes[0].node_id,
    )
    assert step_nodes[3].retry is None
    assert step_nodes[4].retry is None
    assert step_nodes[5].retry is None


def test_execute_step_without_retry_does_not_retry():
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        raise RuntimeError("still waiting")

    def journey():
        journey_sdk.step(poll)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert attempts["poll"] == 1
    assert "failed while it was running" in str(exc_info.value)


def test_execute_step_preserves_actionable_exception_hint():
    def poll():
        error = RuntimeError("missing provider credential")
        setattr(error, "hint", "Set ANTHROPIC_API_KEY before rerunning.")
        raise error

    def journey():
        journey_sdk.step(poll)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "failed while it was running" in str(exc_info.value)
    assert exc_info.value.hint == "Set ANTHROPIC_API_KEY before rerunning."


def test_execute_reports_exhausted_retry_attempts():
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        raise RuntimeError("still waiting")

    def journey():
        journey_sdk.step(poll, retry=1, retry_delay=0)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert attempts["poll"] == 2
    assert "retry attempts were exhausted" in str(exc_info.value)
    assert exc_info.value.hint == (
        "Inspect the step implementation, or increase step(..., retry=...) if "
        "the failure is expected to clear on its own."
    )


def test_execute_retries_current_step_without_rerunning_prior_steps():
    events: list[str] = []
    attempts = {"poll": 0}

    def prepare():
        events.append("prepare")
        return "ready"

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] < 3:
            raise RuntimeError("still waiting")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(poll, retry=2, retry_delay=0)

    report = journey_sdk.execute(journey)

    assert events == ["prepare", "poll_1", "poll_2", "poll_3"]
    assert _record_labels(report.case_reports[0]) == ["prepare", "poll"]


def test_execute_retry_delay_only_does_not_retry():
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        raise RuntimeError("still waiting")

    def journey():
        journey_sdk.step(poll, retry_delay=0)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert attempts["poll"] == 1
    assert "failed while it was running" in str(exc_info.value)


def test_execute_retry_from_only_does_not_retry(monkeypatch):
    events: list[str] = []
    sleeps: list[float] = []

    monkeypatch.setattr(journey_executor.time, "sleep", lambda seconds: sleeps.append(seconds))

    def issue_request():
        issued = len([event for event in events if event.startswith("issue_")]) + 1
        events.append(f"issue_{issued}")
        return issued

    def confirm_request(issued):
        events.append(f"confirm_{issued}")
        if issued < 4:
            raise RuntimeError("not confirmed yet")
        return True

    def journey():
        issued = journey_sdk.step(issue_request)
        journey_sdk.step(confirm_request, issued, retry_from=issued)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert events == [
        "issue_1",
        "confirm_1",
    ]
    assert sleeps == []
    assert "failed while it was running" in str(exc_info.value)


def test_execute_retries_from_prior_step_result_and_reruns_anchor_step():
    events: list[str] = []
    attempts = {"issue": 0}

    def issue_request():
        attempts["issue"] += 1
        events.append(f"issue_{attempts['issue']}")
        return attempts["issue"]

    def confirm_request(issued):
        events.append(f"confirm_{issued}")
        if issued < 2:
            raise RuntimeError("not confirmed yet")
        return True

    def finalize():
        events.append("finalize")
        return True

    def journey():
        issued = journey_sdk.step(issue_request)
        journey_sdk.step(
            confirm_request,
            issued,
            retry=1,
            retry_delay=0,
            retry_from=issued,
        )
        journey_sdk.step(finalize)

    report = journey_sdk.execute(journey)

    assert events == ["issue_1", "confirm_1", "issue_2", "confirm_2", "finalize"]
    assert _record_labels(report.case_reports[0]) == [
        "issue_request",
        "confirm_request",
        "finalize",
    ]


def test_execute_retries_from_step_anchor_reruns_anchor_step():
    events: list[str] = []
    attempts = {"poll": 0}

    def begin_polling():
        events.append("begin")
        return {"request_id": "req-1"}

    def poll(request):
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}_{request['request_id']}")
        if attempts["poll"] < 2:
            raise RuntimeError("pending")
        return True

    def finish(request):
        events.append(f"finish_{request['request_id']}")
        return True

    def journey():
        request = journey_sdk.step(begin_polling)
        journey_sdk.step(
            poll,
            request,
            retry=1,
            retry_delay=0,
            retry_from=request,
        )
        journey_sdk.step(finish, request)

    report = journey_sdk.execute(journey)

    assert events == [
        "begin",
        "poll_1_req-1",
        "begin",
        "poll_2_req-1",
        "finish_req-1",
    ]
    assert _record_labels(report.case_reports[0]) == ["begin_polling", "poll", "finish"]


def test_execute_protocol_value_replays_from_step_anchor_on_retry():
    _ReplayValue.reset()
    events: list[str] = []
    attempts = {"poll": 0}

    def create_value():
        return _ReplayValue("seed-1", mode="step-anchor")

    def refresh(value):
        events.append(f"refresh_{value.seed}")
        return True

    def poll(value):
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}_{value.seed}")
        if attempts["poll"] == 1:
            raise RuntimeError("pending")
        return True

    def journey():
        value = journey_sdk.step(create_value)
        journey_sdk.step(refresh, value)
        journey_sdk.step(
            poll,
            value,
            retry=1,
            retry_delay=0,
            retry_from=value,
        )

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "refresh_seed-1",
        "poll_1_seed-1",
        "refresh_seed-1",
        "poll_2_seed-1",
    ]
    assert _ReplayValue.events == []


def test_execute_state_does_not_store_non_replayable_protocol_values(tmp_path):
    state_file = tmp_path / "journey.state"
    _ReplayValue.reset()

    def create_value():
        return _ReplayValue("seed-1", mode="plain")

    def consume(value):
        return value.seed == "seed-1"

    def journey():
        value = journey_sdk.step(create_value)
        journey_sdk.step(consume, value)

    journey_sdk.execute(journey, state=state_file)

    assert _ReplayValue.events == []
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["active_case"] is None


def test_execute_retry_from_without_retry_does_not_store_replay_values(tmp_path):
    state_file = tmp_path / "journey.state"
    _ReplayValue.reset()

    def create_value():
        return _ReplayValue("seed-1", mode="disabled-retry")

    def confirm(_value):
        raise RuntimeError("not ready")

    def journey():
        value = journey_sdk.step(create_value)
        journey_sdk.step(confirm, value, retry_from=value)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, state=state_file)

    assert _ReplayValue.events == []


def test_execute_same_step_retry_stores_only_replayable_inputs_in_state(tmp_path):
    state_file = tmp_path / "journey.state"
    _ReplayValue.reset()
    attempts = {"poll": 0}

    def poll(value):
        attempts["poll"] += 1
        if attempts["poll"] == 1:
            raise RuntimeError("pending")
        return value.seed

    def finish():
        return True

    def journey():
        token = _ReplayValue("seed-1", mode="same-step-input")
        journey_sdk.step(poll, token, retry=1, retry_delay=0)
        journey_sdk.step(finish)

    journey_sdk.execute(journey, state=state_file)

    assert _ReplayValue.events == ["store_seed-1_same-step-input_state_case_1"]


def test_execute_step_supports_retryable_target_steps():
    events: list[str] = []
    attempts = {"publish": 0}

    def prepare():
        events.append("prepare")
        return True

    def publish():
        attempts["publish"] += 1
        events.append(f"publish_{attempts['publish']}")
        if attempts["publish"] < 2:
            raise RuntimeError("not published yet")
        return True

    def cleanup():
        events.append("cleanup")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(publish, retry=1, retry_delay=0)
        journey_sdk.step(cleanup)

    report = journey_sdk.execute(journey, step="publish")

    assert events == ["prepare", "publish_1", "publish_2"]
    assert len(report.case_reports) == 1
    assert report.case_reports[0].stopped_at_label == "publish"
    assert _record_labels(report.case_reports[0]) == ["prepare", "publish"]


def test_execute_returned_step_exit_objects_run_after_storage_and_before_next_step(tmp_path):
    events: list[str] = []
    state_file = tmp_path / "cleanup.state"

    def allocate():
        events.append("allocate")
        return [
            _StepExitValue(events, "cleanup_first"),
            _StepExitValue(events, "cleanup_second"),
        ]

    def use_resource(resources):
        events.append(f"use_{resources[0].closed}_{resources[1].closed}")
        return True

    def journey():
        resource = journey_sdk.step(allocate)
        journey_sdk.step(use_resource, resource)

    journey_sdk.execute(journey, state=state_file)

    assert _without_closed_store_events(events) == [
        "allocate",
        "cleanup_second:None",
        "cleanup_first:None",
        "use_True_True",
    ]


def test_execute_nested_returned_step_exit_objects_run_lifo_and_deduplicated():
    events: list[str] = []

    def allocate():
        first = _StepExitValue(events, "cleanup_first")
        second = _StepExitValue(events, "cleanup_second")
        return {
            "first": first,
            "nested": [second, first],
        }

    def journey():
        journey_sdk.step(allocate)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "cleanup_second:None",
        "cleanup_first:None",
    ]


def test_execute_returned_case_exit_objects_run_after_case_success():
    events: list[str] = []

    def allocate():
        resource = _CaseExitValue(events, "case_cleanup")
        events.append("allocate")
        return {
            "resource": resource,
            "nested": [resource],
        }

    def use_resource(payload):
        events.append(f"use_{payload['resource'].closed}")
        return True

    def journey():
        resource = journey_sdk.step(allocate)
        journey_sdk.step(use_resource, resource)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "allocate",
        "use_False",
        "case_cleanup:case_exit_None",
    ]


def test_execute_returned_case_exit_objects_run_after_step_failure():
    events: list[str] = []

    def allocate():
        events.append("allocate")
        return _CaseExitValue(events, "case_cleanup")

    def fail(_resource):
        events.append("fail")
        raise RuntimeError("boom")

    def journey():
        resource = journey_sdk.step(allocate)
        journey_sdk.step(fail, resource)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, no_state=True)

    assert events == [
        "allocate",
        "fail",
        "case_cleanup:case_exit_CallableExecutionError",
    ]


def test_execute_returned_case_exit_objects_run_after_keyboard_interrupt():
    events: list[str] = []

    def allocate():
        events.append("allocate")
        return _CaseExitValue(events, "case_cleanup")

    def interrupt(_resource):
        events.append("interrupt")
        raise KeyboardInterrupt()

    def journey():
        resource = journey_sdk.step(allocate)
        journey_sdk.step(interrupt, resource)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, no_state=True)

    assert events == [
        "allocate",
        "interrupt",
        "case_cleanup:case_exit_KeyboardInterrupt",
    ]


def test_execute_case_exit_cleanup_failure_fails_successful_case():
    events: list[str] = []

    def allocate():
        return _CaseExitValue(events, "case_cleanup", fail_message="close failed")

    def journey():
        journey_sdk.step(allocate)

    with pytest.raises(RuntimeError) as exc_info:
        journey_sdk.execute(journey, no_state=True)

    assert "Case-exit cleanup failed" in str(exc_info.value)
    assert "close failed" in str(exc_info.value)
    assert events == ["case_cleanup:case_exit_None"]


def test_execute_restored_case_exit_objects_run_after_replay_case(tmp_path):
    state_file = tmp_path / "case-exit-replay.state"
    _CaseExitValue.restore_events = []

    def start_stack():
        _CaseExitValue.restore_events.append("start")
        return _CaseExitValue(_CaseExitValue.restore_events, "stack")

    def capture_anchor(_stack):
        _CaseExitValue.restore_events.append("anchor")
        return "anchor"

    def first_branch(_stack):
        _CaseExitValue.restore_events.append("first")
        return True

    def second_branch(_stack):
        _CaseExitValue.restore_events.append("second")
        return True

    def journey():
        stack = journey_sdk.step(start_stack)
        anchor = journey_sdk.step(capture_anchor, stack)
        if journey_sdk.branch(start_from=anchor):
            journey_sdk.step(first_branch, stack)
        elif journey_sdk.branch(start_from=anchor):
            journey_sdk.step(second_branch, stack)

    journey_sdk.execute(journey, state=state_file)

    assert _CaseExitValue.restore_events == [
        "start",
        "anchor",
        "first",
        "stack:case_exit_None",
        "stack:restore_branch-anchor",
        "second",
        "stack:case_exit_None",
    ]


def test_execute_step_exit_ignores_return_value_from_exit_method():
    events: list[str] = []

    def allocate():
        return _StepExitValue(events, "cleanup")

    def journey():
        journey_sdk.step(allocate)

    journey_sdk.execute(journey, no_state=True)

    assert events == ["cleanup:None"]


def test_execute_does_not_cleanup_unreturned_values_on_failure_retry_and_interrupt(tmp_path):
    events: list[str] = []
    attempts = {"flaky": 0}

    def flaky():
        attempts["flaky"] += 1
        attempt = attempts["flaky"]
        events.append(f"flaky_{attempt}")
        _StepExitValue(events, f"local_{attempt}")
        if attempt == 1:
            raise RuntimeError("pending")
        return _StepExitValue(events, f"returned_{attempt}")

    def interrupted():
        events.append("interrupted")
        _StepExitValue(events, "local_interrupted")
        raise KeyboardInterrupt()

    def journey():
        journey_sdk.step(flaky, retry=1, retry_delay=0)
        journey_sdk.step(interrupted)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=tmp_path / "interrupt.state")

    assert _without_closed_store_events(events) == [
        "flaky_1",
        "flaky_2",
        "returned_2:None",
        "interrupted",
    ]


def test_execute_returned_step_exit_objects_defer_during_develop_step_pause(tmp_path):
    events: list[str] = []
    state_file = tmp_path / "pause.state"

    def publish():
        events.append("publish")
        return _StepExitValue(events, "cleanup")

    def journey():
        journey_sdk.step(publish)

    plan = journey_sdk.compile_journey(journey)

    paused = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="publish",
        state=state_file,
    )

    assert isinstance(paused, journey_executor._PausedExecution)
    assert "publish" in events
    assert "cleanup:store_open" not in events
    assert "cleanup:None" not in events
    paused.close_pending_exits()
    assert events[-1] == "cleanup:None"
    paused.close_pending_exits()
    assert events[-1] == "cleanup:None"


def test_execute_step_lifecycle_reaches_post_exit_before_graceful_interrupt(tmp_path):
    events: list[str] = []
    state_file = tmp_path / "lifecycle.state"

    class RecordingInterruptController:
        def __init__(self) -> None:
            self.phases: list[str] = []
            self.pending = True

        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            if phase is not None:
                self.phases.append(phase)

        def is_step_interrupt_pending(self) -> bool:
            return self.pending

        def raise_if_interrupted_after_step(self) -> None:
            if self.pending:
                self.pending = False
                raise KeyboardInterrupt()

    def publish():
        events.append("publish")
        return _StepExitValue(events, "cleanup")

    def finish(value):
        events.append(f"finish_{value.closed}")
        return True

    def journey():
        value = journey_sdk.step(publish)
        journey_sdk.step(finish, value)

    controller = RecordingInterruptController()
    with pytest.raises(KeyboardInterrupt):
        with journey_executor._use_step_interrupt_controller(controller):
            journey_sdk.execute(journey, state=state_file)

    assert controller.phases == [
        "initialization",
        "execution",
        "storage",
        "pre-exit",
        "exit",
        "post-exit",
    ]
    assert events[:2] == [
        "publish",
        "cleanup:None",
    ]

    journey_sdk.execute(journey, state=state_file)

    assert events.count("publish") == 2
    assert "finish_True" in events


def test_execute_step_exit_cleanup_failure_fails_successful_step(tmp_path):
    events: list[str] = []
    should_fail = {"cleanup": True}
    attempts = {"publish": 0}

    def publish():
        attempts["publish"] += 1
        return _StepExitValue(
            events,
            "cleanup",
            fail_message="close failed" if should_fail["cleanup"] else None,
        )

    def journey():
        journey_sdk.step(publish)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey, state=tmp_path / "cleanup-failed.state")

    assert "Step-exit cleanup failed" in str(exc_info.value)
    assert "close failed" in str(exc_info.value)
    assert events == ["cleanup:None"]
    assert attempts["publish"] == 1


def test_execute_step_exit_cleanup_failure_discards_successful_step_result(tmp_path):
    events: list[str] = []
    should_fail = {"cleanup": True}
    attempts = {"publish": 0}
    state_file = tmp_path / "cleanup-failed.state"

    def publish():
        attempts["publish"] += 1
        return _StepExitValue(
            events,
            f"cleanup_{attempts['publish']}",
            fail_message="close failed" if should_fail["cleanup"] else None,
        )

    def journey():
        journey_sdk.step(publish)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, state=state_file)

    should_fail["cleanup"] = False
    journey_sdk.execute(journey, state=state_file)

    assert attempts["publish"] == 2
    assert _without_closed_store_events(events) == [
        "cleanup_1:None",
        "cleanup_2:None",
    ]


def test_execute_does_not_cleanup_unreturned_values_after_success():
    events: list[str] = []

    def publish():
        _StepExitValue(events, "local")
        return True

    def journey():
        journey_sdk.step(publish)

    journey_sdk.execute(journey)

    assert events == []


def test_execute_develop_step_continue_restarts_nonreplayable_prefix(
    tmp_path,
):
    state_file = tmp_path / "pause.state"
    events: list[str] = []

    def prepare():
        events.append("prepare")
        return True

    def publish():
        events.append("publish")
        return True

    def cleanup():
        events.append("cleanup")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(publish)
        journey_sdk.step(cleanup)

    plan = journey_sdk.compile_journey(journey)

    first = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="publish",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.label == "publish"
    assert first.paused_step.ok is True
    assert events == ["prepare", "publish"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="cleanup",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "cleanup"
    assert report.paused_step.ok is True
    assert events == ["prepare", "publish", "prepare", "publish", "cleanup"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="cleanup",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_models.ExecutionReport)
    assert events == [
        "prepare",
        "publish",
        "prepare",
        "publish",
        "cleanup",
        "prepare",
        "publish",
        "cleanup",
    ]
    assert report.case_reports[0].stopped_at_label is None
    assert report.case_reports[0].replay_anchor is None
    assert _record_labels(report.case_reports[0]) == ["prepare", "publish", "cleanup"]


def test_execute_develop_step_retry_rewinds_same_step_and_refreshes_retry_budget(
    tmp_path,
):
    state_file = tmp_path / "pause.state"
    events: list[str] = []
    attempts = {"poll": 0}

    def prepare():
        events.append("prepare")
        return True

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] < 3:
            raise RuntimeError("pending")
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(poll, retry=1, retry_delay=0)
        journey_sdk.step(finish)

    plan = journey_sdk.compile_journey(journey)

    first = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.label == "poll"
    assert first.paused_step.ok is False
    assert first.paused_step.attempt == 1
    assert events == ["prepare", "poll_1"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        pause_action="retry",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "poll"
    assert report.paused_step.ok is False
    assert report.paused_step.attempt == 2
    assert events == ["prepare", "poll_1", "poll_2"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        pause_action="retry",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "poll"
    assert report.paused_step.ok is True
    assert report.paused_step.attempt == 3
    assert events == ["prepare", "poll_1", "poll_2", "poll_3"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="finish",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "finish"
    assert report.paused_step.ok is True
    assert events == ["prepare", "poll_1", "poll_2", "poll_3", "poll_4", "finish"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="finish",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_models.ExecutionReport)
    assert events == [
        "prepare",
        "poll_1",
        "poll_2",
        "poll_3",
        "poll_4",
        "finish",
        "poll_5",
        "finish",
    ]
    assert _record_labels(report.case_reports[0]) == ["prepare", "poll", "finish"]


def test_execute_develop_step_retry_rewinds_from_step_result_anchor(
    tmp_path,
):
    state_file = tmp_path / "pause.state"
    events: list[str] = []
    attempts = {"poll": 0, "issue": 0}

    def issue_request():
        attempts["issue"] += 1
        request_id = f"req-{attempts['issue']}"
        events.append(f"issue_{request_id}")
        return {"request_id": request_id}

    def poll(request):
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}_{request['request_id']}")
        if attempts["poll"] < 3:
            raise RuntimeError("pending")
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        request = journey_sdk.step(issue_request)
        journey_sdk.step(poll, request, retry=1, retry_delay=0, retry_from=request)
        journey_sdk.step(finish)

    plan = journey_sdk.compile_journey(journey)

    first = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.ok is False
    assert first.paused_step.attempt == 1
    assert events == ["issue_req-1", "poll_1_req-1"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        pause_action="retry",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "poll"
    assert report.paused_step.ok is False
    assert report.paused_step.attempt == 2
    assert events == [
        "issue_req-1",
        "poll_1_req-1",
        "issue_req-2",
        "poll_2_req-2",
    ]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        pause_action="retry",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "poll"
    assert report.paused_step.ok is True
    assert report.paused_step.attempt == 3
    assert events == [
        "issue_req-1",
        "poll_1_req-1",
        "issue_req-2",
        "poll_2_req-2",
        "issue_req-3",
        "poll_3_req-3",
    ]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="finish",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "finish"
    assert report.paused_step.ok is True
    assert events == [
        "issue_req-1",
        "poll_1_req-1",
        "issue_req-2",
        "poll_2_req-2",
        "issue_req-3",
        "poll_3_req-3",
        "issue_req-4",
        "poll_4_req-4",
        "finish",
    ]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="finish",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_models.ExecutionReport)
    assert events == [
        "issue_req-1",
        "poll_1_req-1",
        "issue_req-2",
        "poll_2_req-2",
        "issue_req-3",
        "poll_3_req-3",
        "issue_req-4",
        "poll_4_req-4",
        "finish",
        "issue_req-5",
        "poll_5_req-5",
        "finish",
    ]


def test_execute_develop_step_cannot_continue_after_failed_pause(tmp_path):
    state_file = tmp_path / "pause.state"

    def poll():
        raise RuntimeError("pending")

    def finish():
        return True

    def journey():
        journey_sdk.step(poll, retry=5, retry_delay=0)
        journey_sdk.step(finish)

    plan = journey_sdk.compile_journey(journey)

    first = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="poll",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.ok is False
    assert first.paused_step.attempt == 1

    with pytest.raises(ExecutionStateMismatchError, match="Cannot continue past failed"):
        journey_executor._execute_plan(
            journey,
            plan=plan,
            develop_step="finish",
            pause_action="continue",
            state=state_file,
        )


def test_execute_develop_step_preserves_pre_target_retry_behavior(tmp_path):
    state_file = tmp_path / "pause.state"
    events: list[str] = []
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] < 2:
            raise RuntimeError("pending")
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        journey_sdk.step(poll, retry=1, retry_delay=0)
        journey_sdk.step(finish)

    plan = journey_sdk.compile_journey(journey)

    paused = journey_executor._execute_plan(
        journey,
        plan=plan,
        develop_step="finish",
        state=state_file,
    )

    assert isinstance(paused, journey_executor._PausedExecution)
    assert paused.paused_step.label == "finish"
    assert paused.paused_step.ok is True
    assert events == ["poll_1", "poll_2", "finish"]


def test_execute_retries_exception_until_step_succeeds():
    events: list[str] = []
    attempts = {"poll": 0}

    def prepare():
        events.append("prepare")
        return True

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] < 3:
            raise Exception("not ready")
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(poll, retry=2, retry_delay=0)
        journey_sdk.step(finish)

    report = journey_sdk.execute(journey)

    assert events == ["prepare", "poll_1", "poll_2", "poll_3", "finish"]
    assert _record_labels(report.case_reports[0]) == ["prepare", "poll", "finish"]


def test_execute_retries_exception_from_step_anchor():
    events: list[str] = []
    attempts = {"refresh": 0, "poll": 0}

    def warmup():
        events.append("warmup")
        return True

    def refresh():
        attempts["refresh"] += 1
        events.append(f"refresh_{attempts['refresh']}")
        return True

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] < 3:
            raise Exception("not ready")
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        journey_sdk.step(warmup)
        anchor = journey_sdk.step(refresh)
        journey_sdk.step(
            poll,
            retry=2,
            retry_delay=0,
            retry_from=anchor,
        )
        journey_sdk.step(finish)

    report = journey_sdk.execute(journey)

    assert events == [
        "warmup",
        "refresh_1",
        "poll_1",
        "refresh_2",
        "poll_2",
        "refresh_3",
        "poll_3",
        "finish",
    ]
    assert _record_labels(report.case_reports[0]) == ["warmup", "refresh", "poll", "finish"]


def test_execute_branch_anchor_protocol_store_failure_surfaces_context():
    _ReplayValue.reset()
    _ReplayValue.fail_store_message = "could not save"
    _ReplayValue.fail_store_boundary = "branch-anchor"

    def create_value():
        return _ReplayValue("seed-1", mode="failure")

    def finish_a(_value):
        return True

    def finish_b(_value):
        return True

    def journey():
        value = journey_sdk.step(create_value)
        if journey_sdk.branch(start_from=value):
            journey_sdk.step(finish_a, value)
        elif journey_sdk.branch(start_from=value):
            journey_sdk.step(finish_b, value)

    with pytest.raises(ExecutionStateSerializationError) as exc_info:
        journey_sdk.execute(journey)

    assert "branch anchor 'create_value' result" in str(exc_info.value)
    assert "could not save" in str(exc_info.value)


def test_execute_branch_anchor_protocol_restore_failure_surfaces_context():
    _ReplayValue.reset()

    def create_value():
        return _ReplayValue("seed-1", mode="failure")

    def finish_a(_value):
        return True

    def finish_b(_value):
        return True

    def journey():
        value = journey_sdk.step(create_value)
        if journey_sdk.branch(start_from=value):
            journey_sdk.step(finish_a, value)
        elif journey_sdk.branch(start_from=value):
            journey_sdk.step(finish_b, value)

    _ReplayValue.fail_restore_message = "could not restore"
    _ReplayValue.fail_restore_boundary = "branch-anchor"

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "saved result for step 'create_value'" in str(exc_info.value)
    assert "could not restore" in str(exc_info.value)


def test_execute_observer_reports_success_and_branch_events():
    observer = _RecordingObserver()

    def prepare():
        return True

    def finish_fast():
        return True

    def finish_manual():
        return True

    def journey():
        journey_sdk.step(prepare)
        if journey_sdk.branch():
            journey_sdk.step(finish_fast)
        elif journey_sdk.branch():
            journey_sdk.step(finish_manual)

    _execute_with_observer(journey, observer)

    assert observer.events == [
        ("journey_start", ["case_1", "case_2"]),
        ("case_start", "case_1"),
        ("step_start", "prepare", 1),
        ("step_ok", "prepare", 1, True),
        ("branch", "bg_1", "branch_1"),
        ("step_start", "finish_fast", 1),
        ("step_ok", "finish_fast", 1, True),
        ("case_complete", "case_1", None, None, True),
        ("case_start", "case_2"),
        ("step_start", "prepare", 1),
        ("step_ok", "prepare", 1, True),
        ("branch", "bg_1", "branch_2"),
        ("step_start", "finish_manual", 1),
        ("step_ok", "finish_manual", 1, True),
        ("case_complete", "case_2", None, None, True),
        ("journey_complete", ["case_1", "case_2"]),
    ]


def test_execute_observer_reports_same_step_retry_events():
    observer = _RecordingObserver()
    attempts = {"poll": 0}

    def prepare():
        return True

    def poll():
        attempts["poll"] += 1
        if attempts["poll"] < 3:
            raise RuntimeError("pending")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(poll, retry=2, retry_delay=0)

    _execute_with_observer(journey, observer)

    assert observer.events == [
        ("journey_start", ["case_1"]),
        ("case_start", "case_1"),
        ("step_start", "prepare", 1),
        ("step_ok", "prepare", 1, True),
        ("step_start", "poll", 1),
        ("retry", "poll", 1, 1, 0, "RuntimeError", True),
        ("step_start", "poll", 2),
        ("retry", "poll", 2, 0, 0, "RuntimeError", True),
        ("step_start", "poll", 3),
        ("step_ok", "poll", 3, True),
        ("case_complete", "case_1", None, None, True),
        ("journey_complete", ["case_1"]),
    ]


def test_execute_observer_reports_step_anchor_retry_events():
    observer = _RecordingObserver()
    attempts = {"issue": 0}

    def issue_request():
        attempts["issue"] += 1
        return attempts["issue"]

    def confirm_request(issued):
        if issued < 2:
            raise RuntimeError("pending")
        return True

    def journey():
        issued = journey_sdk.step(issue_request)
        journey_sdk.step(
            confirm_request,
            issued,
            retry=1,
            retry_delay=0,
            retry_from=issued,
        )

    _execute_with_observer(journey, observer)

    assert observer.events == [
        ("journey_start", ["case_1"]),
        ("case_start", "case_1"),
        ("step_start", "issue_request", 1),
        ("step_ok", "issue_request", 1, True),
        ("step_start", "confirm_request", 1),
        ("retry", "confirm_request", 1, 0, 0, "RuntimeError", True),
        ("step_start", "issue_request", 2),
        ("step_ok", "issue_request", 2, True),
        ("step_start", "confirm_request", 2),
        ("step_ok", "confirm_request", 2, True),
        ("case_complete", "case_1", None, None, True),
        ("journey_complete", ["case_1"]),
    ]


def test_execute_observer_reports_step_anchor_retry_events():
    observer = _RecordingObserver()
    attempts = {"refresh": 0, "poll": 0}

    def warmup():
        return True

    def refresh():
        attempts["refresh"] += 1
        return attempts["refresh"]

    def poll(_request):
        attempts["poll"] += 1
        if attempts["poll"] < 2:
            raise RuntimeError("pending")
        return True

    def journey():
        journey_sdk.step(warmup)
        request = journey_sdk.step(refresh)
        journey_sdk.step(
            poll,
            request,
            retry=1,
            retry_delay=0,
            retry_from=request,
        )

    _execute_with_observer(journey, observer)

    assert observer.events == [
        ("journey_start", ["case_1"]),
        ("case_start", "case_1"),
        ("step_start", "warmup", 1),
        ("step_ok", "warmup", 1, True),
        ("step_start", "refresh", 1),
        ("step_ok", "refresh", 1, True),
        ("step_start", "poll", 1),
        ("retry", "poll", 1, 0, 0, "RuntimeError", True),
        ("step_start", "refresh", 2),
        ("step_ok", "refresh", 2, True),
        ("step_start", "poll", 2),
        ("step_ok", "poll", 2, True),
        ("case_complete", "case_1", None, None, True),
        ("journey_complete", ["case_1"]),
    ]


def test_execute_observer_reports_exhausted_retry_failure():
    observer = _RecordingObserver()
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        raise RuntimeError("pending")

    def journey():
        journey_sdk.step(poll, retry=1, retry_delay=0)

    with pytest.raises(CallableExecutionError):
        _execute_with_observer(journey, observer)

    assert observer.events == [
        ("journey_start", ["case_1"]),
        ("case_start", "case_1"),
        ("step_start", "poll", 1),
        ("retry", "poll", 1, 0, 0, "RuntimeError", True),
        ("step_start", "poll", 2),
        ("step_failed", "poll", 2, "RuntimeError", True),
    ]


def test_execute_observer_reports_interruption_and_resume(tmp_path):
    observer = _RecordingObserver()
    resumed_observer = _RecordingObserver()
    state_file = tmp_path / "journey.state"
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        if attempts["poll"] == 1:
            raise KeyboardInterrupt()
        return True

    def journey():
        journey_sdk.step(poll, retry=1, retry_delay=0)

    with pytest.raises(KeyboardInterrupt):
        _execute_with_observer(journey, observer, state=state_file)

    _execute_with_observer(journey, resumed_observer, state=state_file)

    assert observer.events == [
        ("journey_start", ["case_1"]),
        ("case_start", "case_1"),
        ("step_start", "poll", 1),
        ("step_interrupted", "poll", 1, "KeyboardInterrupt", True),
    ]
    assert resumed_observer.events == [
        ("journey_start", ["case_1"]),
        ("case_resume", "case_1", 0),
        ("step_start", "poll", 2),
        ("step_ok", "poll", 2, True),
        ("case_complete", "case_1", None, None, True),
        ("journey_complete", ["case_1"]),
    ]


def test_execute_resumes_interrupted_nonretryable_step(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"work": 0}

    def prepare():
        events.append("prepare")
        return {"token": "ready"}

    def work(state):
        attempts["work"] += 1
        events.append(f"work_{attempts['work']}_{state['token']}")
        if attempts["work"] == 1:
            raise KeyboardInterrupt()
        return True

    def finish(state):
        events.append(f"finish_{state['token']}")
        return True

    def journey():
        state = journey_sdk.step(prepare)
        journey_sdk.step(work, state)
        journey_sdk.step(finish, state)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    assert state_file.exists()

    report = journey_sdk.execute(journey, state=state_file)

    assert events == [
        "prepare",
        "work_1_ready",
        "prepare",
        "work_2_ready",
        "finish_ready",
    ]
    assert _record_labels(report.case_reports[0]) == ["prepare", "work", "finish"]
    assert state_file.exists()

    resumed_report = journey_sdk.execute(journey, state=state_file)

    assert events == [
        "prepare",
        "work_1_ready",
        "prepare",
        "work_2_ready",
        "finish_ready",
    ]
    assert _record_labels(resumed_report.case_reports[0]) == ["prepare", "work", "finish"]


def test_execute_resumes_dirty_step_with_helper_args_kwargs_and_none_result(tmp_path):
    state_file = tmp_path / "journey.state"
    helper_calls = {"count": 0}
    events: list[str] = []
    attempts = {"work": 0}

    def next_payload():
        helper_calls["count"] += 1
        return {"seed": helper_calls["count"]}

    def prepare():
        events.append("prepare")
        return None

    def work(payload, *, previous):
        attempts["work"] += 1
        events.append(f"work_{attempts['work']}_{payload['seed']}_{previous is None}")
        if attempts["work"] == 1:
            raise KeyboardInterrupt()
        return True

    def journey():
        payload = next_payload()
        previous = journey_sdk.step(prepare)
        journey_sdk.step(work, payload, previous=previous)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    first_seed = events[1].split("_")[2]
    second_seed = events[3].split("_")[2]

    assert events[0] == "prepare"
    assert events[1] == f"work_1_{first_seed}_True"
    assert events[2] == "prepare"
    assert events[3] == f"work_2_{second_seed}_True"
    assert first_seed != second_seed
    assert _record_labels(report.case_reports[0]) == ["prepare", "work"]


def test_execute_resumes_interrupted_retryable_step_from_step_anchor(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"issue": 0, "confirm": 0}

    def issue_request():
        attempts["issue"] += 1
        events.append(f"issue_{attempts['issue']}")
        return attempts["issue"]

    def confirm_request(issued):
        attempts["confirm"] += 1
        events.append(f"confirm_{attempts['confirm']}_{issued}")
        if attempts["confirm"] == 1:
            raise KeyboardInterrupt()
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        issued = journey_sdk.step(issue_request)
        journey_sdk.step(
            confirm_request,
            issued,
            retry=1,
            retry_delay=0,
            retry_from=issued,
        )
        journey_sdk.step(finish)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert events == ["issue_1", "confirm_1_1", "issue_2", "confirm_2_2", "finish"]
    assert _record_labels(report.case_reports[0]) == [
        "issue_request",
        "confirm_request",
        "finish",
    ]
    assert state_file.exists()


def test_execute_resume_preserves_retry_budget_for_same_step(tmp_path):
    state_file = tmp_path / "journey.state"
    attempts = {"poll": 0}

    def poll():
        attempts["poll"] += 1
        if attempts["poll"] == 1:
            raise RuntimeError("pending")
        if attempts["poll"] == 2:
            raise KeyboardInterrupt()
        if attempts["poll"] == 3:
            raise RuntimeError("still pending")
        return True

    def journey():
        journey_sdk.step(poll, retry=1, retry_delay=0)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey, state=state_file)

    assert attempts["poll"] == 3
    assert "retry attempts were exhausted" in str(exc_info.value)
    assert not state_file.exists()


def test_execute_resume_preserves_retry_budget_for_step_anchor(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"issue": 0, "confirm": 0}

    def issue_request():
        attempts["issue"] += 1
        events.append(f"issue_{attempts['issue']}")
        return attempts["issue"]

    def confirm_request(issued):
        attempts["confirm"] += 1
        events.append(f"confirm_{attempts['confirm']}_{issued}")
        if attempts["confirm"] == 1:
            raise RuntimeError("pending")
        if attempts["confirm"] == 2:
            raise KeyboardInterrupt()
        if attempts["confirm"] == 3:
            raise RuntimeError("still pending")
        return True

    def journey():
        issued = journey_sdk.step(issue_request)
        journey_sdk.step(
            confirm_request,
            issued,
            retry=1,
            retry_delay=0,
            retry_from=issued,
        )

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, state=state_file)

    assert events == [
        "issue_1",
        "confirm_1_1",
        "issue_2",
        "confirm_2_2",
        "issue_3",
        "confirm_3_3",
    ]
    assert not state_file.exists()


def test_execute_resumes_interrupted_retryable_step_from_same_step_anchor(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"poll": 0}

    def warmup():
        events.append("warmup")
        return True

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] == 1:
            raise KeyboardInterrupt()
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        journey_sdk.step(warmup)
        journey_sdk.step(
            poll,
            retry=1,
            retry_delay=0,
        )
        journey_sdk.step(finish)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert events == ["warmup", "poll_1", "poll_2", "finish"]
    assert _record_labels(report.case_reports[0]) == ["warmup", "poll", "finish"]
    assert state_file.exists()


def test_execute_resumes_protocol_value_restore_with_saved_state(tmp_path):
    state_file = tmp_path / "journey.state"
    _ReplayValue.reset()
    events: list[str] = []
    attempts = {"poll": 0}

    def create_value():
        return _ReplayValue("seed-1", mode="resume")

    def poll(value):
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}_{value.seed}")
        if attempts["poll"] == 1:
            raise KeyboardInterrupt()
        return True

    def finish():
        events.append("finish")
        return True

    def journey():
        value = journey_sdk.step(create_value)
        journey_sdk.step(
            poll,
            value,
            retry=1,
            retry_delay=0,
            retry_from=value,
        )
        journey_sdk.step(finish)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert events == [
        "poll_1_seed-1",
        "poll_2_seed-1",
        "finish",
    ]
    assert _ReplayValue.events == []
    assert _record_labels(report.case_reports[0]) == ["create_value", "poll", "finish"]


def test_execute_resume_preserves_retry_budget_for_step_anchor(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"refresh": 0, "poll": 0}

    def warmup():
        events.append("warmup")
        return True

    def refresh():
        attempts["refresh"] += 1
        events.append(f"refresh_{attempts['refresh']}")
        return True

    def poll():
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}")
        if attempts["poll"] == 1:
            raise RuntimeError("pending")
        if attempts["poll"] == 2:
            raise KeyboardInterrupt()
        if attempts["poll"] == 3:
            raise RuntimeError("still pending")
        return True

    def journey():
        journey_sdk.step(warmup)
        anchor = journey_sdk.step(refresh)
        journey_sdk.step(
            poll,
            retry=1,
            retry_delay=0,
            retry_from=anchor,
        )

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, state=state_file)

    assert events == [
        "warmup",
        "refresh_1",
        "poll_1",
        "refresh_2",
        "poll_2",
        "refresh_3",
        "poll_3",
    ]
    assert not state_file.exists()


def test_execute_retry_reuses_helper_generated_args_for_same_step():
    helper_calls = {"count": 0}
    seen: list[int] = []

    def next_payload():
        helper_calls["count"] += 1
        return {"seed": helper_calls["count"]}

    def poll(payload):
        seen.append(payload["seed"])
        if len(seen) == 1:
            raise RuntimeError("pending")
        return True

    def journey():
        payload = next_payload()
        journey_sdk.step(poll, payload, retry=1, retry_delay=0)

    report = journey_sdk.execute(journey)

    assert seen == [seen[0], seen[0]]
    assert _record_labels(report.case_reports[0]) == ["poll"]


def test_execute_retry_from_step_anchor_reuses_helper_args_and_refreshes_step_results():
    helper_calls = {"count": 0}
    events: list[str] = []
    attempts = {"issue": 0}

    def next_payload():
        helper_calls["count"] += 1
        return {"seed": helper_calls["count"]}

    def issue_request(payload):
        attempts["issue"] += 1
        events.append(f"issue_{attempts['issue']}_{payload['seed']}")
        return attempts["issue"]

    def confirm_request(issued, payload):
        events.append(f"confirm_{issued}_{payload['seed']}")
        if issued < 2:
            raise RuntimeError("pending")
        return True

    def journey():
        payload = next_payload()
        issued = journey_sdk.step(issue_request, payload)
        journey_sdk.step(
            confirm_request,
            issued,
            payload,
            retry=1,
            retry_delay=0,
            retry_from=issued,
        )

    report = journey_sdk.execute(journey)

    seed = events[0].split("_")[2]

    assert events == [
        f"issue_1_{seed}",
        f"confirm_1_{seed}",
        f"issue_2_{seed}",
        f"confirm_2_{seed}",
    ]
    assert _record_labels(report.case_reports[0]) == ["issue_request", "confirm_request"]


def test_execute_retry_from_step_anchor_reuses_helper_args():
    helper_calls = {"count": 0}
    events: list[str] = []
    attempts = {"poll": 0}

    def next_payload():
        helper_calls["count"] += 1
        return {"seed": helper_calls["count"]}

    def warmup(payload):
        events.append(f"warmup_{payload['seed']}")
        return True

    def refresh(payload):
        events.append(f"refresh_{payload['seed']}")
        return True

    def poll(payload):
        attempts["poll"] += 1
        events.append(f"poll_{attempts['poll']}_{payload['seed']}")
        if attempts["poll"] == 1:
            raise RuntimeError("pending")
        return True

    def journey():
        payload = next_payload()
        journey_sdk.step(warmup, payload)
        anchor = journey_sdk.step(refresh, payload)
        journey_sdk.step(
            poll,
            payload,
            retry=1,
            retry_delay=0,
            retry_from=anchor,
        )

    report = journey_sdk.execute(journey)

    seed = events[0].split("_")[1]

    assert events == [
        f"warmup_{seed}",
        f"refresh_{seed}",
        f"poll_1_{seed}",
        f"refresh_{seed}",
        f"poll_2_{seed}",
    ]
    assert _record_labels(report.case_reports[0]) == ["warmup", "refresh", "poll"]


def test_execute_resumes_interrupted_target_step_and_still_stops_at_target(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"publish": 0}

    def prepare():
        events.append("prepare")
        return True

    def publish():
        attempts["publish"] += 1
        events.append(f"publish_{attempts['publish']}")
        if attempts["publish"] == 1:
            raise KeyboardInterrupt()
        return True

    def cleanup():
        events.append("cleanup")
        return True

    def journey():
        journey_sdk.step(prepare)
        journey_sdk.step(publish)
        journey_sdk.step(cleanup)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, step="publish", state=state_file)

    report = journey_sdk.execute(journey, step="publish", state=state_file)

    assert events == ["prepare", "publish_1", "prepare", "publish_2"]
    assert report.case_reports[0].stopped_at_label == "publish"
    assert _record_labels(report.case_reports[0]) == ["prepare", "publish"]
    assert state_file.exists()


def test_execute_resumes_multi_case_run_without_rerunning_completed_cases(tmp_path):
    state_file = tmp_path / "journey.state"
    events: list[str] = []
    attempts = {"branch_b": 0}

    def shared():
        events.append("shared")
        return True

    def branch_a_step():
        events.append("branch_a")
        return True

    def branch_b_step():
        attempts["branch_b"] += 1
        events.append(f"branch_b_{attempts['branch_b']}")
        if attempts["branch_b"] == 1:
            raise KeyboardInterrupt()
        return True

    def journey():
        journey_sdk.step(shared)
        if journey_sdk.branch():
            journey_sdk.step(branch_a_step)
        elif journey_sdk.branch():
            journey_sdk.step(branch_b_step)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert events == [
        "shared",
        "branch_a",
        "shared",
        "branch_b_1",
        "shared",
        "branch_b_2",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert state_file.exists()


def test_execute_rehydrates_step_started_branch_cases_from_anchor_post_exit():
    helper_calls = {"count": 0}
    events: list[str] = []

    def next_payload():
        helper_calls["count"] += 1
        return {"seed": helper_calls["count"]}

    def prepare(payload):
        events.append(f"prepare_{payload['seed']}")
        return {"token": f"t{payload['seed']}"}

    def shared_after_anchor(token):
        events.append(f"shared_{token['token']}")
        return {"shared": token["token"]}

    def finish_branch_a(shared):
        events.append(f"branch_a_{shared['shared']}")
        return True

    def finish_branch_b(shared):
        events.append(f"branch_b_{shared['shared']}")
        return True

    def journey():
        payload = next_payload()
        token = journey_sdk.step(prepare, payload)
        shared = journey_sdk.step(shared_after_anchor, token)
        if journey_sdk.branch(start_from=token):
            journey_sdk.step(finish_branch_a, shared)
        elif journey_sdk.branch(start_from=token):
            journey_sdk.step(finish_branch_b, shared)

    report = journey_sdk.execute(journey)

    seed = events[0].split("_")[1]

    assert events == [
        f"prepare_{seed}",
        f"shared_t{seed}",
        f"branch_a_t{seed}",
        f"shared_t{seed}",
        f"branch_b_t{seed}",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert _record_labels(report.case_reports[0]) == [
        "prepare",
        "shared_after_anchor",
        "finish_branch_a",
    ]
    assert _record_labels(report.case_reports[1]) == [
        "prepare",
        "shared_after_anchor",
        "finish_branch_b",
    ]


def test_execute_rehydrates_later_branch_anchor_reached_before_first_case_branch():
    events: list[str] = []

    def step_1():
        events.append("step_1")
        return "s1"

    def step_2():
        events.append("step_2")
        return "s2"

    def step_3():
        events.append("step_3")
        return True

    def step_4():
        events.append("step_4")
        return True

    def step_5():
        events.append("step_5")
        return True

    def journey():
        s1 = journey_sdk.step(step_1)
        s2 = journey_sdk.step(step_2)

        if journey_sdk.branch(start_from=s1):
            journey_sdk.step(step_3)
        elif journey_sdk.branch(start_from=s2):
            journey_sdk.step(step_4)

        journey_sdk.step(step_5)

    report = journey_sdk.execute(journey)

    assert events == [
        "step_1",
        "step_2",
        "step_3",
        "step_5",
        "step_4",
        "step_5",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert _record_labels(report.case_reports[1]) == [
        "step_1",
        "step_2",
        "step_4",
        "step_5",
    ]


def test_execute_branch_anchor_handles_duplicate_scalar_step_results():
    events: list[str] = []

    def first_anchor():
        events.append("first")
        return True

    def duplicate_scalar(_anchor):
        events.append("duplicate")
        return True

    def finish(branch_name):
        events.append(f"finish_{branch_name}")
        return True

    def journey():
        anchor = journey_sdk.step(first_anchor)
        journey_sdk.step(duplicate_scalar, anchor)
        if journey_sdk.branch(start_from=anchor):
            journey_sdk.step(finish, "a")
        elif journey_sdk.branch(start_from=anchor):
            journey_sdk.step(finish, "b")

    report = journey_sdk.execute(journey)

    assert events == [
        "first",
        "duplicate",
        "finish_a",
        "duplicate",
        "finish_b",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]


def test_execute_step_started_branches_restore_protocol_value_state():
    _ReplayValue.reset()
    events: list[str] = []

    def create_value():
        return _ReplayValue("seed-1", mode="branch")

    def shared_after_anchor(value):
        events.append(f"shared_{value.seed}")
        return _ReplayValue(f"shared-{value.seed}", mode="branch-local")

    def finish(branch_name, shared):
        events.append(f"finish_{branch_name}_{shared.seed}")
        return True

    def journey():
        value = journey_sdk.step(create_value)
        shared = journey_sdk.step(shared_after_anchor, value)
        if journey_sdk.branch(start_from=value):
            journey_sdk.step(finish, "a", shared)
        elif journey_sdk.branch(start_from=value):
            journey_sdk.step(finish, "b", shared)

    journey_sdk.execute(journey)

    assert events == [
        "shared_seed-1",
        "finish_a_shared-seed-1",
        "shared_seed-1",
        "finish_b_shared-seed-1",
    ]
    assert "store_seed-1_branch_binding_step:n_1" not in _ReplayValue.events
    assert "store_seed-1_branch_state_case_1" in _ReplayValue.events
    assert "store_seed-1_branch_branch-anchor_step:n_1" in _ReplayValue.events
    assert "restore_seed-1_branch_branch-anchor_step:n_1" in _ReplayValue.events
    assert all("branch-local" not in event for event in _ReplayValue.events)


def test_execute_step_started_branches_keep_retry_counters_independent():
    observer = _RecordingObserver()
    events: list[str] = []
    attempts = {"branch_a": 0, "branch_b": 0}

    def prepare():
        events.append("prepare")
        return True

    def poll(branch_name):
        attempts[branch_name] += 1
        events.append(f"{branch_name}_{attempts[branch_name]}")
        if attempts[branch_name] == 1:
            raise RuntimeError("pending")
        return branch_name

    def finish(branch_name):
        events.append(f"finish_{branch_name}")
        return True

    def journey():
        anchor = journey_sdk.step(prepare)
        if journey_sdk.branch(start_from=anchor):
            branch_name = journey_sdk.step(poll, "branch_a", retry=1, retry_delay=0)
            journey_sdk.step(finish, branch_name)
        elif journey_sdk.branch(start_from=anchor):
            branch_name = journey_sdk.step(poll, "branch_b", retry=1, retry_delay=0)
            journey_sdk.step(finish, branch_name)

    report = _execute_with_observer(journey, observer)

    assert events == [
        "prepare",
        "branch_a_1",
        "branch_a_2",
        "finish_branch_a",
        "branch_b_1",
        "branch_b_2",
        "finish_branch_b",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert [event for event in observer.events if event[:2] == ("step_start", "prepare")] == [
        ("step_start", "prepare", 1)
    ]
    assert [event for event in observer.events if event[:2] == ("step_start", "poll")] == [
        ("step_start", "poll", 1),
        ("step_start", "poll", 2),
        ("step_start", "poll", 1),
        ("step_start", "poll", 2),
    ]
    assert [
        event[:4]
        for event in observer.events
        if event[0] == "retry" and event[1] == "poll"
    ] == [
        ("retry", "poll", 1, 0),
        ("retry", "poll", 1, 0),
    ]


def test_execute_state_rejects_plan_mismatch(tmp_path):
    state_file = tmp_path / "journey.state"

    def interrupt():
        raise KeyboardInterrupt()

    def initial_journey():
        journey_sdk.step(interrupt)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(initial_journey, state=state_file)

    def stable():
        return True

    def initial_journey():
        journey_sdk.step(stable)
        journey_sdk.step(stable)

    with pytest.raises(ExecutionStateMismatchError):
        journey_sdk.execute(initial_journey, state=state_file)


def test_execute_state_rejects_corrupt_state_file(tmp_path):
    state_file = tmp_path / "journey.state"
    state_file.write_bytes(b"not a pickle")

    def step():
        return True

    def journey():
        journey_sdk.step(step)

    with pytest.raises(CorruptExecutionStateError) as exc_info:
        journey_sdk.execute(journey, state=state_file)

    assert "Could not read the journey state file" in str(exc_info.value)
    assert exc_info.value.hint is not None


def test_execute_state_rejects_corrupt_json_state_file(tmp_path):
    state_file = tmp_path / "journey.state"
    state_file.write_text(
        '{"format": "journey.execution_state", "version": "bad"}',
        encoding="utf-8",
    )

    def step():
        return True

    def journey():
        journey_sdk.step(step)

    with pytest.raises(CorruptExecutionStateError) as exc_info:
        journey_sdk.execute(journey, state=state_file)

    assert "Could not read the journey state file" in str(exc_info.value)
    assert exc_info.value.hint is not None


def test_execute_state_sanitizes_completed_step_record_results(tmp_path):
    state_file = tmp_path / "state.json"

    def produce():
        return {"token": b"abc"}

    def consume(payload):
        return payload["token"] == b"abc"

    def journey():
        payload = journey_sdk.step(produce)
        journey_sdk.step(consume, payload)

    journey_sdk.execute(journey, state=state_file)

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["format"] == "journey.execution_state"
    assert payload["version"] == 11
    encoded_result = payload["completed_case_reports"][0]["records"][0]["result"]
    assert encoded_result["encoding"] == "pickle-base64"
    assert pickle.loads(base64.b64decode(encoded_result["data"].encode("ascii"))) is None


def test_execute_state_writes_active_step_payloads_as_base64_json(tmp_path):
    state_file = tmp_path / "state.json"

    def produce():
        return {"token": b"abc"}

    def interrupt(payload):
        assert payload["token"] == b"abc"
        raise KeyboardInterrupt()

    def journey():
        payload = journey_sdk.step(produce)
        journey_sdk.step(interrupt, payload)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    active_case = payload["active_case"]
    bindings = active_case["snapshot"]["step_bindings"]
    assert bindings == {}


def test_execute_state_sanitizes_unserializable_nonreplayable_step_result(tmp_path):
    state_file = tmp_path / "journey.state"

    def produce():
        return lambda: None

    def journey():
        journey_sdk.step(produce)

    journey_sdk.execute(journey, state=state_file)

    assert state_file.exists()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    encoded_result = payload["completed_case_reports"][0]["records"][0]["result"]
    assert pickle.loads(base64.b64decode(encoded_result["data"].encode("ascii"))) is None


def test_execute_rehydration_rejects_unserializable_step_args():
    def journey():
        journey_sdk.step(lambda _payload: True, lambda: None, retry=1, retry_delay=0)

    with pytest.raises(ExecutionStateSerializationError):
        journey_sdk.execute(journey)


def test_execute_rehydration_rejects_unserializable_step_result_for_branch_anchor_replay():
    def produce():
        return lambda: None

    def branch_a_step(_payload):
        return True

    def branch_b_step(_payload):
        return True

    def journey():
        payload = journey_sdk.step(produce)
        if journey_sdk.branch(start_from=payload):
            journey_sdk.step(branch_a_step, payload)
        elif journey_sdk.branch(start_from=payload):
            journey_sdk.step(branch_b_step, payload)

    with pytest.raises(ExecutionStateSerializationError):
        journey_sdk.execute(journey)


def test_execute_state_rejects_old_state_format_version(tmp_path):
    state_file = tmp_path / "journey.state"

    def step():
        return True

    def journey():
        journey_sdk.step(step)

    plan = journey_sdk.compile_journey(journey)
    selected_cases = [
        SelectedCaseState(case_id="case_1", stop_after_index=None),
    ]
    envelope = ExecutionStateEnvelope(
        version=4,
        journey_id=plan.journey_id,
        function_ref=plan.function_ref,
        step=None,
        develop_step=None,
        plan_signature=journey_executor._plan_signature(
            plan,
            journey_executor._select_cases(plan, None),
            None,
            None,
        ),
        selected_cases=selected_cases,
        current_case_index=0,
        completed_case_reports=[],
        active_case=None,
    )
    state_file.write_bytes(pickle.dumps(envelope))

    with pytest.raises(ExecutionStateMismatchError) as exc_info:
        journey_sdk.execute(journey, state=state_file)

    assert "format version 4" in str(exc_info.value)


def test_execute_legacy_ctx_step_fails_with_callable_error():
    def legacy_step(ctx):
        return True

    def journey():
        journey_sdk.step(legacy_step)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "legacy_step" in str(exc_info.value)
    assert "TypeError" in str(exc_info.value)


def test_execute_missing_step_exposes_user_friendly_hint():
    def publish():
        return True

    def journey():
        journey_sdk.step(publish)

    with pytest.raises(StepNotFoundError) as exc_info:
        journey_sdk.execute(journey, step="missing")

    assert str(exc_info.value) == "Step label 'missing' was not found in the selected journey."
    assert exc_info.value.hint is not None


def test_branch_start_from_type_error_includes_step_hint():
    def journey():
        if journey_sdk.branch(start_from="missing_step"):
            journey_sdk.step(lambda: True)
        elif journey_sdk.branch():
            journey_sdk.step(lambda: True)

    with pytest.raises(TypeError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "earlier step() result" in str(exc_info.value)
