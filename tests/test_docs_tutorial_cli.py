from __future__ import annotations

import json
from pathlib import Path

import pytest

from journeysdk.cli import main

import docs.cloud_webhook_journey as cloud_webhook_docs
import docs.resume_journey as resume_docs
from journeysdk.tools._webhook_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from tests._cloud_stub import serve_in_background
from tests._resume_tutorial_helpers import (
    INTERRUPT_PROMPT_PREFIX,
    configured_pause_seconds,
    install_live_stderr,
    start_interrupt_on_prompt,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_first_journey_readme_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    execute_exit = main(["--file", "docs/first_journey/first_journey.py"])
    execute_capture = capsys.readouterr()
    execute_output = execute_capture.out
    execute_logs = execute_capture.err
    assert execute_exit == 0
    assert "Plan" in execute_output
    assert "Summary: 1 journey planned, 1 case planned, 0 failed" in execute_output
    assert "Execution" in execute_output
    assert "Journey docs/first_journey/first_journey.py:first_journey" in execute_output
    assert "  step create_customer_profile attempt=1 ok duration=" in execute_logs
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in execute_output


def test_selection_readme_commands_use_journey_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    exit_code = main(
        [
            "--file",
            "docs/selection_journeys/selection_journeys.py",
            "--journey",
            "welcome_email_journey",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["welcome_email_journey"]
    assert payload["errors"] == []

    exit_code = main(
        [
            "--file",
            "docs/selection_journeys/selection_journeys.py",
            "--journey",
            "invoice_reminder_journey",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["invoice_reminder_journey"]
    assert payload["journeys"][0]["report"]["case_reports"][0]["completed"] is True
    assert payload["errors"] == []


def test_branching_readme_target_command_reports_replay_anchor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    exit_code = main(
        [
            "--file",
            "docs/branching_journey/branching_journey.py",
            "--step",
            "assert_manual_review_path",
        ]
    )
    capture = capsys.readouterr()
    output = capture.out
    logs = capture.err

    assert exit_code == 0
    assert "stopped_at=assert_manual_review_path replay_anchor=cp_1" in logs
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_branching_readme_develop_step_command_pauses_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    exit_code = main(
        [
            "--file",
            "docs/branching_journey/branching_journey.py",
            "--develop-step",
            "assert_manual_review_path",
        ]
    )
    capture = capsys.readouterr()
    output = capture.out
    logs = capture.err

    assert exit_code == 0
    assert "Development mode stopped after step assert_manual_review_path attempt=1 ok." in logs
    assert "Summary: 0 journeys executed, 0 cases executed, 0 failed" in output


def test_retry_readme_commands_show_retry_behavior(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    exit_code = main(
        [
            "--file",
            "docs/retry_journey/retry_journey.py",
            "--journey",
            "retry_current_step_journey",
        ]
    )
    output = capsys.readouterr().err
    assert exit_code == 0
    assert "step wait_for_same_step attempt=1 retry" in output
    assert "step wait_for_same_step attempt=2 ok duration=" in output

    exit_code = main(
        [
            "--file",
            "docs/retry_journey/retry_journey.py",
            "--journey",
            "retry_from_step_result_journey",
        ]
    )
    output = capsys.readouterr().err
    assert exit_code == 0
    assert output.count("step issue_report_request attempt=1 start") == 1
    assert output.count("step issue_report_request attempt=2 start") == 1

    exit_code = main(
        [
            "--file",
            "docs/retry_journey/retry_journey.py",
            "--journey",
            "retry_from_checkpoint_journey",
        ]
    )
    output = capsys.readouterr().err
    assert exit_code == 0
    assert output.count("step refresh_status_cache attempt=1 start") == 1
    assert output.count("step refresh_status_cache attempt=2 start") == 1


def test_resume_readme_commands_interrupt_then_resume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    monkeypatch.chdir(_repo_root())
    state_file = tmp_path / "resume.state"
    pause_seconds = configured_pause_seconds(
        resume_docs.resume_journey,
        step_label="wait_for_resume_signal",
    )
    live_stderr = install_live_stderr(monkeypatch)
    resume_docs.reset_demo_state(state_path=state_file)
    stop_event, interrupt_thread = start_interrupt_on_prompt(
        live_stderr,
        pause_seconds=pause_seconds,
    )

    try:
        first_exit = main(
            [
                "--file",
                "docs/resume_journey/resume_journey.py",
                "--state",
                str(state_file),
            ]
        )
    finally:
        stop_event.set()
        interrupt_thread.join(timeout=1)

    first_capture = capsys.readouterr()
    first_output = first_capture.out
    first_error = first_capture.err

    assert first_exit == 130
    assert live_stderr.prompt_seen.is_set()
    assert "Interrupted." in first_output
    assert "Try this: Run the same command again with --state" in first_output
    assert "Loaded support ticket ticket-001" in first_error
    assert INTERRUPT_PROMPT_PREFIX in first_error

    second_exit = main(
        [
            "--file",
            "docs/resume_journey/resume_journey.py",
            "--state",
            str(state_file),
        ]
    )
    second_capture = capsys.readouterr()
    second_output = second_capture.out
    second_error = second_capture.err

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_error
    assert "step wait_for_resume_signal attempt=2 ok duration=" in second_error
    assert "The journey finished." in second_error


def test_cloud_webhook_readme_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    with serve_in_background() as cloud:
        monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, cloud.api_key)
        monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, cloud.base_url)
        cloud_webhook_docs.reset_demo_state()

        execute_exit = main(
            [
                "--file",
                "docs/cloud_webhook_journey/cloud_webhook_journey.py",
            ]
        )
        execute_capture = capsys.readouterr()
        execute_output = execute_capture.out
        execute_logs = execute_capture.err

    assert execute_exit == 0
    assert "Journey docs/cloud_webhook_journey/cloud_webhook_journey.py:cloud_webhook_journey" in execute_output
    assert "step get_webhook_invoice_paid attempt=1 ok duration=" in execute_logs
    assert "step receive_webhook_invoice_paid attempt=1" in execute_logs
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in execute_output


def test_playwright_resume_readme_commands_interrupt_then_resume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    sync_api = pytest.importorskip("playwright.sync_api")
    playwright_resume_example = __import__(
        "docs.playwright_resume_journey.playwright_resume_journey",
        fromlist=["playwright_resume_journey"],
    )
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright browser unavailable: {exc}")

    monkeypatch.chdir(_repo_root())
    state_file = tmp_path / "playwright-resume.state"
    pause_seconds = configured_pause_seconds(
        playwright_resume_example.playwright_resume_journey,
        step_label="continue_authenticated_dashboard",
    )
    live_stderr = install_live_stderr(monkeypatch)
    playwright_resume_example.reset_demo_state(state_path=state_file)
    stop_event, interrupt_thread = start_interrupt_on_prompt(
        live_stderr,
        pause_seconds=pause_seconds,
    )

    try:
        try:
            first_exit = main(
                [
                    "--file",
                    "docs/playwright_resume_journey/playwright_resume_journey.py",
                    "--state",
                    str(state_file),
                ]
            )
        finally:
            stop_event.set()
            interrupt_thread.join(timeout=1)

        first_capture = capsys.readouterr()
        first_output = first_capture.out
        first_error = first_capture.err

        assert first_exit == 130
        assert live_stderr.prompt_seen.is_set()
        assert "Interrupted." in first_output
        assert "step continue_authenticated_dashboard attempt=1 interrupted duration=" in first_error
        assert "Signed in and returned JourneyPlaywrightPage" in first_error
        assert INTERRUPT_PROMPT_PREFIX in first_error

        second_exit = main(
            [
                "--file",
                "docs/playwright_resume_journey/playwright_resume_journey.py",
                "--state",
                str(state_file),
            ]
        )
        second_capture = capsys.readouterr()
        second_output = second_capture.out
        second_error = second_capture.err
    finally:
        playwright_resume_example.reset_demo_state(state_path=state_file)

    assert second_exit == 0
    assert "- case_1 resume branches={}" in second_error
    assert "step continue_authenticated_dashboard attempt=2 ok duration=" in second_error
    assert "step assert_protected_action_complete attempt=1 ok duration=" in second_error
    assert "The protected action completed." in second_error


def test_fail_fast_readme_commands_show_default_and_fail_fast_modes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    default_exit = main(
        [
            "--file",
            "docs/fail_fast_journeys/fail_fast_journeys.py",
        ]
    )
    default_output = capsys.readouterr().out

    assert default_exit == 1
    assert "Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey" in default_output
    assert "Summary: 1 journey executed, 1 case executed, 1 failed" in default_output

    fail_fast_exit = main(
        [
            "--file",
            "docs/fail_fast_journeys/fail_fast_journeys.py",
            "--fail-fast",
        ]
    )
    fail_fast_output = capsys.readouterr().out

    assert fail_fast_exit == 1
    assert "Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey" in fail_fast_output
    assert "step finish_successfully attempt=" not in fail_fast_output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in fail_fast_output
