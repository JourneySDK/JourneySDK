from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from journeysdk.cli import _read_pause_choice, build_parser, main
from journeysdk.logger import configure_logging


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


def _jsonl_events(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _execute_result_payload(output: str) -> dict[str, object]:
    events = _jsonl_events(output)
    result_events = [event for event in events if event["event"] == "execute_result"]
    assert len(result_events) == 1
    payload = result_events[0]["payload"]
    assert isinstance(payload, dict)
    return payload


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
                del context
                state = "closed" if self.closed else "open"
                _append(f"store_{{self.name}}_{{state}}")
                return {{"name": self.name, "closed": self.closed}}

            @classmethod
            def __restore__(cls, payload, context):
                del context
                return cls(payload["name"], closed=payload["closed"])

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback
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


def test_parser_accepts_new_flags_and_rejects_removed_forms():
    parser = build_parser()

    execute_args = parser.parse_args(
        [
            "--file",
            "journeys.py",
            "--journey",
            "alpha",
            "--step",
            "target",
            "--state",
            "run.state",
            "--output",
            "structured",
            "--log-level",
            "debug",
            "--fail-fast",
            "--no-memory",
            "--no-memory-update",
        ]
    )
    assert execute_args.file == "journeys.py"
    assert execute_args.journey == "alpha"
    assert execute_args.step == "target"
    assert execute_args.state == "run.state"
    assert execute_args.output == "structured"
    assert execute_args.log_level == "debug"
    assert execute_args.fail_fast is True
    assert execute_args.no_memory is True
    assert execute_args.no_memory_update is True

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

    assert parser.parse_args(["--output", "pretty"]).output == "pretty"
    assert parser.parse_args(["--output", "structured"]).output == "structured"
    assert parser.parse_args(["--output", "jsonl"]).output == "jsonl"


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
        state: str | None = None,
        stream_live: bool = False,
        no_memory: bool = False,
        no_memory_update: bool = False,
    ) -> tuple[list[object], list[object]]:
        del compiled, root, fail_fast, state, stream_live
        assert no_memory is False
        captured_flags.append(no_memory_update)
        return [], []

    monkeypatch.setattr("journeysdk.cli._execute_all_targets", fake_execute_all_targets)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--file", "flow.py", "--no-memory-update", "--log-level", "off"])

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
        del settings
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
    assert "OK cli pause_prompt" in output
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
        "Journey a.py:alpha",
        "Journey b.py:beta",
        "Summary: 2 journeys planned, 2 cases planned, 0 failed",
        "Execution",
    )
    assert "[journey]" not in log_output
    assert "OK executor step_start" in log_output
    assert "  step alpha_step attempt=1 start" in log_output


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
    assert "ERROR [plan] <selection> (JourneySelectionError)" in output
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
    assert "- case_1 branch_env={'bg_1': 'branch_1'} labels=['prepare', 'finish_fast']" in output
    assert "- case_2 branch_env={'bg_1': 'branch_2'} labels=['prepare', 'finish_manual']" in output
    assert "Summary: 1 journey planned, 2 cases planned, 0 failed" in output
    assert "Execution" in output
    assert "- case_1 start branches={bg_1=branch_1}" in log_output
    assert "- case_1 ok steps=2 duration=" in log_output
    assert "- case_2 start branches={bg_1=branch_2}" in log_output
    assert "- case_2 ok steps=2 duration=" in log_output
    assert "  step prepare attempt=1 start" in log_output
    assert "step prepare attempt=1 ok duration=" in log_output
    assert "  branch bg_1=branch_1" in log_output
    assert "step finish_fast attempt=1 ok duration=" in log_output
    assert "  branch bg_1=branch_2" in log_output
    assert "step finish_manual attempt=1 ok duration=" in log_output
    assert "Summary: 1 journey executed, 2 cases executed, 0 failed" in output
    _assert_ordered(
        output,
        "Plan",
        "Summary: 1 journey planned, 2 cases planned, 0 failed",
        "Execution",
        "Summary: 1 journey executed, 2 cases executed, 0 failed",
    )
    _assert_ordered(
        log_output,
        "- case_1 start branches={bg_1=branch_1}",
        "- case_1 ok steps=2 duration=",
        "- case_2 start branches={bg_1=branch_2}",
        "- case_2 ok steps=2 duration=",
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
    assert "- case_1 branch_env={'bg_1': 'branch_1'} labels=['prepare', 'finish_fast']" in output
    assert "- case_2 branch_env={'bg_1': 'branch_2'} labels=['prepare', 'finish_manual']" in output
    assert "Summary: 1 journey planned, 2 cases planned, 0 failed" in output
    assert "- case_1 start" not in output
    assert "- case_2 start branches={bg_1=branch_2}" in log_output
    assert (
        "- case_2 ok steps=2 duration="
        in log_output
    )
    assert "stopped_at=finish_manual replay_anchor=prepare" in log_output
    assert "step prepare attempt=1 ok duration=" in log_output
    assert "  branch bg_1=branch_2" in log_output
    assert "step finish_manual attempt=1 ok duration=" in log_output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output
    _assert_ordered(
        output,
        "Plan",
        "Summary: 1 journey planned, 2 cases planned, 0 failed",
        "Execution",
    )
    _assert_ordered(
        log_output,
        "- case_2 start branches={bg_1=branch_2}",
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
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

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
    assert "step prepare attempt=1 ok duration=" in log_output
    assert "step publish attempt=1 ok duration=" in log_output
    assert "Development mode paused after step publish attempt=1 ok." in output
    assert "step cleanup attempt=1 ok duration=" in log_output
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
    assert "step prepare attempt=1 ok duration=" in log_output
    assert "step publish attempt=1 ok duration=" in log_output
    assert "Development mode stopped after step publish attempt=1 ok." in log_output
    assert "step cleanup attempt=1 ok duration=" not in log_output
    assert "Press c to continue or r to retry" not in output
    assert "Summary: 0 journeys executed, 0 cases executed, 0 failed" in output


def test_execute_develop_step_state_retries_same_target_by_default_and_later_target_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "dev.state"
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
    first_exit = main(
        ["--file", "flow.py", "--develop-step", "publish", "--state", str(state_file)]
    )
    first_capture = capsys.readouterr()
    first_output = first_capture.out
    first_logs = first_capture.out

    assert first_exit == 0
    assert "Development mode stopped after step publish attempt=1 ok." in first_logs
    assert _event_lines(events_file) == ["prepare", "publish"]

    second_exit = main(
        ["--file", "flow.py", "--develop-step", "publish", "--state", str(state_file)]
    )
    second_logs = capsys.readouterr().out

    assert second_exit == 0
    assert "Development mode stopped after step publish attempt=2 ok." in second_logs
    assert _event_lines(events_file) == ["prepare", "publish", "publish"]

    third_exit = main(
        ["--file", "flow.py", "--develop-step", "cleanup", "--state", str(state_file)]
    )
    third_logs = capsys.readouterr().out

    assert third_exit == 0
    assert "Development mode stopped after step cleanup attempt=1 ok." in third_logs
    assert _event_lines(events_file) == ["prepare", "publish", "publish", "cleanup"]


def test_execute_develop_step_failed_pause_exits_nonzero_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "dev.state"
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

        def poll():
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
    first_exit = main(
        ["--file", "flow.py", "--develop-step", "poll", "--state", str(state_file)]
    )
    first_capture = capsys.readouterr()
    first_output = first_capture.out
    first_logs = first_capture.out

    assert first_exit == 1
    assert "Development mode stopped after step poll attempt=1 failed (pending)." in first_logs
    assert "retry attempts were exhausted" not in first_output
    assert state_file.exists()

    second_exit = main(
        ["--file", "flow.py", "--develop-step", "poll", "--state", str(state_file)]
    )
    second_logs = capsys.readouterr().out

    assert second_exit == 0
    assert "Development mode stopped after step poll attempt=2 ok." in second_logs


def test_execute_develop_step_cannot_continue_later_from_failed_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "dev.state"
    _write(
        tmp_path / "flow.py",
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
    first_exit = main(
        ["--file", "flow.py", "--develop-step", "poll", "--state", str(state_file)]
    )
    capsys.readouterr()

    assert first_exit == 1

    second_exit = main(
        ["--file", "flow.py", "--develop-step", "finish", "--state", str(state_file)]
    )
    second_output = capsys.readouterr().out

    assert second_exit == 1
    assert "cannot continue to develop step 'finish'" in second_output
    assert "Rerun the same --develop-step target to retry the failed step" in second_output


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
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

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
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

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
    assert len(publish_indices) == 2
    assert len(exit_indices) == 2
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
    exit_code = main(["--file", "flow.py", "--develop-step", "publish", "--interactive"])

    output = capsys.readouterr().out
    assert exit_code == 130
    assert "Interrupted." in output
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
    state_file = tmp_path / "pause.state"
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
            "--state",
            str(state_file),
            "--interactive",
        ]
    )
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert state_file.exists()
    assert "Development mode paused after step publish attempt=1 ok." in first_output
    assert "Interrupted." in first_output

    monkeypatch.setattr("builtins.input", resume_input)
    second_exit = main(
        [
            "--file",
            "flow.py",
            "--develop-step",
            "publish",
            "--state",
            str(state_file),
            "--interactive",
        ]
    )
    second_capture = capsys.readouterr()
    second_output = second_capture.out
    second_logs = second_capture.out

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_logs
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
    assert "  step poll attempt=1 failed duration=" in log_output
    assert "Development mode paused after step poll attempt=1 failed (pending)." in output
    assert "  step poll attempt=2 failed duration=" in log_output
    assert "Development mode paused after step poll attempt=2 failed (pending)." in output
    assert "step poll attempt=3 ok duration=" in log_output
    assert "Development mode paused after step poll attempt=3 ok." in output
    assert "step finish attempt=1 ok duration=" in log_output
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
    assert events_file.read_text(encoding="utf-8").splitlines() == ["old", "new"]


def test_execute_develop_step_continue_reloads_later_step_without_rerunning_prior_steps(
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

    prompts = iter(["c", "c"])

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

    prompts = iter(["c", "c"])

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

    prompts = iter(["c", "c", "c"])

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
    assert "restarting case_1" not in log_output
    assert "Development mode paused after step extra attempt=1 ok." in output
    assert events_file.read_text(encoding="utf-8").splitlines() == [
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
    state_file = tmp_path / "pause.state"
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
            "--state",
            str(state_file),
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

    prompts = iter(["c", "c"])

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
            "--state",
            str(state_file),
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
    assert "ERROR [execute] " in output
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
    assert "  step poll attempt=1 start" in log_output
    assert "  step poll attempt=1 retry duration=" in log_output
    assert "remaining=0 error=RuntimeError: pending" in log_output
    assert "  step poll attempt=2 start" in log_output
    assert "step poll attempt=2 ok duration=" in log_output
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
    assert "Journey good.py:good" in output
    assert "ERROR [plan]" in output
    assert "Summary: 1 journey planned, 1 case planned, 1 failed" in output
    assert "Execution" in output
    assert "Summary: 1 journey executed, 1 case executed, 1 failed" in output
    _assert_ordered(
        output,
        "Plan",
        "ERROR [plan]",
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
    assert "Journey good.py:good" in output
    assert "ERROR [execute]" in output
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
    assert "Journey b_good.py:good" in output
    assert "  step finish attempt=" not in output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in output


def test_execute_state_requires_exactly_one_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "a.py",
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
        tmp_path / "b.py",
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
    exit_code = main(["--state", "resume.state"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "What happened: Resuming with --state requires exactly one selected journey." in output


def test_execute_state_interrupts_and_resumes_via_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "resume.state"
    marker_file = tmp_path / "resume.flag"

    _write(
        tmp_path / "flow.py",
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

    first_exit = main(["--file", "flow.py", "--state", str(state_file)])
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert "Interrupted." in first_output
    assert "What happened: Journey execution was interrupted before it finished." in first_output
    assert "Try this: Run the same command again with --state" in first_output
    assert state_file.exists()

    second_exit = main(
        [
            "--file",
            "flow.py",
            "--state",
            str(state_file),
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


def test_execute_state_first_sigint_finishes_step_and_resumes_after_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "resume.state"
    marker_file = tmp_path / "sent.flag"
    events_file = tmp_path / "events.log"

    _write(
        tmp_path / "flow.py",
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
                del context
                state = "closed" if self.closed else "open"
                _append(f"store_{{state}}")
                return {{"closed": self.closed}}

            @classmethod
            def __restore__(cls, payload, context):
                del context
                return cls(closed=payload["closed"])

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback
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

    first_exit = main(["--file", "flow.py", "--state", str(state_file)])
    first_capture = capsys.readouterr()

    assert first_exit == 130
    assert "Interrupted." in first_capture.out
    assert "step publish attempt=1 ok duration=" in first_capture.out
    assert "step publish attempt=1 interrupted" not in first_capture.out
    assert _event_lines(events_file) == [
        "publish",
        "after_signal",
        "store_open",
        "exit",
        "store_closed",
    ]

    second_exit = main(["--file", "flow.py", "--state", str(state_file)])
    second_capture = capsys.readouterr()

    assert second_exit == 0
    assert "step publish attempt=2 start" not in second_capture.out
    assert "step finish attempt=1 ok duration=" in second_capture.out
    events = _event_lines(events_file)
    assert events[:5] == [
        "publish",
        "after_signal",
        "store_open",
        "exit",
        "store_closed",
    ]
    assert events.count("publish") == 1
    assert "finish_True" in events
    assert events.index("exit") < events.index("finish_True")


def test_execute_state_second_sigint_interrupts_dirty_step_and_resumes_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "resume.state"
    marker_file = tmp_path / "sent.flag"
    seed_file = tmp_path / "seed.count"
    events_file = tmp_path / "events.log"

    _write(
        tmp_path / "flow.py",
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

    first_exit = main(["--file", "flow.py", "--state", str(state_file)])
    first_capture = capsys.readouterr()

    assert first_exit == 130
    assert "step publish attempt=1 interrupted duration=" in first_capture.out
    first_events = _event_lines(events_file)
    runtime_seed = first_events[0].rsplit("_", 1)[1]
    assert first_events == [
        f"publish_{runtime_seed}",
        "after_first_signal",
    ]

    second_exit = main(["--file", "flow.py", "--state", str(state_file)])
    second_capture = capsys.readouterr()

    assert second_exit == 0
    assert "step publish attempt=2 ok duration=" in second_capture.out
    assert _event_lines(events_file) == [
        f"publish_{runtime_seed}",
        "after_first_signal",
        f"publish_{runtime_seed}",
        f"finish_{runtime_seed}",
    ]
    assert int(seed_file.read_text(encoding="utf-8")) > int(runtime_seed)


def test_execute_state_resume_streams_case_resume_in_pretty_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "resume.state"
    marker_file = tmp_path / "resume.flag"

    _write(
        tmp_path / "flow.py",
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

    first_exit = main(["--file", "flow.py", "--state", str(state_file)])
    first_capture = capsys.readouterr()
    first_logs = first_capture.out

    assert first_exit == 130
    assert "  step maybe_interrupt attempt=1 interrupted duration=" in first_logs

    second_exit = main(["--file", "flow.py", "--state", str(state_file)])
    second_capture = capsys.readouterr()
    second_logs = second_capture.out

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_logs
    assert "  step maybe_interrupt attempt=2 start" in second_logs
    assert "step maybe_interrupt attempt=2 ok duration=" in second_logs
    assert "- case_1 ok steps=1 duration=" in second_logs


def test_execute_state_resume_rehydrates_same_step_args_and_retries_twice_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "resume.state"
    seed_counter_file = tmp_path / "seed.count"
    attempt_counter_file = tmp_path / "attempt.count"
    events_file = tmp_path / "events.log"

    _write(
        tmp_path / "flow.py",
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

    first_exit = main(["--file", "flow.py", "--state", str(state_file)])
    first_capture = capsys.readouterr()
    first_logs = first_capture.out

    assert first_exit == 130
    assert "  step poll attempt=1 interrupted duration=" in first_logs
    assert state_file.exists()

    second_exit = main(["--file", "flow.py", "--state", str(state_file)])
    second_capture = capsys.readouterr()
    second_logs = second_capture.out

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_logs
    assert "  step poll attempt=2 start" in second_logs
    assert "  step poll attempt=2 retry duration=" in second_logs
    assert "remaining=2 error=RuntimeError: pending" in second_logs
    assert "  step poll attempt=3 start" in second_logs
    assert "  step poll attempt=3 retry duration=" in second_logs
    assert "remaining=1 error=RuntimeError: pending" in second_logs
    assert "  step poll attempt=4 start" in second_logs
    assert "step poll attempt=4 ok duration=" in second_logs

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
