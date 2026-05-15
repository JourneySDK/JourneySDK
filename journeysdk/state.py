"""Persistence helpers for interruptible journey execution."""

from __future__ import annotations

import base64
import json
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

STATE_FORMAT_VERSION = 11
DEFAULT_STATE_FILENAME = "state.json"
DEFAULT_STATE_DIR = ".journey"
LEGACY_DEFAULT_STATE_FILENAME = ".state"


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
    branch_anchor_snapshots: dict[str, RuntimeSnapshotState] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionStateStorage:
    display_path: Path | None
    run_path: Path | None
    artifact_root: Path
    cleanup_root: Path | None = None
    artifact_root_is_temporary: bool = False


def artifact_root_for_state(path: Path | None) -> tuple[Path, bool]:
    """Return the artifact root for this run and whether it is temporary."""

    if path is None:
        return Path(tempfile.mkdtemp(prefix="journey-artifacts.")), True
    return path.parent / f"{path.name}.artifacts", False


def default_execution_state_path(
    source_file: Path,
) -> Path:
    """Return the default persistent state path for a journey source file."""

    source_file = source_file.resolve()
    return source_file.parent / DEFAULT_STATE_DIR / DEFAULT_STATE_FILENAME


def prepare_execution_state_storage(
    path: Path | None,
    *,
    update_enabled: bool,
) -> ExecutionStateStorage:
    """Prepare the state file/artifact paths used by one run."""

    if path is None:
        artifact_root, temporary = artifact_root_for_state(None)
        return ExecutionStateStorage(
            display_path=None,
            run_path=None,
            artifact_root=artifact_root,
            cleanup_root=artifact_root,
            artifact_root_is_temporary=temporary,
        )

    path = Path(path)
    if update_enabled:
        _migrate_legacy_default_state(path)
        artifact_root, temporary = artifact_root_for_state(path)
        return ExecutionStateStorage(
            display_path=path,
            run_path=path,
            artifact_root=artifact_root,
            artifact_root_is_temporary=temporary,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="journey-state-readonly."))
    run_path = temp_root / path.name
    if path.exists():
        shutil.copy2(path, run_path)

    source_artifacts, _ = artifact_root_for_state(path)
    artifact_root = temp_root / f"{path.name}.artifacts"
    if source_artifacts.exists():
        shutil.copytree(source_artifacts, artifact_root)

    return ExecutionStateStorage(
        display_path=path,
        run_path=run_path,
        artifact_root=artifact_root,
        cleanup_root=temp_root,
        artifact_root_is_temporary=True,
    )


def _migrate_legacy_default_state(path: Path) -> None:
    if path.name != DEFAULT_STATE_FILENAME or path.parent.name != DEFAULT_STATE_DIR:
        return

    legacy_path = path.parent.parent / LEGACY_DEFAULT_STATE_FILENAME
    if legacy_path.exists() and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy_path, path)

    legacy_artifacts, _ = artifact_root_for_state(legacy_path)
    artifact_root, _ = artifact_root_for_state(path)
    if legacy_artifacts.exists() and not artifact_root.exists():
        shutil.move(str(legacy_artifacts), str(artifact_root))


def delete_artifact_root(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def load_execution_state(path: Path) -> ExecutionStateEnvelope | None:
    if not path.exists():
        return None

    raw: bytes
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CorruptExecutionStateError(
            f"Could not read the journey state file '{path}': {exc}"
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
        loaded = _decode_execution_state(payload)
    except Exception as json_exc:
        try:
            loaded = pickle.loads(raw)
        except (
            EOFError,
            pickle.PickleError,
            AttributeError,
            ValueError,
            TypeError,
            ImportError,
            OSError,
        ) as pickle_exc:
            raise CorruptExecutionStateError(
                f"Could not read the journey state file '{path}': {json_exc}"
            ) from pickle_exc

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
            mode="w",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            encoding="utf-8",
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(
                _encode_execution_state(state),
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
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


def _encode_execution_state(state: ExecutionStateEnvelope) -> dict[str, object]:
    return {
        "format": "journey.execution_state",
        "version": state.version,
        "journey_id": state.journey_id,
        "function_ref": state.function_ref,
        "step": state.step,
        "develop_step": state.develop_step,
        "plan_signature": state.plan_signature,
        "selected_cases": [
            _encode_selected_case(item) for item in state.selected_cases
        ],
        "current_case_index": state.current_case_index,
        "completed_case_reports": [
            _encode_case_report(report) for report in state.completed_case_reports
        ],
        "active_case": (
            _encode_active_case(state.active_case)
            if state.active_case is not None
            else None
        ),
        "branch_anchor_snapshots": {
            key: _encode_runtime_snapshot(snapshot)
            for key, snapshot in state.branch_anchor_snapshots.items()
        },
    }


def _decode_execution_state(payload: object) -> ExecutionStateEnvelope:
    if not isinstance(payload, dict) or payload.get("format") != "journey.execution_state":
        raise ValueError("state JSON is missing the Journey execution-state marker")
    return ExecutionStateEnvelope(
        version=_require_int(payload, "version"),
        journey_id=_require_str(payload, "journey_id"),
        function_ref=_require_str(payload, "function_ref"),
        step=_optional_str(payload.get("step"), "step"),
        develop_step=_optional_str(payload.get("develop_step"), "develop_step"),
        plan_signature=_require_str(payload, "plan_signature"),
        selected_cases=[
            _decode_selected_case(item)
            for item in _require_list(payload.get("selected_cases"), "selected_cases")
        ],
        current_case_index=_require_int(payload, "current_case_index"),
        completed_case_reports=[
            _decode_case_report(item)
            for item in _require_list(
                payload.get("completed_case_reports"),
                "completed_case_reports",
            )
        ],
        active_case=(
            _decode_active_case(payload["active_case"])
            if payload.get("active_case") is not None
            else None
        ),
        branch_anchor_snapshots={
            key: _decode_runtime_snapshot(value)
            for key, value in _require_dict(
                payload.get("branch_anchor_snapshots", {}),
                "branch_anchor_snapshots",
            ).items()
        },
    )


def _encode_selected_case(state: SelectedCaseState) -> dict[str, object]:
    return {
        "case_id": state.case_id,
        "stop_after_index": state.stop_after_index,
    }


def _decode_selected_case(payload: object) -> SelectedCaseState:
    data = _require_dict(payload, "selected case")
    return SelectedCaseState(
        case_id=_require_str(data, "case_id"),
        stop_after_index=_optional_int(data.get("stop_after_index"), "stop_after_index"),
    )


def _encode_runtime_snapshot(snapshot: RuntimeSnapshotState) -> dict[str, object]:
    return {
        "record_indices": list(snapshot.record_indices),
        "records": [_encode_record(record) for record in snapshot.records],
        "step_bindings": {
            key: _encode_step_binding(binding)
            for key, binding in snapshot.step_bindings.items()
        },
        "retry_remaining": dict(snapshot.retry_remaining),
        "step_attempts": dict(snapshot.step_attempts),
    }


def _decode_runtime_snapshot(payload: object) -> RuntimeSnapshotState:
    data = _require_dict(payload, "runtime snapshot")
    return RuntimeSnapshotState(
        record_indices=[
            _require_int_value(value, "record index")
            for value in _require_list(data.get("record_indices"), "record_indices")
        ],
        records=[
            _decode_record(item)
            for item in _require_list(data.get("records"), "records")
        ],
        step_bindings={
            key: _decode_step_binding(value)
            for key, value in _require_dict(data.get("step_bindings"), "step_bindings").items()
        },
        retry_remaining={
            key: _require_int_value(value, f"retry remaining for {key!r}")
            for key, value in _require_dict(data.get("retry_remaining"), "retry_remaining").items()
        },
        step_attempts={
            key: _require_int_value(value, f"step attempts for {key!r}")
            for key, value in _require_dict(data.get("step_attempts", {}), "step_attempts").items()
        },
    )


def _encode_paused_step(state: PausedStepState) -> dict[str, object]:
    return {
        "node_id": state.node_id,
        "label": state.label,
        "node_index": state.node_index,
        "attempt": state.attempt,
        "ok": state.ok,
        "error": state.error,
        "failure_message": state.failure_message,
        "failure_hint": state.failure_hint,
    }


def _decode_paused_step(payload: object) -> PausedStepState:
    data = _require_dict(payload, "paused step")
    return PausedStepState(
        node_id=_require_str(data, "node_id"),
        label=_optional_str(data.get("label"), "label"),
        node_index=_require_int(data, "node_index"),
        attempt=_require_int(data, "attempt"),
        ok=_require_bool(data, "ok"),
        error=_optional_str(data.get("error"), "error"),
        failure_message=_optional_str(data.get("failure_message"), "failure_message"),
        failure_hint=_optional_str(data.get("failure_hint"), "failure_hint"),
    )


def _encode_active_case(state: ActiveCaseState) -> dict[str, object]:
    return {
        "case_id": state.case_id,
        "snapshot": _encode_runtime_snapshot(state.snapshot),
        "replay_from_index": state.replay_from_index,
        "dirty_node_id": state.dirty_node_id,
        "stop_after_index": state.stop_after_index,
        "paused_step": (
            _encode_paused_step(state.paused_step)
            if state.paused_step is not None
            else None
        ),
    }


def _decode_active_case(payload: object) -> ActiveCaseState:
    data = _require_dict(payload, "active case")
    return ActiveCaseState(
        case_id=_require_str(data, "case_id"),
        snapshot=_decode_runtime_snapshot(data.get("snapshot")),
        replay_from_index=_require_int(data, "replay_from_index"),
        dirty_node_id=_optional_str(data.get("dirty_node_id"), "dirty_node_id"),
        stop_after_index=_optional_int(data.get("stop_after_index"), "stop_after_index"),
        paused_step=(
            _decode_paused_step(data["paused_step"])
            if data.get("paused_step") is not None
            else None
        ),
    )


def _encode_step_binding(binding: StepBindingState) -> dict[str, object]:
    return {
        "args": [_encode_stored_value(value) for value in binding.args],
        "kwargs": {
            key: _encode_stored_value(value)
            for key, value in binding.kwargs.items()
        },
        "has_result": binding.has_result,
        "result": (
            _encode_stored_value(binding.result)
            if binding.result is not None
            else None
        ),
        "fn_ref": binding.fn_ref,
        "source_fingerprint": binding.source_fingerprint,
    }


def _decode_step_binding(payload: object) -> StepBindingState:
    data = _require_dict(payload, "step binding")
    return StepBindingState(
        args=tuple(
            _decode_stored_value(item)
            for item in _require_list(data.get("args"), "args")
        ),
        kwargs={
            key: _decode_stored_value(value)
            for key, value in _require_dict(data.get("kwargs"), "kwargs").items()
        },
        has_result=_require_bool(data, "has_result"),
        result=(
            _decode_stored_value(data["result"])
            if data.get("result") is not None
            else None
        ),
        fn_ref=_optional_str(data.get("fn_ref"), "fn_ref"),
        source_fingerprint=_optional_str(
            data.get("source_fingerprint"),
            "source_fingerprint",
        ),
    )


def _encode_stored_value(value: StoredValue) -> dict[str, object]:
    return {
        "kind": value.kind,
        "payload": _encode_bytes(value.payload) if value.payload is not None else None,
        "items": [_encode_stored_value(item) for item in value.items],
        "entries": [
            {
                "key": _encode_bytes(key),
                "value": _encode_stored_value(item),
            }
            for key, item in value.entries
        ],
        "type_ref": value.type_ref,
    }


def _decode_stored_value(payload: object) -> StoredValue:
    data = _require_dict(payload, "stored value")
    return StoredValue(
        kind=_require_str(data, "kind"),
        payload=(
            _decode_bytes(data["payload"], "stored value payload")
            if data.get("payload") is not None
            else None
        ),
        items=tuple(
            _decode_stored_value(item)
            for item in _require_list(data.get("items", []), "items")
        ),
        entries=tuple(
            (
                _decode_bytes(_require_dict(entry, "stored entry").get("key"), "stored entry key"),
                _decode_stored_value(_require_dict(entry, "stored entry").get("value")),
            )
            for entry in _require_list(data.get("entries", []), "entries")
        ),
        type_ref=_optional_str(data.get("type_ref"), "type_ref"),
    )


def _encode_case_report(report: CaseExecutionReport) -> dict[str, object]:
    return {
        "case_id": report.case_id,
        "branch_env": dict(report.branch_env),
        "records": [_encode_record(record) for record in report.records],
        "completed": report.completed,
        "stopped_at_label": report.stopped_at_label,
        "replay_anchor": report.replay_anchor,
    }


def _decode_case_report(payload: object) -> CaseExecutionReport:
    data = _require_dict(payload, "case report")
    return CaseExecutionReport(
        case_id=_require_str(data, "case_id"),
        branch_env={
            key: _require_str_value(value, f"branch env {key!r}")
            for key, value in _require_dict(data.get("branch_env"), "branch_env").items()
        },
        records=[
            _decode_record(item)
            for item in _require_list(data.get("records"), "records")
        ],
        completed=_require_bool(data, "completed"),
        stopped_at_label=_optional_str(data.get("stopped_at_label"), "stopped_at_label"),
        replay_anchor=_optional_str(data.get("replay_anchor"), "replay_anchor"),
    )


def _encode_record(record: NodeExecutionRecord) -> dict[str, object]:
    return {
        "node_id": record.node_id,
        "node_type": record.node_type,
        "label": record.label,
        "ok": record.ok,
        "result": _encode_pickle(record.result),
        "error": record.error,
    }


def _decode_record(payload: object) -> NodeExecutionRecord:
    data = _require_dict(payload, "node execution record")
    return NodeExecutionRecord(
        node_id=_require_str(data, "node_id"),
        node_type=_require_str(data, "node_type"),
        label=_optional_str(data.get("label"), "label"),
        ok=_require_bool(data, "ok"),
        result=_decode_pickle(data.get("result"), "record result"),
        error=_optional_str(data.get("error"), "error"),
    )


def _encode_pickle(value: object) -> dict[str, str]:
    return {
        "encoding": "pickle-base64",
        "data": _encode_bytes(pickle.dumps(value)),
    }


def _decode_pickle(payload: object, label: str) -> object:
    data = _require_dict(payload, label)
    if data.get("encoding") != "pickle-base64":
        raise ValueError(f"{label} has an unsupported encoding")
    return pickle.loads(_decode_bytes(data.get("data"), label))


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be base64 text")
    return base64.b64decode(value.encode("ascii"))


def _require_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_str(data: dict[str, object], key: str) -> str:
    return _require_str_value(data.get(key), key)


def _require_str_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    return value


def _require_int(data: dict[str, object], key: str) -> int:
    return _require_int_value(data.get(key), key)


def _require_int_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _require_int_value(value, label)


def _require_bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value
