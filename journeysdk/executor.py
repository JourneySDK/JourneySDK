"""Execution runtime for compiled journey plans."""

from __future__ import annotations

import hashlib
import inspect
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any, NoReturn, ParamSpec, Protocol, TypeVar, cast
from uuid import uuid4

from ._branch_handle import BranchHandle
from .errors import (
    AmbiguousStepSelectionError,
    CallableExecutionError,
    ExecutionStateSerializationError,
    ExecutionStateMismatchError,
    InvalidBranchUsageError,
    StepNotFoundError,
)
from .logger import capture_log_records, get_logger, pretty_line, pretty_row
from .models import (
    BranchMarkerNode,
    CaseExecutionReport,
    CasePlan,
    ExecutionReport,
    JourneyPlan,
    NodeExecutionStatus,
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
    DEFAULT_STATE_DIR,
    ExecutionStateStorage,
    ExecutionStateEnvelope,
    PausedStepState,
    RuntimeSnapshotState,
    SelectedCaseState,
    StateFingerprint,
    StateValidityEnvelope,
    StepBindingState,
    artifact_root_for_state,
    default_execution_state_path,
    delete_artifact_root,
    delete_execution_state,
    load_execution_state,
    prepare_execution_state_storage,
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
from .validator import (
    BranchConditionSpec,
    JourneyValidation,
    resolve_branch_call_site,
    validate_journey,
)


@dataclass
class _StopCase(Exception):
    pass


@dataclass
class _RetryRequested(Exception):
    sleep_for: float


@dataclass
class _PauseRequested(Exception):
    paused_step: PausedStepState
    pending_exit_objects: tuple["_StepExitObject", ...] = ()


class _StepExitObject(Protocol):
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> object:
        ...


class _CaseExitObject(Protocol):
    def __case_exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> object:
        ...


_STEP_LIFECYCLE_INITIALIZATION = "initialization"
_STEP_LIFECYCLE_EXECUTION = "execution"
_STEP_LIFECYCLE_STORAGE = "storage"
_STEP_LIFECYCLE_PRE_EXIT = "pre-exit"
_STEP_LIFECYCLE_EXIT = "exit"
_STEP_LIFECYCLE_POST_EXIT = "post-exit"


class _StepInterruptController(Protocol):
    def on_step_lifecycle_phase(self, phase: str | None) -> None:
        ...

    def is_step_interrupt_pending(self) -> bool:
        ...

    def is_step_forced_interrupt_requested(self) -> bool:
        ...

    def raise_if_interrupted_after_step(self) -> None:
        ...


_STEP_INTERRUPT_CONTROLLER: ContextVar[_StepInterruptController | None] = ContextVar(
    "journey_step_interrupt_controller",
    default=None,
)


@contextmanager
def _use_step_interrupt_controller(
    controller: _StepInterruptController,
) -> Iterator[None]:
    token = _STEP_INTERRUPT_CONTROLLER.set(controller)
    try:
        yield
    finally:
        _STEP_INTERRUPT_CONTROLLER.reset(token)


def _notify_step_lifecycle_phase(phase: str | None) -> None:
    controller = _STEP_INTERRUPT_CONTROLLER.get()
    if controller is not None:
        controller.on_step_lifecycle_phase(phase)


def _is_step_interrupt_pending() -> bool:
    controller = _STEP_INTERRUPT_CONTROLLER.get()
    return controller is not None and controller.is_step_interrupt_pending()


def _is_step_forced_interrupt_requested() -> bool:
    controller = _STEP_INTERRUPT_CONTROLLER.get()
    if controller is None:
        return False
    is_forced = getattr(controller, "is_step_forced_interrupt_requested", None)
    return callable(is_forced) and bool(is_forced())


def _register_forced_step_interrupt_callback(
    name: str,
    callback: Callable[[], None],
) -> Callable[[], None]:
    controller = _STEP_INTERRUPT_CONTROLLER.get()
    if controller is None:
        return _noop_forced_step_interrupt_callback_unregister
    register = getattr(controller, "register_forced_interrupt_callback", None)
    if not callable(register):
        return _noop_forced_step_interrupt_callback_unregister
    unregister = register(name, callback)
    if callable(unregister):
        return cast(Callable[[], None], unregister)
    return _noop_forced_step_interrupt_callback_unregister


def _noop_forced_step_interrupt_callback_unregister() -> None:
    return None


def _raise_if_interrupted_after_step() -> None:
    controller = _STEP_INTERRUPT_CONTROLLER.get()
    if controller is not None:
        controller.raise_if_interrupted_after_step()


def _step_exit_objects_from_value(value: Any) -> list[_StepExitObject]:
    return cast(
        list[_StepExitObject],
        _lifecycle_objects_from_value(value, method_name="__exit__"),
    )


def _case_exit_objects_from_value(value: Any) -> list[_CaseExitObject]:
    return cast(
        list[_CaseExitObject],
        _lifecycle_objects_from_value(value, method_name="__case_exit__"),
    )


def _lifecycle_objects_from_value(value: Any, *, method_name: str) -> list[Any]:
    objects: list[Any] = []
    seen_objects: set[int] = set()
    seen_containers: set[int] = set()

    def visit(item: Any) -> None:
        exit_method = getattr(item, method_name, None)
        if callable(exit_method):
            identity = id(item)
            if identity not in seen_objects:
                seen_objects.add(identity)
                objects.append(item)
            return

        if type(item) is tuple or type(item) is list:
            identity = id(item)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
            for child in item:
                visit(child)
            return

        if type(item) is dict:
            identity = id(item)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
            for key, child in item.items():
                visit(key)
                visit(child)

    visit(value)
    return objects


def _dedupe_step_exit_objects(
    *object_groups: tuple[_StepExitObject, ...],
) -> tuple[_StepExitObject, ...]:
    objects: list[_StepExitObject] = []
    seen: set[int] = set()
    for group in object_groups:
        for value in group:
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            objects.append(value)
    return tuple(objects)


@dataclass
class _PausedExecution:
    paused_step: PausedStepState
    _pending_exit_objects: tuple[_StepExitObject, ...] = ()
    _pending_case_exit_objects: tuple[_CaseExitObject, ...] = ()
    _pending_exits_closed: bool = field(default=False, init=False, repr=False)

    def close_pending_exits(self) -> None:
        if self._pending_exits_closed:
            return
        self._pending_exits_closed = True

        step_failures: list[BaseException] = []
        for value in reversed(self._pending_exit_objects):
            try:
                value.__exit__(None, None, None)
            except BaseException as cleanup_exc:  # pragma: no cover - exercised through callers
                step_failures.append(cleanup_exc)

        case_failures: list[BaseException] = []
        for value in reversed(self._pending_case_exit_objects):
            try:
                value.__case_exit__(None, None, None)
            except BaseException as cleanup_exc:  # pragma: no cover - exercised through callers
                case_failures.append(cleanup_exc)

        if step_failures and case_failures:
            raise RuntimeError(
                f"{_cleanup_failure_message(step_failures)}; "
                f"{_case_cleanup_failure_message(case_failures)}"
            )
        if step_failures:
            raise RuntimeError(_cleanup_failure_message(step_failures))
        if case_failures:
            raise RuntimeError(_case_cleanup_failure_message(case_failures))


@dataclass(frozen=True)
class _DevelopStateRefresh:
    restarted_case_id: str | None = None


@dataclass(frozen=True)
class _StateInvalidationReason:
    key: str
    expected: str | None
    actual: str | None


@dataclass(frozen=True)
class _SelectedCase:
    case_plan: CasePlan
    stop_after_index: int | None


@dataclass(frozen=True)
class _ReplayBoundary:
    start_index: int
    preserve_retry_for: str | None = None


@dataclass(frozen=True)
class _ReplayPolicy:
    result_indices: frozenset[int]
    input_indices: frozenset[int]
    boundary_starts_after_step: dict[int, int]


@dataclass(frozen=True)
class _BrowserRecordingContext:
    root: Path
    run_id: str
    sequence: int
    context_index: int
    key: str
    stem: str
    journey_id: str
    function_ref: str
    case_id: str
    branch_env: dict[str, str]
    step_id: str
    step_label: str | None
    step_name: str
    node_index: int
    attempt: int


@dataclass(frozen=True)
class _LogArtifactContext:
    root: Path
    run_id: str
    sequence: int
    key: str
    stem: str
    path: Path
    manifest_path: Path
    journey_id: str
    function_ref: str
    case_id: str
    branch_env: dict[str, str]
    step_id: str
    step_label: str | None
    step_name: str
    node_index: int
    attempt: int
    kind: str
    touchpoint: str
    source: str
    suffix: str
    content_type: str
    recording_key: str | None = None


_RECORDING_SLUG_RE = re.compile(r"[^A-Za-z0-9_.+-]+")


def _recording_slug(value: object, *, fallback: str) -> str:
    slug = _RECORDING_SLUG_RE.sub("-", str(value)).strip("-._")
    return (slug or fallback)[:96]


class _BrowserRecordingController:
    def __init__(
        self,
        *,
        enabled: bool,
        root: Path,
        clean_existing: bool,
        run_id: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.root = root
        self.run_id = run_id or uuid4().hex[:12]
        self._sequence = 0
        if enabled and clean_existing:
            _clean_log_root(root)

    def allocate(
        self,
        *,
        journey_plan: JourneyPlan,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        context_index: int,
    ) -> _BrowserRecordingContext | None:
        if not self.enabled:
            return None
        self._sequence += 1
        sequence = self._sequence
        step_name = node.label or node.node_id
        key = (
            f"{sequence:04d}-"
            f"{_recording_slug(case_plan.case_id, fallback='case')}-"
            f"{_recording_slug(step_name, fallback='step')}-"
            f"attempt-{attempt}-"
            f"context-{context_index}-"
            f"run-{self.run_id}"
        )
        return _BrowserRecordingContext(
            root=self.root,
            run_id=self.run_id,
            sequence=sequence,
            context_index=context_index,
            key=key,
            stem=key,
            journey_id=journey_plan.journey_id,
            function_ref=journey_plan.function_ref,
            case_id=case_plan.case_id,
            branch_env=dict(case_plan.branch_env),
            step_id=node.node_id,
            step_label=node.label,
            step_name=step_name,
            node_index=node_index,
            attempt=attempt,
        )


class _RunArtifactController:
    def __init__(
        self,
        *,
        enabled: bool,
        root: Path,
        clean_existing: bool,
        run_id: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.root = root
        self.run_id = run_id or uuid4().hex[:12]
        self._sequence = 0
        self._journey_log_path: Path | None = None
        self._journey_log_manifest_path: Path | None = None
        self._journey_log_metadata: dict[str, object] | None = None
        if enabled and clean_existing:
            _clean_log_root(root)

    def start_journey_log(self, *, plan: JourneyPlan) -> None:
        if not self.enabled:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        sequence = self._sequence
        stem = f"{sequence:04d}-journey-run-{self.run_id}"
        self._journey_log_path = self.root / f"{stem}.jsonl"
        self._journey_log_manifest_path = self.root / f"{stem}.manifest.json"
        self._journey_log_metadata = {
            "format": "journey.log_artifact",
            "version": 1,
            "kind": "journey_log",
            "touchpoint": "journey",
            "source": "journey",
            "content_type": "application/jsonl",
            "run_id": self.run_id,
            "sequence": sequence,
            "artifact_key": stem,
            "journey_id": plan.journey_id,
            "function_ref": plan.function_ref,
            "case_id": None,
            "branch_env": {},
            "step_id": None,
            "step_label": None,
            "step_name": None,
            "node_index": None,
            "attempt": None,
            "path": str(self._journey_log_path),
            "started_at": _utc_timestamp(),
            "stopped_at": None,
            "line_count": 0,
            "byte_count": 0,
        }
        self._write_journey_log_manifest()

    def write_journey_record(self, record: dict[str, object]) -> None:
        if not self.enabled or self._journey_log_path is None:
            return
        self._journey_log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, default=str, separators=(",", ":")) + "\n"
        with self._journey_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        metadata = self._journey_log_metadata
        if metadata is not None:
            metadata["line_count"] = int(metadata.get("line_count") or 0) + 1
            metadata["byte_count"] = int(metadata.get("byte_count") or 0) + len(
                line.encode("utf-8")
            )

    def finish_journey_log(self) -> None:
        if not self.enabled or self._journey_log_metadata is None:
            return
        self._journey_log_metadata["stopped_at"] = _utc_timestamp()
        self._write_journey_log_manifest()

    def allocate(
        self,
        *,
        journey_plan: JourneyPlan,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        kind: str,
        touchpoint: str,
        source: str,
        suffix: str,
        content_type: str,
        recording_key: str | None = None,
    ) -> _LogArtifactContext | None:
        if not self.enabled:
            return None
        self._sequence += 1
        sequence = self._sequence
        step_name = node.label or node.node_id
        key = (
            f"{sequence:04d}-"
            f"{_recording_slug(case_plan.case_id, fallback='case')}-"
            f"{_recording_slug(step_name, fallback='step')}-"
            f"{_recording_slug(touchpoint, fallback='touchpoint')}-"
            f"{_recording_slug(source, fallback='source')}-"
            f"attempt-{attempt}-"
            f"run-{self.run_id}"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        return _LogArtifactContext(
            root=self.root,
            run_id=self.run_id,
            sequence=sequence,
            key=key,
            stem=key,
            path=self.root / f"{key}{suffix}",
            manifest_path=self.root / f"{key}.manifest.json",
            journey_id=journey_plan.journey_id,
            function_ref=journey_plan.function_ref,
            case_id=case_plan.case_id,
            branch_env=dict(case_plan.branch_env),
            step_id=node.node_id,
            step_label=node.label,
            step_name=step_name,
            node_index=node_index,
            attempt=attempt,
            kind=kind,
            touchpoint=touchpoint,
            source=source,
            suffix=suffix,
            content_type=content_type,
            recording_key=recording_key,
        )

    def _write_journey_log_manifest(self) -> None:
        if (
            self._journey_log_manifest_path is None
            or self._journey_log_metadata is None
        ):
            return
        self._journey_log_manifest_path.write_text(
            json.dumps(self._journey_log_metadata, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_log_root(root: Path) -> None:
    if root.is_symlink() or root.is_file():
        root.unlink(missing_ok=True)
        return
    shutil.rmtree(root, ignore_errors=True)


@dataclass(frozen=True)
class _RuntimeStepAnchor:
    node_ids: frozenset[str]


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

    def on_step_replay(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        record: NodeExecutionRecord,
    ) -> None:
        return

    def on_branch_replay(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
        record: NodeExecutionRecord,
    ) -> None:
        return

    def on_case_replay(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
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

    def on_state_validity(
        self,
        *,
        boundary_id: str,
        status: str,
        reason: str | None,
        expected: str | None,
        actual: str | None,
        action: str,
    ) -> None:
        return


def _format_duration(seconds: float) -> str:
    return f"{seconds:.3f}s"


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _format_branch_env(branch_env: dict[str, str]) -> str:
    entries = ", ".join(f"{key}={value}" for key, value in branch_env.items())
    return "{" + entries + "}"


def _step_name(node: StepNode) -> str:
    return node.label or node.node_id


def _step_detail(action: str, *, attempt: int, duration: str | None = None) -> str:
    parts = [action, f"attempt={attempt}"]
    if duration is not None:
        parts.append(f"duration={duration}")
    return " ".join(parts)


def _step_problem(
    step: str,
    action: str,
    *,
    duration: str | None,
    error: str | None,
    fallback: str,
) -> str:
    if duration is None:
        return f"{step} {fallback}"
    detail = f"{step} {action} after {duration}"
    if error is not None:
        detail += f" ({error})"
    return detail


class _LoggingExecutionObserver(_ExecutionObserver):
    def __init__(self) -> None:
        self._logger = get_logger("executor")

    def on_journey_start(
        self,
        *,
        plan: JourneyPlan,
        selected_cases: list[_SelectedCase],
    ) -> None:
        self._logger.info(
            "journey_start",
            "starting journey execution",
            pretty=pretty_line(f"  {plan.journey_id}", style="context"),
            journey=plan.journey_id,
            function_ref=plan.function_ref,
            cases=len(selected_cases),
        )

    def on_case_start(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
    ) -> None:
        self._logger.info(
            "case_start",
            f"- {case_plan.case_id} start branches={_format_branch_env(case_plan.branch_env)}",
            pretty=pretty_line(
                f"    {case_plan.case_id}"
                + (
                    f"  branches={_format_branch_env(case_plan.branch_env)}"
                    if case_plan.branch_env
                    else ""
                ),
                style="context",
            ),
            case=case_plan.case_id,
            branches=_format_branch_env(case_plan.branch_env),
            replay_anchor=replay_anchor,
            stop_after_index=stop_after_index,
        )

    def on_case_resume(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
        replay_from_index: int,
    ) -> None:
        self._logger.info(
            "case_resume",
            f"- {case_plan.case_id} resume branches={_format_branch_env(case_plan.branch_env)}",
            pretty=pretty_line(
                f"    {case_plan.case_id} resume"
                + (
                    f"  branches={_format_branch_env(case_plan.branch_env)}"
                    if case_plan.branch_env
                    else ""
                ),
                style="context",
            ),
            case=case_plan.case_id,
            branches=_format_branch_env(case_plan.branch_env),
            replay_anchor=replay_anchor,
            replay_from_index=replay_from_index,
            stop_after_index=stop_after_index,
        )

    def on_step_start(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
    ) -> None:
        self._logger.info(
            "step_start",
            f"  step {_step_name(node)} attempt={attempt} start status=executed",
            pretty=pretty_row(
                _step_name(node),
                _step_detail("start executed", attempt=attempt),
                indent=6,
                label_width=29,
                style="context",
            ),
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            status="executed",
        )

    def on_branch(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
    ) -> None:
        self._logger.info(
            "branch_select",
            f"  branch {node.group_id}={node.active_key} status=executed",
            pretty=pretty_row(
                f"branch {node.group_id}",
                node.active_key,
                indent=6,
                label_width=29,
                style="context",
            ),
            case=case_plan.case_id,
            branch_group=node.group_id,
            branch=node.active_key,
            node_index=node_index,
            status="executed",
        )

    def on_step_replay(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        record: NodeExecutionRecord,
    ) -> None:
        self._logger.info(
            "step_replay",
            f"  step {_step_name(node)} replayed",
            pretty=pretty_row(
                _step_name(node),
                "replayed",
                indent=6,
                label_width=29,
                style="context",
            ),
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            status=record.status,
        )

    def on_branch_replay(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
        record: NodeExecutionRecord,
    ) -> None:
        self._logger.info(
            "branch_replay",
            f"  branch {node.group_id}={node.active_key} replayed",
            pretty=pretty_row(
                f"branch {node.group_id}",
                f"replayed {node.active_key}",
                indent=6,
                label_width=29,
                style="context",
            ),
            case=case_plan.case_id,
            branch_group=node.group_id,
            branch=node.active_key,
            node_index=node_index,
            status=record.status,
        )

    def on_case_replay(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
    ) -> None:
        self._logger.info(
            "case_replay",
            f"- {report.case_id} replay branches={_format_branch_env(case_plan.branch_env)}",
            pretty=pretty_line(
                f"    {report.case_id} replay"
                + (
                    f"  branches={_format_branch_env(case_plan.branch_env)}"
                    if case_plan.branch_env
                    else ""
                ),
                style="context",
            ),
            case=report.case_id,
            branches=_format_branch_env(case_plan.branch_env),
            steps=sum(1 for record in report.records if record.node_type == "StepNode"),
            status="replayed",
        )

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
        self._logger.warning(
            "step_retry",
            (
                f"  step {_step_name(node)} attempt={attempt} retry "
                f"duration={_format_duration(duration_seconds)} "
                f"delay={_format_duration(delay_seconds)} "
                f"remaining={remaining_retries} "
                f"error={_format_exception(error)}"
            ),
            pretty=_step_problem(
                _step_name(node),
                "retry",
                duration=_format_duration(duration_seconds),
                error=_format_exception(error),
                fallback="retrying",
            ),
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            delay=_format_duration(delay_seconds),
            remaining=remaining_retries,
            error=_format_exception(error),
        )

    def on_step_success(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
    ) -> None:
        self._logger.info(
            "step_success",
            (
                f"  step {_step_name(node)} attempt={attempt} executed "
                f"duration={_format_duration(duration_seconds)}"
            ),
            pretty=pretty_row(
                _step_name(node),
                _step_detail(
                    "executed",
                    attempt=attempt,
                    duration=_format_duration(duration_seconds),
                ),
                indent=6,
                label_width=29,
                style="success",
            ),
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            status="executed",
        )

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
        self._logger.error(
            "step_failure",
            (
                f"  step {_step_name(node)} attempt={attempt} failed "
                f"duration={_format_duration(duration_seconds)} "
                f"error={_format_exception(error)}"
            ),
            pretty=_step_problem(
                _step_name(node),
                "failed",
                duration=_format_duration(duration_seconds),
                error=_format_exception(error),
                fallback="failed",
            ),
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            error=_format_exception(error),
            status="failed",
        )

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
        self._logger.warning(
            "step_interrupted",
            (
                f"  step {_step_name(node)} attempt={attempt} interrupted "
                f"duration={_format_duration(duration_seconds)} "
                f"error={_format_exception(error)}"
            ),
            pretty=_step_problem(
                _step_name(node),
                "interrupted",
                duration=_format_duration(duration_seconds),
                error=_format_exception(error),
                fallback="interrupted",
            ),
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            error=_format_exception(error),
            status="failed",
        )

    def on_case_complete(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
        duration_seconds: float,
    ) -> None:
        parts = [
            f"- {report.case_id}",
            "ok",
            f"steps={sum(1 for record in report.records if record.node_type == 'StepNode')}",
            f"duration={_format_duration(duration_seconds)}",
        ]
        if report.stopped_at_label is not None:
            parts.append(f"stopped_at={report.stopped_at_label}")
        if report.replay_anchor is not None:
            parts.append(f"replay_anchor={report.replay_anchor}")
        self._logger.info(
            "case_complete",
            " ".join(parts),
            pretty=pretty_line(
                f"    {report.case_id} done "
                f"steps={sum(1 for record in report.records if record.node_type == 'StepNode')} "
                f"duration={_format_duration(duration_seconds)}"
                + (
                    f" stopped_at={report.stopped_at_label}"
                    if report.stopped_at_label is not None
                    else ""
                )
                + (
                    f" replay_anchor={report.replay_anchor}"
                    if report.replay_anchor is not None
                    else ""
                ),
                style="success",
            ),
            case=report.case_id,
            steps=sum(1 for record in report.records if record.node_type == "StepNode"),
            duration=_format_duration(duration_seconds),
            stopped_at=report.stopped_at_label,
            replay_anchor=report.replay_anchor,
        )

    def on_journey_complete(self, *, report: ExecutionReport) -> None:
        self._logger.info(
            "journey_complete",
            "journey execution completed",
            pretty=False,
            journey=report.journey_id,
            cases=len(report.case_reports),
        )

    def on_develop_state_restart(self, *, case_id: str) -> None:
        self._logger.warning(
            "develop_state_restart",
            "Already-run journey code changed before the paused step; "
            f"restarting {case_id} from the beginning.",
            pretty=(
                "Already-run journey code changed before the paused step; "
                f"restarting {case_id} from the beginning."
            ),
            case=case_id,
        )

    def on_state_validity(
        self,
        *,
        boundary_id: str,
        status: str,
        reason: str | None,
        expected: str | None,
        actual: str | None,
        action: str,
    ) -> None:
        if status == "fresh":
            pretty = pretty_line("State: fresh run", indent=2, style="context")
            message = "State: fresh run"
        elif status == "replayed":
            pretty = pretty_line(
                f"State: reused boundary {boundary_id}", indent=2, style="context"
            )
            message = f"State: reused boundary {boundary_id}"
        else:
            reason_text = reason or "state changed"
            pretty = pretty_line(
                f"State: invalidated boundary {boundary_id}; "
                f"Reason: {reason_text}; Action: {action}",
                indent=2,
                style="warning",
            )
            message = (
                f"State: invalidated boundary {boundary_id}; "
                f"Reason: {reason_text}; Action: {action}"
            )

        self._logger.info(
            "state_validity",
            message,
            pretty=pretty,
            boundary_id=boundary_id,
            status=status,
            reason=reason,
            expected=expected,
            actual=actual,
            action=action,
        )


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


def _record_with_status(
    record: NodeExecutionRecord,
    status: NodeExecutionStatus,
) -> NodeExecutionRecord:
    return NodeExecutionRecord(
        node_id=record.node_id,
        node_type=record.node_type,
        label=record.label,
        status=status,
        result=record.result,
        error=record.error,
    )


def _replayed_record(record: NodeExecutionRecord) -> NodeExecutionRecord:
    if record.status == "failed":
        return record
    return _record_with_status(record, "replayed")


def _replayed_records(records: list[NodeExecutionRecord]) -> list[NodeExecutionRecord]:
    return [_replayed_record(record) for record in records]


def _replayed_case_report(report: CaseExecutionReport) -> CaseExecutionReport:
    return CaseExecutionReport(
        case_id=report.case_id,
        branch_env=dict(report.branch_env),
        records=_replayed_records(report.records),
        completed=report.completed,
        stopped_at_label=report.stopped_at_label,
        replay_anchor=report.replay_anchor,
    )


def _state_record(record: NodeExecutionRecord) -> NodeExecutionRecord:
    result = None if record.node_type == "StepNode" else record.result
    return NodeExecutionRecord(
        node_id=record.node_id,
        node_type=record.node_type,
        label=record.label,
        status=record.status,
        result=result,
        error=record.error,
    )


def _state_records(records: list[NodeExecutionRecord]) -> list[NodeExecutionRecord]:
    return [_state_record(record) for record in records]


def _state_report(report: CaseExecutionReport) -> CaseExecutionReport:
    return CaseExecutionReport(
        case_id=report.case_id,
        branch_env=dict(report.branch_env),
        records=_state_records(report.records),
        completed=report.completed,
        stopped_at_label=report.stopped_at_label,
        replay_anchor=report.replay_anchor,
    )


def _execution_state_is_complete(state: ExecutionStateEnvelope) -> bool:
    return (
        state.active_case is None
        and state.current_case_index >= len(state.selected_cases)
    )


def _rehydration_key(*, kind: str, identifier: str, lineage: tuple[str, ...]) -> str:
    if not lineage:
        return f"{kind}:{identifier}"
    return f"{kind}:{identifier}|{'|'.join(lineage)}"


def _case_rehydration_maps(
    case_plan: CasePlan,
) -> dict[str, str]:
    step_keys: dict[str, str] = {}
    lineage: tuple[str, ...] = ()

    for node in case_plan.nodes:
        if isinstance(node, StepNode):
            step_keys[node.node_id] = _rehydration_key(
                kind="step",
                identifier=node.node_id,
                lineage=lineage,
            )
            continue
        if isinstance(node, BranchMarkerNode):
            lineage = lineage + (f"{node.group_id}={node.active_key}",)

    return step_keys


def _branch_anchor_step_ids_for(*, plan: JourneyPlan) -> set[str]:
    return {
        node.start_from
        for case_plan in plan.case_plans
        for node in case_plan.nodes
        if isinstance(node, BranchMarkerNode) and node.start_from is not None
    }


def _case_replay_policy(case_plan: CasePlan) -> _ReplayPolicy:
    step_index_by_id = {
        node.node_id: index
        for index, node in enumerate(case_plan.nodes)
        if isinstance(node, StepNode)
    }
    result_indices: set[int] = set()
    input_indices: set[int] = set()
    boundary_starts_after_step: dict[int, int] = {}

    def mark_boundary(completed_index: int, start_index: int) -> None:
        previous = boundary_starts_after_step.get(completed_index)
        if previous is None or start_index > previous:
            boundary_starts_after_step[completed_index] = start_index

    for index, node in enumerate(case_plan.nodes):
        if isinstance(node, BranchMarkerNode) and node.start_from is not None:
            anchor_index = step_index_by_id.get(node.start_from)
            if anchor_index is None:
                continue
            boundary_index = anchor_index + 1
            mark_boundary(anchor_index, boundary_index)
            for prefix_index in range(boundary_index):
                if isinstance(case_plan.nodes[prefix_index], StepNode):
                    result_indices.add(prefix_index)
            continue

        if isinstance(node, StepNode) and node.retry is not None:
            anchor_index = step_index_by_id.get(node.retry.from_node_id)
            if anchor_index is None or anchor_index > index:
                continue
            mark_boundary(anchor_index, anchor_index)
            for prefix_index in range(anchor_index):
                if isinstance(case_plan.nodes[prefix_index], StepNode):
                    result_indices.add(prefix_index)
            for replay_index in range(anchor_index, index + 1):
                if isinstance(case_plan.nodes[replay_index], StepNode):
                    input_indices.add(replay_index)

    return _ReplayPolicy(
        result_indices=frozenset(result_indices),
        input_indices=frozenset(input_indices),
        boundary_starts_after_step=boundary_starts_after_step,
    )


def _callable_execution_error_for_step(
    node: StepNode,
    exc: Exception,
    *,
    retry_attempts_exhausted: bool = True,
) -> CallableExecutionError:
    details = str(exc)
    notes = getattr(exc, "__notes__", None)
    if notes:
        details = f"{details} {' '.join(str(note) for note in notes)}"
    message = (
        f"Step '{node.label or node.node_id}' failed while it was running: "
        f"{type(exc).__name__}: {details}"
    )
    hint = _exception_hint(exc) or (
        "Inspect the step implementation or rerun after fixing the underlying failure."
    )
    if node.retry is not None and retry_attempts_exhausted:
        message = (
            f"Step '{node.label or node.node_id}' failed while it was running "
            f"and its retry attempts were exhausted: {type(exc).__name__}: {details}"
        )
        hint = _exception_hint(exc) or (
            "Inspect the step implementation, or increase step(..., retry=...) if "
            "the failure is expected to clear on its own."
        )
    return CallableExecutionError(message, hint=hint, step_label=node.label or node.node_id)


def _exception_hint(exc: Exception) -> str | None:
    hint = getattr(exc, "hint", None)
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    return None


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


def _case_cleanup_failure_message(failures: list[BaseException]) -> str:
    if len(failures) == 1:
        failure = failures[0]
        return (
            "Case-exit cleanup failed: "
            f"{type(failure).__name__}: {failure}"
        )
    joined = "; ".join(
        f"{type(failure).__name__}: {failure}"
        for failure in failures
    )
    return f"{len(failures)} case-exit cleanup objects failed: {joined}"


def _add_cleanup_failure_notes(
    exc: BaseException,
    failures: list[BaseException],
    *,
    case_exit: bool = False,
) -> None:
    if not failures:
        return
    add_note = getattr(exc, "add_note", None)
    message = (
        _case_cleanup_failure_message(failures)
        if case_exit
        else _cleanup_failure_message(failures)
    )
    if callable(add_note):
        add_note(message)


class _StateController:
    def __init__(
        self,
        storage: ExecutionStateStorage,
        *,
        journey_plan: JourneyPlan,
        step: str | None,
        develop_step: str | None,
        selected_cases: list[_SelectedCase],
        validity: StateValidityEnvelope,
        observer: _ExecutionObserver,
        reset_completed_state: bool = False,
    ) -> None:
        self.path = storage.display_path
        self._run_path = storage.run_path
        self._cleanup_root = storage.cleanup_root
        self.journey_plan = journey_plan
        self.step = step
        self.develop_step = develop_step
        self.selected_cases = list(selected_cases)
        self.validity = validity
        self._observer = observer
        self.artifact_root = storage.artifact_root
        self._artifact_root_is_temporary = storage.artifact_root_is_temporary
        self._persistent_artifact_root = storage.persistent_artifact_root
        self.plan_signature = _plan_signature(
            journey_plan,
            self.selected_cases,
            step,
            develop_step,
        )
        self._loaded_state_invalid_reason: _StateInvalidationReason | None = None

        loaded: ExecutionStateEnvelope | None = None
        if self._run_path is not None:
            loaded = load_execution_state(self._run_path)
        if (
            reset_completed_state
            and loaded is not None
            and _execution_state_is_complete(loaded)
        ):
            loaded = None

        if loaded is None:
            loaded = ExecutionStateEnvelope(
                version=STATE_FORMAT_VERSION,
                journey_id=journey_plan.journey_id,
                function_ref=journey_plan.function_ref,
                step=step,
                develop_step=develop_step,
                plan_signature=self.plan_signature,
                validity=self.validity,
                selected_cases=_selected_case_refs(self.selected_cases),
                current_case_index=0,
                completed_case_reports=[],
                active_case=None,
            )
            observer.on_state_validity(
                boundary_id="journey",
                status="fresh",
                reason=None,
                expected=None,
                actual=None,
                action="executing from the beginning",
            )
        else:
            self._validate_loaded_state(loaded)

        self._state = loaded
        self._state.validity = self.validity

    @property
    def completed_case_reports(self) -> list[CaseExecutionReport]:
        return [
            _replayed_case_report(report)
            for report in self._state.completed_case_reports
        ]

    @property
    def current_case_index(self) -> int:
        return self._state.current_case_index

    @property
    def updates_state_file(self) -> bool:
        return self._run_path is not None

    def binding_store_context(self, binding_key: str) -> JourneyStoreContext:
        return JourneyStoreContext(
            artifact_root=self._artifact_dir("binding", binding_key),
            boundary_kind="binding",
            boundary_id=binding_key,
            persistent_artifact_root=self._persistent_artifact_dir(
                "binding",
                binding_key,
            ),
        )

    def binding_restore_context(self, binding_key: str) -> JourneyRestoreContext:
        return JourneyRestoreContext(
            artifact_root=self._artifact_dir("binding", binding_key),
            boundary_kind="binding",
            boundary_id=binding_key,
            persistent_artifact_root=self._persistent_artifact_dir(
                "binding",
                binding_key,
            ),
        )

    def branch_anchor_store_context(
        self,
        *,
        anchor_key: str,
        binding_key: str,
    ) -> JourneyStoreContext:
        return JourneyStoreContext(
            artifact_root=self._artifact_dir("branch-anchor", anchor_key, binding_key),
            boundary_kind="branch-anchor",
            boundary_id=anchor_key,
            persistent_artifact_root=self._persistent_artifact_dir(
                "branch-anchor",
                anchor_key,
                binding_key,
            ),
        )

    def branch_anchor_restore_context(
        self,
        *,
        anchor_key: str,
        binding_key: str,
    ) -> JourneyRestoreContext:
        return JourneyRestoreContext(
            artifact_root=self._artifact_dir("branch-anchor", anchor_key, binding_key),
            boundary_kind="branch-anchor",
            boundary_id=anchor_key,
            persistent_artifact_root=self._persistent_artifact_dir(
                "branch-anchor",
                anchor_key,
                binding_key,
            ),
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
            persistent_artifact_root=self._persistent_artifact_dir(
                "active-state",
                case_id,
                binding_key,
            ),
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
            persistent_artifact_root=self._persistent_artifact_dir(
                "active-state",
                case_id,
                binding_key,
            ),
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
        if self._loaded_state_invalid_reason is not None:
            return self._active_case_after_invalidation(
                case_index=case_index,
                active_case=active_case,
                reason=self._loaded_state_invalid_reason,
            )
        self._observer.on_state_validity(
            boundary_id=case_id,
            status="replayed",
            reason=None,
            expected=None,
            actual=None,
            action=f"resuming from step index {active_case.replay_from_index}",
        )
        return active_case

    def branch_anchor_snapshot_for(
        self,
        anchor_key: str,
        *,
        case_plan: CasePlan,
    ) -> RuntimeSnapshotState | None:
        snapshot = self._state.branch_anchor_snapshots.get(anchor_key)
        if snapshot is None:
            return None
        if self._loaded_state_invalid_reason is not None:
            if not _reason_allows_prefix_reuse(self._loaded_state_invalid_reason):
                self._observer.on_state_validity(
                    boundary_id=anchor_key,
                    status="invalidated",
                    reason=self._loaded_state_invalid_reason.key,
                    expected=self._loaded_state_invalid_reason.expected,
                    actual=self._loaded_state_invalid_reason.actual,
                    action="rerunning from the case start",
                )
                return None
            boundary_index = _snapshot_boundary_index(snapshot)
            if not _snapshot_matches_prefix(snapshot, case_plan, boundary_index):
                self._observer.on_state_validity(
                    boundary_id=anchor_key,
                    status="invalidated",
                    reason=self._loaded_state_invalid_reason.key,
                    expected=self._loaded_state_invalid_reason.expected,
                    actual=self._loaded_state_invalid_reason.actual,
                    action="rerunning from the case start",
                )
                return None
        self._observer.on_state_validity(
            boundary_id=anchor_key,
            status="replayed",
            reason=None,
            expected=None,
            actual=None,
            action="using saved branch anchor",
        )
        return _copy_runtime_snapshot(snapshot)

    def _active_case_after_invalidation(
        self,
        *,
        case_index: int,
        active_case: ActiveCaseState,
        reason: _StateInvalidationReason,
    ) -> ActiveCaseState | None:
        if not _reason_allows_prefix_reuse(reason):
            self._invalidate_entire_state(
                self._state,
                boundary_id=active_case.case_id,
                reason=reason,
            )
            return None

        case_plan = self.selected_cases[case_index].case_plan
        boundary_index = _longest_matching_snapshot_prefix(
            active_case.snapshot,
            case_plan,
        )
        if boundary_index <= 0:
            self._invalidate_entire_state(
                self._state,
                boundary_id=active_case.case_id,
                reason=reason,
            )
            return None

        trimmed = replace(
            active_case,
            snapshot=_trim_snapshot_to_prefix(
                active_case.snapshot,
                case_plan,
                boundary_index,
            ),
            replay_from_index=boundary_index,
            dirty_node_id=None,
            paused_step=(
                active_case.paused_step
                if active_case.paused_step is not None
                and active_case.paused_step.node_index < boundary_index
                else None
            ),
        )
        self._state.active_case = trimmed
        self._state.plan_signature = self.plan_signature
        self._state.validity = self.validity
        self._write_state()
        self._observer.on_state_validity(
            boundary_id=active_case.case_id,
            status="invalidated",
            reason=reason.key,
            expected=reason.expected,
            actual=reason.actual,
            action=f"rerunning from step index {boundary_index}",
        )
        return trimmed

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

    def store_branch_anchor_snapshot(
        self,
        anchor_key: str,
        snapshot: RuntimeSnapshotState,
    ) -> None:
        self._state.branch_anchor_snapshots[anchor_key] = _copy_runtime_snapshot(snapshot)
        self._write_state()

    def complete_case(self, report: CaseExecutionReport) -> None:
        expected_case = self.selected_cases[self._state.current_case_index].case_plan
        if report.case_id != expected_case.case_id:
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' tried to complete case '{report.case_id}', "
                f"but this run expected '{expected_case.case_id}'."
            )
        self._state.completed_case_reports.append(_state_report(report))
        self._state.current_case_index += 1
        self._state.active_case = None
        self._write_state()

    def clear(self) -> None:
        if self._run_path is not None and self._run_path == self.path:
            delete_execution_state(self._run_path)
        delete_artifact_root(self.artifact_root)

    def cleanup(self) -> None:
        if self._cleanup_root is not None:
            delete_artifact_root(self._cleanup_root)
            return
        if self._artifact_root_is_temporary:
            delete_artifact_root(self.artifact_root)

    def _write_state(self) -> None:
        if self._run_path is None:
            return
        save_execution_state(self._run_path, self._state)

    def _invalidate_entire_state(
        self,
        state: ExecutionStateEnvelope,
        *,
        boundary_id: str,
        reason: _StateInvalidationReason,
    ) -> None:
        state.plan_signature = self.plan_signature
        state.validity = self.validity
        state.selected_cases = _selected_case_refs(self.selected_cases)
        state.current_case_index = 0
        state.completed_case_reports = []
        state.active_case = None
        state.branch_anchor_snapshots = {}
        self._observer.on_state_validity(
            boundary_id=boundary_id,
            status="invalidated",
            reason=reason.key,
            expected=reason.expected,
            actual=reason.actual,
            action="executing from the beginning",
        )

    def _validate_loaded_state(self, state: ExecutionStateEnvelope) -> None:
        expected_cases = _selected_case_refs(self.selected_cases)
        expected_anchor_keys = {
            step_keys[node.start_from]
            for case_plan in self.journey_plan.case_plans
            for step_keys in [_case_rehydration_maps(case_plan)]
            for node in case_plan.nodes
            if isinstance(node, BranchMarkerNode)
            and node.start_from is not None
            and node.start_from in step_keys
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
            hint = None
            if state.develop_step is not None and self.develop_step is None:
                hint = (
                    "Rerun the same --develop-step target to keep iterating, or use "
                    "`--no-state` for a fresh --step/full journey verification after a develop-step pause."
                )
            raise ExecutionStateMismatchError(
                f"The journey state file '{self.path}' was created for develop_step "
                f"{state.develop_step!r}, not {self.develop_step!r}.",
                hint=hint,
            )
        validity_reason = _validity_mismatch(state.validity, self.validity)
        if state.plan_signature != self.plan_signature and validity_reason is None:
            validity_reason = _StateInvalidationReason(
                key="plan_shape",
                expected=state.plan_signature,
                actual=self.plan_signature,
            )
        if validity_reason is not None:
            self._loaded_state_invalid_reason = validity_reason
        if state.selected_cases != expected_cases:
            self._invalidate_entire_state(
                state,
                boundary_id="journey",
                reason=_StateInvalidationReason(
                    key="selected_cases",
                    expected=_stable_json(state.selected_cases),
                    actual=_stable_json(expected_cases),
                ),
            )
            return
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

        for anchor_key, snapshot in state.branch_anchor_snapshots.items():
            if not isinstance(anchor_key, str) or anchor_key not in expected_anchor_keys:
                raise ExecutionStateMismatchError(
                    f"The journey state file '{self.path}' has invalid branch anchor snapshot data."
                )
            self._validate_runtime_snapshot(
                snapshot,
                label=f"branch anchor snapshot '{anchor_key}'",
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
            if self._loaded_state_invalid_reason is not None:
                self._invalidate_entire_state(
                    state,
                    boundary_id="journey",
                    reason=self._loaded_state_invalid_reason,
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

    def _persistent_artifact_dir(self, *parts: str) -> Path | None:
        if self._persistent_artifact_root is None:
            return None
        path = self._persistent_artifact_root
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
        branch_anchor_seed: RuntimeSnapshotState | None = None,
        branch_anchor_key: str | None = None,
        observer: _ExecutionObserver | None = None,
        prompt_memory_root: Path | None = None,
        prompt_memory_disabled: bool = False,
        prompt_memory_update_disabled: bool = False,
        browser_recording_controller: _BrowserRecordingController | None = None,
        log_artifact_controller: _RunArtifactController | None = None,
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
        self._branch_handle_group_ids: dict[tuple[tuple[int, int], ...], str] = {}
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
        self._state_controller = state_controller
        self._observer = observer or _ExecutionObserver()
        self._prompt_memory_root = prompt_memory_root
        self._prompt_memory_disabled = prompt_memory_disabled
        self._prompt_memory_update_disabled = prompt_memory_update_disabled
        self._browser_recording_controller = browser_recording_controller
        self._log_artifact_controller = log_artifact_controller
        self._browser_recording_context_counts: dict[tuple[str, int], int] = {}
        self._active_step_lifecycle: _StepLifecycle | None = None
        self._case_exit_objects: list[_CaseExitObject] = []
        self._case_exit_object_ids: set[int] = set()
        self._step_key_by_id = _case_rehydration_maps(case_plan)
        self._step_id_by_key = {
            binding_key: node_id
            for node_id, binding_key in self._step_key_by_id.items()
        }
        self._step_index_by_id = {
            node.node_id: index
            for index, node in enumerate(case_plan.nodes)
            if isinstance(node, StepNode)
        }
        self._step_index_by_key = {
            binding_key: self._step_index_by_id[node_id]
            for node_id, binding_key in self._step_key_by_id.items()
            if node_id in self._step_index_by_id
        }
        self._replay_policy = _case_replay_policy(case_plan)
        self._branch_anchor_step_ids = _branch_anchor_step_ids_for(plan=journey_plan)
        self._runtime_step_result_ids: dict[int, set[str]] = {}

        if branch_anchor_seed is not None:
            if branch_anchor_key is None:
                raise ExecutionStateMismatchError(
                    "Branch anchor replay state is missing its anchor key."
                )
            self._restore_branch_anchor_seed(
                branch_anchor_seed,
                anchor_key=branch_anchor_key,
            )
        if restored_state is not None:
            self._restore_state(restored_state)

    def _next_group_id(self) -> str:
        self._group_counter += 1
        return f"bg_{self._group_counter}"

    def _runtime_snapshot(self) -> RuntimeSnapshotState:
        if self._rehydration_enabled:
            step_bindings: dict[str, StepBindingState] = {}
            for key, binding in self._step_bindings.items():
                node_index = self._step_index_by_key.get(key)
                include_result = (
                    node_index is not None
                    and node_index < self.replay_from_index
                    and node_index in self._replay_policy.result_indices
                    and binding.has_result
                )
                include_inputs = (
                    node_index is not None
                    and node_index in self._replay_policy.input_indices
                    and self._binding_has_cached_or_stored_inputs(
                        binding_key=key,
                        binding=binding,
                    )
                )
                if not include_result and not include_inputs:
                    continue
                step_bindings[key] = self._freeze_binding(
                    key,
                    binding,
                    context=self._active_state_store_context(key),
                    description_prefix=f"active state for case '{self.case_plan.case_id}'",
                    include_inputs=include_inputs,
                    include_result=include_result,
                )
        else:
            step_bindings = {}
        return RuntimeSnapshotState(
            record_indices=list(self._record_indices),
            records=_state_records(self.records),
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
        if not self._state_controller.updates_state_file:
            return
        self._state_controller.update_active_case(self.snapshot_state())

    def _restore_branch_anchor_seed(
        self,
        snapshot: RuntimeSnapshotState,
        *,
        anchor_key: str,
    ) -> None:
        restored = _copy_runtime_snapshot(snapshot)
        self._record_indices = restored.record_indices
        self.records = _replayed_records(restored.records)
        for key, binding in restored.step_bindings.items():
            self._step_bindings[key] = binding
            self._step_binding_contexts[key] = self._branch_anchor_restore_context(
                binding_key=key,
                anchor_key=anchor_key,
            )
            self._step_input_cache.pop(key, None)
            self._step_result_cache.pop(key, None)
        self._retry_remaining = restored.retry_remaining
        self._step_attempts = restored.step_attempts
        self.replay_from_index = (max(self._record_indices) + 1) if self._record_indices else 0

    def _restore_state(self, restored_state: ActiveCaseState) -> None:
        if restored_state.case_id != self.case_plan.case_id:
            raise ExecutionStateMismatchError(
                f"The journey state points at case '{restored_state.case_id}', "
                f"not '{self.case_plan.case_id}'."
            )

        restored = _copy_runtime_snapshot(restored_state.snapshot)
        self._record_indices = restored.record_indices
        self.records = _replayed_records(restored.records)
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
        if self._dirty_node_id is None and self._paused_step is None:
            self._trim_from(self.replay_from_index, preserve_retry_for=None)

    def emit_replayed_records(self) -> None:
        _emit_replayed_records(
            self._observer,
            case_plan=self.case_plan,
            record_indices=self._record_indices,
            records=self.records,
        )

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
            if not paused_step.ok:
                step_name = paused_step.label or paused_step.node_id
                raise ExecutionStateMismatchError(
                    f"Cannot continue past failed develop step '{step_name}'.",
                    hint="Retry the failed develop step first, or delete the state file to start fresh.",
                )
            if self._can_restore_step_result_at(paused_step.node_index):
                self.replay_from_index = paused_step.node_index + 1
            else:
                self._apply_replay_boundary(
                    _ReplayBoundary(start_index=self.replay_from_index)
                )
            next_step_index = _next_step_index_after(
                self.case_plan,
                paused_step.node_index,
            )
            if (
                self.stop_after_index is None
                or self.stop_after_index <= paused_step.node_index
            ):
                self.stop_after_index = next_step_index
            self._paused_step = None
            self._persist_state()
            return

        if action == "retry":
            node = self.case_plan.nodes[paused_step.node_index]
            if not isinstance(node, StepNode):
                raise ExecutionStateMismatchError(
                    "Pause-on-step state points at a non-step node."
                )
            if node.retry is None:
                boundary = _ReplayBoundary(start_index=self.replay_from_index)
            else:
                boundary = self._replay_boundary_for_step(
                    node,
                    paused_step.node_index,
                    preserve_retry_for=None,
                )
            self._apply_replay_boundary(boundary)
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

        if node.retry is None:
            boundary = _ReplayBoundary(start_index=self.replay_from_index)
        else:
            boundary = self._replay_boundary_for_step(
                node,
                node_index,
                preserve_retry_for=node.node_id,
            )
        self._apply_replay_boundary(boundary)
        self._dirty_node_id = None
        self._persist_state()

    def _consume(self, expected_type: type[Any]) -> Any:
        if self.cursor >= len(self.case_plan.nodes):
            raise InvalidBranchUsageError(
                "The journey executed more steps than the compiled plan expected.",
                hint="Check for conditional logic or helper calls that add extra step() calls at runtime.",
            )
        node = self.case_plan.nodes[self.cursor]
        if not isinstance(node, expected_type):
            expected = expected_type.__name__
            actual = type(node).__name__
            raise InvalidBranchUsageError(
                f"The journey took a different path than the compiled plan at position {self.cursor + 1}: expected {expected}, got {actual}.",
                hint="Make sure the journey calls step() in the same structure each time it runs.",
            )
        self.cursor += 1
        return node

    def begin_attempt(self) -> None:
        self._journey_webhook_epoch = getattr(self, "_journey_webhook_epoch", 0) + 1
        self.cursor = 0
        self._group_counter = 0
        self._active_branch_chains.clear()
        self._branch_handle_group_ids.clear()
        self._resume_dirty_step()

    def _has_record_for(self, node_index: int) -> bool:
        return node_index in self._record_indices

    def _record(
        self,
        node_index: int,
        node: Any,
        status: NodeExecutionStatus,
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
                status=status,
                result=result,
                error=error,
            )
        )
        self._persist_state()
        return self.stop_after_index is not None and node_index == self.stop_after_index

    def _mark_replay_boundary_available_after(self, node_index: int) -> None:
        start_index = self._replay_policy.boundary_starts_after_step.get(node_index)
        if start_index is not None and start_index > self.replay_from_index:
            self.replay_from_index = start_index

    def _mark_same_step_retry_boundary_before_attempt(
        self,
        node: StepNode,
        node_index: int,
    ) -> None:
        if (
            node.retry is not None
            and node.retry.from_node_id == node.node_id
            and node_index > self.replay_from_index
        ):
            self.replay_from_index = node_index

    def _binding_has_cached_or_stored_inputs(
        self,
        *,
        binding_key: str,
        binding: StepBindingState,
    ) -> bool:
        if binding_key in self._step_input_cache:
            return True
        return bool(binding.args or binding.kwargs)

    def _binding_has_inputs_for_node(
        self,
        node: StepNode,
        binding: StepBindingState,
    ) -> bool:
        binding_key = self._step_key_by_id[node.node_id]
        if binding_key in self._step_input_cache:
            return True
        return len(binding.args) == len(node.args) and set(binding.kwargs) == set(node.kwargs)

    def _can_restore_step_result_at(self, node_index: int) -> bool:
        if not 0 <= node_index < len(self.case_plan.nodes):
            return False
        node = self.case_plan.nodes[node_index]
        if not isinstance(node, StepNode):
            return False
        binding_key = self._step_key_by_id[node.node_id]
        binding = self._step_bindings.get(binding_key)
        if binding is None or not binding.has_result:
            return False
        return binding_key in self._step_result_cache or binding.result is not None

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
            if index not in self._replay_policy.input_indices:
                self._step_input_cache.pop(binding_key, None)
            self._step_result_cache.pop(binding_key, None)
            if node.node_id != preserve_retry_for:
                self._retry_remaining.pop(node.node_id, None)

    def _replay_boundary_for_step(
        self,
        node: StepNode,
        node_index: int,
        *,
        preserve_retry_for: str | None,
    ) -> _ReplayBoundary:
        if node.retry is None:
            return _ReplayBoundary(
                start_index=node_index,
                preserve_retry_for=preserve_retry_for,
            )
        return _ReplayBoundary(
            start_index=self._retry_anchor_index(node, node_index),
            preserve_retry_for=preserve_retry_for,
        )

    def _apply_replay_boundary(self, boundary: _ReplayBoundary) -> None:
        self._trim_from(
            boundary.start_index,
            preserve_retry_for=boundary.preserve_retry_for,
        )
        self.replay_from_index = boundary.start_index

    def _retry_anchor_index(self, node: StepNode, node_index: int) -> int:
        if node.retry is None:
            raise InvalidBranchUsageError(
                "A retryable step was resumed without retry settings.",
                hint="Check the step(..., retry=..., retry_delay=..., retry_from=...) settings for that step.",
            )

        anchor_index: int | None = None
        if node.retry.from_node_id is not None:
            anchor_index = self._step_index_by_id.get(node.retry.from_node_id)

        if anchor_index is None:
            raise InvalidBranchUsageError(
                f"Retry anchor for step '{node.label or node.node_id}' is missing from the compiled journey path.",
                hint="Make sure retry_from=... points to an earlier step() result in the same journey path.",
            )
        if anchor_index > node_index:
            raise InvalidBranchUsageError(
                f"Retry anchor for step '{node.label or node.node_id}' appears after the step itself.",
                hint="Point retry_from=... to an earlier step() result.",
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
        boundary = self._replay_boundary_for_step(
            node,
            node_index,
            preserve_retry_for=node.node_id,
        )
        self._apply_replay_boundary(boundary)
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
        pending_exit_objects: tuple[_StepExitObject, ...] = (),
    ) -> None:
        paused_step = self._build_paused_step(
            node,
            node_index=node_index,
            attempt=attempt,
            ok=ok,
            error=error,
            failure=failure,
        )
        self._paused_step = paused_step
        self._dirty_node_id = None
        self._persist_state()
        raise _PauseRequested(paused_step, pending_exit_objects)

    def _build_paused_step(
        self,
        node: StepNode,
        *,
        node_index: int,
        attempt: int,
        ok: bool,
        error: str | None = None,
        failure: CallableExecutionError | None = None,
    ) -> PausedStepState:
        return PausedStepState(
            node_id=node.node_id,
            label=node.label,
            node_index=node_index,
            attempt=attempt,
            ok=ok,
            error=error,
            failure_message=failure.message if failure is not None else None,
            failure_hint=failure.hint if failure is not None else None,
        )

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
        return binding

    def _discard_step_result(self, node: StepNode) -> None:
        binding_key = self._step_key_by_id[node.node_id]
        binding = self._step_bindings.get(binding_key)
        if binding is not None:
            binding.has_result = False
            binding.result = None
        self._step_result_cache.pop(binding_key, None)

    def _remember_step_result(self, node: StepNode, result: Any) -> None:
        self._runtime_step_result_ids.setdefault(id(result), set()).add(node.node_id)
        self._register_case_exit_objects_from_value(result)

    def step_anchor_for_value(self, value: object) -> _RuntimeStepAnchor:
        node_ids = self._runtime_step_result_ids.get(id(value))
        if not node_ids:
            raise TypeError(
                "branch(start_from=...) accepts a value returned by an earlier step() call. Omit start_from to start from scratch."
            )
        return _RuntimeStepAnchor(frozenset(node_ids))

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
        should_pause_without_retry = (
            self._develop_step_enabled
            and self.stop_after_index is not None
            and node_index == self.stop_after_index
        )
        if should_pause_without_retry:
            self._retry_remaining.pop(node.node_id, None)
            self._dirty_node_id = None
            should_stop = self._record(
                node_index,
                node,
                status="failed",
                error=str(exc),
            )
            self._observer.on_step_failure(
                case_plan=self.case_plan,
                node=node,
                node_index=node_index,
                attempt=attempt,
                duration_seconds=duration_seconds,
                error=exc,
            )
            failure = _callable_execution_error_for_step(
                node,
                exc,
                retry_attempts_exhausted=False,
            )
            if should_stop:
                self._pause_after_step(
                    node,
                    node_index=node_index,
                    attempt=attempt,
                    ok=False,
                    error=str(exc),
                    failure=failure,
                )
            raise failure from exc

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
        should_stop = self._record(
            node_index,
            node,
            status="failed",
            error=str(exc),
        )
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
            cached_args, cached_kwargs = cached
            resolved_args = tuple(
                self._resolve_binding_value(template, value)
                for template, value in zip(node.args, cached_args)
            )
            resolved_kwargs = {
                key: self._resolve_binding_value(node.kwargs.get(key), value)
                for key, value in cached_kwargs.items()
            }
            return resolved_args, resolved_kwargs

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
        if binding_key in self._step_result_cache:
            return self._step_result_cache[binding_key]
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

    def _store_step_input_value(
        self,
        value: Any,
        *,
        template: Any,
        context: JourneyStoreContext,
        description: str,
    ) -> StoredValue:
        if isinstance(template, PlannedValue) and template.kind == "step":
            return StoredValue(kind="step-ref")
        return self._store_runtime_value(
            value,
            context=context,
            description=description,
        )

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
        include_inputs: bool,
        include_result: bool,
    ) -> StepBindingState:
        frozen = StepBindingState(
            args=(),
            kwargs={},
            has_result=False,
            fn_ref=binding.fn_ref,
            source_fingerprint=binding.source_fingerprint,
        )
        if include_inputs:
            cached_inputs = self._step_input_cache.get(binding_key)
            if cached_inputs is not None and not binding.args and not binding.kwargs:
                cached_args, cached_kwargs = cached_inputs
                node_id = self._step_id_by_key.get(binding_key)
                node_index = (
                    self._step_index_by_id.get(node_id)
                    if node_id is not None
                    else None
                )
                node = (
                    self.case_plan.nodes[node_index]
                    if node_index is not None
                    else None
                )
                arg_templates = node.args if isinstance(node, StepNode) else ()
                kw_templates = node.kwargs if isinstance(node, StepNode) else {}
                binding.args = tuple(
                    self._store_step_input_value(
                        value,
                        template=(
                            arg_templates[index]
                            if index < len(arg_templates)
                            else None
                        ),
                        context=context.child(f"arg-{index}"),
                        description=f"{description_prefix} input {index + 1}",
                    )
                    for index, value in enumerate(cached_args)
                )
                binding.kwargs = {
                    key: self._store_step_input_value(
                        value,
                        template=kw_templates.get(key),
                        context=context.child(f"kw-{key}"),
                        description=f"{description_prefix} kwarg {key!r}",
                    )
                    for key, value in cached_kwargs.items()
                }
            frozen.args = tuple(binding.args)
            frozen.kwargs = dict(binding.kwargs)
        if include_result and binding.has_result:
            frozen.has_result = True
            if binding_key in self._step_result_cache:
                if binding.result is None:
                    cached_result = self._step_result_cache[binding_key]
                    binding.result = self._store_runtime_value(
                        cached_result,
                        context=context.child("result"),
                        description=f"{description_prefix} result",
                    )
            frozen.result = binding.result
        return frozen

    def _freeze_branch_anchor_binding(
        self,
        binding_key: str,
        binding: StepBindingState,
        *,
        context: JourneyStoreContext,
        description_prefix: str,
    ) -> StepBindingState:
        frozen = StepBindingState(
            args=(),
            kwargs={},
            has_result=binding.has_result,
            fn_ref=binding.fn_ref,
            source_fingerprint=binding.source_fingerprint,
        )
        if binding.has_result:
            if binding_key in self._step_result_cache:
                cached_result = self._step_result_cache[binding_key]
                frozen.result = self._store_runtime_value(
                    cached_result,
                    context=context.child("result"),
                    description=f"{description_prefix} result",
                )
            else:
                frozen.result = binding.result
        return frozen

    def _binding_store_context(self, binding_key: str) -> JourneyStoreContext:
        return self._require_state_controller().binding_store_context(binding_key)

    def _binding_restore_context(self, binding_key: str) -> JourneyRestoreContext:
        return self._require_state_controller().binding_restore_context(binding_key)

    def _branch_anchor_store_context(
        self,
        anchor_key: str,
        binding_key: str,
    ) -> JourneyStoreContext:
        return self._require_state_controller().branch_anchor_store_context(
            anchor_key=anchor_key,
            binding_key=binding_key,
        )

    def _branch_anchor_restore_context(
        self,
        *,
        binding_key: str,
        anchor_key: str,
    ) -> JourneyRestoreContext:
        return self._require_state_controller().branch_anchor_restore_context(
            anchor_key=anchor_key,
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

    def _is_step_executing(self) -> bool:
        lifecycle = self._active_step_lifecycle
        return (
            lifecycle is not None
            and lifecycle.phase == _STEP_LIFECYCLE_EXECUTION
        )

    def _register_step_exit_object(self, value: _StepExitObject) -> None:
        lifecycle = self._active_step_lifecycle
        if lifecycle is None or lifecycle.phase != _STEP_LIFECYCLE_EXECUTION:
            raise InvalidBranchUsageError(
                "Step-exit cleanup objects can only be registered while a step is running.",
                hint=(
                    "Call lifecycle-aware touchpoints from inside a function passed to "
                    "step(...), not during planning, module import, or between steps."
                ),
            )
        lifecycle._register_exit_object(value)

    def _register_case_exit_object(self, value: _CaseExitObject) -> None:
        lifecycle = self._active_step_lifecycle
        if lifecycle is None or lifecycle.phase != _STEP_LIFECYCLE_EXECUTION:
            raise InvalidBranchUsageError(
                "Case-exit cleanup objects can only be registered while a step is running.",
                hint=(
                    "Call lifecycle-aware touchpoints from inside a function passed to "
                    "step(...), not during planning, module import, or between steps."
                ),
            )
        lifecycle._register_case_exit_object(value)

    def _allocate_browser_recording(self) -> _BrowserRecordingContext | None:
        controller = self._browser_recording_controller
        if controller is None or not controller.enabled:
            return None
        lifecycle = self._active_step_lifecycle
        if lifecycle is None or lifecycle.phase != _STEP_LIFECYCLE_EXECUTION:
            return None
        key = (lifecycle.node.node_id, lifecycle.attempt)
        context_index = self._browser_recording_context_counts.get(key, 0) + 1
        self._browser_recording_context_counts[key] = context_index
        return controller.allocate(
            journey_plan=self.journey_plan,
            case_plan=self.case_plan,
            node=lifecycle.node,
            node_index=lifecycle.node_index,
            attempt=lifecycle.attempt,
            context_index=context_index,
        )

    def _allocate_log_artifact(
        self,
        *,
        kind: str,
        touchpoint: str,
        source: str,
        suffix: str,
        content_type: str,
        recording_key: str | None = None,
    ) -> _LogArtifactContext | None:
        controller = self._log_artifact_controller
        if controller is None or not controller.enabled:
            return None
        lifecycle = self._active_step_lifecycle
        if lifecycle is None or lifecycle.phase != _STEP_LIFECYCLE_EXECUTION:
            return None
        return controller.allocate(
            journey_plan=self.journey_plan,
            case_plan=self.case_plan,
            node=lifecycle.node,
            node_index=lifecycle.node_index,
            attempt=lifecycle.attempt,
            kind=kind,
            touchpoint=touchpoint,
            source=source,
            suffix=suffix,
            content_type=content_type,
            recording_key=recording_key,
        )

    def prompt_memory_root(self) -> Path | None:
        return self._prompt_memory_root

    def prompt_memory_disabled(self) -> bool:
        return self._prompt_memory_disabled

    def prompt_memory_update_disabled(self) -> bool:
        return self._prompt_memory_update_disabled

    def _ensure_no_active_step_lifecycle(self) -> None:
        if self._active_step_lifecycle is not None:
            raise InvalidBranchUsageError(
                "A journey step started while another step was already running.",
                hint="Do not call step(...) from inside another step function.",
            )

    def _set_step_lifecycle_phase(
        self,
        lifecycle: "_StepLifecycle",
        phase: str,
    ) -> None:
        active = self._active_step_lifecycle
        if active is not None and active is not lifecycle:
            self._ensure_no_active_step_lifecycle()
        self._active_step_lifecycle = lifecycle
        lifecycle.phase = phase
        _notify_step_lifecycle_phase(phase)

    def _clear_step_lifecycle(self, lifecycle: "_StepLifecycle") -> None:
        if self._active_step_lifecycle is lifecycle:
            self._active_step_lifecycle = None
            _notify_step_lifecycle_phase(None)
        lifecycle.phase = None

    def _begin_step_attempt(
        self,
        node: StepNode,
        *,
        node_index: int,
    ) -> tuple[int, float]:
        self._dirty_node_id = node.node_id
        self._mark_same_step_retry_boundary_before_attempt(node, node_index)
        attempt = self._step_attempts.get(node.node_id, 0) + 1
        self._step_attempts[node.node_id] = attempt
        self._persist_state()
        self._observer.on_step_start(
            case_plan=self.case_plan,
            node=node,
            node_index=node_index,
            attempt=attempt,
        )
        return attempt, time.perf_counter()

    def _observe_step_interrupted(
        self,
        node: StepNode,
        *,
        node_index: int,
        attempt: int,
        started_at: float,
        error: BaseException,
    ) -> None:
        self._observer.on_step_interrupted(
            case_plan=self.case_plan,
            node=node,
            node_index=node_index,
            attempt=attempt,
            duration_seconds=time.perf_counter() - started_at,
            error=error,
        )

    def _commit_step_success(
        self,
        node: StepNode,
        *,
        node_index: int,
        attempt: int,
        started_at: float,
        output: Any,
        binding: StepBindingState,
    ) -> bool:
        self._remember_step_result(node, output)
        self._retry_remaining.pop(node.node_id, None)
        self._dirty_node_id = None
        self._mark_replay_boundary_available_after(node_index)
        should_stop = self._record(
            node_index,
            node,
            status="executed",
            result=output,
        )
        self._observer.on_step_success(
            case_plan=self.case_plan,
            node=node,
            node_index=node_index,
            attempt=attempt,
            duration_seconds=time.perf_counter() - started_at,
        )
        return should_stop

    def _store_branch_anchor_snapshot(self, node: StepNode) -> None:
        if (
            self._state_controller is None
            or not self._rehydration_enabled
            or node.node_id not in self._branch_anchor_step_ids
        ):
            return

        anchor_key = self._step_key_by_id[node.node_id]
        anchor_index = self._step_index_by_id[node.node_id]
        prefix_step_keys = {
            self._step_key_by_id[item.node_id]
            for item in self.case_plan.nodes[: anchor_index + 1]
            if isinstance(item, StepNode)
        }
        prefix_records = [
            (index, record)
            for index, record in zip(self._record_indices, self.records)
            if index <= anchor_index
        ]
        snapshot = RuntimeSnapshotState(
            record_indices=[index for index, _ in prefix_records],
            records=_state_records([record for _, record in prefix_records]),
            step_bindings={
                key: self._freeze_branch_anchor_binding(
                    key,
                    binding,
                    context=self._branch_anchor_store_context(anchor_key, key),
                    description_prefix=f"branch anchor '{node.label or node.node_id}'",
                )
                for key, binding in self._step_bindings.items()
                if key in prefix_step_keys and binding.has_result
            },
            retry_remaining=dict(self._retry_remaining),
            step_attempts=dict(self._step_attempts),
        )
        self._state_controller.store_branch_anchor_snapshot(anchor_key, snapshot)

    def _close_step_exit_objects(
        self,
        exit_objects: tuple[_StepExitObject, ...],
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> list[BaseException]:
        failures: list[BaseException] = []
        for value in reversed(exit_objects):
            try:
                value.__exit__(exc_type, exc, traceback)
            except BaseException as cleanup_exc:  # pragma: no cover - exercised through callers
                failures.append(cleanup_exc)
        return failures

    def _register_case_exit_objects_from_value(self, value: Any) -> None:
        self._register_case_exit_objects(tuple(_case_exit_objects_from_value(value)))

    def _register_case_exit_objects(
        self,
        case_exit_objects: tuple[_CaseExitObject, ...],
    ) -> None:
        for case_exit_object in case_exit_objects:
            identity = id(case_exit_object)
            if identity in self._case_exit_object_ids:
                continue
            self._case_exit_object_ids.add(identity)
            self._case_exit_objects.append(case_exit_object)

    def _close_case_exit_objects(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
        *,
        case_exit_objects: tuple[_CaseExitObject, ...] | None = None,
    ) -> list[BaseException]:
        if case_exit_objects is None:
            case_exit_objects = tuple(self._case_exit_objects)
            self._case_exit_objects.clear()
            self._case_exit_object_ids.clear()

        failures: list[BaseException] = []
        for value in reversed(case_exit_objects):
            try:
                value.__case_exit__(exc_type, exc, traceback)
            except BaseException as cleanup_exc:  # pragma: no cover - exercised through callers
                failures.append(cleanup_exc)
        return failures

    def _pop_case_exit_objects(self) -> tuple[_CaseExitObject, ...]:
        case_exit_objects = tuple(self._case_exit_objects)
        self._case_exit_objects.clear()
        self._case_exit_object_ids.clear()
        return case_exit_objects

    def step(
        self,
        fn: StepFunction[P, R],
        *args: P.args,
        retry: int = 0,
        retry_delay: StepRetryDelay = 5,
        retry_from: StepRetryFrom = None,
        **kwargs: P.kwargs,
    ) -> R:
        self._ensure_no_active_step_lifecycle()
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
            result = self._materialize_step_result(
                binding_key,
                binding,
                description=f"saved result for step '{node.label or node.node_id}'",
            )
            self._remember_step_result(node, result)
            return cast(R, result)

        if binding is not None and binding.has_result and not self._has_record_for(node_index):
            result = self._materialize_step_result(
                binding_key,
                binding,
                description=f"saved result for step '{node.label or node.node_id}'",
            )
            self._remember_step_result(node, result)
            self.replay_from_index = max(self.replay_from_index, node_index + 1)
            should_stop = self._record(
                node_index,
                node,
                status="replayed",
                result=result,
            )
            self._observer.on_step_replay(
                case_plan=self.case_plan,
                node=node,
                node_index=node_index,
                record=self.records[-1],
            )
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

        lifecycle = _StepLifecycle(
            session=self,
            node=node,
            node_index=node_index,
            binding=binding,
        )
        return lifecycle.run(fn, tuple(args), dict(kwargs))

    def branch(
        self,
        *,
        start_from: _RuntimeStepAnchor | None,
        frame: FrameType,
    ) -> BranchHandle:
        site = resolve_branch_call_site(frame)
        handle_def = self.validation.branch_handle_definitions.get(site)
        if handle_def is not None:
            return BranchHandle(
                definition_site=site,
                name=handle_def.name,
                start_from=start_from,
            )

        spec = self.validation.branch_conditions.get(site)
        if spec is None or spec.handle_site is not None:
            raise InvalidBranchUsageError(
                "journey.branch(...) is only valid as a direct if/elif condition.",
                hint="Use journey.branch(...) directly as `if journey.branch(...):`, or assign it to a variable and check that variable directly in an if/elif chain.",
            )

        return BranchHandle(
            definition_site=site,
            name=spec.branch_key,
            start_from=start_from,
        )

    def branch_handle(self, *, handle: BranchHandle, frame: FrameType) -> bool:
        site = resolve_branch_call_site(frame)
        spec = self.validation.branch_conditions.get(site)
        if spec is None:
            raise InvalidBranchUsageError(
                "Branch handles are only valid as direct if/elif conditions.",
                hint="Use the branch handle directly as `if branch_a:` or `elif branch_b:`.",
            )

        expected_definition_site = spec.handle_site or site
        if expected_definition_site != handle.definition_site:
            raise InvalidBranchUsageError(
                "Branch handle condition does not match the branch() call that created it.",
                hint="Use the same branch handle variable directly in the if/elif chain where it was declared.",
            )
        return self._select_branch(
            spec=spec,
            start_from=cast(_RuntimeStepAnchor | None, handle.start_from),
        )

    def _group_id_for_branch_condition(
        self,
        spec: BranchConditionSpec,
    ) -> str:
        handle_group_key = spec.handle_group_key
        if handle_group_key is None:
            return self._next_group_id()
        group_id = self._branch_handle_group_ids.get(handle_group_key)
        if group_id is None:
            group_id = self._next_group_id()
            self._branch_handle_group_ids[handle_group_key] = group_id
        return group_id

    def _select_branch(
        self,
        *,
        spec: BranchConditionSpec,
        start_from: _RuntimeStepAnchor | None,
    ) -> bool:
        state = self._active_branch_chains.get(spec.template_key)
        if spec.condition_index == 1:
            if state is not None:
                raise InvalidBranchUsageError(
                    "journey.branch(...) re-entered the same if/elif chain before the prior branch selection finished.",
                    hint="Keep journey.branch(...) in one direct if/elif chain without reusing it in helper callbacks.",
                )
            state = _ActiveBranchChain(
                group_id=self._group_id_for_branch_condition(spec),
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
        if node.start_from is None:
            start_from_matches_plan = start_from is None
        elif start_from is None:
            start_from_matches_plan = False
        else:
            start_from_matches_plan = node.start_from in start_from.node_ids
        if not start_from_matches_plan:
            raise InvalidBranchUsageError(
                "journey.branch(start_from=...) used a step that does not match the compiled plan.",
                hint="Make sure each journey.branch(start_from=...) points to the same earlier step result it used during planning.",
            )

        if marker_index >= self.replay_from_index:
            should_stop = self._record(
                marker_index,
                node,
                status="executed",
                result=node.active_key,
            )
            self._observer.on_branch(
                case_plan=self.case_plan,
                node=node,
                node_index=marker_index,
            )
            if should_stop:
                raise _StopCase()
        self._active_branch_chains.pop(spec.template_key, None)
        return True


@dataclass
class _StepLifecycle:
    session: _RunSession
    node: StepNode
    node_index: int
    binding: StepBindingState | None
    phase: str | None = None
    attempt: int = 0
    started_at: float = 0.0
    registered_exit_objects: list[_StepExitObject] = field(default_factory=list)
    registered_case_exit_objects: list[_CaseExitObject] = field(default_factory=list)
    exit_objects: tuple[_StepExitObject, ...] = ()
    _success_committed: bool = False

    def run(
        self,
        fn: StepFunction[P, R],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> R:
        self.session._ensure_no_active_step_lifecycle()
        try:
            self._enter(_STEP_LIFECYCLE_INITIALIZATION)
            bound_args, bound_kwargs = self._initialize(args, kwargs)
            self.attempt, self.started_at = self.session._begin_step_attempt(
                self.node,
                node_index=self.node_index,
            )

            output = self._execute(fn, bound_args, bound_kwargs)
            binding = self._store(output)

            should_stop = self._pre_exit(output, binding)
            self._exit()
            if not self._success_committed:
                should_stop = self._commit_success(output, binding)
            self._post_exit()
            if should_stop:
                raise _StopCase()
            return cast(R, output)
        finally:
            self.session._clear_step_lifecycle(self)

    def _enter(self, phase: str) -> None:
        self.session._set_step_lifecycle_phase(self, phase)

    def _initialize(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if (
            self.binding is not None
            and not self.binding.has_result
            and self.session._binding_has_inputs_for_node(self.node, self.binding)
        ):
            return self.session._resolve_step_inputs(self.node, self.binding)

        self.binding = self.session._store_step_inputs(self.node, args, kwargs)
        return self.session._resolve_step_inputs(self.node, self.binding)

    def _execute(
        self,
        fn: StepFunction[P, R],
        bound_args: tuple[Any, ...],
        bound_kwargs: dict[str, Any],
    ) -> R:
        self._enter(_STEP_LIFECYCLE_EXECUTION)
        try:
            return fn(*bound_args, **bound_kwargs)
        except KeyboardInterrupt as exc:
            cleanup_failures = self.session._close_step_exit_objects(
                tuple(self.registered_exit_objects),
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            case_cleanup_failures = self._close_registered_case_exit_objects(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, case_cleanup_failures, case_exit=True)
            self.session._clear_step_lifecycle(self)
            self.session._observe_step_interrupted(
                self.node,
                node_index=self.node_index,
                attempt=self.attempt,
                started_at=self.started_at,
                error=exc,
            )
            raise
        except Exception as exc:  # pragma: no cover - surfaced to caller
            cleanup_failures = self.session._close_step_exit_objects(
                tuple(self.registered_exit_objects),
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            case_cleanup_failures = self._close_registered_case_exit_objects(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, case_cleanup_failures, case_exit=True)
            self.session._clear_step_lifecycle(self)
            self.session._handle_step_exception(
                self.node,
                node_index=self.node_index,
                attempt=self.attempt,
                started_at=self.started_at,
                exc=exc,
            )
        except BaseException as exc:
            cleanup_failures = self.session._close_step_exit_objects(
                tuple(self.registered_exit_objects),
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            case_cleanup_failures = self._close_registered_case_exit_objects(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, case_cleanup_failures, case_exit=True)
            self.session._clear_step_lifecycle(self)
            raise
        raise AssertionError("unreachable")

    def _register_exit_object(self, value: _StepExitObject) -> None:
        if all(id(value) != id(existing) for existing in self.registered_exit_objects):
            self.registered_exit_objects.append(value)

    def _register_case_exit_object(self, value: _CaseExitObject) -> None:
        if all(
            id(value) != id(existing)
            for existing in self.registered_case_exit_objects
        ):
            self.registered_case_exit_objects.append(value)

    def _close_registered_case_exit_objects(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> list[BaseException]:
        case_exit_objects = tuple(self.registered_case_exit_objects)
        self.registered_case_exit_objects.clear()
        return self.session._close_case_exit_objects(
            exc_type,
            exc,
            traceback,
            case_exit_objects=case_exit_objects,
        )

    def _promote_registered_case_exit_objects(self) -> None:
        case_exit_objects = tuple(self.registered_case_exit_objects)
        self.registered_case_exit_objects.clear()
        self.session._register_case_exit_objects(case_exit_objects)

    def _exit_objects_for_output(self, output: Any) -> tuple[_StepExitObject, ...]:
        return _dedupe_step_exit_objects(
            tuple(self.registered_exit_objects),
            tuple(_step_exit_objects_from_value(output)),
        )

    def _store(self, output: Any) -> StepBindingState:
        self._enter(_STEP_LIFECYCLE_STORAGE)
        exit_objects = self._exit_objects_for_output(output)
        try:
            binding = self.session._set_step_result(self.node, output)
        except Exception as exc:
            cleanup_failures = self.session._close_step_exit_objects(
                exit_objects,
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            case_cleanup_failures = self._close_registered_case_exit_objects(
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, case_cleanup_failures, case_exit=True)
            raise
        self.exit_objects = exit_objects
        return binding

    def _pre_exit(self, output: Any, binding: StepBindingState) -> bool:
        self._enter(_STEP_LIFECYCLE_PRE_EXIT)
        should_pause_for_develop = (
            self.session._develop_step_enabled
            and self.session.stop_after_index is not None
            and self.node_index == self.session.stop_after_index
            and not _is_step_interrupt_pending()
        )
        if not should_pause_for_develop:
            return False

        try:
            should_stop = self._commit_success(output, binding)
            if should_stop:
                self.session._paused_step = self.session._build_paused_step(
                    self.node,
                    node_index=self.node_index,
                    attempt=self.attempt,
                    ok=True,
                )
                self.session._persist_state()
                raise _PauseRequested(
                    self.session._paused_step,
                    self.exit_objects,
                )
        except _PauseRequested:
            raise
        except Exception as exc:
            cleanup_failures = self.session._close_step_exit_objects(
                self.exit_objects,
                type(exc),
                exc,
                exc.__traceback__,
            )
            _add_cleanup_failure_notes(exc, cleanup_failures)
            raise
        return False

    def _exit(self) -> None:
        self._enter(_STEP_LIFECYCLE_EXIT)
        cleanup_failures = self.session._close_step_exit_objects(self.exit_objects)
        if cleanup_failures:
            self.session._discard_step_result(self.node)
            cleanup_error = RuntimeError(_cleanup_failure_message(cleanup_failures))
            case_cleanup_failures = self._close_registered_case_exit_objects(
                type(cleanup_error),
                cleanup_error,
                cleanup_error.__traceback__,
            )
            _add_cleanup_failure_notes(
                cleanup_error,
                case_cleanup_failures,
                case_exit=True,
            )
            self.session._handle_step_exception(
                self.node,
                node_index=self.node_index,
                attempt=self.attempt,
                started_at=self.started_at,
                exc=cleanup_error,
            )

    def _commit_success(self, output: Any, binding: StepBindingState) -> bool:
        self._success_committed = True
        self._promote_registered_case_exit_objects()
        return self.session._commit_step_success(
            self.node,
            node_index=self.node_index,
            attempt=self.attempt,
            started_at=self.started_at,
            output=output,
            binding=binding,
        )

    def _post_exit(self) -> None:
        self._enter(_STEP_LIFECYCLE_POST_EXIT)
        self.session._store_branch_anchor_snapshot(self.node)
        _raise_if_interrupted_after_step()


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
    step_by_id = {
        node.node_id: node
        for node in case_plan.nodes
        if isinstance(node, StepNode)
    }
    upto = min(stop_after_index, len(case_plan.nodes) - 1)
    for index in range(upto, -1, -1):
        node = case_plan.nodes[index]
        if isinstance(node, BranchMarkerNode) and node.start_from is not None:
            anchor = step_by_id.get(node.start_from)
            if anchor is None:
                return node.start_from
            return anchor.label or anchor.node_id
    return None


def _branch_start_anchor_key_for(case_plan: CasePlan) -> str | None:
    step_keys = _case_rehydration_maps(case_plan)
    for node in case_plan.nodes:
        if isinstance(node, BranchMarkerNode) and node.start_from is not None:
            return step_keys.get(node.start_from)
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
    for selected_case in selected_cases:
        for node in selected_case.case_plan.nodes:
            if isinstance(node, StepNode) and node.retry is not None:
                return True
            if isinstance(node, BranchMarkerNode) and node.start_from is not None:
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


def _stable_json(value: object) -> str:
    return json.dumps(_stable_value(value), sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _package_version() -> str:
    try:
        return importlib.metadata.version("journey-sdk")
    except importlib.metadata.PackageNotFoundError:
        return "editable"


def _state_validity_envelope(
    journey_fn: JourneyEntrypoint,
    *,
    plan: JourneyPlan,
    selected_cases: list[_SelectedCase],
    step: str | None,
    develop_step: str | None,
) -> StateValidityEnvelope:
    source_file = inspect.getsourcefile(journey_fn) or inspect.getfile(journey_fn)
    source_root = Path(source_file).resolve().parent
    plan_signature = _plan_signature(plan, selected_cases, step, develop_step)
    fingerprints: list[StateFingerprint] = [
        StateFingerprint("journey_id", plan.journey_id),
        StateFingerprint("function_ref", plan.function_ref),
        StateFingerprint("step", step or ""),
        StateFingerprint("develop_step", develop_step or ""),
        StateFingerprint(
            "selected_cases",
            _hash_text(_stable_json(_selected_case_refs(selected_cases))),
        ),
        StateFingerprint("plan_shape", plan_signature),
        StateFingerprint("state_format", str(STATE_FORMAT_VERSION)),
        StateFingerprint("journey_sdk_version", _package_version()),
        StateFingerprint(
            "python",
            f"{sys.version_info.major}.{sys.version_info.minor}",
        ),
    ]
    fingerprints.extend(_workspace_fingerprints(source_root))
    return StateValidityEnvelope(fingerprints=tuple(sorted(fingerprints, key=lambda item: item.key)))


def _workspace_fingerprints(source_root: Path) -> list[StateFingerprint]:
    fingerprints: list[StateFingerprint] = []
    for name in (
        "uv.lock",
        "pyproject.toml",
        "requirements.txt",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ):
        path = _find_upwards(source_root, name)
        digest = _hash_file(path) if path is not None else None
        if digest is not None:
            fingerprints.append(StateFingerprint(f"file:{name}", digest))
    fingerprints.extend(_git_fingerprints(source_root))
    return fingerprints


def _find_upwards(start: Path, name: str) -> Path | None:
    current = start.resolve()
    for candidate_root in (current, *current.parents):
        candidate = candidate_root / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _git_fingerprints(source_root: Path) -> list[StateFingerprint]:
    git_root = _run_git(source_root, "rev-parse", "--show-toplevel")
    if git_root is None:
        return []
    commit = _run_git(Path(git_root), "rev-parse", "HEAD")
    status = _run_git(Path(git_root), "status", "--porcelain=v1")
    fingerprints: list[StateFingerprint] = []
    if commit is not None:
        fingerprints.append(StateFingerprint("git:commit", commit))
    if status is not None:
        fingerprints.append(StateFingerprint("git:dirty", _hash_text(status)))
    return fingerprints


def _run_git(cwd: Path, *args: str) -> str | None:
    if getattr(subprocess.run, "__module__", "subprocess") != "subprocess":
        return None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _validity_mismatch(
    expected: StateValidityEnvelope,
    actual: StateValidityEnvelope,
) -> _StateInvalidationReason | None:
    expected_map = {item.key: item.value for item in expected.fingerprints}
    actual_map = {item.key: item.value for item in actual.fingerprints}
    for key in sorted(set(expected_map) | set(actual_map)):
        if expected_map.get(key) != actual_map.get(key):
            return _StateInvalidationReason(
                key=key,
                expected=expected_map.get(key),
                actual=actual_map.get(key),
            )
    return None


def _reason_allows_prefix_reuse(reason: _StateInvalidationReason) -> bool:
    return reason.key == "plan_shape"


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


def _replay_boundary_for_case_step(
    case_plan: CasePlan,
    node: StepNode,
    node_index: int,
) -> _ReplayBoundary | None:
    if node.retry is None:
        return _ReplayBoundary(start_index=0)

    step_index_by_id = {
        item.node_id: index
        for index, item in enumerate(case_plan.nodes)
        if isinstance(item, StepNode)
    }
    if node.retry.from_node_id is not None:
        anchor_index = step_index_by_id.get(node.retry.from_node_id)
    else:
        anchor_index = node_index

    if anchor_index is None or anchor_index > node_index:
        return None
    return _ReplayBoundary(start_index=anchor_index)


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
    if isinstance(node, BranchMarkerNode):
        return record.result == node.active_key
    return False


def _node_index_for_record(
    case_plan: CasePlan,
    record: NodeExecutionRecord,
) -> int | None:
    for index, node in enumerate(case_plan.nodes):
        if node.node_id == record.node_id and type(node).__name__ == record.node_type:
            return index
    return None


def _emit_replayed_record(
    observer: _ExecutionObserver,
    *,
    case_plan: CasePlan,
    node_index: int,
    record: NodeExecutionRecord,
) -> None:
    if record.status != "replayed":
        return
    node = case_plan.nodes[node_index]
    if isinstance(node, StepNode):
        observer.on_step_replay(
            case_plan=case_plan,
            node=node,
            node_index=node_index,
            record=record,
        )
    elif isinstance(node, BranchMarkerNode):
        observer.on_branch_replay(
            case_plan=case_plan,
            node=node,
            node_index=node_index,
            record=record,
        )


def _emit_replayed_records(
    observer: _ExecutionObserver,
    *,
    case_plan: CasePlan,
    record_indices: list[int],
    records: list[NodeExecutionRecord],
) -> None:
    for node_index, record in zip(record_indices, records):
        _emit_replayed_record(
            observer,
            case_plan=case_plan,
            node_index=node_index,
            record=record,
        )


def _emit_replayed_case_report(
    observer: _ExecutionObserver,
    *,
    case_plan: CasePlan,
    report: CaseExecutionReport,
) -> None:
    observer.on_case_replay(case_plan=case_plan, report=report)
    for record in report.records:
        node_index = _node_index_for_record(case_plan, record)
        if node_index is None:
            continue
        _emit_replayed_record(
            observer,
            case_plan=case_plan,
            node_index=node_index,
            record=record,
        )


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

    step_keys = _case_rehydration_maps(case_plan)
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


def _snapshot_boundary_index(snapshot: RuntimeSnapshotState) -> int:
    if not snapshot.record_indices:
        return 0
    return max(snapshot.record_indices) + 1


def _longest_matching_snapshot_prefix(
    snapshot: RuntimeSnapshotState,
    case_plan: CasePlan,
) -> int:
    upper_bound = min(_snapshot_boundary_index(snapshot), len(case_plan.nodes))
    for boundary_index in range(upper_bound, -1, -1):
        if _snapshot_matches_prefix(snapshot, case_plan, boundary_index):
            return boundary_index
    return 0


def _trim_snapshot_to_prefix(
    snapshot: RuntimeSnapshotState,
    case_plan: CasePlan,
    boundary_index: int,
) -> RuntimeSnapshotState:
    step_keys = _case_rehydration_maps(case_plan)
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
    observer.on_state_validity(
        boundary_id=case_id,
        status="invalidated",
        reason="plan_shape",
        expected=None,
        actual=None,
        action="executing from the beginning",
    )
    observer.on_develop_state_restart(case_id=case_id)
    return _DevelopStateRefresh(restarted_case_id=case_id)


def _refresh_develop_state_for_plan(
    path: Path | None,
    *,
    journey_plan: JourneyPlan,
    step: str | None,
    develop_step: str | None,
    selected_cases: list[_SelectedCase],
    validity: StateValidityEnvelope,
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
        or state.develop_step is None
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
        replay_boundary: _ReplayBoundary | None = None
    else:
        replay_boundary = _replay_boundary_for_case_step(
            case_plan,
            paused_node,
            paused_index,
        )
        if replay_boundary is None:
            return _restart_develop_state(
                path,
                case_id=active_case.case_id,
                observer=observer,
            )
        reusable_boundary = replay_boundary.start_index

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
    if pause_action == "continue":
        active_case.stop_after_index = selected_case.stop_after_index
    else:
        active_case.stop_after_index = paused_index
    if replay_boundary is not None:
        active_case.snapshot = _trim_snapshot_to_prefix(
            active_case.snapshot,
            case_plan,
            replay_boundary.start_index,
        )
        active_case.replay_from_index = replay_boundary.start_index
        active_case.dirty_node_id = None

    observer.on_state_validity(
        boundary_id=active_case.case_id,
        status="invalidated",
        reason="plan_shape",
        expected=state.plan_signature,
        actual=new_signature,
        action=f"rerunning from step index {active_case.replay_from_index}",
    )
    state.plan_signature = new_signature
    state.validity = validity
    state.develop_step = develop_step
    state.selected_cases = expected_cases
    save_execution_state(path, state)
    return _DevelopStateRefresh()


def _resolve_prompt_memory_root(
    _journey_fn: JourneyEntrypoint,
    *,
    prompt_memory_root: str | Path | None,
    state_path: Path | None,
) -> Path:
    if prompt_memory_root is not None:
        return Path(prompt_memory_root).resolve()
    if state_path is not None:
        return _prompt_memory_root_for_state_path(state_path)
    return Path.cwd().resolve()


def _prompt_memory_root_for_state_path(state_path: str | Path) -> Path:
    path = Path(state_path).resolve()
    state_dir = path.parent
    if state_dir.name == DEFAULT_STATE_DIR:
        return state_dir.parent
    return state_dir


def _resolve_browser_recording_root(
    journey_fn: JourneyEntrypoint,
) -> Path:
    source_file = inspect.getsourcefile(journey_fn) or inspect.getfile(journey_fn)
    return Path(source_file).resolve().parent / DEFAULT_STATE_DIR / "logs"


def _resolve_log_root(
    journey_fn: JourneyEntrypoint,
) -> Path:
    source_file = inspect.getsourcefile(journey_fn) or inspect.getfile(journey_fn)
    return Path(source_file).resolve().parent / DEFAULT_STATE_DIR / "logs"


def _resolve_default_state_path(
    journey_fn: JourneyEntrypoint,
) -> Path:
    source_file = inspect.getsourcefile(journey_fn) or inspect.getfile(journey_fn)
    return default_execution_state_path(Path(source_file))


def _execute_plan(
    journey_fn: JourneyEntrypoint,
    *,
    plan: JourneyPlan,
    step: str | None = None,
    develop_step: str | None = None,
    pause_action: str | None = None,
    state: str | Path | None = None,
    observer: _ExecutionObserver | None = None,
    no_state: bool = False,
    no_state_update: bool = False,
    no_memory: bool = False,
    no_memory_update: bool = False,
    no_browser_recording: bool = False,
    clean_browser_recordings: bool = True,
    no_logs: bool = False,
    clean_logs: bool = True,
    prompt_memory_root: str | Path | None = None,
) -> ExecutionReport | _PausedExecution:
    if no_state and state is not None:
        raise ValueError("execute(...) cannot combine a custom state path with disabled state.")
    if not clean_browser_recordings:
        clean_logs = False
    target_step = develop_step if develop_step is not None else step
    selected_cases = _select_cases(plan, target_step)
    validation = validate_journey(journey_fn)
    execution_observer = observer or _LoggingExecutionObserver()
    log_root = _resolve_log_root(journey_fn)
    log_artifact_controller = _RunArtifactController(
        enabled=not no_logs,
        root=log_root,
        clean_existing=clean_logs,
    )
    log_artifact_controller.start_journey_log(plan=plan)
    log_capture = (
        capture_log_records(log_artifact_controller.write_journey_record)
        if log_artifact_controller.enabled
        else nullcontext()
    )
    with log_capture:
        execution_observer.on_journey_start(plan=plan, selected_cases=selected_cases)
        default_state_path = _resolve_default_state_path(journey_fn)
        using_default_state = not no_state and state is None
        state_path = (
            None
            if no_state
            else Path(state)
            if state is not None
            else default_state_path
        )
        state_storage = prepare_execution_state_storage(
            state_path,
            update_enabled=not no_state_update,
        )
        if no_state:
            default_artifact_root, _ = artifact_root_for_state(default_state_path)
            state_storage = replace(
                state_storage,
                persistent_artifact_root=default_artifact_root,
            )
        rehydration_enabled = _needs_rehydration(
            selected_cases,
            step=step,
            develop_step=develop_step,
            state=state_storage.run_path,
        )
        validity = _state_validity_envelope(
            journey_fn,
            plan=plan,
            selected_cases=selected_cases,
            step=step,
            develop_step=develop_step,
        )
        develop_refresh = _refresh_develop_state_for_plan(
            state_storage.run_path,
            journey_plan=plan,
            step=step,
            develop_step=develop_step,
            selected_cases=selected_cases,
            validity=validity,
            pause_action=pause_action,
            observer=execution_observer,
        )
        effective_pause_action = (
            None if develop_refresh.restarted_case_id is not None else pause_action
        )
        state_controller = _StateController(
            state_storage,
            journey_plan=plan,
            step=step,
            develop_step=develop_step,
            selected_cases=selected_cases,
            validity=validity,
            observer=execution_observer,
            reset_completed_state=using_default_state,
        )
        resolved_prompt_memory_root = _resolve_prompt_memory_root(
            journey_fn,
            prompt_memory_root=prompt_memory_root,
            state_path=state_path if state_path is not None else default_state_path,
        )
        browser_recording_controller = _BrowserRecordingController(
            enabled=not no_browser_recording and not no_logs,
            root=log_root,
            clean_existing=False,
            run_id=log_artifact_controller.run_id,
        )

        case_reports: list[CaseExecutionReport] = state_controller.completed_case_reports
        for completed_index, report in enumerate(case_reports):
            if completed_index >= len(selected_cases):
                break
            _emit_replayed_case_report(
                execution_observer,
                case_plan=selected_cases[completed_index].case_plan,
                report=report,
            )

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
                start_anchor_key = _branch_start_anchor_key_for(selected_case.case_plan)
                branch_anchor_seed = (
                    state_controller.branch_anchor_snapshot_for(
                        start_anchor_key,
                        case_plan=selected_case.case_plan,
                    )
                    if rehydration_enabled
                    and restored_state is None
                    and selected_case.stop_after_index is None
                    and start_anchor_key is not None
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
                    branch_anchor_seed=branch_anchor_seed,
                    branch_anchor_key=(
                        start_anchor_key if branch_anchor_seed is not None else None
                    ),
                    observer=execution_observer,
                    prompt_memory_root=resolved_prompt_memory_root,
                    prompt_memory_disabled=no_memory,
                    prompt_memory_update_disabled=no_memory or no_memory_update,
                    browser_recording_controller=browser_recording_controller,
                    log_artifact_controller=log_artifact_controller,
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
                run_session.emit_replayed_records()

                if develop_step is not None:
                    if run_session.paused_step is not None and effective_pause_action is None:
                        return _PausedExecution(run_session.paused_step)
                    run_session.apply_pause_action(effective_pause_action)

                case_started_at = time.perf_counter()
                stopped_label: str | None = None

                try:
                    while True:
                        run_session.begin_attempt()
                        try:
                            with use_session(run_session):
                                journey_fn()
                        except _RetryRequested as retry_request:
                            if retry_request.sleep_for > 0:
                                time.sleep(retry_request.sleep_for)
                            continue
                        except _PauseRequested:
                            raise
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
                except _PauseRequested as pause_request:
                    return _PausedExecution(
                        pause_request.paused_step,
                        pause_request.pending_exit_objects,
                        run_session._pop_case_exit_objects(),
                    )
                except BaseException as exc:
                    cleanup_failures = run_session._close_case_exit_objects(
                        type(exc),
                        exc,
                        exc.__traceback__,
                    )
                    _add_cleanup_failure_notes(exc, cleanup_failures, case_exit=True)
                    raise

                cleanup_failures = run_session._close_case_exit_objects()
                if cleanup_failures:
                    raise RuntimeError(_case_cleanup_failure_message(cleanup_failures))

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
            log_artifact_controller.finish_journey_log()

    return result


def execute(
    journey_fn: JourneyEntrypoint,
    *,
    step: str | None = None,
    state: str | Path | None = None,
    no_state: bool = False,
    no_state_update: bool = False,
    no_memory: bool = False,
    no_memory_update: bool = False,
    no_browser_recording: bool = False,
    clean_browser_recordings: bool = True,
    no_logs: bool = False,
    clean_logs: bool = True,
) -> ExecutionReport:
    """Compile a journey and execute full cases or one targeted step flow.

    Args:
        journey_fn: Journey entrypoint to compile and execute.
        step: Optional target step label.
        state: Optional custom state file for replay and resume.
        no_state: Disable persistent state reads and writes for this run.
        no_state_update: Disable persistent state writes while still allowing reads.
        no_memory: Disable prompt-memory reads and writes for this run.
        no_memory_update: Disable prompt-memory writes while still allowing reads.
        no_browser_recording: Disable browser trace/video artifacts for this run.
        clean_browser_recordings: Deprecated alias for clean_logs.
        no_logs: Disable persistent logs and trace/video artifacts for this run.
        clean_logs: Remove existing Journey logs before this run.
    """

    plan = compile_journey(journey_fn)
    return _execute_plan(
        journey_fn,
        plan=plan,
        step=step,
        state=state,
        no_state=no_state,
        no_state_update=no_state_update,
        no_memory=no_memory,
        no_memory_update=no_memory_update,
        no_browser_recording=no_browser_recording,
        clean_browser_recordings=clean_browser_recordings,
        no_logs=no_logs,
        clean_logs=clean_logs,
    )
