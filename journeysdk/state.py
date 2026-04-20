"""Persistence helpers for interruptible journey execution."""

from __future__ import annotations

import os
import pickle
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    CorruptExecutionStateError,
    ExecutionStateSerializationError,
)
from .models import (
    CaseExecutionReport,
    NodeExecutionRecord,
)
from .rehydration import StoredValue

STATE_FORMAT_VERSION = 10


@dataclass
class SelectedCaseState:
    case_id: str
    stop_after_index: int | None


@dataclass
class StepBindingState:
    args: tuple[StoredValue, ...]
    kwargs: dict[str, StoredValue]
    has_result: bool
    result: StoredValue | None = None
    fn_ref: str | None = None
    source_fingerprint: str | None = None


@dataclass
class RuntimeSnapshotState:
    record_indices: list[int]
    records: list[NodeExecutionRecord]
    step_bindings: dict[str, StepBindingState]
    retry_remaining: dict[str, int]
    step_attempts: dict[str, int] = field(default_factory=dict)


@dataclass
class PausedStepState:
    node_id: str
    label: str | None
    node_index: int
    attempt: int
    ok: bool
    error: str | None = None
    failure_message: str | None = None
    failure_hint: str | None = None


@dataclass
class ActiveCaseState:
    case_id: str
    snapshot: RuntimeSnapshotState
    replay_from_index: int
    dirty_node_id: str | None
    stop_after_index: int | None = None
    paused_step: PausedStepState | None = None


@dataclass
class ExecutionStateEnvelope:
    version: int
    journey_id: str
    function_ref: str
    step: str | None
    develop_step: str | None
    plan_signature: str
    selected_cases: list[SelectedCaseState]
    current_case_index: int
    completed_case_reports: list[CaseExecutionReport]
    active_case: ActiveCaseState | None
    successful_step_bindings: dict[str, StepBindingState] = field(default_factory=dict)
    checkpoint_snapshots: dict[str, RuntimeSnapshotState] = field(default_factory=dict)


def artifact_root_for_state(path: Path | None) -> tuple[Path, bool]:
    """Return the artifact root for this run and whether it is temporary."""

    if path is None:
        return Path(tempfile.mkdtemp(prefix="journey-artifacts.")), True
    return path.parent / f"{path.name}.artifacts", False


def delete_artifact_root(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def load_execution_state(path: Path) -> ExecutionStateEnvelope | None:
    if not path.exists():
        return None

    try:
        with path.open("rb") as handle:
            loaded = pickle.load(handle)
    except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError) as exc:
        raise CorruptExecutionStateError(
            f"Could not read the journey state file '{path}': {exc}"
        ) from exc

    if not isinstance(loaded, ExecutionStateEnvelope):
        raise CorruptExecutionStateError(
            f"The file '{path}' does not contain valid journey state data."
        )

    return loaded


def save_execution_state(path: Path, state: ExecutionStateEnvelope) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
        ) as handle:
            tmp_path = Path(handle.name)
            pickle.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise ExecutionStateSerializationError(
            f"Could not save journey progress to the state file '{path}': {exc}"
        ) from exc


def delete_execution_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
