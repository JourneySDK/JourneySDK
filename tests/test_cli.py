from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from journey.cli import build_parser, main


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_parser_accepts_new_flags_and_rejects_removed_forms():
    parser = build_parser()

    plan_args = parser.parse_args(
        ["plan", "--file", "journeys.py", "--journey", "alpha", "--json", "--fail-fast"]
    )
    assert plan_args.file == "journeys.py"
    assert plan_args.journey == "alpha"
    assert plan_args.json is True
    assert plan_args.fail_fast is True

    execute_args = parser.parse_args(
        [
            "execute",
            "--file",
            "journeys.py",
            "--journey",
            "alpha",
            "--step",
            "target",
            "--state",
            "run.state",
            "--json",
            "--fail-fast",
        ]
    )
    assert execute_args.file == "journeys.py"
    assert execute_args.journey == "alpha"
    assert execute_args.step == "target"
    assert execute_args.state == "run.state"
    assert execute_args.json is True
    assert execute_args.fail_fast is True

    pause_args = parser.parse_args(
        [
            "execute",
            "--file",
            "journeys.py",
            "--pause-on-step",
            "target",
        ]
    )
    assert pause_args.pause_on_step == "target"
    assert pause_args.step is None

    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "journeys.py:alpha"])
    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "--step", "target"])
    with pytest.raises(SystemExit):
        parser.parse_args(["execute", "--only-step", "target"])
    with pytest.raises(SystemExit):
        parser.parse_args(["execute", "--case-id", "case_1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["execute", "--step", "target", "--pause-on-step", "target"])


def test_execute_pause_on_step_rejects_json_mode(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        main(["execute", "--pause-on-step", "target", "--json"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--pause-on-step cannot be used with --json" in captured.err


def test_plan_discovers_decorated_journeys_recursively_and_via_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "pkg" / "module_alias.py",
        """
        import journey as j

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
        import journey
        from journey import journey as workflow

        def beta_step():
            return True

        @workflow
        def beta():
            journey.step(beta_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["plan", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert sorted(item["journey_name"] for item in payload["journeys"]) == ["alpha", "beta"]
    assert payload["errors"] == []


def test_plan_file_and_journey_filters_limit_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "pkg" / "first.py",
        """
        import journey

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
        import journey

        def beta_step():
            return True

        @journey.journey
        def beta():
            journey.step(beta_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["plan", "--file", "pkg/first.py", "--journey", "alpha", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["alpha"]
    assert payload["journeys"][0]["file"].endswith("pkg/first.py")
    assert payload["errors"] == []


def test_plan_errors_when_journey_name_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    content = """
        import journey

        def shared_step():
            return True

        @journey.journey
        def shared():
            journey.step(shared_step)
    """
    _write(tmp_path / "a.py", content)
    _write(tmp_path / "nested" / "b.py", content)

    monkeypatch.chdir(tmp_path)
    exit_code = main(["plan", "--journey", "shared"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ambiguous" in output.lower()
    assert "a.py" in output
    assert "nested/b.py" in output or "nested\\b.py" in output


def test_plan_errors_when_no_decorated_journeys_are_found(
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
    exit_code = main(["plan"])

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
    exit_code = main(["execute", "--file", "missing.py"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ERROR [discover] <selection> (JourneySelectionError)" in output
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
        import journey

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
        import journey

        def other():
            return True

        @journey.journey
        def beta():
            journey.step(other)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute", "--step", "target", "--json"])

    payload = json.loads(capsys.readouterr().out)
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
        import journey

        def shared():
            return True

        @journey.journey
        def flow():
            journey.step(shared)
    """
    _write(tmp_path / "a.py", content)
    _write(tmp_path / "b.py", content.replace("def flow()", "def other_flow()"))

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute", "--step", "shared"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ambiguous" in output.lower()
    assert "shared" in output
    assert "case_1" in output


def test_execute_json_errors_include_hint_for_missing_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "alpha.py",
        """
        import journey

        def publish():
            return True

        @journey.journey
        def alpha():
            journey.step(publish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute", "--file", "alpha.py", "--step", "missing", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["errors"][0]["error_type"] == "StepNotFoundError"
    assert payload["errors"][0]["message"] == (
        "Step label 'missing' was not found in the selected journey."
    )
    assert "Run `journey plan`" in payload["errors"][0]["hint"]


def test_execute_streams_live_case_progress_for_all_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journey

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
    exit_code = main(["execute", "--file", "flow.py"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "- case_1 start branches={bg_1=branch_1}" in output
    assert "- case_1 ok steps=2 duration=" in output
    assert "- case_2 start branches={bg_1=branch_2}" in output
    assert "- case_2 ok steps=2 duration=" in output
    assert "  step prepare attempt=1 start" in output
    assert "  step prepare attempt=1 ok duration=" in output
    assert "  branch bg_1=branch_1" in output
    assert "  step finish_fast attempt=1 ok duration=" in output
    assert "  branch bg_1=branch_2" in output
    assert "  step finish_manual attempt=1 ok duration=" in output
    assert "Summary: 1 journey executed, 2 cases executed, 0 failed" in output


def test_execute_step_streams_live_target_progress_and_replay_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journey

        def prepare():
            return True

        def finish_fast():
            return True

        def finish_manual():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            after_prepare = journey.checkpoint()
            if journey.branch():
                journey.step(finish_fast)
            elif journey.branch(start_from=after_prepare):
                journey.step(finish_manual)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute", "--file", "flow.py", "--step", "finish_manual"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "case_1" not in output
    assert "- case_2 start branches={bg_1=branch_2}" in output
    assert (
        "- case_2 ok steps=2 duration="
        in output
    )
    assert "stopped_at=finish_manual replay_anchor=cp_1" in output
    assert "  step prepare attempt=1 ok duration=" in output
    assert "  branch bg_1=branch_2" in output
    assert "  step finish_manual attempt=1 ok duration=" in output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_execute_pause_on_step_steps_forward_with_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journey

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
    exit_code = main(["execute", "--file", "flow.py", "--pause-on-step", "publish"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "  step prepare attempt=1 ok duration=" in output
    assert "  step publish attempt=1 ok duration=" in output
    assert "Paused after step publish attempt=1 ok." in output
    assert "  step cleanup attempt=1 ok duration=" in output
    assert "Paused after step cleanup attempt=1 ok." in output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_execute_pause_on_step_resume_reopens_prompt_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "pause.state"
    _write(
        tmp_path / "flow.py",
        """
        import journey

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
            "execute",
            "--file",
            "flow.py",
            "--pause-on-step",
            "publish",
            "--state",
            str(state_file),
        ]
    )
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert state_file.exists()
    assert "Paused after step publish attempt=1 ok." in first_output
    assert "Interrupted." in first_output

    monkeypatch.setattr("builtins.input", resume_input)
    second_exit = main(
        [
            "execute",
            "--file",
            "flow.py",
            "--pause-on-step",
            "publish",
            "--state",
            str(state_file),
        ]
    )
    second_output = capsys.readouterr().out

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_output
    assert "Paused after step publish attempt=1 ok." in second_output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in second_output


def test_execute_pause_on_step_retry_from_checkpoint_after_failed_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journey

        ATTEMPTS = {"poll": 0}

        def prepare():
            return True

        def poll():
            ATTEMPTS["poll"] += 1
            if ATTEMPTS["poll"] < 3:
                raise RuntimeError("pending")
            return True

        def finish():
            return True

        @journey.journey
        def flow():
            journey.step(prepare)
            anchor = journey.checkpoint()
            journey.step(poll, retry=1, retry_delay=0, retry_from=anchor)
            journey.step(finish)
        """,
    )

    prompts = iter(["r", "c", "c"])

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        return next(prompts)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", fake_input)
    exit_code = main(["execute", "--file", "flow.py", "--pause-on-step", "poll"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "  step poll attempt=1 retry duration=" in output
    assert "  step poll attempt=2 failed duration=" in output
    assert "Paused after step poll attempt=2 failed (pending)." in output
    assert "  step poll attempt=3 ok duration=" in output
    assert "Paused after step poll attempt=3 ok." in output
    assert "  step finish attempt=1 ok duration=" in output
    assert "Paused after step finish attempt=1 ok." in output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_execute_pause_on_step_continue_from_failed_pause_exits_with_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journey

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
    exit_code = main(["execute", "--file", "flow.py", "--pause-on-step", "poll"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Paused after step poll attempt=2 failed (pending)." in output
    assert "ERROR [execute] " in output
    assert "CallableExecutionError" in output
    assert "retry attempts were exhausted" in output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in output


def test_execute_streams_retry_events_in_text_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "flow.py",
        """
        import journey

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
    exit_code = main(["execute", "--file", "flow.py"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "  step poll attempt=1 start" in output
    assert "  step poll attempt=1 retry duration=" in output
    assert "remaining=0 error=RuntimeError: pending" in output
    assert "  step poll attempt=2 start" in output
    assert "  step poll attempt=2 ok duration=" in output
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_plan_continues_and_summarizes_failures_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "good.py",
        """
        import journey

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
        import journey

        def branch_a_step():
            return True

        def branch_b_step():
            return True

        @journey.journey
        def broken():
            if journey.branch(start_from="missing_checkpoint"):
                journey.step(branch_a_step)
            elif journey.branch():
                journey.step(branch_b_step)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["plan"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Journey good.py:good" in output
    assert "ERROR [plan]" in output
    assert "Summary: 1 journey planned, 1 case planned, 1 failed" in output


def test_execute_continues_and_summarizes_failures_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "good.py",
        """
        import journey

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
        import journey

        def explode():
            raise RuntimeError("boom")

        @journey.journey
        def broken():
            journey.step(explode)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute"])

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
        import journey

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
        import journey

        def finish():
            return True

        @journey.journey
        def good():
            journey.step(finish)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute", "--fail-fast"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Journey b_good.py:good" not in output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in output


def test_execute_state_requires_exactly_one_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _write(
        tmp_path / "a.py",
        """
        import journey

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
        import journey

        def second():
            return True

        @journey.journey
        def beta():
            journey.step(second)
        """,
    )

    monkeypatch.chdir(tmp_path)
    exit_code = main(["execute", "--state", "resume.state"])

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
        import journey
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

    first_exit = main(["execute", "--file", "flow.py", "--state", str(state_file)])
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert "Interrupted." in first_output
    assert "What happened: Journey execution was interrupted before it finished." in first_output
    assert "Try this: Run the same command again with --state" in first_output
    assert state_file.exists()

    second_exit = main(
        ["execute", "--file", "flow.py", "--state", str(state_file), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert second_exit == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["flow"]
    assert payload["errors"] == []
    assert state_file.exists()


def test_execute_state_resume_streams_case_resume_in_text_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_file = tmp_path / "resume.state"
    marker_file = tmp_path / "resume.flag"

    _write(
        tmp_path / "flow.py",
        f"""
        import journey
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

    first_exit = main(["execute", "--file", "flow.py", "--state", str(state_file)])
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert "  step maybe_interrupt attempt=1 interrupted duration=" in first_output

    second_exit = main(["execute", "--file", "flow.py", "--state", str(state_file)])
    second_output = capsys.readouterr().out

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_output
    assert "  step maybe_interrupt attempt=2 start" in second_output
    assert "  step maybe_interrupt attempt=2 ok duration=" in second_output
    assert "- case_1 ok steps=1 duration=" in second_output


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
        import journey
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

    first_exit = main(["execute", "--file", "flow.py", "--state", str(state_file)])
    first_output = capsys.readouterr().out

    assert first_exit == 130
    assert "  step poll attempt=1 interrupted duration=" in first_output
    assert state_file.exists()

    second_exit = main(["execute", "--file", "flow.py", "--state", str(state_file)])
    second_output = capsys.readouterr().out

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_output
    assert "  step poll attempt=2 start" in second_output
    assert "  step poll attempt=2 retry duration=" in second_output
    assert "remaining=2 error=RuntimeError: pending" in second_output
    assert "  step poll attempt=3 start" in second_output
    assert "  step poll attempt=3 retry duration=" in second_output
    assert "remaining=1 error=RuntimeError: pending" in second_output
    assert "  step poll attempt=4 start" in second_output
    assert "  step poll attempt=4 ok duration=" in second_output

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
