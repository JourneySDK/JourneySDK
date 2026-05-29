"""Browser recording discovery and case-level artifact helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import zipfile

from .state import DEFAULT_STATE_DIR


_CASE_ARTIFACT_FORMAT = "journey.case_recording"
_EXECUTION_ARTIFACT_FORMAT = "journey.execution_recording"
_BROWSER_RECORDING_FORMAT = "journey.browser_recording"
_RECORDING_SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_SLUG_RE = re.compile(r"[^A-Za-z0-9_.+-]+")


class RecordingError(RuntimeError):
    """Raised when Journey cannot discover, merge, or open a recording artifact."""


@dataclass(frozen=True)
class RecordingManifest:
    manifest_path: Path
    recordings_dir: Path
    run_id: str
    sequence: int
    journey_id: str
    function_ref: str
    case_id: str
    branch_env: dict[str, str]
    step_id: str
    step_label: str | None
    step_name: str
    attempt: int
    context_index: int
    status: str
    started_at: str | None
    stopped_at: str | None
    trace_path: Path | None
    video_path: Path | None
    trace_saved: bool
    video_saved: bool


@dataclass(frozen=True)
class CaseRecording:
    recordings_dir: Path
    run_id: str
    journey_id: str
    function_ref: str
    case_id: str
    branch_env: dict[str, str]
    manifests: tuple[RecordingManifest, ...]

    @property
    def step_count(self) -> int:
        return len({manifest.step_id for manifest in self.manifests})

    @property
    def trace_count(self) -> int:
        return len(self.trace_inputs())

    @property
    def video_count(self) -> int:
        return len(self.video_inputs())

    @property
    def started_at(self) -> str | None:
        values = [manifest.started_at for manifest in self.manifests if manifest.started_at]
        return min(values) if values else None

    @property
    def stopped_at(self) -> str | None:
        values = [manifest.stopped_at for manifest in self.manifests if manifest.stopped_at]
        return max(values) if values else None

    def branch_summary(self) -> str:
        if not self.branch_env:
            return "{}"
        return "{" + ", ".join(
            f"{key}={value}" for key, value in sorted(self.branch_env.items())
        ) + "}"

    def trace_inputs(self) -> tuple[RecordingManifest, ...]:
        return tuple(
            manifest
            for manifest in self.manifests
            if manifest.trace_saved
            and manifest.trace_path is not None
            and manifest.trace_path.exists()
        )

    def video_inputs(self) -> tuple[RecordingManifest, ...]:
        return tuple(
            manifest
            for manifest in self.manifests
            if manifest.video_saved
            and manifest.video_path is not None
            and manifest.video_path.exists()
        )


@dataclass(frozen=True)
class ExecutionRecording:
    recordings_dir: Path
    run_id: str
    journey_id: str
    function_ref: str
    cases: tuple[CaseRecording, ...]

    @property
    def case_count(self) -> int:
        return len(self.cases)

    @property
    def step_count(self) -> int:
        return sum(case.step_count for case in self.cases)

    @property
    def trace_count(self) -> int:
        return len(self.trace_inputs())

    @property
    def video_count(self) -> int:
        return len(self.video_inputs())

    @property
    def started_at(self) -> str | None:
        values = [case.started_at for case in self.cases if case.started_at]
        return min(values) if values else None

    @property
    def stopped_at(self) -> str | None:
        values = [case.stopped_at for case in self.cases if case.stopped_at]
        return max(values) if values else None

    @property
    def manifests(self) -> tuple[RecordingManifest, ...]:
        return tuple(
            sorted(
                (manifest for case in self.cases for manifest in case.manifests),
                key=lambda manifest: manifest.sequence,
            )
        )

    def trace_inputs(self) -> tuple[RecordingManifest, ...]:
        return tuple(
            manifest
            for manifest in self.manifests
            if manifest.trace_saved
            and manifest.trace_path is not None
            and manifest.trace_path.exists()
        )

    def video_inputs(self) -> tuple[RecordingManifest, ...]:
        return tuple(
            manifest
            for manifest in self.manifests
            if manifest.video_saved
            and manifest.video_path is not None
            and manifest.video_path.exists()
        )


@dataclass(frozen=True)
class RecordingDiscoveryResult:
    cases: tuple[CaseRecording, ...]
    warnings: tuple[str, ...]
    executions: tuple[ExecutionRecording, ...] = ()


@dataclass(frozen=True)
class RecordingArtifact:
    path: Path
    manifest_path: Path
    created: bool


def discover_recording_cases(root: str | Path) -> RecordingDiscoveryResult:
    """Discover browser recording manifests grouped into executed cases."""

    root_path = Path(root).expanduser().resolve()
    warnings: list[str] = []
    manifests: list[RecordingManifest] = []
    for recordings_dir in _recording_dirs(root_path):
        for manifest_path in sorted(recordings_dir.glob("*.manifest.json")):
            try:
                manifests.append(_load_recording_manifest(manifest_path))
            except RecordingError as exc:
                warnings.append(str(exc))

    groups: dict[
        tuple[Path, str, str, str, tuple[tuple[str, str], ...]],
        list[RecordingManifest],
    ] = {}
    for manifest in manifests:
        key = (
            manifest.recordings_dir,
            manifest.run_id,
            manifest.journey_id,
            manifest.case_id,
            tuple(sorted(manifest.branch_env.items())),
        )
        groups.setdefault(key, []).append(manifest)

    cases = [
        CaseRecording(
            recordings_dir=recordings_dir,
            run_id=run_id,
            journey_id=journey_id,
            function_ref=items[0].function_ref,
            case_id=case_id,
            branch_env=dict(branch_items),
            manifests=tuple(sorted(items, key=lambda item: item.sequence)),
        )
        for (
            recordings_dir,
            run_id,
            journey_id,
            case_id,
            branch_items,
        ), items in groups.items()
    ]
    cases.sort(
        key=lambda case: (
            case.journey_id,
            case.case_id,
            case.branch_summary(),
            case.run_id,
            case.recordings_dir.as_posix(),
        ),
    )
    case_tuple = tuple(cases)
    return RecordingDiscoveryResult(
        cases=case_tuple,
        warnings=tuple(warnings),
        executions=group_execution_recordings(case_tuple),
    )


def group_execution_recordings(
    cases: tuple[CaseRecording, ...],
) -> tuple[ExecutionRecording, ...]:
    """Group discovered cases into whole execution recordings."""

    groups: dict[tuple[Path, str, str, str], list[CaseRecording]] = {}
    for case in cases:
        key = (
            case.recordings_dir,
            case.run_id,
            case.journey_id,
            case.function_ref,
        )
        groups.setdefault(key, []).append(case)

    executions = [
        ExecutionRecording(
            recordings_dir=recordings_dir,
            run_id=run_id,
            journey_id=journey_id,
            function_ref=function_ref,
            cases=tuple(
                sorted(
                    items,
                    key=lambda case: (
                        case.started_at or "",
                        case.case_id,
                        case.branch_summary(),
                    ),
                )
            ),
        )
        for (recordings_dir, run_id, journey_id, function_ref), items in groups.items()
    ]
    executions.sort(
        key=lambda execution: (
            execution.journey_id,
            execution.started_at or "",
            execution.run_id,
            execution.recordings_dir.as_posix(),
        ),
    )
    return tuple(executions)


def ensure_case_trace(case: CaseRecording) -> RecordingArtifact:
    """Create or reuse one Playwright-compatible trace archive for a case."""

    inputs = case.trace_inputs()
    if not inputs:
        raise RecordingError(f"No saved trace files were found for {case.case_id}.")

    artifact_path, manifest_path = _artifact_paths(case, "trace", ".zip")
    source_fingerprint = _source_fingerprint(
        manifest.trace_path for manifest in inputs if manifest.trace_path is not None
    )
    if _artifact_is_current(
        artifact_path,
        manifest_path,
        case=case,
        kind="trace",
        sources=source_fingerprint,
    ):
        return RecordingArtifact(artifact_path, manifest_path, created=False)

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{artifact_path.stem}.",
        suffix=".tmp",
        dir=artifact_path.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _merge_trace_archives(inputs, tmp_path)
        tmp_path.replace(artifact_path)
        _write_artifact_manifest(
            manifest_path,
            artifact_path=artifact_path,
            case=case,
            kind="trace",
            sources=source_fingerprint,
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return RecordingArtifact(artifact_path, manifest_path, created=True)


def ensure_case_video(case: CaseRecording) -> RecordingArtifact:
    """Create or reuse one merged WebM video for a case."""

    inputs = case.video_inputs()
    if not inputs:
        raise RecordingError(f"No saved video files were found for {case.case_id}.")

    artifact_path, manifest_path = _artifact_paths(case, "video", ".webm")
    source_fingerprint = _source_fingerprint(
        manifest.video_path for manifest in inputs if manifest.video_path is not None
    )
    if _artifact_is_current(
        artifact_path,
        manifest_path,
        case=case,
        kind="video",
        sources=source_fingerprint,
    ):
        return RecordingArtifact(artifact_path, manifest_path, created=False)

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = artifact_path.with_name(f".{artifact_path.stem}.{os.getpid()}.tmp.webm")
    try:
        if len(inputs) == 1:
            source = inputs[0].video_path
            if source is None:
                raise RecordingError(f"No saved video file was found for {case.case_id}.")
            shutil.copyfile(source, tmp_path)
        else:
            _merge_webm_videos(
                [
                    manifest.video_path
                    for manifest in inputs
                    if manifest.video_path is not None
                ],
                tmp_path,
            )
        tmp_path.replace(artifact_path)
        _write_artifact_manifest(
            manifest_path,
            artifact_path=artifact_path,
            case=case,
            kind="video",
            sources=source_fingerprint,
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return RecordingArtifact(artifact_path, manifest_path, created=True)


def ensure_execution_trace(execution: ExecutionRecording) -> RecordingArtifact:
    """Create or reuse one Playwright-compatible trace archive for all cases in a run."""

    inputs = execution.trace_inputs()
    if not inputs:
        raise RecordingError(
            f"No saved trace files were found for run {execution.run_id}."
        )

    artifact_path, manifest_path = _execution_artifact_paths(execution, "trace", ".zip")
    source_fingerprint = _source_fingerprint(
        manifest.trace_path for manifest in inputs if manifest.trace_path is not None
    )
    if _execution_artifact_is_current(
        artifact_path,
        manifest_path,
        execution=execution,
        kind="trace",
        sources=source_fingerprint,
    ):
        return RecordingArtifact(artifact_path, manifest_path, created=False)

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{artifact_path.stem}.",
        suffix=".tmp",
        dir=artifact_path.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _merge_trace_archives(inputs, tmp_path)
        tmp_path.replace(artifact_path)
        _write_execution_artifact_manifest(
            manifest_path,
            artifact_path=artifact_path,
            execution=execution,
            kind="trace",
            sources=source_fingerprint,
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return RecordingArtifact(artifact_path, manifest_path, created=True)


def ensure_execution_video(execution: ExecutionRecording) -> RecordingArtifact:
    """Create or reuse one merged WebM video for all cases in a run."""

    inputs = execution.video_inputs()
    if not inputs:
        raise RecordingError(
            f"No saved video files were found for run {execution.run_id}."
        )

    artifact_path, manifest_path = _execution_artifact_paths(execution, "video", ".webm")
    source_fingerprint = _source_fingerprint(
        manifest.video_path for manifest in inputs if manifest.video_path is not None
    )
    if _execution_artifact_is_current(
        artifact_path,
        manifest_path,
        execution=execution,
        kind="video",
        sources=source_fingerprint,
    ):
        return RecordingArtifact(artifact_path, manifest_path, created=False)

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = artifact_path.with_name(f".{artifact_path.stem}.{os.getpid()}.tmp.webm")
    try:
        if len(inputs) == 1:
            source = inputs[0].video_path
            if source is None:
                raise RecordingError(
                    f"No saved video file was found for run {execution.run_id}."
                )
            shutil.copyfile(source, tmp_path)
        else:
            _merge_webm_videos(
                [manifest.video_path for manifest in inputs if manifest.video_path is not None],
                tmp_path,
            )
        tmp_path.replace(artifact_path)
        _write_execution_artifact_manifest(
            manifest_path,
            artifact_path=artifact_path,
            execution=execution,
            kind="video",
            sources=source_fingerprint,
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return RecordingArtifact(artifact_path, manifest_path, created=True)


def open_trace_viewer(trace_path: str | Path) -> None:
    """Open a Playwright trace archive with the active Python environment."""

    path = Path(trace_path)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "show-trace", str(path)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RecordingError(
            f"Could not open Playwright Trace Viewer for '{path}': exit code {exc.returncode}."
        ) from exc
    except OSError as exc:
        raise RecordingError(
            f"Could not open Playwright Trace Viewer for '{path}': {exc}."
        ) from exc


def open_video_recording(video_path: str | Path) -> None:
    """Open a merged video recording with the host OS viewer."""

    path = Path(video_path)
    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif os.name == "nt":
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            raise RecordingError(f"Could not open video recording '{path}': {exc}.") from exc
        return
    else:
        command = ["xdg-open", str(path)]

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RecordingError(
            f"Could not open video recording '{path}': exit code {exc.returncode}."
        ) from exc
    except OSError as exc:
        raise RecordingError(f"Could not open video recording '{path}': {exc}.") from exc


def _recording_dirs(root: Path) -> tuple[Path, ...]:
    if root.name == "recordings" and root.parent.name == DEFAULT_STATE_DIR:
        return (root,)
    if root.name == DEFAULT_STATE_DIR and (root / "recordings").is_dir():
        return (root / "recordings",)

    discovered: list[Path] = []
    if not root.is_dir():
        return ()
    for current_root, dirnames, _filenames in os.walk(root):
        dirnames[:] = [
            dirname for dirname in dirnames if dirname not in _RECORDING_SKIP_DIRS
        ]
        current = Path(current_root)
        if current.name != DEFAULT_STATE_DIR:
            continue
        recordings_dir = current / "recordings"
        if recordings_dir.is_dir():
            discovered.append(recordings_dir.resolve())
            dirnames[:] = [dirname for dirname in dirnames if dirname != "recordings"]
    return tuple(sorted(dict.fromkeys(discovered)))


def _load_recording_manifest(manifest_path: Path) -> RecordingManifest:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecordingError(
            f"Skipping unreadable browser recording manifest '{manifest_path}': {exc}."
        ) from exc
    if not isinstance(payload, Mapping):
        raise RecordingError(
            f"Skipping invalid browser recording manifest '{manifest_path}': expected a JSON object."
        )
    if payload.get("format") != _BROWSER_RECORDING_FORMAT:
        raise RecordingError(
            f"Skipping unsupported browser recording manifest '{manifest_path}'."
        )

    recordings_dir = manifest_path.parent.resolve()
    try:
        run_id = _required_str(payload, "run_id")
        sequence = _required_int(payload, "sequence")
        journey_id = _required_str(payload, "journey_id")
        function_ref = _required_str(payload, "function_ref")
        case_id = _required_str(payload, "case_id")
        step_id = _required_str(payload, "step_id")
        step_name = _required_str(payload, "step_name")
        attempt = _required_int(payload, "attempt")
        context_index = _required_int(payload, "context_index")
    except RecordingError as exc:
        raise RecordingError(
            f"Skipping incomplete browser recording manifest '{manifest_path}': {exc}"
        ) from exc

    branch_env = payload.get("branch_env", {})
    if not isinstance(branch_env, Mapping):
        raise RecordingError(
            f"Skipping incomplete browser recording manifest '{manifest_path}': branch_env must be an object."
        )

    return RecordingManifest(
        manifest_path=manifest_path.resolve(),
        recordings_dir=recordings_dir,
        run_id=run_id,
        sequence=sequence,
        journey_id=journey_id,
        function_ref=function_ref,
        case_id=case_id,
        branch_env={str(key): str(value) for key, value in branch_env.items()},
        step_id=step_id,
        step_label=_optional_str(payload.get("step_label")),
        step_name=step_name,
        attempt=attempt,
        context_index=context_index,
        status=_optional_str(payload.get("status")) or "unknown",
        started_at=_optional_str(payload.get("started_at")),
        stopped_at=_optional_str(payload.get("stopped_at")),
        trace_path=_optional_path(payload.get("trace_path"), base=recordings_dir),
        video_path=_optional_path(payload.get("video_path"), base=recordings_dir),
        trace_saved=bool(payload.get("trace_saved")),
        video_saved=bool(payload.get("video_saved")),
    )


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RecordingError(f"{key} must be a non-empty string.")
    return value


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise RecordingError(f"{key} must be an integer.")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_path(value: object, *, base: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _artifact_paths(
    case: CaseRecording,
    kind: str,
    suffix: str,
) -> tuple[Path, Path]:
    stem = (
        f"{_slug(case.journey_id, fallback='journey')}-"
        f"{_slug(case.case_id, fallback='case')}-"
        f"run-{_slug(case.run_id, fallback='run')}"
    )
    cases_dir = case.recordings_dir / "cases"
    artifact_path = cases_dir / f"{stem}.{kind}{suffix}"
    return artifact_path, cases_dir / f"{stem}.{kind}.manifest.json"


def _execution_artifact_paths(
    execution: ExecutionRecording,
    kind: str,
    suffix: str,
) -> tuple[Path, Path]:
    stem = (
        f"{_slug(execution.journey_id, fallback='journey')}-"
        f"run-{_slug(execution.run_id, fallback='run')}"
    )
    executions_dir = execution.recordings_dir / "executions"
    artifact_path = executions_dir / f"{stem}.{kind}{suffix}"
    return artifact_path, executions_dir / f"{stem}.{kind}.manifest.json"


def _slug(value: object, *, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", str(value)).strip("-._")
    return (slug or fallback)[:96]


def _source_fingerprint(paths: Any) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    for path_value in paths:
        path = Path(path_value)
        stat = path.stat()
        sources.append(
            {
                "path": str(path.resolve()),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
        )
    return sources


def _artifact_is_current(
    artifact_path: Path,
    manifest_path: Path,
    *,
    case: CaseRecording,
    kind: str,
    sources: list[dict[str, object]],
) -> bool:
    if not artifact_path.exists() or not manifest_path.exists():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        isinstance(payload, Mapping)
        and payload.get("format") == _CASE_ARTIFACT_FORMAT
        and payload.get("version") == 1
        and payload.get("kind") == kind
        and payload.get("run_id") == case.run_id
        and payload.get("journey_id") == case.journey_id
        and payload.get("case_id") == case.case_id
        and payload.get("branch_env") == case.branch_env
        and payload.get("sources") == sources
    )


def _write_artifact_manifest(
    manifest_path: Path,
    *,
    artifact_path: Path,
    case: CaseRecording,
    kind: str,
    sources: list[dict[str, object]],
) -> None:
    manifest_path.write_text(
        json.dumps(
            {
                "format": _CASE_ARTIFACT_FORMAT,
                "version": 1,
                "kind": kind,
                "output_path": str(artifact_path.resolve()),
                "recordings_dir": str(case.recordings_dir),
                "run_id": case.run_id,
                "journey_id": case.journey_id,
                "function_ref": case.function_ref,
                "case_id": case.case_id,
                "branch_env": case.branch_env,
                "started_at": case.started_at,
                "stopped_at": case.stopped_at,
                "source_manifests": [
                    str(manifest.manifest_path) for manifest in case.manifests
                ],
                "sources": sources,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _execution_artifact_is_current(
    artifact_path: Path,
    manifest_path: Path,
    *,
    execution: ExecutionRecording,
    kind: str,
    sources: list[dict[str, object]],
) -> bool:
    if not artifact_path.exists() or not manifest_path.exists():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        isinstance(payload, Mapping)
        and payload.get("format") == _EXECUTION_ARTIFACT_FORMAT
        and payload.get("version") == 1
        and payload.get("kind") == kind
        and payload.get("run_id") == execution.run_id
        and payload.get("journey_id") == execution.journey_id
        and payload.get("function_ref") == execution.function_ref
        and payload.get("case_ids") == [case.case_id for case in execution.cases]
        and payload.get("sources") == sources
    )


def _write_execution_artifact_manifest(
    manifest_path: Path,
    *,
    artifact_path: Path,
    execution: ExecutionRecording,
    kind: str,
    sources: list[dict[str, object]],
) -> None:
    manifest_path.write_text(
        json.dumps(
            {
                "format": _EXECUTION_ARTIFACT_FORMAT,
                "version": 1,
                "kind": kind,
                "output_path": str(artifact_path.resolve()),
                "recordings_dir": str(execution.recordings_dir),
                "run_id": execution.run_id,
                "journey_id": execution.journey_id,
                "function_ref": execution.function_ref,
                "case_ids": [case.case_id for case in execution.cases],
                "cases": [
                    {
                        "case_id": case.case_id,
                        "branch_env": case.branch_env,
                        "source_manifests": [
                            str(manifest.manifest_path) for manifest in case.manifests
                        ],
                    }
                    for case in execution.cases
                ],
                "started_at": execution.started_at,
                "stopped_at": execution.stopped_at,
                "source_manifests": [
                    str(manifest.manifest_path) for manifest in execution.manifests
                ],
                "sources": sources,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _merge_trace_archives(
    manifests: tuple[RecordingManifest, ...],
    output_path: Path,
) -> None:
    written_entries: set[str] = set()
    resource_payloads: dict[str, bytes] = {}
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for index, manifest in enumerate(manifests, start=1):
            if manifest.trace_path is None:
                continue
            ordinal = (
                f"{index:04d}-"
                f"{_slug(manifest.step_name, fallback='step')}-"
                f"attempt-{manifest.attempt}-"
                f"context-{manifest.context_index}"
            )
            with zipfile.ZipFile(manifest.trace_path) as source:
                source_names = set(source.namelist())
                if "trace.trace" not in source_names:
                    raise RecordingError(
                        f"Trace archive '{manifest.trace_path}' does not contain trace.trace."
                    )
                resource_remap = _write_trace_resources(
                    source,
                    output,
                    ordinal=ordinal,
                    written_entries=written_entries,
                    resource_payloads=resource_payloads,
                )
                for source_name, extension in (
                    ("trace.trace", "trace"),
                    ("trace.network", "network"),
                    ("trace.stacks", "stacks"),
                ):
                    if source_name not in source_names:
                        continue
                    data = source.read(source_name)
                    if any(old != new for old, new in resource_remap.items()):
                        data = _rewrite_resource_refs(data, resource_remap)
                    output.writestr(f"{ordinal}.{extension}", data)


def _write_trace_resources(
    source: zipfile.ZipFile,
    output: zipfile.ZipFile,
    *,
    ordinal: str,
    written_entries: set[str],
    resource_payloads: dict[str, bytes],
) -> dict[str, str]:
    resource_remap: dict[str, str] = {}
    for entry in source.infolist():
        if not entry.filename.startswith("resources/") or entry.is_dir():
            continue
        resource_name = entry.filename.removeprefix("resources/")
        dest_name = entry.filename
        dest_resource_name = resource_name
        data = source.read(entry.filename)
        existing = resource_payloads.get(dest_name)
        if existing is not None:
            if existing == data:
                resource_remap[resource_name] = dest_resource_name
                continue
            dest_resource_name = f"{ordinal}-{resource_name}"
            dest_name = f"resources/{dest_resource_name}"
            counter = 2
            while dest_name in resource_payloads and resource_payloads[dest_name] != data:
                dest_resource_name = f"{ordinal}-{counter}-{resource_name}"
                dest_name = f"resources/{dest_resource_name}"
                counter += 1
        resource_payloads[dest_name] = data
        resource_remap[resource_name] = dest_resource_name
        if dest_name not in written_entries:
            output.writestr(dest_name, data)
            written_entries.add(dest_name)
    return resource_remap


def _rewrite_resource_refs(data: bytes, resource_remap: dict[str, str]) -> bytes:
    text = data.decode("utf-8")
    output_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        suffix = "\n" if line.endswith("\n") else ""
        body = line[:-1] if suffix else line
        if not body:
            output_lines.append(line)
            continue
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            output_lines.append(_replace_resource_strings(body, resource_remap) + suffix)
            continue
        output_lines.append(
            json.dumps(_rewrite_json_value(value, resource_remap), separators=(",", ":"))
            + suffix
        )
    return "".join(output_lines).encode("utf-8")


def _rewrite_json_value(value: object, resource_remap: dict[str, str]) -> object:
    if isinstance(value, str):
        return resource_remap.get(value, value)
    if isinstance(value, list):
        return [_rewrite_json_value(item, resource_remap) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_json_value(item, resource_remap)
            for key, item in value.items()
        }
    return value


def _replace_resource_strings(text: str, resource_remap: dict[str, str]) -> str:
    for old, new in resource_remap.items():
        if old != new:
            text = text.replace(old, new)
    return text


def _merge_webm_videos(paths: list[Path], output_path: Path) -> None:
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RecordingError(
            "Merged video recordings require the imageio-ffmpeg package."
        ) from exc

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="journey-video-merge.") as tmp_dir:
        concat_file = Path(tmp_dir) / "inputs.txt"
        concat_file.write_text(
            "".join(f"file '{_ffmpeg_quote(path)}'\n" for path in paths),
            encoding="utf-8",
        )
        copy_cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ]
        copy_result = subprocess.run(copy_cmd, capture_output=True, text=True)
        if copy_result.returncode == 0:
            return

        failures = [_format_ffmpeg_failure("concat copy", copy_result)]
        for codec in ("libvpx-vp9", "libvpx"):
            output_path.unlink(missing_ok=True)
            reencode_cmd = [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c:v",
                codec,
                "-b:v",
                "0",
                "-crf",
                "35",
                "-an",
                str(output_path),
            ]
            result = subprocess.run(reencode_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return
            failures.append(_format_ffmpeg_failure(f"re-encode with {codec}", result))

    raise RecordingError(
        "Could not merge WebM recordings with ffmpeg.\n" + "\n".join(failures)
    )


def _ffmpeg_quote(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _format_ffmpeg_failure(label: str, result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or "<no output>"
    return f"{label} failed with exit code {result.returncode}: {detail}"
