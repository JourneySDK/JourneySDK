from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

from journeysdk import recordings


def _write_trace_zip(path: Path, *, title: str, resource_body: bytes = b"resource") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "trace.trace",
            json.dumps(
                {
                    "version": 8,
                    "type": "context-options",
                    "origin": "library",
                    "browserName": "chromium",
                    "playwrightVersion": "1.60.0",
                    "options": {"viewport": {"width": 1280, "height": 720}},
                    "platform": "test",
                    "wallTime": 1,
                    "monotonicTime": 1,
                    "sdkLanguage": "python",
                    "contextId": f"context-{title}",
                    "title": title,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "screencast-frame",
                    "pageId": f"page-{title}",
                    "sha1": "shared.jpeg",
                    "width": 1280,
                    "height": 720,
                    "timestamp": 2,
                }
            )
            + "\n",
        )
        archive.writestr(
            "trace.network",
            json.dumps(
                {
                    "type": "resource-snapshot",
                    "snapshot": {
                        "request": {"method": "GET", "url": "http://example.test"},
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "image/jpeg",
                                "_sha1": "shared.jpeg",
                            },
                        },
                    },
                }
            )
            + "\n",
        )
        archive.writestr("trace.stacks", json.dumps({"files": [], "stacks": []}))
        archive.writestr("resources/shared.jpeg", resource_body)


def _write_manifest(
    recordings_dir: Path,
    *,
    sequence: int,
    case_id: str = "case_1",
    run_id: str = "run123",
    journey_id: str = "demo_journey",
    step_name: str | None = None,
    branch_env: dict[str, str] | None = None,
) -> Path:
    step = step_name or f"step_{sequence}"
    trace_path = recordings_dir / f"{sequence:04d}-{case_id}-{step}.trace.zip"
    video_path = recordings_dir / f"{sequence:04d}-{case_id}-{step}.webm"
    _write_trace_zip(trace_path, title=step, resource_body=f"{step} image".encode())
    video_path.write_bytes(f"{step} video".encode())
    manifest_path = recordings_dir / f"{sequence:04d}-{case_id}-{step}.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "format": "journey.log_artifact",
                "version": 1,
                "kind": "browser_recording",
                "touchpoint": "browser",
                "source": "page",
                "content_type": "application/vnd.journey.browser-recording",
                "status": "success",
                "started_at": f"2026-05-28T12:00:0{sequence}Z",
                "stopped_at": f"2026-05-28T12:00:1{sequence}Z",
                "run_id": run_id,
                "sequence": sequence,
                "artifact_key": f"{sequence:04d}-{case_id}-{step}-run-{run_id}",
                "recording_key": f"{sequence:04d}-{case_id}-{step}-run-{run_id}",
                "journey_id": journey_id,
                "function_ref": f"module:{journey_id}",
                "case_id": case_id,
                "branch_env": branch_env or {},
                "step_id": f"node_{sequence}",
                "step_label": step,
                "step_name": step,
                "node_index": sequence,
                "attempt": 1,
                "context_index": 1,
                "browser": "chromium",
                "headless": True,
                "initial_url": "http://example.test/start",
                "final_url": "http://example.test/end",
                "trace_path": str(trace_path),
                "video_path": str(video_path),
                "trace_saved": True,
                "video_saved": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def test_discover_recording_cases_groups_manifests_and_skips_bad_json(tmp_path: Path):
    recordings_dir = tmp_path / "journeys" / ".journey" / "logs"
    recordings_dir.mkdir(parents=True)
    _write_manifest(recordings_dir, sequence=1, branch_env={"bg_1": "branch_1"})
    _write_manifest(recordings_dir, sequence=2, branch_env={"bg_1": "branch_1"})
    (recordings_dir / "bad.manifest.json").write_text("{", encoding="utf-8")

    result = recordings.discover_recording_cases(tmp_path)

    assert len(result.cases) == 1
    [case] = result.cases
    assert case.case_id == "case_1"
    assert case.journey_id == "demo_journey"
    assert case.branch_env == {"bg_1": "branch_1"}
    assert case.step_count == 2
    assert case.trace_count == 2
    assert case.video_count == 2
    assert len(result.warnings) == 1
    assert "Skipping unreadable Journey log manifest" in result.warnings[0]


def test_discover_recording_cases_groups_text_log_artifacts(tmp_path: Path):
    logs_dir = tmp_path / ".journey" / "logs"
    logs_dir.mkdir(parents=True)
    log_path = logs_dir / "web.log"
    log_path.write_text("server ready\nendpoint unreachable\n", encoding="utf-8")
    (logs_dir / "web.manifest.json").write_text(
        json.dumps(
            {
                "format": "journey.log_artifact",
                "version": 1,
                "kind": "docker_compose_logs",
                "touchpoint": "docker",
                "source": "web",
                "content_type": "text/plain",
                "status": "success",
                "run_id": "run123",
                "sequence": 1,
                "artifact_key": "web-run-run123",
                "journey_id": "demo_journey",
                "function_ref": "module:demo_journey",
                "case_id": "case_1",
                "branch_env": {},
                "step_id": "node_1",
                "step_label": "start_services",
                "step_name": "start_services",
                "node_index": 0,
                "attempt": 1,
                "path": str(log_path),
                "line_count": 2,
                "byte_count": log_path.stat().st_size,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = recordings.discover_recording_cases(tmp_path)

    [case] = result.cases
    assert case.case_id == "case_1"
    assert case.trace_count == 0
    assert case.log_count == 1
    [artifact] = case.log_inputs()
    assert artifact.touchpoint == "docker"
    assert artifact.source == "web"
    assert artifact.path == log_path.resolve()


def test_discover_recording_cases_sorts_cases_alphabetically(tmp_path: Path):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, case_id="case_b")
    _write_manifest(recordings_dir, sequence=2, case_id="case_a")

    result = recordings.discover_recording_cases(recordings_dir)

    assert [case.case_id for case in result.cases] == ["case_a", "case_b"]


def test_discover_recording_cases_groups_cases_into_executions(tmp_path: Path):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, case_id="case_b")
    _write_manifest(recordings_dir, sequence=2, case_id="case_a")
    _write_manifest(recordings_dir, sequence=3, case_id="case_old", run_id="oldrun")

    result = recordings.discover_recording_cases(recordings_dir)

    assert len(result.executions) == 2
    current = [
        execution for execution in result.executions if execution.run_id == "run123"
    ][0]
    assert current.case_count == 2
    assert current.step_count == 2
    assert current.trace_count == 2
    assert current.video_count == 2
    assert [manifest.sequence for manifest in current.manifests] == [1, 2]


def test_ensure_case_trace_merges_playwright_streams_and_reuses_current_artifact(
    tmp_path: Path,
):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, step_name="first")
    _write_manifest(recordings_dir, sequence=2, step_name="second")
    [case] = recordings.discover_recording_cases(recordings_dir).cases

    created = recordings.ensure_case_trace(case)
    reused = recordings.ensure_case_trace(case)

    assert created.created is True
    assert reused.created is False
    assert created.path == reused.path
    assert created.path.name == "demo_journey-case_1-run-run123.trace.zip"
    with zipfile.ZipFile(created.path) as archive:
        names = set(archive.namelist())
        assert "0001-first-attempt-1-context-1.trace" in names
        assert "0001-first-attempt-1-context-1.network" in names
        assert "0002-second-attempt-1-context-1.trace" in names
        assert "0002-second-attempt-1-context-1.network" in names
        assert "resources/shared.jpeg" in names
        assert "resources/0002-second-attempt-1-context-1-shared.jpeg" in names
        second_trace = archive.read("0002-second-attempt-1-context-1.trace").decode()
        assert "0002-second-attempt-1-context-1-shared.jpeg" in second_trace

    manifest = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "journey.case_recording"
    assert manifest["kind"] == "trace"
    assert len(manifest["sources"]) == 2


def test_ensure_execution_trace_merges_all_cases_in_sequence(tmp_path: Path):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, case_id="case_b", step_name="first")
    _write_manifest(recordings_dir, sequence=2, case_id="case_a", step_name="second")
    [execution] = recordings.discover_recording_cases(recordings_dir).executions

    created = recordings.ensure_execution_trace(execution)
    reused = recordings.ensure_execution_trace(execution)

    assert created.created is True
    assert reused.created is False
    assert created.path == reused.path
    assert created.path.name == "demo_journey-run-run123.trace.zip"
    with zipfile.ZipFile(created.path) as archive:
        names = set(archive.namelist())
        assert "0001-first-attempt-1-context-1.trace" in names
        assert "0002-second-attempt-1-context-1.trace" in names

    manifest = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "journey.execution_recording"
    assert manifest["kind"] == "trace"
    assert manifest["case_ids"] == ["case_b", "case_a"]
    assert len(manifest["sources"]) == 2


def test_ensure_case_trace_regenerates_stale_artifact(tmp_path: Path):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, step_name="first")
    [case] = recordings.discover_recording_cases(recordings_dir).cases
    first = recordings.ensure_case_trace(case)
    first.path.write_text("stale", encoding="utf-8")

    trace_input = case.trace_inputs()[0].trace_path
    assert trace_input is not None
    with zipfile.ZipFile(trace_input, "a") as archive:
        archive.writestr("resources/new.dat", b"new")
    refreshed_case = recordings.discover_recording_cases(recordings_dir).cases[0]
    second = recordings.ensure_case_trace(refreshed_case)

    assert second.created is True
    assert second.path.read_bytes() != b"stale"


def test_ensure_case_video_uses_ffmpeg_copy_then_reencode_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, step_name="first")
    _write_manifest(recordings_dir, sequence=2, step_name="second")
    [case] = recordings.discover_recording_cases(recordings_dir).cases
    monkeypatch.setitem(
        __import__("sys").modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "/fake/ffmpeg"),
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, capture_output: bool, text: bool):
        del capture_output, text
        calls.append(cmd)
        if "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy":
            return subprocess.CompletedProcess(cmd, 1, stderr="copy failed")
        assert cmd[-1].endswith(".webm")
        Path(cmd[-1]).write_bytes(b"merged video")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(recordings.subprocess, "run", fake_run)

    artifact = recordings.ensure_case_video(case)
    reused = recordings.ensure_case_video(case)

    assert artifact.created is True
    assert reused.created is False
    assert artifact.path.read_bytes() == b"merged video"
    assert calls[0][calls[0].index("-c") + 1] == "copy"
    assert calls[1][calls[1].index("-c:v") + 1] == "libvpx-vp9"


def test_ensure_execution_video_merges_all_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    recordings_dir = tmp_path / ".journey" / "logs"
    _write_manifest(recordings_dir, sequence=1, case_id="case_1", step_name="first")
    _write_manifest(recordings_dir, sequence=2, case_id="case_2", step_name="second")
    [execution] = recordings.discover_recording_cases(recordings_dir).executions
    monkeypatch.setitem(
        __import__("sys").modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "/fake/ffmpeg"),
    )
    concat_inputs: list[str] = []

    def fake_run(cmd: list[str], *, capture_output: bool, text: bool):
        del capture_output, text
        concat_path = Path(cmd[cmd.index("-i") + 1])
        concat_inputs.append(concat_path.read_text(encoding="utf-8"))
        Path(cmd[-1]).write_bytes(b"merged execution video")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(recordings.subprocess, "run", fake_run)

    artifact = recordings.ensure_execution_video(execution)
    reused = recordings.ensure_execution_video(execution)

    assert artifact.created is True
    assert reused.created is False
    assert artifact.path.name == "demo_journey-run-run123.video.webm"
    assert artifact.path.read_bytes() == b"merged execution video"
    assert "0001-case_1-first.webm" in concat_inputs[0]
    assert "0002-case_2-second.webm" in concat_inputs[0]
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "journey.execution_recording"
    assert manifest["kind"] == "video"
