from __future__ import annotations

from pathlib import Path

import pytest

from journeysdk.cli import main
from journeysdk.logger import configure_logging
from journeysdk.recordings import CaseRecording, RecordingDiscoveryResult


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging("info")
    yield
    configure_logging("info")


def test_recordings_command_interactively_opens_selected_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    case = CaseRecording(
        recordings_dir=tmp_path / ".journey" / "recordings",
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

    exit_code = main(["recordings", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert opened == ["case_1"]
    assert "Recordings" in output
    assert "case_1" in output
    assert "skipped bad manifest" in output


def test_recordings_command_interactively_opens_all_cases_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    cases = (
        CaseRecording(
            recordings_dir=tmp_path / ".journey" / "recordings",
            run_id="run123",
            journey_id="demo_journey",
            function_ref="module:demo_journey",
            case_id="case_1",
            branch_env={},
            manifests=(),
        ),
        CaseRecording(
            recordings_dir=tmp_path / ".journey" / "recordings",
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

    exit_code = main(["recordings", "--dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert opened == [("run123", ("case_1", "case_2"))]
    assert "a. all cases" in output


def test_recordings_command_reports_missing_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(
        "journeysdk.cli.discover_recording_cases",
        lambda root: RecordingDiscoveryResult((), ()),
    )

    exit_code = main(["recordings", "--dir", str(tmp_path)])

    assert exit_code == 1
    assert "No browser recording cases found" in capsys.readouterr().out
