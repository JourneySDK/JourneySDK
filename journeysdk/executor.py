"""Execution runtime for compiled journey plans."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any, NoReturn, ParamSpec, Protocol, TypeVar, cast

from .errors import (
    AmbiguousStepSelectionError,
    CallableExecutionError,
    ExecutionStateSerializationError,
    ExecutionStateMismatchError,
    InvalidBranchUsageError,
    StepNotFoundError,
)
from .models import (
    BranchMarkerNode,
    CaseExecutionReport,
    CasePlan,
    CheckpointNode,
    CheckpointRef,
    ExecutionReport,
    JourneyPlan,
    NodeExecutionRecord,
    PlannedValue,
    StepNode,
    StepRetryDelay,
    StepRetryFrom,
)
from .planner import compile_journey
from .session import use_session
from .state import (
    STATE_FORMAT_VERSION,
    ActiveCaseState,
    ExecutionStateEnvelope,
    PausedStepState,
    RuntimeSnapshotState,
    SelectedCaseState,
    StepBindingState,
    artifact_root_for_state,
    delete_artifact_root,
    delete_execution_state,
    load_execution_state,
    save_execution_state,
)
from .rehydration import (
    JourneyRestoreContext,
    JourneyStoreContext,
    StoredValue,
    StoredValueRestoreError,
    StoredValueSerializationError,
    restore_value,
    store_value,
)
from .types import JourneyEntrypoint, StepFunction
from .utils import callable_ref
from .validator import JourneyValidation, resolve_branch_call_site, validate_journey


@dataclass
class _StopCase(Exception):
    pass


@dataclass
class _RetryRequested(Exception):
    sleep_for: float


@dataclass
class _PauseRequested(Exception):
    paused_step: PausedStepState


class _StepExitObject(Protocol):
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> object:
        ...


@dataclass(frozen=True)
class _PausedExecution:
    paused_step: PausedStepState


@dataclass(frozen=True)
class _DevelopStateRefresh:
    restarted_case_id: str | None = None


@dataclass(frozen=True)
class _SelectedCase:
    case_plan: CasePlan
    stop_after_index: int | None


@dataclass
class _ActiveBranchChain:
    group_id: str
    seen_keys: list[str]


P = ParamSpec("P")
R = TypeVar("R")


class _ExecutionObserver:
    def on_journey_start(
        self,
        *,
        plan: JourneyPlan,
        selected_cases: list[_SelectedCase],
    ) -> None:
        return

    def on_case_start(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
    ) -> None:
        return

    def on_case_resume(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
        replay_from_index: int,
    ) -> None:
        return

    def on_step_start(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
    ) -> None:
        return

    def on_branch(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
    ) -> None:
        return

    def on_retry(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
        delay_seconds: float,
        remaining_retries: int,
        error: Exception,
    ) -> None:
        return

    def on_step_success(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
    ) -> None:
        return

    def on_step_failure(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
        error: Exception,
    ) -> None:
        return

    def on_step_interrupted(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
        error: BaseException,
    ) -> None:
        return

    def on_case_complete(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
        duration_seconds: float,
    ) -> None:
        return

    def on_journey_complete(self, *, report: ExecutionReport) -> None:
        return

    def on_develop_state_restart(self, *, case_id: str) -> None:
        return


def _copy_binding(binding: StepBindingState) -> StepBindingState:
    return StepBindingState(
        args=tuple(binding.args),
        kwargs=dict(binding.kwargs),
        has_result=binding.has_result,
        result=binding.result,
        fn_ref=binding.fn_ref,
        source_fingerprint=binding.source_fingerprint,
    )


def _copy_runtime_snapshot(snapshot: RuntimeSnapshotState) -> RuntimeSnapshotState:
    return RuntimeSnapshotState(
        record_indices=list(snapshot.record_indices),
        records=list(snapshot.records),
        step_bindings={
            key: _copy_binding(binding)
            for key, binding in snapshot.step_bindings.items()
        },
        retry_remaining=dict(snapshot.retry_remaining),
        step_attempts=dict(snapshot.step_attempts),
    )


def _rehydration_key(*, kind: str, identifier: str, lineage: tuple[str, ...]) -> str:
    if not lineage:
        return f"{kind}:{identifier}"
    return f"{kind}:{identifier}|{'|'.join(lineage)}"


def _case_rehydration_maps(
    case_plan: CasePlan,
) -> tuple[dict[str, str], dict[str, str], str | None]:
    step_keys: dict[str, str] = {}
    checkpoint_keys: dict[str, str] = {}
    lineage: tuple[str, ...] = ()
    start_checkpoint_key: str | None = None

    for node in case_plan.nodes:
        if isinstance(node, StepNode):
            step_keys[node.node_id] = _rehydration_key(
                kind="step",
                identifier=node.node_id,
                lineage=lineage,
            )
            continue
        if isinstance(node, CheckpointNode):
            checkpoint_keys[node.name] = _rehydration_key(
                kind="checkpoint",
                identifier=node.name,
                lineage=lineage,
            )
            continue
        if isinstance(node, BranchMarkerNode):
            if start_checkpoint_key is None and node.start_from is not None:
                start_checkpoint_key = checkpoint_keys.get(node.start_from)
            lineage = lineage + (f"{node.group_id}={node.active_key}",)

    return step_keys, checkpoint_keys, start_checkpoint_key


def _callable_execution_error_for_step(
    node: StepNode,
    exc: Exception,
) -> CallableExecutionError:
    details = str(exc)
    notes = getattr(exc, "__notes__", None)
    if notes:
        details = f"{details} {' '.join(str(note) for note in notes)}"
    message = (
        f"Step '{node.label or node.node_id}' failed while it was running: "
        f"{type(exc).__name__}: {details}"
    )
    hint = "Inspect the step implementation or rerun after fixing the underlying failure."
    if node.retry is not None:
        message = (
            f"Step '{node.label or node.node_id}' failed while it was running "
            f"and its retry attempts were exhausted: {type(exc).__name__}: {details}"
        )
        hint = (
            "Inspect the step implementation, or increase step(..., retry=...) if "
            "the failure is expected to clear on its own."
        )
    return CallableExecutionError(message, hint=hint)


def _cleanup_failure_message(failures: list[BaseException]) -> str:
    if len(failures) == 1:
        failure = failures[0]
        return (
            "Step-exit cleanup failed: "
            f"{type(failure).__name__}: {failure}"
        )
    joined = "; ".join(
        f"{type(failure).__name__}: {failure}"
        for failure in failures
    )
    return f"{len(failures)} step-exit cleanup objects failed: {joined}"


def _add_cleanup_failure_notes(exc: BaseException, failures: list[BaseException]) -> None:
    if not failures:
        return
    add_note = getattr(exc, "add_note", None)
    message = _cleanup_failure_message(failures)
    if callable(add_note):
        add_note(message)


class _StateController:
    def __init__(
        self,
        path: Path | None,
        *,
        journey_plan: JourneyPlan,
        step: str | None,
        develop_step: str | None,
        selected_cases: list[_SelectedCase],
        allow_stale_develop_pause: bool = False,
    ) -> None:
        self.path = path
        self.journey_plan = journey_plan
        self.step = step
        self.develop_step = develop_step
        self.selected_cases = list(selected_cases)
        self._allow_stale_develop_pause = allow_stale_develop_pause
        self.artifact_root, self._artifact_root_is_temporary = artifact_root_for_state(path)
        self.plan_signature = _plan_signature(
            journey_plan,
            self.selected_cases,
            step,
            develop_step,
        )

        loaded: ExecutionStateEnvelope | None = None
        if path is not None:
            loaded = load_execution_state(path)

        if loaded is None:
            loaded = ExecutionStateEnvelope(
                version=STATE_FORMAT_VERSION,
                journey_id=journey_plan.journey_id,
                function_ref=journey_plan.function_ref,
                step=step,
                develop_step=develop_step,
                plan_signature=self.plan_signature,
                selected_cases=_selected_case_refs(self.selected_cases),
                current_case_index=0,
                completed_case_reports=[],
                active_case=None,
            )
        else:
            self._validate_loaded_state(loaded)

        self._state = loaded

    @property
    def completed_case_reports(self) -> list[CaseExecutionReport]:
        return list(self._state.completed_case_reports)

    @property
    def current_case_index(self) -> int:
        return self._state.current_case_index

    def binding_store_context(self, binding_key: str) -> JourneyStoreContext:
        return JourneyStoreContext(
            artifact_root=self._artifact_dir("binding", binding_key),
            boundary_kind="binding",
            boundary_id=binding_key,
        )

    def binding_restore_context(self, binding_key: str) -> JourneyRestoreContext:
        return JourneyRestoreContext(
            artifact_root=self._artifact_dir("binding", binding_key),
            boundary_kind="binding",
            boundary_id=binding_key,
        )

    def checkpoint_store_context(
        self,
        *,
        checkpoint_key: str,
        checkpoint_name: str,
        binding_key: str,
    ) -> JourneyStoreContext:
        return JourneyStoreContext(
            artifact_root=self._artifact_dir("checkpoint", checkpoint_key, binding_key),
            boundary_kind="checkpoint",
            boundary_id=checkpoint_key,
            checkpoint_name=checkpoint_name,
        )

    def checkpoint_restore_context(
        self,
        *,
        checkpoint_key: str,
        checkpoint_name: str,
        binding_key: str,
    ) -> JourneyRestoreContext:
        return JourneyRestoreContext(
            artifact_root=self._artifact_dir("checkpoint", checkpoint_key, binding_key),
            boundary_kind="checkpoint",
            boundary_id=checkpoint_key,
            checkpoint_name=checkpoint_name,
        )

    def active_state_store_context(
        self,
        *,
        case_id: str,
        binding_key: str,
    ) -> JourneyStoreContext:
        return JourneyStoreContext(
            artifact_root=self._artifact_dir("active-state", case_id, binding_key),
            boundary_kind="state",
            boundary_id=case_id,
        )

    def active_state_restore_context(
        self,
        *,
        case_id: str,
        binding_key: str,
    ) -> JourneyRestoreContext:
        return JourneyRestoreContext(
            artifact_root=self._artifact_dir("active-state", case_id, binding_key),
            boundary_kind="state",
            boundary_id=case_id,
        )

    def active_case_for(
        self,
        *,
        case_index: int,
        case_id: str,
    ) -> ActiveCaseState | None:
        active_case = self._state.active_case
        if active_case is None:
            return None
        if self._state.current_case_index != case_index:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' points at a different case than this run expects."
            )
        if active_case.case_id != case_id:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' points at case '{active_case.case_id}', "
                f"but this run expects case '{case_id}'."
            )
        return active_case

    def successful_step_bindings(self) -> dict[str, StepBindingState]:
        return {
            key: _copy_binding(binding)
            for key, binding in self._state.successful_step_bindings.items()
        }

    def checkpoint_snapshot_for(self, checkpoint_key: str) -> RuntimeSnapshotState | None:
        snapshot = self._state.checkpoint_snapshots.get(checkpoint_key)
        if snapshot is None:
            return None
        return _copy_runtime_snapshot(snapshot)

    def begin_case(
        self,
        *,
        case_index: int,
        snapshot: ActiveCaseState,
    ) -> None:
        self._state.current_case_index = case_index
        self._state.active_case = snapshot
        self._write_state()

    def update_active_case(self, snapshot: ActiveCaseState) -> None:
        if self._state.current_case_index >= len(self.selected_cases):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' is already marked as complete."
            )
        self._state.active_case = snapshot
        self._write_state()

    def promote_successful_binding(
        self,
        key: str,
        binding: StepBindingState,
    ) -> None:
        self._state.successful_step_bindings[key] = _copy_binding(binding)
        self._write_state()

    def store_checkpoint_snapshot(
        self,
        checkpoint_key: str,
        snapshot: RuntimeSnapshotState,
    ) -> None:
        self._state.checkpoint_snapshots[checkpoint_key] = _copy_runtime_snapshot(snapshot)
        self._write_state()

    def complete_case(self, report: CaseExecutionReport) -> None:
        expected_case = self.selected_cases[self._state.current_case_index].case_plan
        if report.case_id != expected_case.case_id:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' tried to complete case '{report.case_id}', "
                f"but this run expected '{expected_case.case_id}'."
            )
        self._state.completed_case_reports.append(report)
        self._state.current_case_index += 1
        self._state.active_case = None
        self._write_state()

    def clear(self) -> None:
        if self.path is not None:
            delete_execution_state(self.path)
        delete_artifact_root(self.artifact_root)

    def cleanup(self) -> None:
        if self._artifact_root_is_temporary:
            delete_artifact_root(self.artifact_root)

    def _write_state(self) -> None:
        if self.path is None:
            return
        save_execution_state(self.path, self._state)

    def _validate_loaded_state(self, state: ExecutionStateEnvelope) -> None:
        expected_cases = _selected_case_refs(self.selected_cases)
        expected_checkpoint_keys = {
            checkpoint_key
            for selected_case in self.selected_cases
            for checkpoint_key in _case_rehydration_maps(selected_case.case_plan)[1].values()
        }
        if state.version != STATE_FORMAT_VERSION:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' uses format version {state.version}, which this version of journey does not understand."
            )
        if state.journey_id != self.journey_plan.journey_id:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' belongs to journey '{state.journey_id}', "
                f"not '{self.journey_plan.journey_id}'."
            )
        if state.function_ref != self.journey_plan.function_ref:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' belongs to '{state.function_ref}', "
                f"not '{self.journey_plan.function_ref}'."
            )
        if state.step != self.step:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' was created for step {state.step!r}, "
                f"not {self.step!r}."
            )
        if state.develop_step != self.develop_step:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' was created for develop_step "
                f"{state.develop_step!r}, not {self.develop_step!r}."
            )
        if (
            self._allow_stale_develop_pause
            and state.active_case is not None
            and state.active_case.paused_step is not None
            and (
                state.plan_signature != self.plan_signature
                or state.selected_cases != expected_cases
            )
        ):
            self._validate_stale_develop_pause(state)
            return
        if state.plan_signature != self.plan_signature:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' no longer matches the current journey plan."
            )
        if state.selected_cases != expected_cases:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' no longer matches the selected case order."
            )
        if not 0 <= state.current_case_index <= len(self.selected_cases):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has invalid case index {state.current_case_index}."
            )
        if len(state.completed_case_reports) > len(self.selected_cases):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' contains more completed cases than this run has."
            )
        if len(state.completed_case_reports) > state.current_case_index:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has inconsistent completed-case progress."
            )

        self._validate_step_bindings(
            state.successful_step_bindings,
            label="run-wide step bindings",
        )
        for checkpoint_key, snapshot in state.checkpoint_snapshots.items():
            if not isinstance(checkpoint_key, str) or checkpoint_key not in expected_checkpoint_keys:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid checkpoint snapshot data."
                )
            self._validate_runtime_snapshot(
                snapshot,
                label=f"checkpoint snapshot '{checkpoint_key}'",
            )

        for index, report in enumerate(state.completed_case_reports):
            expected_case = self.selected_cases[index].case_plan
            if report.case_id != expected_case.case_id:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' contains completed case "
                    f"'{report.case_id}', but this run expected '{expected_case.case_id}'."
                )

        active_case = state.active_case
        if active_case is None:
            if state.current_case_index != len(state.completed_case_reports):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' is missing the active case snapshot."
                )
            return

        if state.current_case_index >= len(self.selected_cases):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' still has an active case even though all cases are complete."
            )
        if state.current_case_index != len(state.completed_case_reports):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has inconsistent active-case progress."
            )

        expected_case = self.selected_cases[state.current_case_index].case_plan
        if active_case.case_id != expected_case.case_id:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' points at case '{active_case.case_id}', "
                f"but this run expected '{expected_case.case_id}'."
            )

        self._validate_runtime_snapshot(
            active_case.snapshot,
            label=f"active case '{active_case.case_id}'",
        )
        node_ids = {
            node.node_id
            for node in expected_case.nodes
            if isinstance(node, StepNode)
        }
        if active_case.dirty_node_id is not None and active_case.dirty_node_id not in node_ids:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' points at missing step "
                f"'{active_case.dirty_node_id}'."
            )
        if active_case.stop_after_index is not None:
            if not isinstance(active_case.stop_after_index, int):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid active-case stop index."
                )
            if not 0 <= active_case.stop_after_index < len(expected_case.nodes):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' points at invalid stop index "
                    f"{active_case.stop_after_index}."
                )
        if active_case.paused_step is not None:
            paused_step = active_case.paused_step
            if paused_step.node_id not in node_ids:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' points at missing paused step "
                    f"'{paused_step.node_id}'."
                )
            if (
                isinstance(paused_step.node_index, bool)
                or not isinstance(paused_step.node_index, int)
                or not 0 <= paused_step.node_index < len(expected_case.nodes)
            ):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid paused-step index."
                )
            paused_node = expected_case.nodes[paused_step.node_index]
            if not isinstance(paused_node, StepNode) or paused_node.node_id != paused_step.node_id:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' points at an invalid paused step."
                )
            if isinstance(paused_step.attempt, bool) or not isinstance(paused_step.attempt, int):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid paused-step attempt data."
                )
        for node_id, remaining in active_case.snapshot.retry_remaining.items():
            if node_id not in node_ids:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' points at missing retry state "
                    f"for step '{node_id}'."
                )
            if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid retry state for step '{node_id}'."
                )
        for node_id, attempts in active_case.snapshot.step_attempts.items():
            if node_id not in node_ids:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' points at missing attempt state "
                    f"for step '{node_id}'."
                )
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid attempt state for step '{node_id}'."
                )

    def _validate_stale_develop_pause(self, state: ExecutionStateEnvelope) -> None:
        if state.active_case is None or state.active_case.paused_step is None:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' is not paused on a develop-step prompt."
            )
        if state.current_case_index < 0:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has invalid case index {state.current_case_index}."
            )
        if state.current_case_index != len(state.completed_case_reports):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has inconsistent active-case progress."
            )
        paused_step = state.active_case.paused_step
        if (
            isinstance(paused_step.node_index, bool)
            or not isinstance(paused_step.node_index, int)
            or paused_step.node_index < 0
        ):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has invalid paused-step index."
            )
        if isinstance(paused_step.attempt, bool) or not isinstance(paused_step.attempt, int):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has invalid paused-step attempt data."
            )
        self._validate_runtime_snapshot(
            state.active_case.snapshot,
            label=f"active case '{state.active_case.case_id}'",
        )

    def _validate_runtime_snapshot(
        self,
        snapshot: RuntimeSnapshotState,
        *,
        label: str,
    ) -> None:
        if not isinstance(snapshot, RuntimeSnapshotState):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has invalid {label}."
            )
        if len(snapshot.record_indices) != len(snapshot.records):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has inconsistent {label} records."
            )
        for remaining in snapshot.retry_remaining.values():
            if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid retry data in {label}."
                )
        for attempts in snapshot.step_attempts.values():
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid attempt data in {label}."
                )
        self._validate_step_bindings(snapshot.step_bindings, label=f"{label} step bindings")

    def _validate_step_bindings(
        self,
        bindings: dict[str, StepBindingState],
        *,
        label: str,
    ) -> None:
        if not isinstance(bindings, dict):
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' has invalid {label}."
            )
        for key, binding in bindings.items():
            if not isinstance(key, str) or not isinstance(binding, StepBindingState):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid {label}."
                )
            if binding.result is not None and not isinstance(binding.result, StoredValue):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid stored result data in {label}."
                )
            if binding.fn_ref is not None and not isinstance(binding.fn_ref, str):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid callable data in {label}."
                )
            if binding.source_fingerprint is not None and not isinstance(binding.source_fingerprint, str):
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid source fingerprint data in {label}."
                )
            for stored in binding.args:
                if not isinstance(stored, StoredValue):
                    raise ExecutionStateMismatchError(
                        f"The journey state file '{self.path}' has invalid stored args in {label}."
                    )
            for stored in binding.kwargs.values():
                if not isinstance(stored, StoredValue):
                    raise ExecutionStateMismatchError(
                        f"The journey state file '{self.path}' has invalid stored kwargs in {label}."
                    )

    def _artifact_dir(self, *parts: str) -> Path:
        path = self.artifact_root
        for part in parts:
            path = path / _artifact_segment(part)
        return path


def _artifact_segment(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class _RunSession:
    mode = "run"

    def __init__(
        self,
        journey_plan: JourneyPlan,
        case_plan: CasePlan,
        *,
        validation: JourneyValidation,
        stop_after_index: int | None,
        develop_step_enabled: bool,
        rehydration_enabled: bool,
        state_controller: _StateController | None = None,
        restored_state: ActiveCaseState | None = None,
        checkpoint_seed: RuntimeSnapshotState | None = None,
        checkpoint_restore_name: str | None = None,
        successful_bindings: dict[str, StepBindingState] | None = None,
        observer: _ExecutionObserver | None = None,
    ) -> None:
        self.journey_plan = journey_plan
        self.case_plan = case_plan
        self.validation = validation
        self.stop_after_index = stop_after_index
        self._develop_step_enabled = develop_step_enabled
        self._rehydration_enabled = rehydration_enabled
        self.cursor = 0
        self._group_counter = 0
        self._active_branch_chains: dict[tuple[int, int], _ActiveBranchChain] = {}
        self.records: list[NodeExecutionRecord] = []
        self._record_indices: list[int] = []
        self._step_bindings: dict[str, StepBindingState] = {}
        self._step_input_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._step_result_cache: dict[str, Any] = {}
        self._step_binding_contexts: dict[str, JourneyRestoreContext] = {}
        self._retry_remaining: dict[str, int] = {}
        self._step_attempts: dict[str, int] = {}
        self.replay_from_index = 0
        self._dirty_node_id: str | None = None
        self._paused_step: PausedStepState | None = None
        self._pending_checkpoint_restore_name = checkpoint_restore_name
        self._state_controller = state_controller
        self._observer = observer or _ExecutionObserver()
        self._active_step_exit_objects: list[_StepExitObject] | None = None
        (
            self._step_key_by_id,
            self._checkpoint_key_by_name,
            _,
        ) = _case_rehydration_maps(case_plan)
        self._checkpoint_name_by_key = {
            key: name
            for name, key in self._checkpoint_key_by_name.items()
        }
        self._step_index_by_id = {
            node.node_id: index
            for index, node in enumerate(case_plan.nodes)
            if isinstance(node, StepNode)
        }
        self._checkpoint_index_by_name = {
            node.name: index
            for index, node in enumerate(case_plan.nodes)
            if isinstance(node, CheckpointNode)
        }

        if successful_bindings is not None:
            for key, binding in successful_bindings.items():
                self._step_bindings[key] = _copy_binding(binding)
                self._step_binding_contexts[key] = self._binding_restore_context(key)
        if checkpoint_seed is not None:
            self._restore_checkpoint_seed(
                checkpoint_seed,
                checkpoint_name=checkpoint_restore_name,
            )
            self._pending_checkpoint_restore_name = None
        if restored_state is not None:
            self._restore_state(restored_state)

    def _next_group_id(self) -> str:
        self._group_counter += 1
        return f"bg_{self._group_counter}"

    def _runtime_snapshot(self) -> RuntimeSnapshotState:
        if self._rehydration_enabled:
            step_bindings = {
                key: self._freeze_binding(
                    key,
                    binding,
                    context=self._active_state_store_context(key),
                    description_prefix=f"active state for case '{self.case_plan.case_id}'",
                )
                for key, binding in self._step_bindings.items()
            }
        else:
            step_bindings = {
                key: _copy_binding(binding)
                for key, binding in self._step_bindings.items()
            }
        return RuntimeSnapshotState(
            record_indices=list(self._record_indices),
            records=list(self.records),
            step_bindings=step_bindings,
            retry_remaining=dict(self._retry_remaining),
            step_attempts=dict(self._step_attempts),
        )

    def snapshot_state(self) -> ActiveCaseState:
        return ActiveCaseState(
            case_id=self.case_plan.case_id,
            snapshot=self._runtime_snapshot(),
            replay_from_index=self.replay_from_index,
            dirty_node_id=self._dirty_node_id,
            stop_after_index=self.stop_after_index,
            paused_step=self._paused_step,
        )

    def _persist_state(self) -> None:
        if self._state_controller is None:
            return
        self._state_controller.update_active_case(self.snapshot_state())

    def _restore_checkpoint_seed(
        self,
        snapshot: RuntimeSnapshotState,
        *,
        checkpoint_name: str | None,
    ) -> None:
        restored = _copy_runtime_snapshot(snapshot)
        self._record_indices = restored.record_indices
        self.records = restored.records
        checkpoint_key = None
        if checkpoint_name is not None:
            checkpoint_key = self._checkpoint_key_by_name[checkpoint_name]
        for key, binding in restored.step_bindings.items():
            self._step_bindings[key] = binding
            self._step_binding_contexts[key] = self._checkpoint_restore_context(
                binding_key=key,
                checkpoint_key=checkpoint_key,
            )
            self._step_input_cache.pop(key, None)
            self._step_result_cache.pop(key, None)
        self._retry_remaining = restored.retry_remaining
        self._step_attempts = restored.step_attempts
        self.replay_from_index = (max(self._record_indices) + 1) if self._record_indices else 0
        self._restore_snapshot_results(restored.step_bindings)

    def _apply_checkpoint_restore(
        self,
        snapshot: RuntimeSnapshotState,
        *,
        checkpoint_name: str,
    ) -> None:
        restored = _copy_runtime_snapshot(snapshot)
        checkpoint_key = self._checkpoint_key_by_name[checkpoint_name]
        self._record_indices = restored.record_indices
        self.records = restored.records
        for key, binding in restored.step_bindings.items():
            self._step_bindings[key] = binding
            self._step_binding_contexts[key] = self._checkpoint_restore_context(
                binding_key=key,
                checkpoint_key=checkpoint_key,
            )
            self._step_input_cache.pop(key, None)
            self._step_result_cache.pop(key, None)
        self.replay_from_index = (max(self._record_indices) + 1) if self._record_indices else 0
        self._restore_snapshot_results(restored.step_bindings)

    def _restore_state(self, restored_state: ActiveCaseState) -> None:
        if restored_state.case_id != self.case_plan.case_id:
            raise ExecutionStateMismatchError(
                f"The journey state points at case '{restored_state.case_id}', "
                f"not '{self.case_plan.case_id}'."
            )

        restored = _copy_runtime_snapshot(restored_state.snapshot)
        self._record_indices = restored.record_indices
        self.records = restored.records
        self._step_bindings = restored.step_bindings
        self._step_binding_contexts = {
            key: self._active_state_restore_context(key)
            for key in restored.step_bindings
        }
        self._step_input_cache = {}
        self._step_result_cache = {}
        self._retry_remaining = restored.retry_remaining
        self.replay_from_index = restored_state.replay_from_index
        self._dirty_node_id = restored_state.dirty_node_id
        self._step_attempts = restored.step_attempts
        self.stop_after_index = restored_state.stop_after_index
        self._paused_step = restored_state.paused_step
        self._restore_snapshot_results(restored.step_bindings)

    @property
    def paused_step(self) -> PausedStepState | None:
        return self._paused_step

    def apply_pause_action(self, action: str | None) -> None:
        if self._paused_step is None:
            if action is not None:
                raise ExecutionStateMismatchError(
                    "Pause-on-step state was not available for the requested action."
                )
            return
        if action is None:
            return

        paused_step = self._paused_step
        if action == "continue":
            self.replay_from_index = paused_step.node_index + 1
            self.stop_after_index = _next_step_index_after(
                self.case_plan,
                paused_step.node_index,
            )
            self._pending_checkpoint_restore_name = None
            self._paused_step = None
            self._persist_state()
            return

        if action == "retry":
            node = self.case_plan.nodes[paused_step.node_index]
            if not isinstance(node, StepNode):
                raise ExecutionStateMismatchError(
                    "Pause-on-step state points at a non-step node."
                )
            anchor_index = paused_step.node_index
            if node.retry is not None:
                anchor_index = self._retry_anchor_index(node, paused_step.node_index)
                if node.retry.from_checkpoint is not None:
                    self._pending_checkpoint_restore_name = node.retry.from_checkpoint
            self._trim_from(anchor_index, preserve_retry_for=None)
            self.replay_from_index = anchor_index
            self.stop_after_index = paused_step.node_index
            self._dirty_node_id = None
            self._paused_step = None
            self._persist_state()
            return

        raise ExecutionStateMismatchError(
            f"Pause-on-step action {action!r} is not supported."
        )

    def _resume_dirty_step(self) -> None:
        if self._dirty_node_id is None:
            return

        node_index = self._step_index_by_id.get(self._dirty_node_id)
        if node_index is None:
            raise ExecutionStateMismatchError(
                f"The journey state points at missing step '{self._dirty_node_id}'."
            )

        node = self.case_plan.nodes[node_index]
        if not isinstance(node, StepNode):
            raise ExecutionStateMismatchError(
                f"The journey state points at '{self._dirty_node_id}', which is not a step."
            )

        anchor_index = node_index
        preserve_retry_for: str | None = None
        if node.retry is not None:
            anchor_index = self._retry_anchor_index(node, node_index)
            preserve_retry_for = node.node_id
            if node.retry.from_checkpoint is not None:
                self._pending_checkpoint_restore_name = node.retry.from_checkpoint

        self._trim_from(anchor_index, preserve_retry_for=preserve_retry_for)
        self.replay_from_index = anchor_index
        self._dirty_node_id = None
        self._persist_state()

    def _consume(self, expected_type: type[Any]) -> Any:
        if self.cursor >= len(self.case_plan.nodes):
            raise InvalidBranchUsageError(
                "The journey executed more steps than the compiled plan expected.",
                hint="Check for conditional logic or helper calls that add extra step() or checkpoint() calls at runtime.",
            )
        node = self.case_plan.nodes[self.cursor]
        if not isinstance(node, expected_type):
            expected = expected_type.__name__
            actual = type(node).__name__
            raise InvalidBranchUsageError(
                f"The journey took a different path than the compiled plan at position {self.cursor + 1}: expected {expected}, got {actual}.",
                hint="Make sure the journey calls step() and checkpoint() in the same structure each time it runs.",
            )
        self.cursor += 1
        return node

    def begin_attempt(self) -> None:
        self._journey_webhook_epoch = getattr(self, "_journey_webhook_epoch", 0) + 1
        self.cursor = 0
        self._group_counter = 0
        self._active_branch_chains.clear()
        self._resume_dirty_step()

    def _has_record_for(self, node_index: int) -> bool:
        return node_index in self._record_indices

    def _record(
        self,
        node_index: int,
        node: Any,
        ok: bool,
        result: Any = None,
        error: str | None = None,
    ) -> bool:
        label = getattr(node, "label", None)
        self._record_indices.append(node_index)
        self.records.append(
            NodeExecutionRecord(
                node_id=node.node_id,
                node_type=type(node).__name__,
                label=label,
                ok=ok,
                result=result,
                error=error,
            )
        )
        self._persist_state()
        return self.stop_after_index is not None and node_index == self.stop_after_index

    def _trim_from(self, start_index: int, *, preserve_retry_for: str | None) -> None:
        kept_records: list[NodeExecutionRecord] = []
        kept_indices: list[int] = []
        for node_index, record in zip(self._record_indices, self.records):
            if node_index < start_index:
                kept_indices.append(node_index)
                kept_records.append(record)
        self._record_indices = kept_indices
        self.records = kept_records

        for index in range(start_index, len(self.case_plan.nodes)):
            node = self.case_plan.nodes[index]
            if not isinstance(node, StepNode):
                continue
            binding_key = self._step_key_by_id[node.node_id]
            binding = self._step_bindings.get(binding_key)
            if binding is not None:
                binding.has_result = False
                binding.result = None
            self._step_input_cache.pop(binding_key, None)
            self._step_result_cache.pop(binding_key, None)
            if node.node_id != preserve_retry_for:
                self._retry_remaining.pop(node.node_id, None)

    def _retry_anchor_index(self, node: StepNode, node_index: int) -> int:
        if node.retry is None:
            raise InvalidBranchUsageError(
                "A retryable step was resumed without retry settings.",
                hint="Check the step(..., retry=..., retry_delay=..., retry_from=...) settings for that step.",
            )

        anchor_index: int | None = None
        if node.retry.from_node_id is not None:
            anchor_index = self._step_index_by_id.get(node.retry.from_node_id)
        elif node.retry.from_checkpoint is not None:
            anchor_index = self._checkpoint_index_by_name.get(node.retry.from_checkpoint)

        if anchor_index is None:
            raise InvalidBranchUsageError(
                f"Retry anchor for step '{node.label or node.node_id}' is missing from the compiled journey path.",
                hint="Make sure retry_from=... points to an earlier step() result or checkpoint() in the same journey path.",
            )
        if anchor_index > node_index:
            raise InvalidBranchUsageError(
                f"Retry anchor for step '{node.label or node.node_id}' appears after the step itself.",
                hint="Point retry_from=... to an earlier step() result or checkpoint().",
            )
        return anchor_index

    def _schedule_retry(self, node: StepNode, node_index: int) -> float | None:
        if node.retry is None:
            return None

        remaining = self._retry_remaining.get(node.node_id, node.retry.retries)
        if remaining <= 0:
            self._retry_remaining.pop(node.node_id, None)
            return None

        self._retry_remaining[node.node_id] = remaining - 1
        anchor_index = self._retry_anchor_index(node, node_index)
        if node.retry.from_checkpoint is not None:
            self._pending_checkpoint_restore_name = node.retry.from_checkpoint
        self._trim_from(anchor_index, preserve_retry_for=node.node_id)
        self.replay_from_index = anchor_index
        return node.retry.delay_seconds

    def _pause_after_step(
        self,
        node: StepNode,
        *,
        node_index: int,
        attempt: int,
        ok: bool,
        error: str | None = None,
        failure: CallableExecutionError | None = None,
    ) -> None:
        paused_step = PausedStepState(
            node_id=node.node_id,
            label=node.label,
            node_index=node_index,
            attempt=attempt,
            ok=ok,
            error=error,
            failure_message=failure.message if failure is not None else None,
            failure_hint=failure.hint if failure is not None else None,
        )
        self._paused_step = paused_step
        self._dirty_node_id = None
        self._persist_state()
        raise _PauseRequested(paused_step)

    def _store_step_inputs(
        self,
        node: StepNode,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> StepBindingState:
        binding_key = self._step_key_by_id[node.node_id]
        binding = StepBindingState(
            args=(),
            kwargs={},
            has_result=False,
            fn_ref=node.fn_ref,
            source_fingerprint=node.source_fingerprint,
        )
        if self._rehydration_enabled:
            base_context = self._binding_store_context(binding_key)
            binding = StepBindingState(
                args=tuple(
                    self._store_runtime_value(
                        value,
                        context=base_context.child(f"arg-{index}"),
                        description=f"step input {index + 1} for '{node.label or node.node_id}'",
                    )
                    for index, value in enumerate(args)
                ),
                kwargs={
                    key: self._store_runtime_value(
                        value,
                        context=base_context.child(f"kw-{key}"),
                        description=f"step kwarg {key!r} for '{node.label or node.node_id}'",
                    )
                    for key, value in kwargs.items()
                },
                has_result=False,
                fn_ref=node.fn_ref,
                source_fingerprint=node.source_fingerprint,
            )
            self._step_binding_contexts[binding_key] = self._binding_restore_context(binding_key)
        self._step_input_cache[binding_key] = (tuple(args), dict(kwargs))
        self._step_bindings[binding_key] = binding
        return binding

    def _set_step_result(self, node: StepNode, output: Any) -> StepBindingState:
        binding_key = self._step_key_by_id[node.node_id]
        binding = self._step_bindings.get(binding_key)
        if binding is None:
            binding = self._store_step_inputs(node, (), {})
        binding.fn_ref = node.fn_ref
        binding.source_fingerprint = node.source_fingerprint
        self._step_result_cache[binding_key] = output
        binding.has_result = True
        if self._rehydration_enabled:
            base_context = self._binding_store_context(binding_key)
            binding.result = self._store_runtime_value(
                output,
                context=base_context.child("result"),
                description=f"step result for '{node.label or node.node_id}'",
            )
        return binding

    def _discard_step_result(self, node: StepNode) -> None:
        binding_key = self._step_key_by_id[node.node_id]
        binding = self._step_bindings.get(binding_key)
        if binding is not None:
            binding.has_result = False
            binding.result = None
        self._step_result_cache.pop(binding_key, None)

    def _promote_step_result(
        self,
        node: StepNode,
        binding: StepBindingState,
    ) -> None:
        if self._state_controller is None or not self._rehydration_enabled:
            return
        binding_key = self._step_key_by_id[node.node_id]
        self._state_controller.promote_successful_binding(binding_key, binding)

    def _handle_step_exception(
        self,
        node: StepNode,
        *,
        node_index: int,
        attempt: int,
        started_at: float,
        exc: Exception,
    ) -> NoReturn:
        duration_seconds = time.perf_counter() - started_at
        sleep_for = self._schedule_retry(node, node_index)
        if sleep_for is not None:
            self._dirty_node_id = None
            self._persist_state()
            self._observer.on_retry(
                case_plan=self.case_plan,
                node=node,
                node_index=node_index,
                attempt=attempt,
                duration_seconds=duration_seconds,
                delay_seconds=sleep_for,
                remaining_retries=self._retry_remaining[node.node_id],
                error=exc,
            )
            raise _RetryRequested(sleep_for=sleep_for)
        self._retry_remaining.pop(node.node_id, None)
        self._dirty_node_id = None
        should_stop = self._record(node_index, node, ok=False, error=str(exc))
        self._observer.on_step_failure(
            case_plan=self.case_plan,
            node=node,
            node_index=node_index,
            attempt=attempt,
            duration_seconds=duration_seconds,
            error=exc,
        )
        failure = _callable_execution_error_for_step(node, exc)
        if should_stop and self._develop_step_enabled:
            self._pause_after_step(
                node,
                node_index=node_index,
                attempt=attempt,
                ok=False,
                error=str(exc),
                failure=failure,
            )
        raise failure from exc

    def _resolve_binding_value(self, template: Any, restored_value: Any) -> Any:
        if isinstance(template, PlannedValue) and template.kind == "step":
            binding_key = self._step_key_by_id.get(template.node_id)
            binding = self._step_bindings.get(binding_key) if binding_key is not None else None
            if binding is None or not binding.has_result:
                raise InvalidBranchUsageError(
                    f"Replay is missing the saved result for step reference '{template.node_id}'.",
                    hint="This usually means the journey changed after the run started. Start over or use a new state file.",
                )
            resolved = self._materialize_step_result(
                binding_key,
                binding,
                description=f"saved result for step '{template.node_id}'",
            )
            for attribute in template.access_path:
                if not hasattr(resolved, attribute):
                    raise InvalidBranchUsageError(
                        f"Replay is missing attribute '{attribute}' on the saved result for step '{template.node_id}'.",
                        hint="This usually means the step result type changed after the run started.",
                    )
                resolved = getattr(resolved, attribute)
            return resolved
        if isinstance(template, tuple) and isinstance(restored_value, tuple) and len(template) == len(restored_value):
            return tuple(
                self._resolve_binding_value(item_template, item_value)
                for item_template, item_value in zip(template, restored_value)
            )
        if isinstance(template, list) and isinstance(restored_value, list) and len(template) == len(restored_value):
            return [
                self._resolve_binding_value(item_template, item_value)
                for item_template, item_value in zip(template, restored_value)
            ]
        if isinstance(template, dict) and isinstance(restored_value, dict):
            return {
                key: self._resolve_binding_value(template[key], restored_value[key])
                if key in template
                else restored_value[key]
                for key in restored_value
            }
        return restored_value

    def _resolve_step_inputs(self, node: StepNode, binding: StepBindingState) -> tuple[tuple[Any, ...], dict[str, Any]]:
        binding_key = self._step_key_by_id[node.node_id]
        cached = self._step_input_cache.get(binding_key)
        if cached is not None:
            return cached

        base_context = self._step_binding_contexts.get(binding_key)
        if base_context is None:
            base_context = self._binding_restore_context(binding_key)
            self._step_binding_contexts[binding_key] = base_context

        resolved_args = tuple(
            self._materialize_input_value(
                template=node.args[index],
                stored=binding.args[index],
                context=base_context.child(f"arg-{index}"),
                description=f"step input {index + 1} for '{node.label or node.node_id}'",
            )
            for index in range(len(binding.args))
        )
        resolved_kwargs = {
            key: self._materialize_input_value(
                template=node.kwargs.get(key),
                stored=binding.kwargs[key],
                context=base_context.child(f"kw-{key}"),
                description=f"step kwarg {key!r} for '{node.label or node.node_id}'",
            )
            for key in binding.kwargs
        }
        self._step_input_cache[binding_key] = (resolved_args, resolved_kwargs)
        return resolved_args, resolved_kwargs

    def _materialize_input_value(
        self,
        *,
        template: Any,
        stored: StoredValue,
        context: JourneyRestoreContext,
        description: str,
    ) -> Any:
        if isinstance(template, PlannedValue) and template.kind == "step":
            return self._resolve_binding_value(template, None)
        restored_value = self._restore_stored_value(
            stored,
            context=context,
            description=description,
        )
        return self._resolve_binding_value(template, restored_value)

    def _materialize_step_result(
        self,
        binding_key: str,
        binding: StepBindingState,
        *,
        description: str,
    ) -> Any:
        cached = self._step_result_cache.get(binding_key)
        if cached is not None:
            return cached
        if not binding.has_result or binding.result is None:
            raise InvalidBranchUsageError(
                f"Replay is missing the saved result for step binding '{binding_key}'.",
                hint="This usually means the journey changed after the run started. Start over or use a new state file.",
            )
        context = self._step_binding_contexts.get(binding_key)
        if context is None:
            context = self._binding_restore_context(binding_key)
            self._step_binding_contexts[binding_key] = context
        restored = self._restore_stored_value(
            binding.result,
            context=context.child("result"),
            description=description,
        )
        self._step_result_cache[binding_key] = restored
        return restored

    def _restore_snapshot_results(self, bindings: dict[str, StepBindingState]) -> None:
        for key, binding in bindings.items():
            if binding.has_result and binding.result is not None:
                self._materialize_step_result(
                    key,
                    binding,
                    description=f"saved step result '{key}'",
                )

    def _store_runtime_value(
        self,
        value: Any,
        *,
        context: JourneyStoreContext,
        description: str,
    ) -> StoredValue:
        try:
            return store_value(value, context=context, description=description)
        except StoredValueSerializationError as exc:
            raise ExecutionStateSerializationError(str(exc)) from exc

    def _restore_stored_value(
        self,
        stored: StoredValue,
        *,
        context: JourneyRestoreContext,
        description: str,
    ) -> Any:
        try:
            return restore_value(stored, context=context, description=description)
        except StoredValueRestoreError as exc:
            raise CallableExecutionError(
                str(exc),
                hint="Inspect the custom __restore__ implementation or restart the run after fixing the underlying issue.",
            ) from exc

    def _freeze_binding(
        self,
        binding_key: str,
        binding: StepBindingState,
        *,
        context: JourneyStoreContext,
        description_prefix: str,
    ) -> StepBindingState:
        frozen = _copy_binding(binding)
        cached_inputs = self._step_input_cache.get(binding_key)
        if cached_inputs is not None:
            cached_args, cached_kwargs = cached_inputs
            frozen.args = tuple(
                self._store_runtime_value(
                    value,
                    context=context.child(f"arg-{index}"),
                    description=f"{description_prefix} input {index + 1}",
                )
                for index, value in enumerate(cached_args)
            )
            frozen.kwargs = {
                key: self._store_runtime_value(
                    value,
                    context=context.child(f"kw-{key}"),
                    description=f"{description_prefix} kwarg {key!r}",
                )
                for key, value in cached_kwargs.items()
            }
        if binding.has_result:
            cached_result = self._step_result_cache.get(binding_key)
            if cached_result is not None:
                frozen.result = self._store_runtime_value(
                    cached_result,
                    context=context.child("result"),
                    description=f"{description_prefix} result",
                )
        return frozen

    def _binding_store_context(self, binding_key: str) -> JourneyStoreContext:
        return self._require_state_controller().binding_store_context(binding_key)

    def _binding_restore_context(self, binding_key: str) -> JourneyRestoreContext:
        return self._require_state_controller().binding_restore_context(binding_key)

    def _checkpoint_store_context(
        self,
        checkpoint_name: str,
        binding_key: str,
    ) -> JourneyStoreContext:
        checkpoint_key = self._checkpoint_key_by_name[checkpoint_name]
        return self._require_state_controller().checkpoint_store_context(
            checkpoint_key=checkpoint_key,
            checkpoint_name=checkpoint_name,
            binding_key=binding_key,
        )

    def _checkpoint_restore_context(
        self,
        *,
        binding_key: str,
        checkpoint_key: str | None,
    ) -> JourneyRestoreContext:
        if checkpoint_key is None:
            return self._binding_restore_context(binding_key)
        checkpoint_name = self._checkpoint_name_by_key[checkpoint_key]
        return self._require_state_controller().checkpoint_restore_context(
            checkpoint_key=checkpoint_key,
            checkpoint_name=checkpoint_name,
            binding_key=binding_key,
        )

    def _active_state_store_context(self, binding_key: str) -> JourneyStoreContext:
        return self._require_state_controller().active_state_store_context(
            case_id=self.case_plan.case_id,
            binding_key=binding_key,
        )

    def _active_state_restore_context(self, binding_key: str) -> JourneyRestoreContext:
        return self._require_state_controller().active_state_restore_context(
            case_id=self.case_plan.case_id,
            binding_key=binding_key,
        )

    def _require_state_controller(self) -> _StateController:
        if self._state_controller is None:
            raise ExecutionStateMismatchError(
                "Replay state is unavailable for this run."
            )
        return self._state_controller

    def _register_step_exit_object(self, value: object) -> None:
        """Register one lifecycle object for the current step attempt."""

        if self._active_step_exit_objects is None:
            raise InvalidBranchUsageError(
                "Step-exit cleanup can only be registered while a journey step is running.",
                hint=(
                    "Call lifecycle-aware tools from inside a function passed to "
                    "step(...), not during planning, module import, or between steps."
                ),
            )
        exit_method = getattr(value, "__exit__", None)
        if not callable(exit_method):
            raise TypeError(
                "Step-exit lifecycle objects must define a callable "
                "__exit__(exc_type, exc, traceback) method."
            )
        self._active_step_exit_objects.append(cast(_StepExitObject, value))

    def _start_step_exit_lifecycle(self) -> None:
        if self._active_step_exit_objects is not None:
            raise InvalidBranchUsageError(
                "A journey step started while another step was already running.",
                hint="Do not call step(...) from inside another step function.",
            )
        self._active_step_exit_objects = []

    def _finish_step_exit_lifecycle(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> list[BaseException]:
        objects = self._active_step_exit_objects
        self._active_step_exit_objects = None
        if objects is None:
            return []

        failures: list[BaseException] = []
        for value in reversed(objects):
            try:
                value.__exit__(exc_type, exc, traceback)
            except BaseException as cleanup_exc:  # pragma: no cover - exercised through callers
                failures.append(cleanup_exc)
        return failures

    def step(
        self,
        fn: StepFunction[P, R],
        *args: P.args,
        retry: int = 0,
        retry_delay: StepRetryDelay = 5,
        retry_from: StepRetryFrom = None,
        **kwargs: P.kwargs,
    ) -> R:
        del retry, retry_delay, retry_from
        node_index = self.cursor
        node = self._consume(StepNode)
        if callable_ref(fn) != node.fn_ref:
            raise InvalidBranchUsageError(
                f"step() was called with '{callable_ref(fn)}', but the compiled plan expected '{node.fn_ref}'.",
                hint="Make sure the journey calls the same step functions during execution that it used during planning.",
            )

        binding_key = self._step_key_by_id[node.node_id]
        binding = self._step_bindings.get(binding_key)
        if node_index < self.replay_from_index:
            if binding is None or not binding.has_result:
                raise InvalidBranchUsageError(
                    f"Retry replay is missing the saved result for step '{node.label or node.node_id}'.",
                    hint="This usually means the journey changed after the run started. Start over or use a new state file.",
                )
            return cast(
                R,
                self._materialize_step_result(
                    binding_key,
                    binding,
                    description=f"saved result for step '{node.label or node.node_id}'",
                ),
            )

        if binding is not None and binding.has_result and not self._has_record_for(node_index):
            result = self._materialize_step_result(
                binding_key,
                binding,
                description=f"saved result for step '{node.label or node.node_id}'",
            )
            should_stop = self._record(node_index, node, ok=True, result=result)
            if should_stop:
                if self._develop_step_enabled:
                    self._pause_after_step(
                        node,
                        node_index=node_index,
                        attempt=self._step_attempts.get(node.node_id, 0),
                        ok=True,
                    )
                raise _StopCase()
            return cast(R, result)

        bound_args: tuple[Any, ...]
        bound_kwargs: dict[str, Any]
        if binding is not None and not binding.has_result:
            bound_args, bound_kwargs = self._resolve_step_inputs(node, binding)
        else:
            binding = self._store_step_inputs(node, tuple(args), dict(kwargs))
            bound_args, bound_kwargs = self._resolve_step_inputs(node, binding)

        self._dirty_node_id = node.node_id
        attempt = self._step_attempts.get(node.node_id, 0) + 1
        self._step_attempts[node.node_id] = attempt
        self._persist_state()
        self._observer.on_step_start(
            case_plan=self.case_plan,
            node=node,
            node_index=node_index,
            attempt=attempt,
        )
        started_at = time.perf_counter()

        self._start_step_exit_lifecycle()
        try:
            output = fn(*bound_args, **bound_kwargs)
        except KeyboardInterrupt as exc:
            cleanup_failures = self._finish_step_exit_lifecycle(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            self._observer.on_step_interrupted(
                case_plan=self.case_plan,
                node=node,
                node_index=node_index,
                attempt=attempt,
                duration_seconds=time.perf_counter() - started_at,
                error=exc,
            )
            raise
        except Exception as exc:  # pragma: no cover - surfaced to caller
            cleanup_failures = self._finish_step_exit_lifecycle(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            self._handle_step_exception(
                node,
                node_index=node_index,
                attempt=attempt,
                started_at=started_at,
                exc=exc,
            )

        try:
            binding = self._set_step_result(node, output)
        except Exception as exc:
            cleanup_failures = self._finish_step_exit_lifecycle(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            raise

        cleanup_failures = self._finish_step_exit_lifecycle()
        if cleanup_failures:
            self._discard_step_result(node)
            cleanup_error = RuntimeError(_cleanup_failure_message(cleanup_failures))
            self._handle_step_exception(
                node,
                node_index=node_index,
                attempt=attempt,
                started_at=started_at,
                exc=cleanup_error,
            )

        self._promote_step_result(node, binding)
        self._retry_remaining.pop(node.node_id, None)
        self._dirty_node_id = None
        should_stop = self._record(node_index, node, ok=True, result=output)
        self._observer.on_step_success(
            case_plan=self.case_plan,
            node=node,
            node_index=node_index,
            attempt=attempt,
            duration_seconds=time.perf_counter() - started_at,
        )
        if should_stop:
            if self._develop_step_enabled:
                self._pause_after_step(
                    node,
                    node_index=node_index,
                    attempt=attempt,
                    ok=True,
                )
            raise _StopCase()
        return cast(R, output)

    def checkpoint(self) -> CheckpointRef:
        checkpoint_index = self.cursor
        checkpoint_node = self._consume(CheckpointNode)
        checkpoint_key = self._checkpoint_key_by_name[checkpoint_node.name]
        if self._pending_checkpoint_restore_name == checkpoint_node.name:
            snapshot = self._require_state_controller().checkpoint_snapshot_for(checkpoint_key)
            if snapshot is None:
                raise InvalidBranchUsageError(
                    f"Replay is missing the saved checkpoint snapshot for '{checkpoint_node.name}'.",
                    hint="This usually means the journey changed after the run started. Start over or use a new state file.",
                )
            self._apply_checkpoint_restore(
                snapshot,
                checkpoint_name=checkpoint_node.name,
            )
            self._pending_checkpoint_restore_name = None
            return CheckpointRef(name=checkpoint_node.name)

        if checkpoint_index >= self.replay_from_index:
            should_stop = self._record(
                checkpoint_index,
                checkpoint_node,
                ok=True,
                result=checkpoint_node.name,
            )
            if should_stop:
                raise _StopCase()
            if self._state_controller is not None and self._rehydration_enabled:
                snapshot = RuntimeSnapshotState(
                    record_indices=list(self._record_indices),
                    records=list(self.records),
                    step_bindings={
                        key: self._freeze_binding(
                            key,
                            binding,
                            context=self._checkpoint_store_context(checkpoint_node.name, key),
                            description_prefix=f"checkpoint '{checkpoint_node.name}'",
                        )
                        for key, binding in self._step_bindings.items()
                    },
                    retry_remaining=dict(self._retry_remaining),
                    step_attempts=dict(self._step_attempts),
                )
                self._state_controller.store_checkpoint_snapshot(
                    checkpoint_key,
                    snapshot,
                )

        return CheckpointRef(name=checkpoint_node.name)

    def branch(self, *, start_from: str | None, frame: FrameType) -> bool:
        site = resolve_branch_call_site(frame)
        spec = self.validation.branch_conditions.get(site)
        if spec is None:
            raise InvalidBranchUsageError(
                "journey.branch(...) is only valid as a direct if/elif condition.",
                hint="Use journey.branch(...) directly as `if journey.branch(...):` or `elif journey.branch(...):`.",
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
                seen_keys=[],
            )
            self._active_branch_chains[spec.template_key] = state
        elif state is None:
            raise InvalidBranchUsageError(
                "journey.branch(...) did not follow the expected if/elif chain.",
                hint="Use journey.branch(...) only in one direct if/elif chain.",
            )

        state.seen_keys.append(spec.branch_key)
        active_key = self.case_plan.branch_env.get(state.group_id)
        if active_key is None:
            self._active_branch_chains.pop(spec.template_key, None)
            raise InvalidBranchUsageError(
                f"The journey took a different path than the compiled plan at branch group '{state.group_id}'.",
                hint="Make sure the journey calls journey.branch(...) in the same structure each time it runs.",
            )

        if active_key != spec.branch_key:
            if spec.condition_index == spec.total_conditions:
                self._active_branch_chains.pop(spec.template_key, None)
                raise InvalidBranchUsageError(
                    f"The compiled plan selected branch key '{active_key}', but the current if/elif chain only reached {state.seen_keys}.",
                    hint="Make sure the journey calls the same journey.branch(...) conditions during execution that it used during planning.",
                )
            return False

        marker_index = self.cursor
        node = self._consume(BranchMarkerNode)
        if node.group_id != state.group_id or node.active_key != active_key:
            raise InvalidBranchUsageError(
                "The journey took a different branch path than the compiled plan expected.",
                hint="Make sure the journey calls journey.branch(...) in the same order during execution that it used during planning.",
            )
        if start_from != node.start_from:
            raise InvalidBranchUsageError(
                "journey.branch(start_from=...) used a checkpoint that does not match the compiled plan.",
                hint="Make sure each journey.branch(start_from=...) points to the same checkpoint it used during planning.",
            )

        if marker_index >= self.replay_from_index:
            should_stop = self._record(marker_index, node, ok=True, result=node.active_key)
            self._observer.on_branch(
                case_plan=self.case_plan,
                node=node,
                node_index=marker_index,
            )
            if should_stop:
                raise _StopCase()
        self._active_branch_chains.pop(spec.template_key, None)
        return True


def _node_label(node: Any) -> str | None:
    return getattr(node, "label", None)


def _next_step_index_after(case_plan: CasePlan, node_index: int) -> int | None:
    for index in range(node_index + 1, len(case_plan.nodes)):
        if isinstance(case_plan.nodes[index], StepNode):
            return index
    return None


def _locate_step_matches(plan: JourneyPlan, step: str) -> list[tuple[CasePlan, int]]:
    matches: list[tuple[CasePlan, int]] = []
    for case in plan.case_plans:
        for index, node in enumerate(case.nodes):
            label = _node_label(node)
            if label == step:
                matches.append((case, index))
    return matches


def _select_cases(plan: JourneyPlan, step: str | None) -> list[_SelectedCase]:
    if step is None:
        return [_SelectedCase(case_plan=case, stop_after_index=None) for case in plan.case_plans]

    matches = _locate_step_matches(plan, step)
    if not matches:
        raise StepNotFoundError(step)

    matching_case_ids = sorted({case.case_id for case, _ in matches})
    if len(matching_case_ids) != 1:
        raise AmbiguousStepSelectionError(step, matching_case_ids)

    chosen_case, stop_after_index = min(matches, key=lambda item: item[1])
    return [_SelectedCase(case_plan=chosen_case, stop_after_index=stop_after_index)]


def _replay_anchor_for(case_plan: CasePlan, stop_after_index: int | None) -> str | None:
    if stop_after_index is None:
        return None
    upto = min(stop_after_index, len(case_plan.nodes) - 1)
    for index in range(upto, -1, -1):
        node = case_plan.nodes[index]
        if isinstance(node, BranchMarkerNode) and node.start_from is not None:
            return node.start_from
    return None


def _start_checkpoint_name_for(case_plan: CasePlan) -> str | None:
    for node in case_plan.nodes:
        if isinstance(node, BranchMarkerNode) and node.start_from is not None:
            return node.start_from
    return None


def _selected_case_refs(selected_cases: list[_SelectedCase]) -> list[SelectedCaseState]:
    return [
        SelectedCaseState(
            case_id=item.case_plan.case_id,
            stop_after_index=item.stop_after_index,
        )
        for item in selected_cases
    ]


def _needs_rehydration(
    selected_cases: list[_SelectedCase],
    *,
    step: str | None,
    develop_step: str | None,
    state: str | Path | None,
) -> bool:
    if develop_step is not None:
        return True
    if state is not None:
        return True
    if step is None:
        for selected_case in selected_cases:
            for node in selected_case.case_plan.nodes:
                if isinstance(node, StepNode) and node.retry is not None:
                    return True
                if isinstance(node, BranchMarkerNode) and node.start_from is not None:
                    return True
        return False

    for selected_case in selected_cases:
        for node in selected_case.case_plan.nodes:
            if isinstance(node, StepNode) and node.retry is not None:
                return True
    return False


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _stable_value(asdict(value))
    if isinstance(value, tuple):
        return [_stable_value(item) for item in value]
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, set):
        return sorted(repr(_stable_value(item)) for item in value)
    return repr(value)


def _stable_arg_template(value: Any) -> Any:
    if isinstance(value, PlannedValue):
        payload = {
            "kind": value.kind,
            "node_id": value.node_id,
        }
        if value.access_path:
            payload["access_path"] = list(value.access_path)
        return payload
    if isinstance(value, tuple):
        return [_stable_arg_template(item) for item in value]
    if isinstance(value, list):
        return [_stable_arg_template(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_arg_template(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    return {"literal_type": type(value).__name__}


def _stable_node_payload(node: Any) -> Any:
    if isinstance(node, StepNode):
        return {
            "node_id": node.node_id,
            "label": node.label,
            "fn_ref": node.fn_ref,
            "args": _stable_arg_template(node.args),
            "kwargs": _stable_arg_template(node.kwargs),
            "retry": _stable_value(asdict(node.retry)) if node.retry is not None else None,
            "source_fingerprint": node.source_fingerprint,
        }
    if isinstance(node, CheckpointNode):
        return {
            "node_id": node.node_id,
            "name": node.name,
        }
    return _stable_value(asdict(node))


def _plan_signature(
    journey_plan: JourneyPlan,
    selected_cases: list[_SelectedCase],
    step: str | None,
    develop_step: str | None,
) -> str:
    payload = {
        "journey_id": journey_plan.journey_id,
        "function_ref": journey_plan.function_ref,
        "step": step,
        "develop_step": develop_step,
        "cases": [
            {
                "case_id": item.case_plan.case_id,
                "stop_after_index": item.stop_after_index,
                "branch_env": _stable_value(item.case_plan.branch_env),
                "nodes": [_stable_node_payload(node) for node in item.case_plan.nodes],
            }
            for item in selected_cases
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _find_paused_step_index(
    case_plan: CasePlan,
    paused_step: PausedStepState,
) -> int | None:
    if 0 <= paused_step.node_index < len(case_plan.nodes):
        node = case_plan.nodes[paused_step.node_index]
        if (
            isinstance(node, StepNode)
            and node.node_id == paused_step.node_id
            and (paused_step.label is None or node.label == paused_step.label)
        ):
            return paused_step.node_index

    for index, node in enumerate(case_plan.nodes):
        if (
            isinstance(node, StepNode)
            and node.node_id == paused_step.node_id
            and (paused_step.label is None or node.label == paused_step.label)
        ):
            return index

    if paused_step.label is None:
        return None
    matches = [
        index
        for index, node in enumerate(case_plan.nodes)
        if isinstance(node, StepNode) and node.label == paused_step.label
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _retry_anchor_index_for_case(
    case_plan: CasePlan,
    node: StepNode,
    node_index: int,
) -> int | None:
    if node.retry is None:
        return node_index

    step_index_by_id = {
        item.node_id: index
        for index, item in enumerate(case_plan.nodes)
        if isinstance(item, StepNode)
    }
    checkpoint_index_by_name = {
        item.name: index
        for index, item in enumerate(case_plan.nodes)
        if isinstance(item, CheckpointNode)
    }
    if node.retry.from_node_id is not None:
        return step_index_by_id.get(node.retry.from_node_id)
    if node.retry.from_checkpoint is not None:
        return checkpoint_index_by_name.get(node.retry.from_checkpoint)
    return node_index


def _record_matches_node(
    record: NodeExecutionRecord,
    node: Any,
    binding: StepBindingState | None,
) -> bool:
    if record.node_id != node.node_id:
        return False
    if record.node_type != type(node).__name__:
        return False
    if record.label != getattr(node, "label", None):
        return False

    if isinstance(node, StepNode):
        if binding is None:
            return False
        return (
            binding.fn_ref == node.fn_ref
            and binding.source_fingerprint == node.source_fingerprint
        )
    if isinstance(node, CheckpointNode):
        return record.result == node.name
    if isinstance(node, BranchMarkerNode):
        return record.result == node.active_key
    return False


def _snapshot_matches_prefix(
    snapshot: RuntimeSnapshotState,
    case_plan: CasePlan,
    boundary_index: int,
) -> bool:
    if boundary_index < 0 or boundary_index > len(case_plan.nodes):
        return False

    prefix = [
        (index, record)
        for index, record in zip(snapshot.record_indices, snapshot.records)
        if index < boundary_index
    ]
    if [index for index, _ in prefix] != list(range(boundary_index)):
        return False

    step_keys, _, _ = _case_rehydration_maps(case_plan)
    for index, record in prefix:
        node = case_plan.nodes[index]
        binding = (
            snapshot.step_bindings.get(step_keys[node.node_id])
            if isinstance(node, StepNode)
            else None
        )
        if not _record_matches_node(record, node, binding):
            return False
    return True


def _trim_snapshot_to_prefix(
    snapshot: RuntimeSnapshotState,
    case_plan: CasePlan,
    boundary_index: int,
) -> RuntimeSnapshotState:
    step_keys, _, _ = _case_rehydration_maps(case_plan)
    keep_step_ids = {
        node.node_id
        for node in case_plan.nodes[:boundary_index]
        if isinstance(node, StepNode)
    }
    keep_binding_keys = {
        step_keys[node_id]
        for node_id in keep_step_ids
        if node_id in step_keys
    }
    kept_records = [
        (index, record)
        for index, record in zip(snapshot.record_indices, snapshot.records)
        if index < boundary_index
    ]
    return RuntimeSnapshotState(
        record_indices=[index for index, _ in kept_records],
        records=[record for _, record in kept_records],
        step_bindings={
            key: _copy_binding(binding)
            for key, binding in snapshot.step_bindings.items()
            if key in keep_binding_keys
        },
        retry_remaining={
            node_id: remaining
            for node_id, remaining in snapshot.retry_remaining.items()
            if node_id in keep_step_ids
        },
        step_attempts={
            node_id: attempts
            for node_id, attempts in snapshot.step_attempts.items()
            if node_id in keep_step_ids
        },
    )


def _restart_develop_state(
    path: Path,
    *,
    case_id: str,
    observer: _ExecutionObserver,
) -> _DevelopStateRefresh:
    delete_execution_state(path)
    artifact_root, _ = artifact_root_for_state(path)
    delete_artifact_root(artifact_root)
    observer.on_develop_state_restart(case_id=case_id)
    return _DevelopStateRefresh(restarted_case_id=case_id)


def _refresh_develop_state_for_plan(
    path: Path | None,
    *,
    journey_plan: JourneyPlan,
    step: str | None,
    develop_step: str | None,
    selected_cases: list[_SelectedCase],
    pause_action: str | None,
    observer: _ExecutionObserver,
) -> _DevelopStateRefresh:
    if path is None or develop_step is None or pause_action not in {"continue", "retry"}:
        return _DevelopStateRefresh()

    state = load_execution_state(path)
    if state is None:
        return _DevelopStateRefresh()

    expected_cases = _selected_case_refs(selected_cases)
    new_signature = _plan_signature(journey_plan, selected_cases, step, develop_step)
    if state.plan_signature == new_signature and state.selected_cases == expected_cases:
        return _DevelopStateRefresh()

    if (
        state.version != STATE_FORMAT_VERSION
        or state.journey_id != journey_plan.journey_id
        or state.function_ref != journey_plan.function_ref
        or state.step != step
        or state.develop_step != develop_step
        or state.active_case is None
        or state.active_case.paused_step is None
        or not 0 <= state.current_case_index < len(selected_cases)
    ):
        return _DevelopStateRefresh()

    active_case = state.active_case
    selected_case = selected_cases[state.current_case_index]
    case_plan = selected_case.case_plan
    if active_case.case_id != case_plan.case_id:
        return _restart_develop_state(
            path,
            case_id=active_case.case_id,
            observer=observer,
        )

    paused_index = _find_paused_step_index(case_plan, active_case.paused_step)
    if paused_index is None:
        return _restart_develop_state(
            path,
            case_id=active_case.case_id,
            observer=observer,
        )
    paused_node = case_plan.nodes[paused_index]
    if not isinstance(paused_node, StepNode):
        return _restart_develop_state(
            path,
            case_id=active_case.case_id,
            observer=observer,
        )

    if pause_action == "continue":
        reusable_boundary = paused_index + 1
        trim_boundary: int | None = None
    else:
        retry_anchor = _retry_anchor_index_for_case(case_plan, paused_node, paused_index)
        if retry_anchor is None:
            return _restart_develop_state(
                path,
                case_id=active_case.case_id,
                observer=observer,
            )
        reusable_boundary = retry_anchor
        trim_boundary = retry_anchor

    if not _snapshot_matches_prefix(
        active_case.snapshot,
        case_plan,
        reusable_boundary,
    ):
        return _restart_develop_state(
            path,
            case_id=active_case.case_id,
            observer=observer,
        )

    active_case.paused_step = replace(
        active_case.paused_step,
        node_id=paused_node.node_id,
        label=paused_node.label,
        node_index=paused_index,
    )
    active_case.stop_after_index = paused_index
    if trim_boundary is not None:
        active_case.snapshot = _trim_snapshot_to_prefix(
            active_case.snapshot,
            case_plan,
            trim_boundary,
        )
        active_case.replay_from_index = trim_boundary
        active_case.dirty_node_id = None

    state.plan_signature = new_signature
    state.selected_cases = expected_cases
    save_execution_state(path, state)
    return _DevelopStateRefresh()


def _execute_plan(
    journey_fn: JourneyEntrypoint,
    *,
    plan: JourneyPlan,
    step: str | None = None,
    develop_step: str | None = None,
    pause_action: str | None = None,
    state: str | Path | None = None,
    observer: _ExecutionObserver | None = None,
) -> ExecutionReport | _PausedExecution:
    target_step = develop_step if develop_step is not None else step
    selected_cases = _select_cases(plan, target_step)
    validation = validate_journey(journey_fn)
    execution_observer = observer or _ExecutionObserver()
    execution_observer.on_journey_start(plan=plan, selected_cases=selected_cases)
    rehydration_enabled = _needs_rehydration(
        selected_cases,
        step=step,
        develop_step=develop_step,
        state=state,
    )
    state_path = Path(state) if state is not None else None
    develop_refresh = _refresh_develop_state_for_plan(
        state_path,
        journey_plan=plan,
        step=step,
        develop_step=develop_step,
        selected_cases=selected_cases,
        pause_action=pause_action,
        observer=execution_observer,
    )
    effective_pause_action = (
        None if develop_refresh.restarted_case_id is not None else pause_action
    )
    state_controller = _StateController(
        state_path,
        journey_plan=plan,
        step=step,
        develop_step=develop_step,
        selected_cases=selected_cases,
        allow_stale_develop_pause=(
            develop_step is not None and effective_pause_action is None
        ),
    )

    case_reports: list[CaseExecutionReport] = state_controller.completed_case_reports

    try:
        start_index = state_controller.current_case_index

        for case_index in range(start_index, len(selected_cases)):
            selected_case = selected_cases[case_index]
            replay_anchor = _replay_anchor_for(
                selected_case.case_plan,
                selected_case.stop_after_index,
            )
            restored_state = state_controller.active_case_for(
                case_index=case_index,
                case_id=selected_case.case_plan.case_id,
            )
            _, checkpoint_keys, _ = _case_rehydration_maps(selected_case.case_plan)
            start_checkpoint_name = _start_checkpoint_name_for(selected_case.case_plan)
            start_checkpoint_key = (
                checkpoint_keys.get(start_checkpoint_name)
                if start_checkpoint_name is not None
                else None
            )
            checkpoint_seed = (
                state_controller.checkpoint_snapshot_for(start_checkpoint_key)
                if rehydration_enabled
                and restored_state is None
                and selected_case.stop_after_index is None
                and start_checkpoint_key is not None
                else None
            )
            successful_bindings = (
                state_controller.successful_step_bindings()
                if checkpoint_seed is not None
                else None
            )
            run_session = _RunSession(
                journey_plan=plan,
                case_plan=selected_case.case_plan,
                validation=validation,
                stop_after_index=selected_case.stop_after_index,
                develop_step_enabled=develop_step is not None,
                rehydration_enabled=rehydration_enabled,
                state_controller=state_controller,
                restored_state=restored_state,
                checkpoint_seed=checkpoint_seed,
                checkpoint_restore_name=(
                    start_checkpoint_name if checkpoint_seed is not None else None
                ),
                successful_bindings=successful_bindings,
                observer=execution_observer,
            )
            if restored_state is None:
                state_controller.begin_case(
                    case_index=case_index,
                    snapshot=run_session.snapshot_state(),
                )

            if restored_state is None:
                execution_observer.on_case_start(
                    case_plan=selected_case.case_plan,
                    stop_after_index=run_session.stop_after_index,
                    replay_anchor=replay_anchor,
                )
            elif pause_action is None:
                execution_observer.on_case_resume(
                    case_plan=selected_case.case_plan,
                    stop_after_index=run_session.stop_after_index,
                    replay_anchor=replay_anchor,
                    replay_from_index=restored_state.replay_from_index,
                )

            if develop_step is not None:
                if run_session.paused_step is not None and effective_pause_action is None:
                    return _PausedExecution(run_session.paused_step)
                run_session.apply_pause_action(effective_pause_action)

            case_started_at = time.perf_counter()
            stopped_label: str | None = None

            while True:
                run_session.begin_attempt()
                try:
                    with use_session(run_session):
                        journey_fn()
                except _RetryRequested as retry_request:
                    if retry_request.sleep_for > 0:
                        time.sleep(retry_request.sleep_for)
                    continue
                except _PauseRequested as pause_request:
                    return _PausedExecution(pause_request.paused_step)
                except _StopCase:
                    stopped_label = step
                if (
                    run_session.cursor < len(selected_case.case_plan.nodes)
                    and run_session.stop_after_index is None
                ):
                    raise InvalidBranchUsageError(
                        "The journey finished before it reached every step in the compiled plan.",
                        hint="Check for conditional logic that exits early or skips step() calls during execution.",
                    )
                if (
                    run_session.stop_after_index is not None
                    and run_session.cursor <= run_session.stop_after_index
                ):
                    raise InvalidBranchUsageError(
                        "The journey finished before it reached the targeted step label.",
                        hint="Check that the step label exists on the path you selected.",
                    )
                break

            report = CaseExecutionReport(
                case_id=selected_case.case_plan.case_id,
                branch_env=dict(selected_case.case_plan.branch_env),
                records=list(run_session.records),
                completed=True,
                stopped_at_label=stopped_label,
                replay_anchor=replay_anchor if step is not None else None,
            )
            case_reports.append(report)
            state_controller.complete_case(report)
            execution_observer.on_case_complete(
                case_plan=selected_case.case_plan,
                report=report,
                duration_seconds=time.perf_counter() - case_started_at,
            )

        result = ExecutionReport(
            journey_id=plan.journey_id,
            function_ref=plan.function_ref,
            case_reports=case_reports,
        )
        execution_observer.on_journey_complete(report=result)
    except KeyboardInterrupt:
        raise
    except Exception:
        state_controller.clear()
        raise
    finally:
        state_controller.cleanup()

    return result


def execute(
    journey_fn: JourneyEntrypoint,
    *,
    step: str | None = None,
    state: str | Path | None = None,
) -> ExecutionReport:
    """Compile a journey and execute full cases or one targeted step flow."""

    plan = compile_journey(journey_fn)
    return _execute_plan(journey_fn, plan=plan, step=step, state=state)
