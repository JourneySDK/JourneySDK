from __future__ import annotations

import inspect
import pickle

import journey.errors as journey_errors
import journey.executor as journey_executor
import journey.models as journey_models
import journey as journey_sdk
import pytest
from journey.errors import (
    AmbiguousStepSelectionError,
    CallableExecutionError,
    CorruptExecutionStateError,
    ExecutionStateMismatchError,
    ExecutionStateSerializationError,
    InvalidBranchUsageError,
    StepNotFoundError,
    UnknownCheckpointError,
    UnsupportedControlFlowError,
    UnsupportedLoopError,
)
from journey.models import BranchMarkerNode, StepNode, StepRetry
from journey.state import ExecutionStateEnvelope, SelectedCaseState


def _labels(case_plan):
    labels: list[str] = []
    for node in case_plan.nodes:
        label = getattr(node, "label", None)
        if label is not None:
            labels.append(label)
    return labels


def _record_labels(case_report):
    return [record.label for record in case_report.records if record.label is not None]


class _RecordingObserver(journey_executor._ExecutionObserver):
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def on_journey_start(self, *, plan, selected_cases) -> None:
        del plan
        self.events.append(
            ("journey_start", [item.case_plan.case_id for item in selected_cases])
        )

    def on_case_start(self, *, case_plan, stop_after_index, replay_anchor) -> None:
        del stop_after_index, replay_anchor
        self.events.append(("case_start", case_plan.case_id))

    def on_case_resume(
        self,
        *,
        case_plan,
        stop_after_index,
        replay_anchor,
        replay_from_index,
    ) -> None:
        del stop_after_index, replay_anchor
        self.events.append(("case_resume", case_plan.case_id, replay_from_index))

    def on_step_start(self, *, case_plan, node, node_index, attempt) -> None:
        del case_plan, node_index
        self.events.append(("step_start", node.label, attempt))

    def on_branch(self, *, case_plan, node, node_index) -> None:
        del case_plan, node_index
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
        del case_plan, node_index
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
        del case_plan, node_index
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
        del case_plan, node_index
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
        del case_plan, node_index
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
        del case_plan
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
        "from journey.models import EvalResult",
        "from journey.models import WaitResult",
        "from journey.models import RunResult",
        "from journey.models import RunContext",
        "from journey.models import RetryPolicy",
        "from journey import EvalResult",
        "from journey import WaitResult",
        "from journey import RunResult",
        "from journey import RetryPolicy",
        "from journey import retry",
        "from journey.errors import DuplicateBranchKeyError",
        "from journey import DuplicateBranchKeyError",
        "from journey.errors import EvaluationFailedError",
        "from journey.errors import WaitFailedError",
        "from journey import EvaluationFailedError",
        "from journey import WaitFailedError",
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
        journey_sdk.step(assert_ok, signup)
        after_signup = journey_sdk.checkpoint()
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


def test_unknown_checkpoint_is_rejected():
    def path_a():
        return True

    def path_b():
        return True

    def journey():
        if journey_sdk.branch(start_from="missing_checkpoint"):
            journey_sdk.step(path_a)
        elif journey_sdk.branch():
            journey_sdk.step(path_b)

    with pytest.raises(UnknownCheckpointError):
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
        journey_sdk.step(assert_ok, signup)
        after_signup = journey_sdk.checkpoint()
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
    assert case_report.replay_anchor == "cp_1"

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
        cp1 = journey_sdk.checkpoint()
        if journey_sdk.branch(start_from=cp1):
            journey_sdk.step(step_a)
        elif journey_sdk.branch():
            journey_sdk.step(step_b)

    plan = journey_sdk.compile_journey(journey)
    assert len(plan.case_plans) == 2

    for case in plan.case_plans:
        markers = [node for node in case.nodes if isinstance(node, BranchMarkerNode)]
        assert len(markers) == 1
        if _labels(case) == ["step_a"]:
            assert markers[0].start_from == "cp_1"
        else:
            assert _labels(case) == ["step_b"]
            assert markers[0].start_from is None


def test_checkpoint_branches_keyword_is_rejected_with_migration_hint():
    def a():
        return True

    def b():
        return True

    def journey():
        journey_sdk.checkpoint(branches=[a, b])

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "no longer supported" in str(exc_info.value)
    assert exc_info.value.hint is not None


def test_checkpoint_signature_has_no_branches_keyword():
    assert list(inspect.signature(journey_sdk.checkpoint).parameters) == []


def test_branch_selector_is_rejected_with_migration_hint():
    def a():
        return True

    def b():
        return True

    def journey():
        selected = journey_sdk.checkpoint()
        if selected.is_(a):
            journey_sdk.step(a)
        elif selected.is_(b):
            journey_sdk.step(b)

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "no longer supported" in str(exc_info.value)
    assert exc_info.value.hint is not None


def test_branch_call_outside_inline_if_chain_is_rejected():
    def journey():
        journey_sdk.branch()

    with pytest.raises(InvalidBranchUsageError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "direct if/elif condition" in str(exc_info.value)
    assert exc_info.value.hint is not None


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
    def branch_a_step():
        return "a"

    def branch_b_step():
        return "b"

    def journey():
        anchor = journey_sdk.checkpoint()
        if journey_sdk.branch(start_from=anchor):
            journey_sdk.step(branch_a_step)
        elif journey_sdk.branch():
            journey_sdk.step(branch_b_step)

    plan = journey_sdk.compile_journey(journey)
    assert sorted(_labels(case) for case in plan.case_plans) == [
        ["branch_a_step"],
        ["branch_b_step"],
    ]

    branch_a_case = next(
        case for case in plan.case_plans if _labels(case) == ["branch_a_step"]
    )
    marker = next(node for node in branch_a_case.nodes if isinstance(node, BranchMarkerNode))
    assert marker.start_from == "cp_1"

    report = journey_sdk.execute(journey, step="branch_a_step")
    assert len(report.case_reports) == 1
    assert report.case_reports[0].replay_anchor == "cp_1"


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


def test_plan_includes_retry_metadata_for_step_and_checkpoint_anchors():
    def prepare():
        return "prepared"

    def poll_from_step():
        return True

    def poll_from_checkpoint():
        return True

    def journey():
        prepared = journey_sdk.step(prepare)
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(
            poll_from_step,
            retry=6,
            retry_delay=5,
            retry_from=prepared,
        )
        journey_sdk.step(
            poll_from_checkpoint,
            retry=10,
            retry_delay=2,
            retry_from=anchor,
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
        from_checkpoint="cp_1",
    )


def test_plan_requires_positive_retry_to_emit_retry_metadata():
    def same_step_retry():
        return True

    def checkpoint_retry():
        return True

    def delay_only():
        return True

    def anchor_only():
        return True

    def disabled():
        return True

    def journey():
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(same_step_retry, retry=2, retry_delay=1.5)
        journey_sdk.step(checkpoint_retry, retry=1, retry_from=anchor)
        journey_sdk.step(delay_only, retry_delay=1.5)
        journey_sdk.step(anchor_only, retry_from=anchor)
        journey_sdk.step(disabled, retry=0, retry_delay=0, retry_from=anchor)

    plan = journey_sdk.compile_journey(journey)
    step_nodes = [node for node in plan.case_plans[0].nodes if isinstance(node, StepNode)]

    assert step_nodes[0].retry == StepRetry(
        retries=2,
        delay_seconds=1.5,
        from_node_id=step_nodes[0].node_id,
    )
    assert step_nodes[1].retry == StepRetry(
        retries=1,
        delay_seconds=5.0,
        from_checkpoint="cp_1",
    )
    assert step_nodes[2].retry is None
    assert step_nodes[3].retry is None
    assert step_nodes[4].retry is None


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


def test_execute_retries_from_checkpoint_without_rerunning_prior_steps():
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
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(
            poll,
            request,
            retry=1,
            retry_delay=0,
            retry_from=anchor,
        )
        journey_sdk.step(finish, request)

    report = journey_sdk.execute(journey)

    assert events == ["begin", "poll_1_req-1", "poll_2_req-1", "finish_req-1"]
    assert _record_labels(report.case_reports[0]) == ["begin_polling", "poll", "finish"]


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


def test_execute_pause_on_step_continues_to_later_steps_without_rerunning_prior_steps(
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
        pause_on_step="publish",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.label == "publish"
    assert first.paused_step.ok is True
    assert events == ["prepare", "publish"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="publish",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "cleanup"
    assert report.paused_step.ok is True
    assert events == ["prepare", "publish", "cleanup"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="publish",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_models.ExecutionReport)
    assert events == ["prepare", "publish", "cleanup"]
    assert report.case_reports[0].stopped_at_label is None
    assert report.case_reports[0].replay_anchor is None
    assert _record_labels(report.case_reports[0]) == ["prepare", "publish", "cleanup"]


def test_execute_pause_on_step_retry_rewinds_from_checkpoint_and_refreshes_retry_budget(
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
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(poll, retry=1, retry_delay=0, retry_from=anchor)
        journey_sdk.step(finish)

    plan = journey_sdk.compile_journey(journey)

    first = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="poll",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.label == "poll"
    assert first.paused_step.ok is False
    assert first.paused_step.attempt == 2
    assert events == ["prepare", "poll_1", "poll_2"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="poll",
        pause_action="retry",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "poll"
    assert report.paused_step.ok is True
    assert events == ["prepare", "poll_1", "poll_2", "poll_3"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="poll",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "finish"
    assert report.paused_step.ok is True
    assert events == ["prepare", "poll_1", "poll_2", "poll_3", "finish"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="poll",
        pause_action="continue",
        state=state_file,
    )

    assert isinstance(report, journey_models.ExecutionReport)
    assert events == ["prepare", "poll_1", "poll_2", "poll_3", "finish"]
    assert _record_labels(report.case_reports[0]) == ["prepare", "poll", "finish"]


def test_execute_pause_on_step_retry_rewinds_from_step_result_anchor(
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
        pause_on_step="poll",
        state=state_file,
    )

    assert isinstance(first, journey_executor._PausedExecution)
    assert first.paused_step.ok is False
    assert events == ["issue_req-1", "poll_1_req-1", "issue_req-2", "poll_2_req-2"]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="poll",
        pause_action="retry",
        state=state_file,
    )

    assert isinstance(report, journey_executor._PausedExecution)
    assert report.paused_step.label == "poll"
    assert report.paused_step.ok is True
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
        pause_on_step="poll",
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
        "finish",
    ]

    report = journey_executor._execute_plan(
        journey,
        plan=plan,
        pause_on_step="poll",
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
        "finish",
    ]


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


def test_execute_retries_exception_from_checkpoint_anchor():
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
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(refresh)
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


def test_execute_observer_reports_checkpoint_anchor_retry_events():
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
        anchor = journey_sdk.checkpoint()
        request = journey_sdk.step(refresh)
        journey_sdk.step(
            poll,
            request,
            retry=1,
            retry_delay=0,
            retry_from=anchor,
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

    assert events == ["prepare", "work_1_ready", "work_2_ready", "finish_ready"]
    assert _record_labels(report.case_reports[0]) == ["prepare", "work", "finish"]
    assert state_file.exists()

    resumed_report = journey_sdk.execute(journey, state=state_file)

    assert events == ["prepare", "work_1_ready", "work_2_ready", "finish_ready"]
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
    second_seed = events[2].split("_")[2]

    assert events[0] == "prepare"
    assert events[1] == f"work_1_{first_seed}_True"
    assert events[2] == f"work_2_{second_seed}_True"
    assert first_seed == second_seed
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


def test_execute_resumes_interrupted_retryable_step_from_checkpoint_anchor(tmp_path):
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
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(
            poll,
            retry=1,
            retry_delay=0,
            retry_from=anchor,
        )
        journey_sdk.step(finish)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert events == ["warmup", "poll_1", "poll_2", "finish"]
    assert _record_labels(report.case_reports[0]) == ["warmup", "poll", "finish"]
    assert state_file.exists()


def test_execute_resume_preserves_retry_budget_for_checkpoint_anchor(tmp_path):
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
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(refresh)
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


def test_execute_retry_from_checkpoint_anchor_reuses_helper_args():
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
        anchor = journey_sdk.checkpoint()
        journey_sdk.step(refresh, payload)
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

    assert events == ["prepare", "publish_1", "publish_2"]
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
        "branch_b_2",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert state_file.exists()


def test_execute_rehydrates_checkpoint_started_branch_cases_without_rerunning_common_steps():
    helper_calls = {"count": 0}
    events: list[str] = []

    def next_payload():
        helper_calls["count"] += 1
        return {"seed": helper_calls["count"]}

    def prepare(payload):
        events.append(f"prepare_{payload['seed']}")
        return {"token": f"t{payload['seed']}"}

    def shared_after_checkpoint(token):
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
        after_setup = journey_sdk.checkpoint()
        shared = journey_sdk.step(shared_after_checkpoint, token)
        if journey_sdk.branch(start_from=after_setup):
            journey_sdk.step(finish_branch_a, shared)
        elif journey_sdk.branch(start_from=after_setup):
            journey_sdk.step(finish_branch_b, shared)

    report = journey_sdk.execute(journey)

    seed = events[0].split("_")[1]

    assert events == [
        f"prepare_{seed}",
        f"shared_t{seed}",
        f"branch_a_t{seed}",
        f"branch_b_t{seed}",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert _record_labels(report.case_reports[0]) == [
        "prepare",
        "shared_after_checkpoint",
        "finish_branch_a",
    ]
    assert _record_labels(report.case_reports[1]) == [
        "prepare",
        "shared_after_checkpoint",
        "finish_branch_b",
    ]


def test_execute_checkpoint_started_branches_keep_retry_counters_independent():
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
        journey_sdk.step(prepare)
        anchor = journey_sdk.checkpoint()
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


def test_execute_state_rejects_unserializable_step_result(tmp_path):
    state_file = tmp_path / "journey.state"

    def produce():
        return lambda: None

    def journey():
        journey_sdk.step(produce)

    with pytest.raises(ExecutionStateSerializationError):
        journey_sdk.execute(journey, state=state_file)

    assert not state_file.exists()


def test_execute_rehydration_rejects_unserializable_step_args():
    def journey():
        journey_sdk.step(lambda _payload: True, lambda: None, retry=1, retry_delay=0)

    with pytest.raises(ExecutionStateSerializationError):
        journey_sdk.execute(journey)


def test_execute_rehydration_rejects_unserializable_step_result_for_checkpoint_replay():
    def produce():
        return lambda: None

    def branch_a_step(_payload):
        return True

    def branch_b_step(_payload):
        return True

    def journey():
        payload = journey_sdk.step(produce)
        anchor = journey_sdk.checkpoint()
        if journey_sdk.branch(start_from=anchor):
            journey_sdk.step(branch_a_step, payload)
        elif journey_sdk.branch(start_from=anchor):
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
        pause_on_step=None,
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
        del ctx
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


def test_unknown_checkpoint_error_includes_next_step_hint():
    def journey():
        if journey_sdk.branch(start_from="missing_checkpoint"):
            journey_sdk.step(lambda: True)
        elif journey_sdk.branch():
            journey_sdk.step(lambda: True)

    with pytest.raises(UnknownCheckpointError) as exc_info:
        journey_sdk.compile_journey(journey)

    assert "missing_checkpoint" in str(exc_info.value)
    assert exc_info.value.hint is not None
