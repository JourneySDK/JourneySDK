from __future__ import annotations

import json
import os
import pickle
import sys
import threading
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from journeysdk.cli import (
    _CliStepInterruptController,
    _active_environment_python,
    _read_pause_choice,
    build_agent_parser,
    build_parser,
    main,
)
from journeysdk.logger import configure_logging
from journeysdk.models import ExecutionReport
from journeysdk.state import default_execution_state_path, load_execution_state


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging("info")
    yield
    configure_logging("info")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _assert_ordered(output: str, *needles: str) -> None:
    position = -1
    for needle in needles:
        next_position = output.index(needle)
        assert next_position > position
        position = next_position


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _event_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _capture_prompt_memory_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path | None]:
    captured_roots: list[Path | None] = []

    def fake_execute_plan(
        journey_fn: object,
        *,
        plan: Any,
        step: str | None = None,
        develop_step: str | None = None,
        pause_action: str | None = None,
        state: str | None = None,
        observer: object | None = None,
        no_state: bool = False,
        no_state_update: bool = False,
        no_memory: bool = False,
        no_memory_update: bool = False,
        no_browser_recording: bool = False,
        clean_browser_recordings: bool = True,
        no_logs: bool = False,
        clean_logs: bool = True,
        prompt_memory_root: str | Path | None = None,
    ) -> ExecutionReport:
        del journey_fn, step, develop_step, pause_action, state, observer
        del no_state, no_state_update, no_memory, no_memory_update
        del no_browser_recording, clean_browser_recordings, no_logs, clean_logs
        captured_roots.append(
            Path(prompt_memory_root) if prompt_memory_root is not None else None
        )
        return ExecutionReport(
            journey_id=plan.journey_id,
            function_ref=plan.function_ref,
            case_reports=[],
        )

    monkeypatch.setattr("journeysdk.cli._execute_plan", fake_execute_plan)
    return captured_roots


def _state_path(
    flow_file: Path,
) -> Path:
    return default_execution_state_path(flow_file)


def _jsonl_events(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _execute_result_payload(output: str) -> dict[str, object]:
    events = _jsonl_events(output)
    result_events = [event for event in events if event["event"] == "execute_result"]
    assert len(result_events) == 1
    payload = result_events[0]["payload"]
    assert isinstance(payload, dict)
    return payload


def test_cli_step_interrupt_controller_logs_forced_interrupt_once(
    capsys: pytest.CaptureFixture[str],
):
    controller = _CliStepInterruptController()
    controller.on_step_lifecycle_phase("execution")
    cleanup_calls: list[str] = []
    cleanup_ran = threading.Event()

    def cleanup() -> None:
        cleanup_calls.append("cleanup")
        cleanup_ran.set()

    controller.register_forced_interrupt_callback(
        "fake cleanup",
        cleanup,
    )

    controller.handle_sigint(2, None)
    with pytest.raises(KeyboardInterrupt):
        controller.handle_sigint(2, None)
    with pytest.raises(KeyboardInterrupt):
        controller.handle_sigint(2, None)

    output = capsys.readouterr().out
    assert output.count("Ctrl-C received. Finishing the active step") == 1
    assert output.count("Ctrl-C received again. Stopping now") == 1
    assert cleanup_ran.wait(timeout=1)
    assert cleanup_calls == ["cleanup"]


def _write_develop_lifecycle_flow(
    flow_file: Path,
    events_file: Path,
    *,
    include_cleanup: bool,
    fail_exit: bool = False,
) -> None:
    cleanup_step = """
        def cleanup():
            _append("cleanup")
            return True
    """ if include_cleanup else ""
    cleanup_call = "journey.step(cleanup)" if include_cleanup else ""
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        class LifecycleValue:
            def __init__(self, name: str, closed: bool = False):
                self.name = name
                self.closed = closed

            def __store__(self, context):
                state = "closed" if self.closed else "open"
                _append(f"store_{{self.name}}_{{state}}")
                return {{"name": self.name, "closed": self.closed}}

            @classmethod
            def __restore__(cls, payload, context):
                return cls(payload["name"], closed=payload["closed"])

            def __exit__(self, exc_type, exc, traceback):
                if self.closed:
                    return
                self.closed = True
                _append(f"exit_{{self.name}}")
                if {fail_exit!r}:
                    raise RuntimeError("close failed")

        def publish():
            _append("publish")
            return LifecycleValue("publish")

        {cleanup_step}

        @journey.journey
        def flow():
            journey.step(publish)
            {cleanup_call}
        """,
    )


def test_parser_accepts_new_flags_and_rejects_removed_forms(
    capsys: pytest.CaptureFixture[str],
):
    parser = build_parser()

    execute_args = parser.parse_args(
        [
            "--file",
            "journeys.py",
            "--journey",
            "alpha",
            "--step",
            "target",
            "--output",
            "structured",
            "--log-level",
            "debug",
            "--fail-fast",
            "--no-state",
            "--no-state-update",
            "--no-memory",
            "--no-memory-update",
            "--no-browser-recording",
            "--debug-plan",
        ]
    )
    assert execute_args.file == "journeys.py"
    assert execute_args.journey == "alpha"
    assert execute_args.step == "target"
    assert execute_args.output == "structured"
    assert execute_args.log_level == "debug"
    assert execute_args.fail_fast is True
    assert execute_args.no_state is True
    assert execute_args.no_state_update is True
    assert execute_args.no_memory is True
    assert execute_args.no_memory_update is True
    assert execute_args.no_browser_recording is True
    assert execute_args.debug_plan is True

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--plan-only"])
    assert exc_info.value.code == 2
    removed_output = capsys.readouterr().out
    assert "unrecognized arguments: --plan-only" in removed_output

    agent_parser = build_agent_parser()
    agent_args = agent_parser.parse_args(["codex"])
    assert agent_args.target == "codex"
    assert agent_args.install is False
    assert agent_args.force is False

    install_args = agent_parser.parse_args(["claude", "--install", "--force"])
    assert install_args.target == "claude"
    assert install_args.install is True
    assert install_args.force is True

    alias_args = parser.parse_args(["--level", "warning"])
    assert alias_args.log_level == "warning"

    pause_args = parser.parse_args(
        [
            "--file",
            "journeys.py",
            "--develop-step",
            "target",
            "--interactive",
        ]
    )
    assert pause_args.develop_step == "target"
    assert pause_args.interactive is True
    assert pause_args.step is None

    with pytest.raises(SystemExit):
        parser.parse_args(["plan"])
    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "--file", "journeys.py"])
    with pytest.raises(SystemExit):
        parser.parse_args(["execute"])
    with pytest.raises(SystemExit):
        parser.parse_args(["execute", "--file", "journeys.py"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--only-step", "target"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--case-id", "case_1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--step", "target", "--develop-step", "target"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--json"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--agent-instructions", "codex"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--agent-bootstrap", "codex"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--touchpoint-docs", "ftp"])
    with pytest.raises(SystemExit):
        agent_parser.parse_args(["vim"])
    removed_flag = "--" + "state"
    with pytest.raises(SystemExit):
        parser.parse_args([removed_flag, "run.json"])

    assert parser.parse_args(["--output", "pretty"]).output == "pretty"
    assert parser.parse_args(["--output", "structured"]).output == "structured"
    assert parser.parse_args(["--output", "jsonl"]).output == "jsonl"


def test_agent_prints_complete_packet_without_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    rendered = "sentinel agent bootstrap\n"

    def fail_discovery(*args: object, **kwargs: object) -> None:
        raise AssertionError("agent command should not discover journeys")

    def fake_render_agent_bootstrap(target: str) -> str:
        assert target == "codex"
        return rendered

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("journeysdk.cli.discover_journeys", fail_discovery)
    monkeypatch.setattr(
        "journeysdk.cli.render_agent_bootstrap",
        fake_render_agent_bootstrap,
    )

    exit_code = main(["agent", "codex"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == rendered


def test_touchpoint_docs_prints_reference_without_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    rendered = "sentinel docker docs\n"

    def fail_discovery(*args: object, **kwargs: object) -> None:
        raise AssertionError("touchpoint docs should not discover journeys")

    def fake_render_touchpoint_docs(target: str) -> str:
        assert target == "docker"
        return rendered

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("journeysdk.cli.discover_journeys", fail_discovery)
    monkeypatch.setattr(
        "journeysdk.cli.render_touchpoint_docs",
        fake_render_touchpoint_docs,
    )

    exit_code = main(["--touchpoint-docs", "docker"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == rendered


def test_touchpoint_docs_all_prints_index_and_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    rendered = "sentinel all touchpoint docs\n"

    def fake_render_touchpoint_docs(target: str) -> str:
        assert target == "all"
        return rendered

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "journeysdk.cli.render_touchpoint_docs",
        fake_render_touchpoint_docs,
    )

    exit_code = main(["--touchpoint-docs", "all"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == rendered


def test_agent_install_writes_default_project_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    rendered = "installed cursor instructions\n"

    def fake_render_agent_instructions(target: str) -> str:
        assert target == "cursor"
        return rendered

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "journeysdk.agent_instructions.render_agent_instructions",
        fake_render_agent_instructions,
    )

    exit_code = main(["agent", "cursor", "--install"])

    captured = capsys.readouterr()
    target = tmp_path / ".cursor" / "rules" / "journey-developer.mdc"
    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == rendered
    assert "Installed agent instructions:" in captured.out


def test_agent_install_refuses_existing_file_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    target = tmp_path / "JOURNEY_AGENT.md"
    target.write_text("keep me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["agent", "generic", "--install"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert "--force" in captured.out


def test_agent_install_force_replaces_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    rendered = "forced generic instructions\n"

    def fake_render_agent_instructions(target: str) -> str:
        assert target == "generic"
        return rendered

    target = tmp_path / "JOURNEY_AGENT.md"
    target.write_text("replace me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "journeysdk.agent_instructions.render_agent_instructions",
        fake_render_agent_instructions,
    )

    exit_code = main(["agent", "generic", "--install", "--force"])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == rendered


def test_agent_force_requires_install(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as force_exc:
        main(["agent", "generic", "--force"])

    assert force_exc.value.code == 2
    assert "--force requires --install" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    (
        ["--agent-instructions", "codex"],
        ["--agent-bootstrap", "codex"],
        ["--install-agent-instructions"],
        ["--force-agent-instructions"],
    ),
)
def test_removed_agent_flags_are_rejected(argv: list[str]):
    with pytest.raises(SystemExit) as exc_info:
        main(argv)

    assert exc_info.value.code == 2


def test_execute_develop_step_rejects_json_mode(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        main(["--develop-step", "target", "--json"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --json" in captured.out
    assert captured.err == ""


def test_execute_forwards_no_memory_update_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )
    captured_flags: list[bool] = []

    def fake_execute_all_targets(
        compiled: object,
        *,
        root: Path,
        fail_fast: bool,
        no_state: bool = False,
        no_state_update: bool = False,
        stream_live: bool = False,
        no_memory: bool = False,
        no_memory_update: bool = False,
        no_browser_recording: bool = False,
        no_logs: bool = False,
    ) -> tuple[list[object], list[object]]:
        assert no_memory is False
        assert no_browser_recording is False
        assert no_logs is False
        captured_flags.append(no_memory_update)
        return [], []

    monkeypatch.setattr("journeysdk.cli._execute_all_targets", fake_execute_all_targets)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--file", "flow.py", "--no-memory-update", "--log-level", "off"])

    assert exit_code == 0
    assert captured_flags == [True]


def test_execute_uses_command_root_for_nested_prompt_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write(
        tmp_path / "pkg" / "flow.py",
        """
        import journeysdk as journey

        def target():
            return True

        @journey.journey
        def flow():
            journey.step(target)
        """,
    )
    captured_roots = _capture_prompt_memory_roots(monkeypatch)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--file", "pkg/flow.py", "--log-level", "off"])

    assert exit_code == 0
    assert captured_roots == [tmp_path.resolve()]


@pytest.mark.parametrize(
    "target_args",
    [
        ["--step", "target"],
        ["--develop-step", "target"],
    ],
)
def test_targeted_execute_uses_command_root_for_nested_prompt_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_args: list[str],
):
    _write(
        tmp_path / "pkg" / "flow.py",
        """
        import journeysdk as journey

        def target():
            return True

        @journey.journey
        def flow():
            journey.step(target)
        """,
    )
    captured_roots = _capture_prompt_memory_roots(monkeypatch)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--file", "pkg/flow.py", *target_args, "--log-level", "off"])

    assert exit_code == 0
    assert captured_roots == [tmp_path.resolve()]


def test_execute_forwards_no_browser_recording_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )
    captured_flags: list[bool] = []

    def fake_execute_all_targets(
        compiled: object,
        *,
        root: Path,
        fail_fast: bool,
        no_state: bool = False,
        no_state_update: bool = False,
        stream_live: bool = False,
        no_memory: bool = False,
        no_memory_update: bool = False,
        no_browser_recording: bool = False,
        no_logs: bool = False,
    ) -> tuple[list[object], list[object]]:
        assert no_logs is False
        captured_flags.append(no_browser_recording)
        return [], []

    monkeypatch.setattr("journeysdk.cli._execute_all_targets", fake_execute_all_targets)
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        ["--file", "flow.py", "--no-browser-recording", "--log-level", "off"]
    )

    assert exit_code == 0
    assert captured_flags == [True]


def test_execute_interactive_requires_develop_step(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        main(["--interactive"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--interactive requires --develop-step" in captured.out
    assert captured.err == ""


def test_execute_debug_plan_compiles_without_running_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    events_file = tmp_path / "events.log"
    _write(
        tmp_path / "flow.py",
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def run_target():
            EVENTS.write_text("ran", encoding="utf-8")
            return True

        @journey.journey
        def flow():
            journey.step(run_target)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--debug-plan"])

    captured = capsys.readouterr()
    output = captured.out
    assert exit_code == 0
    assert "Plan" in output
    assert "run_target" in output
    assert "Debug-plan mode: execution skipped." in output
    assert "Execution" not in output
    assert not events_file.exists()


def test_execute_debug_plan_validates_requested_step_without_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    events_file = tmp_path / "events.log"
    _write(
        tmp_path / "flow.py",
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def run_target():
            EVENTS.write_text("ran", encoding="utf-8")
            return True

        @journey.journey
        def flow():
            journey.step(run_target)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--debug-plan", "--step", "missing"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Step label 'missing' was not found in the selected journey." in output
    assert "Debug-plan mode: execution skipped." in output
    assert "Execution" not in output
    assert not events_file.exists()


def test_execute_debug_plan_rejects_interactive_mode(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        main(["--debug-plan", "--develop-step", "target", "--interactive"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--interactive cannot be used with --debug-plan" in captured.out
    assert captured.err == ""


def test_main_reexecs_with_uv_active_environment_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    active_env = tmp_path / "public" / ".venv"
    active_python = active_env / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    active_python.parent.mkdir(parents=True)
    active_python.write_text("", encoding="utf-8")

    wrong_env = tmp_path / "workspace" / ".venv"
    wrong_python = wrong_env / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    wrong_python.parent.mkdir(parents=True)
    wrong_python.write_text("", encoding="utf-8")

    class Reexec(RuntimeError):
        pass

    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execve(
        path: str,
        args: list[str],
        env: dict[str, str],
    ) -> None:
        calls.append((path, args, env))
        raise Reexec

    monkeypatch.setattr(sys, "argv", ["journey", "--develop-step", "first"])
    monkeypatch.setattr(sys, "prefix", str(wrong_env))
    monkeypatch.setattr(sys, "executable", str(wrong_python))
    monkeypatch.setenv("UV_RUN_RECURSION_DEPTH", "1")
    monkeypatch.setenv("VIRTUAL_ENV", str(active_env))
    monkeypatch.setattr(os, "execve", fake_execve)

    with pytest.raises(Reexec):
        main()

    assert calls == [
        (
            str(active_python),
            [str(active_python), "-m", "journeysdk.cli", "--develop-step", "first"],
            {**os.environ, "JOURNEY_ACTIVE_ENV_REEXEC": "1"},
        )
    ]


def test_active_environment_python_ignores_shared_base_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    base_python = tmp_path / "python"
    base_python.write_text("", encoding="utf-8")

    active_env = tmp_path / "public" / ".venv"
    active_python = active_env / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    wrong_env = tmp_path / "workspace" / ".venv"
    wrong_python = wrong_env / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    active_python.parent.mkdir(parents=True)
    wrong_python.parent.mkdir(parents=True)
    try:
        active_python.symlink_to(base_python)
        wrong_python.symlink_to(base_python)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    monkeypatch.setattr(sys, "prefix", str(wrong_env))
    monkeypatch.setattr(sys, "executable", str(wrong_python))
    monkeypatch.setenv("UV_RUN_RECURSION_DEPTH", "1")
    monkeypatch.setenv("VIRTUAL_ENV", str(active_env))

    assert _active_environment_python() == active_python


def test_execute_output_jsonl_emits_parseable_log_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--output", "jsonl"])

    captured = capsys.readouterr()
    events = _jsonl_events(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert any(event["event"] == "plan_start" for event in events)
    assert any(event["event"] == "step_success" for event in events)
    result_events = [event for event in events if event["event"] == "execute_result"]
    assert len(result_events) == 1
    payload = result_events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["journeys"][0]["journey_name"] == "flow"
    assert payload["errors"] == []


def test_pause_choice_reads_one_key_from_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    calls: list[tuple[object, ...]] = []

    class FakeStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 123

        def read(self, size: int) -> str:
            calls.append(("read", size))
            return "r"

    def fake_tcgetattr(fd: int) -> object:
        calls.append(("get", fd))
        return object()

    def fake_tcsetattr(fd: int, when: int, settings: object) -> None:
        calls.append(("set", fd, when))

    def fake_setcbreak(fd: int) -> None:
        calls.append(("cbreak", fd))

    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setitem(
        sys.modules,
        "termios",
        SimpleNamespace(TCSADRAIN=7, tcgetattr=fake_tcgetattr, tcsetattr=fake_tcsetattr),
    )
    monkeypatch.setitem(sys.modules, "tty", SimpleNamespace(setcbreak=fake_setcbreak))

    assert _read_pause_choice("Press c to continue or r to retry: ") == "r"

    output = capsys.readouterr().out
    assert "Press c to continue or r to retry: " in output
    assert ("read", 1) in calls
    assert ("set", 123, 7) in calls


def test_execute_discovers_decorated_journeys_recursively_and_via_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "pkg" / "module_alias.py",
        """
        import journeysdk as j

        def alpha_step():
            return True

        @j.journey
        def alpha():
            j.step(alpha_step)
        """,
    )
    _write(
        tmp_path / "pkg" / "decorator_alias.py",
        """
        import journeysdk as journey
        from journeysdk import journey as workflow

        def beta_step():
            return True

        @workflow
        def beta():
            journey.step(beta_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--output", "jsonl"])

    payload = _execute_result_payload(capsys.readouterr().out)
    assert exit_code == 0
    assert sorted(item["journey_name"] for item in payload["journeys"]) == ["alpha", "beta"]
    assert all("plan" not in item for item in payload["journeys"])
    assert payload["errors"] == []


def test_execute_prints_all_selected_plans_before_any_journey_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "a.py",
        """
        import journeysdk as journey

        def alpha_step():
            return True

        @journey.journey
        def alpha():
            journey.step(alpha_step)
        """,
    )
    _write(
        tmp_path / "b.py",
        """
        import journeysdk as journey

        def beta_step():
            return True

        @journey.journey
        def beta():
            journey.step(beta_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main([])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    _assert_ordered(
        output,
        "Plan",
        "a.py:alpha",
        "b.py:beta",
        "Summary: 2 journeys planned, 2 cases planned, 0 failed",
        "Execution",
    )
    assert "[journey]" not in log_output
    assert "OK " not in log_output
    assert "alpha_step" in log_output
    assert "start attempt=1" in log_output


def test_execute_output_structured_preserves_logfmt_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--output", "structured"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "[journey]" in captured.out
    assert "component=cli event=plan_start message=Plan" in captured.out
    assert "component=executor event=step_success" in captured.out
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in captured.out


def test_execute_log_level_off_suppresses_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--log-level", "off"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_execute_output_jsonl_keeps_stdout_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--output", "jsonl"])

    captured = capsys.readouterr()
    payload = _execute_result_payload(captured.out)
    assert exit_code == 0
    assert payload["errors"] == []
    assert captured.err == ""
    assert any(event["event"] == "step_success" for event in _jsonl_events(captured.out))


def test_execute_output_jsonl_reports_state_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare_v1")
            return True

        def target():
            _append("target")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(target)
        """,
    )

    monkeypatch.chdir(tmp_path)
    first_exit = main(["--file", "flow.py", "--develop-step", "target"])
    capsys.readouterr()
    assert first_exit == 0

    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare_v2")
            return True

        def target():
            _append("target")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(target)
        """,
    )

    second_exit = main(["--file", "flow.py", "--develop-step", "target", "--output", "jsonl"])
    events = _jsonl_events(capsys.readouterr().out)
    validity_events = [
        event for event in events if event["event"] == "state_validity"
    ]

    assert second_exit == 0
    assert any(
        event["status"] == "invalidated"
        and event["reason"] == "plan_shape"
        for event in validity_events
    )
    assert _event_lines(events_file) == [
        "prepare_v1",
        "target",
        "prepare_v2",
        "target",
    ]


def test_execute_file_and_journey_filters_limit_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "pkg" / "first.py",
        """
        import journeysdk as journey
        def alpha_step():
            return True

        @journey.journey
        def alpha():
            journey.step(alpha_step)
        """,
    )
    _write(
        tmp_path / "pkg" / "second.py",
        """
        import journeysdk as journey
        def beta_step():
            return True

        @journey.journey
        def beta():
            journey.step(beta_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(
        ["--file", "pkg/first.py", "--journey", "alpha", "--output", "jsonl"]
    )

    payload = _execute_result_payload(capsys.readouterr().out)
    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["alpha"]
    assert payload["journeys"][0]["file"].endswith("pkg/first.py")
    assert "plan" not in payload["journeys"][0]
    assert payload["errors"] == []


def test_execute_errors_when_journey_name_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    content = """
        import journeysdk as journey
        def shared_step():
            return True

        @journey.journey
        def shared():
            journey.step(shared_step)
    """
    _write(tmp_path / "a.py", content)
    _write(tmp_path / "nested" / "b.py", content)

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--journey", "shared"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ambiguous" in output.lower()
    assert "a.py" in output
    assert "nested/b.py" in output or "nested\\b.py" in output


def test_execute_errors_when_no_decorated_journeys_are_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "plain.py",
        """
        def plain():
            return True
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main([])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "What happened: No journeys were found" in output
    assert "Try this:" in output


def test_cli_renders_user_friendly_missing_file_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "missing.py"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Error: JourneySelectionError during plan at <selection>" in output
    assert "What happened: Python file 'missing.py' was not found." in output
    assert "Try this: Check the path or run the command from the directory" in output


def test_execute_step_runs_only_the_unique_matching_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "alpha.py",
        """
        import journeysdk as journey
        def target():
            return True

        @journey.journey
        def alpha():
            journey.step(target)
        """,
    )
    _write(
        tmp_path / "beta.py",
        """
        import journeysdk as journey
        def other():
            return True

        @journey.journey
        def beta():
            journey.step(other)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--step", "target", "--output", "jsonl"])

    payload = _execute_result_payload(capsys.readouterr().out)
    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["alpha"]
    assert payload["journeys"][0]["report"]["case_reports"][0]["stopped_at_label"] == "target"
    assert payload["errors"] == []


def test_execute_step_errors_when_label_is_ambiguous_across_journeys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    content = """
        import journeysdk as journey
        def shared():
            return True

        @journey.journey
        def flow():
            journey.step(shared)
    """
    _write(tmp_path / "a.py", content)
    _write(tmp_path / "b.py", content.replace("def flow()", "def other_flow()"))

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--step", "shared"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ambiguous" in output.lower()
    assert "shared" in output
    assert "case_1" in output


def test_execute_output_jsonl_errors_include_hint_for_missing_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "alpha.py",
        """
        import journeysdk as journey
        def publish():
            return True

        @journey.journey
        def alpha():
            journey.step(publish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "alpha.py", "--step", "missing", "--output", "jsonl"])

    payload = _execute_result_payload(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["errors"][0]["error_type"] == "StepNotFoundError"
    assert payload["errors"][0]["message"] == (
        "Step label 'missing' was not found in the selected journey."
    )
    assert "Check that the target step label exists" in payload["errors"][0]["hint"]


def test_execute_streams_live_case_progress_for_all_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey
        def prepare():
            return True

        def finish_fast():
            return True

        def finish_manual():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            if journey.branch():
                journey.step(finish_fast)
            elif journey.branch():
                journey.step(finish_manual)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "Plan" in output
    assert "case_1  labels: prepare, finish_fast; branches: {bg_1=branch_1}" in output
    assert "case_2  labels: prepare, finish_manual; branches: {bg_1=branch_2}" in output
    assert "Summary: 1 journey planned, 2 cases planned, 0 failed" in output
    assert "Execution" in output
    assert "case_1  branches={bg_1=branch_1}" in log_output
    assert "case_1 done steps=2 duration=" in log_output
    assert "case_2  branches={bg_1=branch_2}" in log_output
    assert "case_2 done steps=2 duration=" in log_output
    assert "prepare" in log_output
    assert "start attempt=1" in log_output
    assert "ok attempt=1 duration=" in log_output
    assert "branch bg_1" in log_output
    assert "finish_fast" in log_output
    assert "finish_manual" in log_output
    assert "Summary: 1 journey executed, 2 cases executed, 0 failed" in output
    assert "Summary: 1 journey executed, 2 cases executed, 0 failed, duration=" in output
    _assert_ordered(
        output,
        "Plan",
        "Summary: 1 journey planned, 2 cases planned, 0 failed",
        "Execution",
        "Summary: 1 journey executed, 2 cases executed, 0 failed",
    )
    _assert_ordered(
        log_output,
        "case_1  branches={bg_1=branch_1}",
        "case_1 done steps=2 duration=",
        "case_2  branches={bg_1=branch_2}",
        "case_2 done steps=2 duration=",
    )


def test_execute_step_streams_live_target_progress_and_replay_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey
        def prepare():
            return True

        def finish_fast():
            return True

        def finish_manual():
            return True

        @journey.journey
        def flow():
            after_prepare = journey.step(prepare)
            if journey.branch():
                journey.step(finish_fast)
            elif journey.branch(start_from=after_prepare):
                journey.step(finish_manual)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--step", "finish_manual"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "case_1  labels: prepare, finish_fast; branches: {bg_1=branch_1}" in output
    assert "case_2  labels: prepare, finish_manual; branches: {bg_1=branch_2}" in output
    assert "Summary: 1 journey planned, 2 cases planned, 0 failed" in output
    assert "case_1  branches" not in output
    assert "case_2  branches={bg_1=branch_2}" in log_output
    assert (
        "case_2 done steps=2 duration="
        in log_output
    )
    assert "stopped_at=finish_manual replay_anchor=prepare" in log_output
    assert "prepare" in log_output
    assert "ok attempt=1 duration=" in log_output
    assert "branch bg_1" in log_output
    assert "finish_manual" in log_output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output
    _assert_ordered(
        output,
        "Plan",
        "Summary: 1 journey planned, 2 cases planned, 0 failed",
        "Execution",
    )
    _assert_ordered(
        log_output,
        "case_2  branches={bg_1=branch_2}",
    )


def test_execute_develop_step_steps_forward_with_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey
        def prepare():
            return True

        def publish():
            return True

        def cleanup():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    prompts = iter(["c", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(
        ["--file", "flow.py", "--develop-step", "publish", "--interactive", "--no-state"]
    )

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    _assert_ordered(
        output,
        "Plan",
        "Execution",
        "Development mode paused after step publish attempt=1 ok.",
    )
    assert "prepare" in log_output
    assert "publish" in log_output
    assert "ok attempt=1 duration=" in log_output
    assert "Development mode paused after step publish attempt=1 ok." in output
    assert "cleanup" in log_output
    assert "Development mode paused after step cleanup attempt=1 ok." in output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_execute_develop_step_exits_after_target_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey
        def prepare():
            return True

        def publish():
            return True

        def cleanup():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--develop-step", "publish"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "prepare" in log_output
    assert "publish" in log_output
    assert "ok attempt=1 duration=" in log_output
    assert "Development mode stopped after step publish attempt=1 ok." in log_output
    assert "cleanup                       ok attempt=1 duration=" not in log_output
    assert "Press c to continue or r to retry" not in output
    assert "Summary: develop-step publish stopped after target, 0 failed" in output
    assert "Summary: develop-step publish stopped after target, 0 failed, duration=" in output


def test_execute_develop_step_state_retries_same_target_by_default_and_later_target_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return True

        def publish():
            _append("publish")
            return True

        def cleanup():
            _append("cleanup")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    monkeypatch.chdir(tmp_path)
    first_exit = main(["--file", "flow.py", "--develop-step", "publish"])
    first_capture = capsys.readouterr()
    first_output = first_capture.out
    first_logs = first_capture.out

    assert first_exit == 0
    assert "Development mode stopped after step publish attempt=1 ok." in first_logs
    assert _event_lines(events_file) == ["prepare", "publish"]

    second_exit = main(["--file", "flow.py", "--develop-step", "publish"])
    second_logs = capsys.readouterr().out

    assert second_exit == 0
    assert "Development mode stopped after step publish attempt=2 ok." in second_logs
    assert _event_lines(events_file) == ["prepare", "publish", "prepare", "publish"]

    third_exit = main(["--file", "flow.py", "--develop-step", "cleanup"])
    third_logs = capsys.readouterr().out

    assert third_exit == 0
    assert "Development mode stopped after step cleanup attempt=1 ok." in third_logs
    assert _event_lines(events_file) == [
        "prepare",
        "publish",
        "prepare",
        "publish",
        "prepare",
        "publish",
        "cleanup",
    ]


def test_execute_after_develop_step_guides_fresh_broad_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey

        def prepare():
            return True

        def publish():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    develop_exit = main(["--file", "flow.py", "--develop-step", "publish"])
    capsys.readouterr()

    broad_exit = main(["--file", "flow.py"])
    broad_output = capsys.readouterr().out

    assert develop_exit == 0
    assert broad_exit == 1
    assert "created for develop_step 'publish', not None" in broad_output
    assert (
        "Rerun the same --develop-step target to keep iterating, or use `--no-state` "
        "for a fresh --step/full journey verification after a develop-step pause."
    ) in broad_output

    fresh_exit = main(["--file", "flow.py", "--no-state"])
    fresh_output = capsys.readouterr().out

    assert fresh_exit == 0
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in fresh_output


def test_execute_develop_step_retry_allows_unrelated_branch_anchor_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    events_file = tmp_path / "events.log"
    _write(
        tmp_path / "flow.py",
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare_workspace():
            _append("prepare_workspace")
            return {{"workspace": "ready"}}

        def create_thread(workspace):
            _append("create_thread")
            return {{"thread": workspace["workspace"]}}

        def review_thread(thread):
            _append("review_thread")
            return True

        def check_visibility(workspace):
            _append("check_visibility")
            return True

        @journey.journey
        def flow():
            workspace = journey.step(prepare_workspace)
            if journey.branch():
                thread = journey.step(create_thread, workspace)
                if journey.branch(start_from=thread):
                    journey.step(review_thread, thread)
            elif journey.branch(start_from=workspace):
                journey.step(check_visibility, workspace)
        """,
    )

    monkeypatch.chdir(tmp_path)
    first_exit = main(["--file", "flow.py", "--develop-step", "review_thread"])
    first_output = capsys.readouterr().out
    second_exit = main(["--file", "flow.py", "--develop-step", "review_thread"])
    second_output = capsys.readouterr().out

    assert first_exit == 0
    assert second_exit == 0
    assert "invalid branch anchor snapshot data" not in first_output
    assert "invalid branch anchor snapshot data" not in second_output
    assert "Development mode stopped after step review_thread attempt=1 ok." in first_output
    assert "Development mode stopped after step review_thread attempt=2 ok." in second_output


def test_execute_develop_step_failed_pause_exits_nonzero_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    recording_dir = tmp_path / ".journey" / "logs"
    attempts_file = tmp_path / "attempts.count"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        ATTEMPTS = Path({str(attempts_file)!r})
        RECORDINGS = Path({str(recording_dir)!r})

        def _read_attempts():
            if not ATTEMPTS.exists():
                return 0
            return int(ATTEMPTS.read_text(encoding="utf-8"))

        def poll():
            RECORDINGS.mkdir(parents=True, exist_ok=True)
            attempts = _read_attempts() + 1
            ATTEMPTS.write_text(str(attempts), encoding="utf-8")
            if attempts < 2:
                raise RuntimeError("pending")
            return True

        @journey.journey
        def flow():
            journey.step(poll, retry=10, retry_delay=0)
        """,
    )

    monkeypatch.chdir(tmp_path)
    first_exit = main(["--file", "flow.py", "--develop-step", "poll"])
    first_capture = capsys.readouterr()
    first_output = first_capture.out
    first_logs = first_capture.out

    assert first_exit == 1
    assert "Development mode stopped after step poll attempt=1 failed (pending)." in first_logs
    assert "retry attempts were exhausted" not in first_output
    assert (
        "Retry failed step: journey --file flow.py --journey flow --develop-step poll"
        in first_output
    )
    assert "Artifacts: .journey/logs (run `journey logs` to inspect)" in first_output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed, duration=" in first_output
    assert state_file.exists()

    second_exit = main(["--file", "flow.py", "--develop-step", "poll"])
    second_logs = capsys.readouterr().out

    assert second_exit == 0
    assert "Development mode stopped after step poll attempt=2 ok." in second_logs


def test_execute_develop_step_cannot_continue_later_from_failed_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    _write(
        flow_file,
        """
        import journeysdk as journey

        def poll():
            raise RuntimeError("pending")

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(poll, retry=10, retry_delay=0)
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    first_exit = main(["--file", "flow.py", "--develop-step", "poll"])
    capsys.readouterr()

    assert first_exit == 1

    second_exit = main(["--file", "flow.py", "--develop-step", "finish"])
    second_output = capsys.readouterr().out

    assert second_exit == 1
    assert "cannot continue to develop step 'finish'" in second_output
    assert "retry the failed step" in second_output


def test_execute_develop_step_closes_returned_handles_after_continue_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write_develop_lifecycle_flow(
        flow_file,
        events_file,
        include_cleanup=True,
    )

    prompts = iter(["c", "c"])
    prompt_count = {"value": 0}

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        prompt_count["value"] += 1
        lines = _event_lines(events_file)
        if prompt_count["value"] == 1:
            assert "exit_publish" not in lines
            _append_line(events_file, "prompt_publish")
        else:
            _append_line(events_file, "prompt_cleanup")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(
        ["--file", "flow.py", "--develop-step", "publish", "--interactive", "--no-state"]
    )

    capsys.readouterr()
    assert exit_code == 0
    lines = _event_lines(events_file)
    assert lines.index("prompt_publish") < lines.index("exit_publish")
    assert lines.index("exit_publish") < lines.index("cleanup")


def test_execute_develop_step_closes_returned_handles_after_retry_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write_develop_lifecycle_flow(
        flow_file,
        events_file,
        include_cleanup=False,
    )

    prompts = iter(["r", "c"])
    prompt_count = {"value": 0}

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        prompt_count["value"] += 1
        lines = _event_lines(events_file)
        if prompt_count["value"] == 1:
            assert "exit_publish" not in lines
            _append_line(events_file, "prompt_retry")
        else:
            assert lines.count("exit_publish") == 1
            _append_line(events_file, "prompt_continue")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(
        ["--file", "flow.py", "--develop-step", "publish", "--interactive", "--no-state"]
    )

    capsys.readouterr()
    assert exit_code == 0
    lines = _event_lines(events_file)
    publish_indices = [
        index
        for index, line in enumerate(lines)
        if line == "publish"
    ]
    exit_indices = [
        index
        for index, line in enumerate(lines)
        if line == "exit_publish"
    ]
    assert len(publish_indices) == 3
    assert len(exit_indices) == 3
    assert lines.index("prompt_retry") < exit_indices[0] < publish_indices[1]
    assert lines.index("prompt_continue") < exit_indices[1]


def test_execute_develop_step_closes_returned_handles_when_prompt_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write_develop_lifecycle_flow(
        flow_file,
        events_file,
        include_cleanup=False,
    )

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        lines = _event_lines(events_file)
        assert "exit_publish" not in lines
        _append_line(events_file, "prompt_interrupt")
        raise KeyboardInterrupt()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(
        ["--file", "flow.py", "--develop-step", "publish", "--interactive", "--no-state"]
    )

    output = capsys.readouterr().out
    assert exit_code == 130
    assert "Interrupted: Journey execution was interrupted before it finished." in output
    assert "This run could not save new progress, so it cannot resume" in output
    lines = _event_lines(events_file)
    assert lines.index("prompt_interrupt") < lines.index("exit_publish")


def test_execute_develop_step_cleanup_failure_after_prompt_stops_before_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write_develop_lifecycle_flow(
        flow_file,
        events_file,
        include_cleanup=True,
        fail_exit=True,
    )

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        _append_line(events_file, "prompt_continue")
        return "c"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Step-exit cleanup failed" in output
    assert "close failed" in output
    lines = _event_lines(events_file)
    assert lines.index("prompt_continue") < lines.index("exit_publish")
    assert "cleanup" not in lines


def test_execute_develop_step_resume_reopens_prompt_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    _write(
        flow_file,
        """
        import journeysdk as journey
        def prepare():
            return True

        def publish():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
        """,
    )

    def interrupting_input(prompt: str = "") -> str:
        print(prompt, end="")
        raise KeyboardInterrupt()

    prompts = iter(["c"])

    def resume_input(prompt: str = "") -> str:
        print(prompt, end="")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", interrupting_input)

    first_exit = main(
        [
            "--file",
            "flow.py",
            "--develop-step",
            "publish",
            "--interactive",
        ]
    )
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert state_file.exists()
    assert "Development mode paused after step publish attempt=1 ok." in first_output
    assert "Interrupted: Journey execution was interrupted before it finished." in first_output

    monkeypatch.setattr("builtins.input", resume_input)
    second_exit = main(
        [
            "--file",
            "flow.py",
            "--develop-step",
            "publish",
            "--interactive",
        ]
    )
    second_capture = capsys.readouterr()
    second_output = second_capture.out
    second_logs = second_capture.out

    assert second_exit == 0
    assert "case_1 resume" in second_logs
    assert "Development mode paused after step publish attempt=1 ok." in second_output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in second_output


def test_execute_develop_step_retry_same_step_after_failed_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    attempts_file = tmp_path / "attempts.count"
    _write(
        tmp_path / "flow.py",
        f"""
        import journeysdk as journey
        from pathlib import Path

        ATTEMPTS = Path({str(attempts_file)!r})

        def _read_attempts():
            if not ATTEMPTS.exists():
                return 0
            return int(ATTEMPTS.read_text(encoding="utf-8"))

        def prepare():
            return True

        def poll():
            attempts = _read_attempts() + 1
            ATTEMPTS.write_text(str(attempts), encoding="utf-8")
            if attempts < 3:
                raise RuntimeError("pending")
            return True

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(poll, retry=1, retry_delay=0)
            journey.step(finish)
        """,
    )

    prompts = iter(["r", "r", "c", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "poll", "--interactive"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "Error: poll failed after" in log_output
    assert "Development mode paused after step poll attempt=1 failed (pending)." in output
    assert "Error: poll failed after" in log_output
    assert "Development mode paused after step poll attempt=2 failed (pending)." in output
    assert "poll" in log_output
    assert "ok attempt=3 duration=" in log_output
    assert "Development mode paused after step poll attempt=3 ok." in output
    assert "finish" in log_output
    assert "ok attempt=1 duration=" in log_output
    assert "Development mode paused after step finish attempt=1 ok." in output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_execute_develop_step_retry_reloads_changed_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def publish():
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write("old\\n")
            return True

        @journey.journey
        def flow():
            journey.step(publish)
        """,
    )

    prompts = iter(["r", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        choice = next(prompts)
        if choice == "r":
            _write(
                flow_file,
                f"""
                import journeysdk as journey
                from pathlib import Path

                EVENTS = Path({str(events_file)!r})

                def publish():
                    with EVENTS.open("a", encoding="utf-8") as handle:
                        handle.write("new\\n")
                    return True

                @journey.journey
                def flow():
                    journey.step(publish)
                """,
            )
        return choice

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "Reloaded and recompiled flow.py:flow after retry." in log_output
    assert events_file.read_text(encoding="utf-8").splitlines() == ["old", "new", "new"]


def test_execute_develop_step_continue_reloads_later_step_from_replay_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return True

        def publish():
            _append("publish")
            return True

        def cleanup():
            _append("cleanup_old")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    prompts = iter(["c", "c", "c", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        choice = next(prompts)
        if choice == "c" and not hasattr(fake_input, "edited"):
            fake_input.edited = True
            _write(
                flow_file,
                f"""
                import journeysdk as journey
                from pathlib import Path

                EVENTS = Path({str(events_file)!r})

                def _append(message: str):
                    with EVENTS.open("a", encoding="utf-8") as handle:
                        handle.write(message + "\\n")

                def prepare():
                    _append("prepare")
                    return True

                def publish():
                    _append("publish")
                    return True

                def cleanup():
                    _append("cleanup_new")
                    return True

                @journey.journey
                def flow():
                    journey.step(prepare)
                    journey.step(publish)
                    journey.step(cleanup)
                """,
            )
        return choice

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "Reloaded and recompiled flow.py:flow after continue." in log_output
    assert events_file.read_text(encoding="utf-8").splitlines() == [
        "prepare",
        "publish",
        "prepare",
        "publish",
        "prepare",
        "publish",
        "cleanup_new",
        "prepare",
        "publish",
        "cleanup_new",
    ]


def test_execute_develop_step_restarts_when_already_run_step_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare_old")
            return True

        def publish():
            _append("publish")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
        """,
    )

    prompts = iter(["c", "c", "c", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        choice = next(prompts)
        if choice == "c" and not hasattr(fake_input, "edited"):
            fake_input.edited = True
            _write(
                flow_file,
                f"""
                import journeysdk as journey
                from pathlib import Path

                EVENTS = Path({str(events_file)!r})

                def _append(message: str):
                    with EVENTS.open("a", encoding="utf-8") as handle:
                        handle.write(message + "\\n")

                def prepare():
                    _append("prepare_new")
                    return True

                def publish():
                    _append("publish")
                    return True

                @journey.journey
                def flow():
                    journey.step(prepare)
                    journey.step(publish)
                """,
            )
        return choice

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "Already-run journey code changed before the paused step; restarting case_1" in log_output
    assert events_file.read_text(encoding="utf-8").splitlines() == [
        "prepare_old",
        "publish",
        "prepare_new",
        "publish",
        "prepare_new",
        "publish",
    ]


def test_execute_develop_step_accepts_future_plan_changes_after_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return True

        def publish():
            _append("publish")
            return True

        def cleanup():
            _append("cleanup")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    prompts = iter(["c", "c", "c", "c", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        choice = next(prompts)
        if choice == "c" and not hasattr(fake_input, "edited"):
            fake_input.edited = True
            _write(
                flow_file,
                f"""
                import journeysdk as journey
                from pathlib import Path

                EVENTS = Path({str(events_file)!r})

                def _append(message: str):
                    with EVENTS.open("a", encoding="utf-8") as handle:
                        handle.write(message + "\\n")

                def prepare():
                    _append("prepare")
                    return True

                def publish():
                    _append("publish")
                    return True

                def extra():
                    _append("extra")
                    return True

                def cleanup():
                    _append("cleanup")
                    return True

                @journey.journey
                def flow():
                    journey.step(prepare)
                    journey.step(publish)
                    journey.step(extra)
                    journey.step(cleanup)
                """,
            )
        return choice

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "restarting case_1" in log_output
    assert "Development mode paused after step extra attempt=1 ok." in output
    assert events_file.read_text(encoding="utf-8").splitlines() == [
        "prepare",
        "publish",
        "prepare",
        "publish",
        "prepare",
        "publish",
        "extra",
        "prepare",
        "publish",
        "extra",
        "cleanup",
        "prepare",
        "publish",
        "extra",
        "cleanup",
    ]


def test_execute_develop_step_state_resume_reloads_future_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return True

        def publish():
            _append("publish")
            return True

        def cleanup():
            _append("cleanup_old")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    def interrupting_input(prompt: str = "") -> str:
        print(prompt, end="")
        raise KeyboardInterrupt()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", interrupting_input)
    first_exit = main(
        [
            "--file",
            "flow.py",
            "--develop-step",
            "publish",
            "--interactive",
        ]
    )
    capsys.readouterr()

    assert first_exit == 130
    assert state_file.exists()

    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return True

        def publish():
            _append("publish")
            return True

        def cleanup():
            _append("cleanup_new")
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            journey.step(publish)
            journey.step(cleanup)
        """,
    )

    prompts = iter(["c", "c", "c", "c"])

    def resume_input(prompt: str = "") -> str:
        print(prompt, end="")
        return next(prompts)

    monkeypatch.setattr("builtins.input", resume_input)
    second_exit = main(
        [
            "--file",
            "flow.py",
            "--develop-step",
            "publish",
            "--interactive",
        ]
    )

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert second_exit == 0
    assert "Reloaded and recompiled flow.py:flow after continue." in log_output
    assert events_file.read_text(encoding="utf-8").splitlines() == [
        "prepare",
        "publish",
        "prepare",
        "publish",
        "prepare",
        "publish",
        "cleanup_new",
        "prepare",
        "publish",
        "cleanup_new",
    ]


def test_execute_develop_step_continue_from_failed_pause_exits_with_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey
        ATTEMPTS = {"poll": 0}

        def poll():
            ATTEMPTS["poll"] += 1
            raise RuntimeError("pending")

        @journey.journey
        def flow():
            journey.step(poll, retry=1, retry_delay=0)
        """,
    )

    prompts = iter(["c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["--file", "flow.py", "--develop-step", "poll", "--interactive"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Development mode paused after step poll attempt=1 failed (pending)." in output
    assert "Error: CallableExecutionError during execute" in output
    assert "CallableExecutionError" in output
    assert "retry attempts were exhausted" not in output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in output


def test_execute_streams_retry_events_in_pretty_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journeysdk as journey
        ATTEMPTS = {"poll": 0}

        def poll():
            ATTEMPTS["poll"] += 1
            if ATTEMPTS["poll"] == 1:
                raise RuntimeError("pending")
            return True

        @journey.journey
        def flow():
            journey.step(poll, retry=1, retry_delay=0)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py"])

    captured = capsys.readouterr()
    output = captured.out
    log_output = captured.out
    assert exit_code == 0
    assert "poll" in log_output
    assert "start attempt=1" in log_output
    assert "Warning: poll retry after" in log_output
    assert "RuntimeError: pending" in log_output
    assert "start attempt=2" in log_output
    assert "ok attempt=2 duration=" in log_output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_execute_continues_and_summarizes_compile_failures_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "good.py",
        """
        import journeysdk as journey
        def finish():
            return True

        @journey.journey
        def good():
            journey.step(finish)
        """,
    )
    _write(
        tmp_path / "broken.py",
        """
        import journeysdk as journey
        def branch_a_step():
            return True

        def branch_b_step():
            return True

        @journey.journey
        def broken():
            if journey.branch(start_from="missing_step"):
                journey.step(branch_a_step)
            elif journey.branch():
                journey.step(branch_b_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main([])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "good.py:good" in output
    assert "Error:" in output
    assert "Summary: 1 journey planned, 1 case planned, 1 failed" in output
    assert "Execution" in output
    assert "Summary: 1 journey executed, 1 case executed, 1 failed" in output
    _assert_ordered(
        output,
        "Plan",
        "Error:",
        "Summary: 1 journey planned, 1 case planned, 1 failed",
        "Execution",
        "Summary: 1 journey executed, 1 case executed, 1 failed",
    )


def test_execute_continues_and_summarizes_failures_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "good.py",
        """
        import journeysdk as journey
        def finish():
            return True

        @journey.journey
        def good():
            journey.step(finish)
        """,
    )
    _write(
        tmp_path / "broken.py",
        """
        import journeysdk as journey
        def explode():
            raise RuntimeError("boom")

        @journey.journey
        def broken():
            journey.step(explode)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main([])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "good.py:good" in output
    assert "Error:" in output
    assert "What happened:" in output
    assert "Try this:" in output
    assert "Summary: 1 journey executed, 1 case executed, 1 failed" in output


def test_fail_fast_stops_before_later_journeys_are_processed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "a_broken.py",
        """
        import journeysdk as journey
        def explode():
            raise RuntimeError("boom")

        @journey.journey
        def broken():
            journey.step(explode)
        """,
    )
    _write(
        tmp_path / "b_good.py",
        """
        import journeysdk as journey
        def finish():
            return True

        @journey.journey
        def good():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--fail-fast"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "b_good.py:good" in output
    assert "finish                         start attempt=" not in output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in output


def test_execute_default_state_uses_single_file_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    alpha_file = tmp_path / "a.py"
    beta_file = tmp_path / "b.py"
    _write(
        alpha_file,
        """
        import journeysdk as journey
        def first():
            return True

        @journey.journey
        def alpha():
            journey.step(first)
        """,
    )
    _write(
        beta_file,
        """
        import journeysdk as journey
        def second():
            return True

        @journey.journey
        def beta():
            journey.step(second)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--log-level", "off"])

    assert exit_code == 0
    assert _state_path(alpha_file).exists()
    assert _state_path(alpha_file) == _state_path(beta_file)


def test_execute_default_state_persists_but_reruns_after_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    flow_file = tmp_path / "flow.py"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        EVENTS = Path({str(events_file)!r})

        def finish():
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write("finish\\n")
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py", "--log-level", "off", "--no-logs"])
    second_exit = main(["--file", "flow.py", "--log-level", "off", "--no-logs"])

    assert first_exit == 0
    assert second_exit == 0
    assert _state_path(flow_file).exists()
    assert _event_lines(events_file) == ["finish", "finish"]


def test_execute_no_state_skips_default_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    flow_file = tmp_path / "flow.py"
    _write(
        flow_file,
        """
        import journeysdk as journey
        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["--file", "flow.py", "--no-state", "--log-level", "off"])

    assert exit_code == 0
    assert not _state_path(flow_file).exists()


def test_execute_no_state_update_reads_without_persisting_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    flow_file = tmp_path / "flow.py"
    marker_file = tmp_path / "marker.flag"
    events_file = tmp_path / "events.log"
    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        MARKER = Path({str(marker_file)!r})
        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return {{"token": "ready"}}

        def work(payload):
            _append(f"work_{{payload['token']}}")
            if not MARKER.exists():
                MARKER.write_text("ready", encoding="utf-8")
                raise KeyboardInterrupt()
            return True

        def finish():
            _append("finish")
            return True

        @journey.journey
        def flow():
            payload = journey.step(prepare)
            journey.step(work, payload)
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    first_exit = main(["--file", "flow.py", "--log-level", "off", "--no-logs"])
    state_file = _state_path(flow_file)
    before = state_file.read_bytes()

    second_exit = main(
        ["--file", "flow.py", "--no-state-update", "--log-level", "off"]
    )

    assert first_exit == 130
    assert second_exit == 0
    assert state_file.read_bytes() == before
    assert _event_lines(events_file) == [
        "prepare",
        "work_ready",
        "prepare",
        "work_ready",
        "finish",
    ]


def test_execute_state_interrupts_and_resumes_via_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    marker_file = tmp_path / "resume.flag"

    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        MARKER = Path({str(marker_file)!r})

        def maybe_interrupt():
            if not MARKER.exists():
                MARKER.write_text("ready", encoding="utf-8")
                raise KeyboardInterrupt()
            return True

        @journey.journey
        def flow():
            journey.step(maybe_interrupt)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py"])
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert "Interrupted: Journey execution was interrupted before it finished." in first_output
    assert "Hint: Run the same command again to resume from saved progress." in first_output
    assert state_file.exists()

    second_exit = main(
        [
            "--file",
            "flow.py",
            "--output",
            "jsonl",
        ]
    )
    payload = _execute_result_payload(capsys.readouterr().out)

    assert second_exit == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["flow"]
    assert "plan" not in payload["journeys"][0]
    assert payload["errors"] == []
    assert state_file.exists()


def test_execute_ignores_legacy_default_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    legacy_state_file = tmp_path / ".state"
    marker_file = tmp_path / "resume.flag"
    events_file = tmp_path / "events.log"

    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        MARKER = Path({str(marker_file)!r})
        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def prepare():
            _append("prepare")
            return {{"token": "ready"}}

        def maybe_interrupt(payload):
            _append(f"work_{{payload['token']}}")
            if not MARKER.exists():
                MARKER.write_text("ready", encoding="utf-8")
                raise KeyboardInterrupt()
            return True

        @journey.journey
        def flow():
            payload = journey.step(prepare)
            journey.step(maybe_interrupt, payload)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py", "--log-level", "off", "--no-logs"])
    capsys.readouterr()

    assert first_exit == 130
    state = load_execution_state(state_file)
    assert state is not None
    legacy_state_file.write_bytes(pickle.dumps(state))
    state_file.unlink()
    state_file.parent.rmdir()

    second_exit = main(["--file", "flow.py", "--log-level", "off", "--no-logs"])
    capsys.readouterr()

    assert second_exit == 0
    assert legacy_state_file.exists()
    assert state_file.exists()
    assert _event_lines(events_file) == [
        "prepare",
        "work_ready",
        "prepare",
        "work_ready",
    ]


def test_execute_state_first_sigint_finishes_step_and_resumes_after_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    marker_file = tmp_path / "sent.flag"
    events_file = tmp_path / "events.log"

    _write(
        flow_file,
        f"""
        import signal
        import journeysdk as journey
        from pathlib import Path

        MARKER = Path({str(marker_file)!r})
        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        class LifecycleValue:
            def __init__(self, closed: bool = False):
                self.closed = closed

            def __store__(self, context):
                state = "closed" if self.closed else "open"
                _append(f"store_{{state}}")
                return {{"closed": self.closed}}

            @classmethod
            def __restore__(cls, payload, context):
                return cls(closed=payload["closed"])

            def __exit__(self, exc_type, exc, traceback):
                self.closed = True
                _append("exit")

        def publish():
            _append("publish")
            if not MARKER.exists():
                MARKER.write_text("sent", encoding="utf-8")
                signal.raise_signal(signal.SIGINT)
                _append("after_signal")
            return LifecycleValue()

        def finish(value):
            _append(f"finish_{{value.closed}}")
            return True

        @journey.journey
        def flow():
            value = journey.step(publish)
            journey.step(finish, value)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py"])
    first_capture = capsys.readouterr()

    assert first_exit == 130
    assert "Interrupted: Journey execution was interrupted before it finished." in first_capture.out
    assert "Ctrl-C received. Finishing the active step so Journey can save progress." in first_capture.out
    assert "phase=execution" not in first_capture.out
    assert "publish" in first_capture.out
    assert "ok attempt=1 duration=" in first_capture.out
    assert "publish interrupted" not in first_capture.out
    assert _event_lines(events_file) == [
        "publish",
        "after_signal",
        "exit",
    ]

    second_exit = main(["--file", "flow.py"])
    second_capture = capsys.readouterr()

    assert second_exit == 0
    assert "publish" in second_capture.out
    assert "finish" in second_capture.out
    assert "ok attempt=2 duration=" in second_capture.out
    events = _event_lines(events_file)
    assert events[:3] == [
        "publish",
        "after_signal",
        "exit",
    ]
    assert events.count("publish") == 2
    assert "finish_True" in events
    assert events.index("exit") < events.index("finish_True")


def test_execute_state_second_sigint_interrupts_dirty_step_and_resumes_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    marker_file = tmp_path / "sent.flag"
    seed_file = tmp_path / "seed.count"
    events_file = tmp_path / "events.log"

    _write(
        flow_file,
        f"""
        import signal
        import journeysdk as journey
        from pathlib import Path

        MARKER = Path({str(marker_file)!r})
        SEED = Path({str(seed_file)!r})
        EVENTS = Path({str(events_file)!r})

        def _append(message: str):
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def next_payload():
            value = int(SEED.read_text(encoding="utf-8")) + 1 if SEED.exists() else 1
            SEED.write_text(str(value), encoding="utf-8")
            return {{"seed": value}}

        def publish(payload):
            _append(f"publish_{{payload['seed']}}")
            if not MARKER.exists():
                MARKER.write_text("sent", encoding="utf-8")
                signal.raise_signal(signal.SIGINT)
                _append("after_first_signal")
                signal.raise_signal(signal.SIGINT)
                _append("after_second_signal")
            return payload

        def finish(payload):
            _append(f"finish_{{payload['seed']}}")
            return True

        @journey.journey
        def flow():
            payload = next_payload()
            result = journey.step(publish, payload)
            journey.step(finish, result)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py"])
    first_capture = capsys.readouterr()

    assert first_exit == 130
    assert "Ctrl-C received. Finishing the active step so Journey can save progress." in first_capture.out
    assert (
        "Ctrl-C received again. Stopping now; this step will restart from the nearest replay boundary on resume."
        in first_capture.out
    )
    assert "Warning: publish interrupted after" in first_capture.out
    first_events = _event_lines(events_file)
    runtime_seed = first_events[0].rsplit("_", 1)[1]
    assert first_events == [
        f"publish_{runtime_seed}",
        "after_first_signal",
    ]

    second_exit = main(["--file", "flow.py"])
    second_capture = capsys.readouterr()

    assert second_exit == 0
    assert "publish" in second_capture.out
    assert "ok attempt=2 duration=" in second_capture.out
    final_events = _event_lines(events_file)
    resumed_seed = final_events[2].rsplit("_", 1)[1]
    assert final_events == [
        f"publish_{runtime_seed}",
        "after_first_signal",
        f"publish_{resumed_seed}",
        f"finish_{resumed_seed}",
    ]
    assert int(resumed_seed) > int(runtime_seed)
    assert int(seed_file.read_text(encoding="utf-8")) >= int(resumed_seed)


def test_execute_state_resume_streams_case_resume_in_pretty_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    marker_file = tmp_path / "resume.flag"

    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        MARKER = Path({str(marker_file)!r})

        def maybe_interrupt():
            if not MARKER.exists():
                MARKER.write_text("ready", encoding="utf-8")
                raise KeyboardInterrupt()
            return True

        @journey.journey
        def flow():
            journey.step(maybe_interrupt)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py"])
    first_capture = capsys.readouterr()
    first_logs = first_capture.out

    assert first_exit == 130
    assert "Warning: maybe_interrupt interrupted after" in first_logs

    second_exit = main(["--file", "flow.py"])
    second_capture = capsys.readouterr()
    second_logs = second_capture.out

    assert second_exit == 0
    assert "case_1 resume" in second_logs
    assert "maybe_interrupt" in second_logs
    assert "start attempt=2" in second_logs
    assert "ok attempt=2 duration=" in second_logs
    assert "case_1 done steps=1 duration=" in second_logs


def test_execute_state_resume_rehydrates_same_step_args_and_retries_twice_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    flow_file = tmp_path / "flow.py"
    state_file = _state_path(flow_file)
    seed_counter_file = tmp_path / "seed.count"
    attempt_counter_file = tmp_path / "attempt.count"
    events_file = tmp_path / "events.log"

    _write(
        flow_file,
        f"""
        import journeysdk as journey
        from pathlib import Path

        SEED_COUNTER = Path({str(seed_counter_file)!r})
        ATTEMPT_COUNTER = Path({str(attempt_counter_file)!r})
        EVENTS = Path({str(events_file)!r})

        def _read_count(path: Path) -> int:
            if not path.exists():
                return 0
            return int(path.read_text(encoding="utf-8"))

        def _write_count(path: Path, value: int) -> None:
            path.write_text(str(value), encoding="utf-8")

        def _append_event(message: str) -> None:
            with EVENTS.open("a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        def next_payload():
            seed = _read_count(SEED_COUNTER) + 1
            _write_count(SEED_COUNTER, seed)
            return {{"seed": seed}}

        def poll(payload):
            attempt = _read_count(ATTEMPT_COUNTER) + 1
            _write_count(ATTEMPT_COUNTER, attempt)
            _append_event(f"poll_{{attempt}}_{{payload['seed']}}")
            if attempt == 1:
                raise KeyboardInterrupt()
            if attempt in (2, 3):
                raise RuntimeError("pending")
            return True

        @journey.journey
        def flow():
            payload = next_payload()
            journey.step(poll, payload, retry=3, retry_delay=0)
        """,
    )

    monkeypatch.chdir(tmp_path)

    first_exit = main(["--file", "flow.py"])
    first_capture = capsys.readouterr()
    first_logs = first_capture.out

    assert first_exit == 130
    assert "Warning: poll interrupted after" in first_logs
    assert state_file.exists()

    second_exit = main(["--file", "flow.py"])
    second_capture = capsys.readouterr()
    second_logs = second_capture.out

    assert second_exit == 0
    assert "case_1 resume" in second_logs
    assert "poll" in second_logs
    assert "start attempt=2" in second_logs
    assert "Warning: poll retry after" in second_logs
    assert "RuntimeError: pending" in second_logs
    assert "start attempt=3" in second_logs
    assert "start attempt=4" in second_logs
    assert "ok attempt=4 duration=" in second_logs

    assert attempt_counter_file.read_text(encoding="utf-8") == "4"
    events = events_file.read_text(encoding="utf-8").splitlines()
    runtime_seed = events[0].rsplit("_", 1)[1]

    assert events == [
        f"poll_1_{runtime_seed}",
        f"poll_2_{runtime_seed}",
        f"poll_3_{runtime_seed}",
        f"poll_4_{runtime_seed}",
    ]
    assert int(seed_counter_file.read_text(encoding="utf-8")) > int(runtime_seed)
