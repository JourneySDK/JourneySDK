from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from journeysdk.cli import main
from journeysdk.logger import configure_logging
from journeysdk.recordings import (
    CaseRecording,
    LogArtifactManifest,
    RecordingDiscoveryResult,
    RecordingManifest,
)


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging("info")
    yield
    configure_logging("info")


def _log_artifact(
    tmp_path: Path,
    *,
    sequence: int,
    text: str,
    case_id: str = "case_1",
    branch_env: dict[str, str] | None = None,
    step_id: str = "node_1",
    step_label: str = "start_services",
    step_name: str = "start_services",
    touchpoint: str = "docker",
    source: str = "web",
) -> LogArtifactManifest:
    log_path = tmp_path / ".journey" / "logs" / f"{sequence:04d}-{source}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text, encoding="utf-8")
    return LogArtifactManifest(
        manifest_path=log_path.with_suffix(".manifest.json"),
        logs_dir=log_path.parent,
        run_id="run123",
        sequence=sequence,
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id=case_id,
        branch_env=branch_env or {},
        step_id=step_id,
        step_label=step_label,
        step_name=step_name,
        node_index=sequence,
        attempt=1,
        kind="docker_compose_logs",
        touchpoint=touchpoint,
        source=source,
        content_type="text/plain",
        status="success",
        started_at=None,
        stopped_at=None,
        path=log_path,
        line_count=len(text.splitlines()),
        byte_count=log_path.stat().st_size,
    )


def _browser_manifest(
    tmp_path: Path,
    *,
    sequence: int,
    case_id: str = "case_1",
    branch_env: dict[str, str] | None = None,
    step_id: str = "node_1",
    step_label: str = "start_services",
    step_name: str = "start_services",
) -> RecordingManifest:
    logs_dir = tmp_path / ".journey" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    trace_path = logs_dir / f"{sequence:04d}-{step_name}.trace.zip"
    video_path = logs_dir / f"{sequence:04d}-{step_name}.webm"
    trace_path.write_bytes(b"trace")
    video_path.write_bytes(b"video")
    return RecordingManifest(
        manifest_path=logs_dir / f"{sequence:04d}-{step_name}.manifest.json",
        recordings_dir=logs_dir,
        run_id="run123",
        sequence=sequence,
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id=case_id,
        branch_env=branch_env or {},
        step_id=step_id,
        step_label=step_label,
        step_name=step_name,
        attempt=1,
        context_index=1,
        status="success",
        started_at=None,
        stopped_at=None,
        trace_path=trace_path,
        video_path=video_path,
        trace_saved=True,
        video_saved=True,
    )


def test_evidence_command_interactively_opens_selected_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={"bg_1": "branch_1"},
        manifests=(),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ("skipped bad manifest",)),
    )
    opened: list[str] = []
    monkeypatch.setattr(
        "journeysdk.cli._open_case_trace",
        lambda selected: opened.append(selected.case_id),
    )
    prompts = iter(["1", "t", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert opened == ["case_1"]
    assert "Logs" in output
    assert "case_1" in output
    assert "skipped bad manifest" in output


def test_evidence_command_interactively_opens_all_cases_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    cases = (
        CaseRecording(
            recordings_dir=tmp_path / ".journey" / "logs",
            run_id="run123",
            journey_id="demo_journey",
            function_ref="module:demo_journey",
            case_id="case_1",
            branch_env={},
            manifests=(),
        ),
        CaseRecording(
            recordings_dir=tmp_path / ".journey" / "logs",
            run_id="run123",
            journey_id="demo_journey",
            function_ref="module:demo_journey",
            case_id="case_2",
            branch_env={"bg_1": "branch_1"},
            manifests=(),
        ),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult(cases, ()),
    )
    opened: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        "journeysdk.cli._open_execution_trace",
        lambda selected: opened.append(
            (selected.run_id, tuple(case.case_id for case in selected.cases))
        ),
    )
    prompts = iter(["a", "t", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert opened == [("run123", ("case_1", "case_2"))]
    assert "a. all cases" in output


def test_evidence_command_reports_missing_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((), ()),
    )

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "No Journey logs found" in output
    assert "What happened:" in output
    assert "Try this:" in output
    assert "Next commands:" in output
    assert "journey evidence --help" in output


def test_evidence_command_invalid_branch_filter_is_instructional(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    exit_code = main(["evidence", "--dir", str(tmp_path), "--branch", "nope", "--list"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Traceback" not in output
    assert "--branch expects KEY=VALUE." in output
    assert "What happened:" in output
    assert "Try this:" in output
    assert "Next commands:" in output
    assert "journey evidence --list-scopes" in output


def test_evidence_command_noninteractive_zero_match_is_instructional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(
            _log_artifact(
                tmp_path,
                sequence=1,
                text="ready\n",
                step_label="start_services",
            ),
        ),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    exit_code = main(["evidence", "--dir", str(tmp_path), "--show", "--case", "missing"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "No Journey logs matched filters: --case missing." in output
    assert "Try this:" in output
    assert "Next commands:" in output
    assert "journey evidence --list-scopes" in output


def test_evidence_command_noninteractively_shows_filtered_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    artifact = _log_artifact(
        tmp_path,
        sequence=1,
        text="ready\nerror endpoint unreachable\n",
    )
    unrelated = _log_artifact(
        tmp_path,
        sequence=2,
        text="browser endpoint noise\n",
        touchpoint="browser",
        source="page",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(artifact, unrelated),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--show",
            "--case",
            "case_1",
            "--touchpoint",
            "docker",
            "--grep",
            "endpoint",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "error endpoint unreachable" in output
    assert "ready" not in output
    assert "browser endpoint noise" not in output


@pytest.mark.parametrize("step_filter", ["node_1", "start_services", "service_setup"])
def test_evidence_command_noninteractively_matches_step_id_label_or_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    step_filter: str,
):
    artifact = _log_artifact(
        tmp_path,
        sequence=1,
        text="matched service log\n",
        step_id="node_1",
        step_label="start_services",
        step_name="service_setup",
    )
    unrelated = _log_artifact(
        tmp_path,
        sequence=2,
        text="unrelated service log\n",
        step_id="node_2",
        step_label="other_step",
        step_name="other_step",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(artifact, unrelated),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--show",
            "--case",
            "case_1",
            "--step",
            step_filter,
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "matched service log" in output
    assert "unrelated service log" not in output


def test_evidence_command_paths_filter_browser_artifacts_by_step_and_touchpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    first = _browser_manifest(
        tmp_path,
        sequence=1,
        step_id="node_1",
        step_label="first_step",
        step_name="first_step",
    )
    second = _browser_manifest(
        tmp_path,
        sequence=2,
        step_id="node_2",
        step_label="target_step",
        step_name="target_step",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(first, second),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    monkeypatch.setattr(
        "journeysdk.cli.ensure_case_trace",
        lambda selected: SimpleNamespace(
            path=tmp_path / f"trace-{selected.manifests[0].step_id}.zip",
            created=False,
        ),
    )
    monkeypatch.setattr(
        "journeysdk.cli.ensure_case_video",
        lambda selected: SimpleNamespace(
            path=tmp_path / f"video-{selected.manifests[0].step_id}.webm",
            created=False,
        ),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--paths",
            "--case",
            "case_1",
            "--step",
            "target_step",
            "--touchpoint",
            "browser",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "trace-node_2.zip" in output
    assert "video-node_2.webm" in output
    assert "trace-node_1.zip" not in output
    assert "video-node_1.webm" not in output


def test_evidence_command_paths_touchpoint_docker_excludes_browser_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    browser = _browser_manifest(tmp_path, sequence=1)
    docker = _log_artifact(tmp_path, sequence=2, text="docker only\n")
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(browser,),
        log_artifacts=(docker,),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    monkeypatch.setattr(
        "journeysdk.cli.ensure_execution_trace",
        lambda selected: pytest.fail("browser trace should be filtered out"),
    )
    monkeypatch.setattr(
        "journeysdk.cli.ensure_execution_video",
        lambda selected: pytest.fail("browser video should be filtered out"),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--paths",
            "--touchpoint",
            "docker",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "log:docker" in output
    assert "trace:" not in output
    assert "video:" not in output


def test_evidence_command_list_branch_filter_narrows_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    blue = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_blue",
        branch_env={"route": "blue"},
        manifests=(),
        log_artifacts=(
            _log_artifact(
                tmp_path,
                sequence=1,
                text="blue log\n",
                case_id="case_blue",
                branch_env={"route": "blue"},
            ),
        ),
    )
    red = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_red",
        branch_env={"route": "red"},
        manifests=(),
        log_artifacts=(
            _log_artifact(
                tmp_path,
                sequence=2,
                text="red log\n",
                case_id="case_red",
                branch_env={"route": "red"},
            ),
        ),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((blue, red), ()),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--list",
            "--branch",
            "route=blue",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "cases=1" in output
    assert "case_blue" in output
    assert "case_red" not in output


def test_evidence_command_interactively_browses_step_and_docker_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    docker = _log_artifact(
        tmp_path,
        sequence=1,
        text="docker target log\n",
        step_id="node_1",
        step_label="target_step",
        step_name="target_step",
        touchpoint="docker",
        source="web",
    )
    browser = _log_artifact(
        tmp_path,
        sequence=2,
        text="browser target log\n",
        step_id="node_1",
        step_label="target_step",
        step_name="target_step",
        touchpoint="browser",
        source="page",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(docker, browser),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    prompts = iter(["s", "1", "l", "3", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "docker target log" in output
    assert "browser target log" not in output


def test_evidence_command_interactively_browses_branch_for_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    blue_log = _log_artifact(
        tmp_path,
        sequence=1,
        text="blue log\n",
        case_id="case_blue",
        branch_env={"route": "blue"},
        source="blue",
    )
    red_log = _log_artifact(
        tmp_path,
        sequence=2,
        text="red log\n",
        case_id="case_red",
        branch_env={"route": "red"},
        source="red",
    )
    blue = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_blue",
        branch_env={"route": "blue"},
        manifests=(),
        log_artifacts=(blue_log,),
    )
    red = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_red",
        branch_env={"route": "red"},
        manifests=(),
        log_artifacts=(red_log,),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((blue, red), ()),
    )
    prompts = iter(["b", "1", "p", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert str(blue_log.path) in output
    assert str(red_log.path) not in output


def test_evidence_command_interactively_all_log_sources_aggregates_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    docker = _log_artifact(
        tmp_path,
        sequence=1,
        text="docker restored log\n",
        touchpoint="docker",
        source="web",
    )
    browser = _log_artifact(
        tmp_path,
        sequence=2,
        text="browser restored log\n",
        touchpoint="browser",
        source="page",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(docker, browser),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    prompts = iter(["1", "l", "a", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "docker restored log" in output
    assert "browser restored log" in output


def test_evidence_command_interactively_step_scope_narrows_trace_video_and_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    first = _browser_manifest(
        tmp_path,
        sequence=1,
        step_id="node_1",
        step_label="first_step",
        step_name="first_step",
    )
    second = _browser_manifest(
        tmp_path,
        sequence=2,
        step_id="node_2",
        step_label="target_step",
        step_name="target_step",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(first, second),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    trace_steps: list[tuple[str, ...]] = []
    video_steps: list[tuple[str, ...]] = []

    def fake_trace(selected):
        trace_steps.append(tuple(manifest.step_id for manifest in selected.manifests))
        return SimpleNamespace(path=tmp_path / "target.trace.zip", created=False)

    def fake_video(selected):
        video_steps.append(tuple(manifest.step_id for manifest in selected.manifests))
        return SimpleNamespace(path=tmp_path / "target.webm", created=False)

    monkeypatch.setattr("journeysdk.cli.ensure_execution_trace", fake_trace)
    monkeypatch.setattr("journeysdk.cli.ensure_execution_video", fake_video)
    monkeypatch.setattr("journeysdk.cli.open_trace_viewer", lambda path: None)
    monkeypatch.setattr("journeysdk.cli.open_video_recording", lambda path: None)
    prompts = iter(["s", "2", "t", "v", "p", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert trace_steps == [("node_2",), ("node_2",)]
    assert video_steps == [("node_2",), ("node_2",)]
    assert "target.trace.zip" in output
    assert "target.webm" in output


def test_evidence_command_interactively_docker_parent_aggregates_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    web = _log_artifact(tmp_path, sequence=1, text="web log\n", source="web")
    worker = _log_artifact(tmp_path, sequence=2, text="worker log\n", source="worker")
    browser = _log_artifact(
        tmp_path,
        sequence=3,
        text="browser log\n",
        touchpoint="browser",
        source="page",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(web, worker, browser),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    prompts = iter(["1", "l", "3", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "web log" in output
    assert "worker log" in output
    assert "browser log" not in output


def test_evidence_command_interactively_multiple_log_sources_are_aggregated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    web = _log_artifact(tmp_path, sequence=1, text="web log\n", source="web")
    worker = _log_artifact(tmp_path, sequence=2, text="worker log\n", source="worker")
    browser = _log_artifact(
        tmp_path,
        sequence=3,
        text="browser log\n",
        touchpoint="browser",
        source="page",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(web, worker, browser),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )
    prompts = iter(["1", "l", "2,4", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))

    exit_code = main(["evidence", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "browser log" in output
    assert "web log" in output
    assert "worker log" not in output


def test_evidence_command_list_scopes_reports_agent_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    browser = _browser_manifest(
        tmp_path,
        sequence=1,
        case_id="case_blue",
        branch_env={"route": "blue"},
        step_id="node_1",
        step_label="target_step",
        step_name="target_step",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_blue",
        branch_env={"route": "blue"},
        manifests=(browser,),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    exit_code = main(["evidence", "--dir", str(tmp_path), "--list-scopes"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Log scopes" in output
    assert "all  filter=--run run123" in output
    assert "case case_blue  filter=--run run123 --case case_blue" in output
    assert "filter=--run run123 --branch route=blue" in output
    assert "filter=--run run123 --step target_step" in output


def test_evidence_command_list_log_sources_reports_agent_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    web = _log_artifact(tmp_path, sequence=1, text="web log\n", source="web")
    worker = _log_artifact(tmp_path, sequence=2, text="worker log\n", source="worker")
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(web, worker),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--list-log-sources",
            "--case",
            "case_1",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Log sources" in output
    assert "docker  filter=--case case_1 --touchpoint docker logs=2" in output
    assert "docker:web  filter=--case case_1 --touchpoint docker --source web logs=1" in output
    assert "docker:worker  filter=--case case_1 --touchpoint docker --source worker logs=1" in output


def test_evidence_command_noninteractive_repeated_touchpoints_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    docker = _log_artifact(tmp_path, sequence=1, text="docker log\n")
    browser = _log_artifact(
        tmp_path,
        sequence=2,
        text="browser log\n",
        touchpoint="browser",
        source="page",
    )
    http = _log_artifact(
        tmp_path,
        sequence=3,
        text="http log\n",
        touchpoint="http",
        source="poll",
    )
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(docker, browser, http),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    exit_code = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--show",
            "--touchpoint",
            "docker",
            "--touchpoint",
            "browser",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "docker log" in output
    assert "browser log" in output
    assert "http log" not in output


def test_evidence_command_noninteractive_touchpoint_parent_and_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    web = _log_artifact(tmp_path, sequence=1, text="web log\n", source="web")
    worker = _log_artifact(tmp_path, sequence=2, text="worker log\n", source="worker")
    db = _log_artifact(tmp_path, sequence=3, text="db log\n", source="db")
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "logs",
        run_id="run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        manifests=(),
        log_artifacts=(web, worker, db),
    )
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((case,), ()),
    )

    parent_exit = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--show",
            "--touchpoint",
            "docker",
        ]
    )
    parent_output = capsys.readouterr().out

    child_exit = main(
        [
            "evidence",
            "--dir",
            str(tmp_path),
            "--show",
            "--touchpoint",
            "docker",
            "--source",
            "web",
            "--source",
            "worker",
        ]
    )
    child_output = capsys.readouterr().out

    assert parent_exit == 0
    assert "web log" in parent_output
    assert "worker log" in parent_output
    assert "db log" in parent_output
    assert child_exit == 0
    assert "web log" in child_output
    assert "worker log" in child_output
    assert "db log" not in child_output
