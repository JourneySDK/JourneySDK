from __future__ import annotations

import json
from pathlib import Path

import pytest

from journeysdk.cli import main

import docs.cloud_webhook_journey as cloud_webhook_docs
import docs.branching_journey as branching_docs
import docs.first_journey as first_docs
import docs.retry_journey as retry_docs
import docs.selection_journeys as selection_docs
import docs.resume_journey as resume_docs
from journeysdk.touchpoints._webhook_cloud import (
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


def _jsonl_events(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _execute_result_payload(output: str) -> dict[str, object]:
    result_events = [
        event for event in _jsonl_events(output) if event["event"] == "execute_result"
    ]
    assert len(result_events) == 1
    payload = result_events[0]["payload"]
    assert isinstance(payload, dict)
    return payload


def test_first_journey_readme_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())
    first_docs.reset_demo_state()

    execute_exit = main(["verify", "--fresh", "--file", "docs/first_journey/first_journey.py"])
    execute_capture = capsys.readouterr()
    execute_output = execute_capture.out
    execute_logs = execute_capture.out
    assert execute_exit == 0
    assert "Plan" in execute_output
    assert "Summary: 1 journey planned, 1 case planned, 0 failed" in execute_output
    assert "Execution" in execute_output
    assert "docs/first_journey/first_journey.py:first_journey" in execute_output
    assert "create_customer_profile" in execute_logs
    assert "executed attempt=1 duration=" in execute_logs
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in execute_output


def test_selection_readme_commands_use_journey_and_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())
    selection_docs.reset_demo_state()

    exit_code = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/selection_journeys/selection_journeys.py",
            "--journey",
            "welcome_email_journey",
            "--output",
            "jsonl",
        ]
    )
    payload = _execute_result_payload(capsys.readouterr().out)

    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["welcome_email_journey"]
    assert payload["errors"] == []

    exit_code = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/selection_journeys/selection_journeys.py",
            "--journey",
            "invoice_reminder_journey",
            "--output",
            "jsonl",
        ]
    )
    payload = _execute_result_payload(capsys.readouterr().out)

    assert exit_code == 0
    assert [item["journey_name"] for item in payload["journeys"]] == ["invoice_reminder_journey"]
    assert payload["journeys"][0]["report"]["case_reports"][0]["completed"] is True
    assert payload["errors"] == []


def test_branching_readme_target_command_reports_replay_anchor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())
    branching_docs.reset_demo_state()

    exit_code = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/branching_journey/branching_journey.py",
            "--step",
            "queue_manual_review_signup",
        ]
    )
    capture = capsys.readouterr()
    output = capture.out
    logs = capture.out

    assert exit_code == 0
    assert "stopped_at=queue_manual_review_signup replay_anchor=classify_signup_request" in logs
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in output


def test_branching_readme_develop_step_command_pauses_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())
    branching_docs.reset_demo_state()

    exit_code = main(
        [
            "loop",
            "queue_manual_review_signup",
            "--file",
            "docs/branching_journey/branching_journey.py",
        ]
    )
    capture = capsys.readouterr()
    output = capture.out
    logs = capture.out

    assert exit_code == 0
    assert "Loop stopped after step queue_manual_review_signup attempt=1 executed." in logs
    assert "Summary: loop queue_manual_review_signup stopped after target, 0 failed" in output


def test_retry_readme_commands_show_retry_behavior(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())
    retry_docs.reset_demo_state()

    exit_code = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/retry_journey/retry_journey.py",
            "--journey",
            "retry_current_step_journey",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Warning: wait_for_same_step retry after" in output
    assert "wait_for_same_step" in output
    assert "executed attempt=2 duration=" in output

    exit_code = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/retry_journey/retry_journey.py",
            "--journey",
            "retry_from_step_result_journey",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "issue_report_request" in output
    assert "start executed attempt=1" in output
    assert "start executed attempt=2" in output

    exit_code = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/retry_journey/retry_journey.py",
            "--journey",
            "retry_from_step_anchor_journey",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "refresh_status_cache" in output
    assert "start executed attempt=1" in output
    assert "start executed attempt=2" in output


def test_resume_readme_commands_interrupt_then_resume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    monkeypatch.chdir(_repo_root())
    pause_seconds = configured_pause_seconds(
        resume_docs.resume_journey,
        step_label="wait_for_resume_signal",
    )
    live_stderr = install_live_stderr(monkeypatch)
    resume_docs.reset_demo_state()
    stop_event, interrupt_thread = start_interrupt_on_prompt(
        live_stderr,
        pause_seconds=pause_seconds,
    )

    try:
        first_exit = main(
            [
                "verify",
                "--reuse-state",
                "--file",
                "docs/resume_journey/resume_journey.py",
            ]
        )
    finally:
        stop_event.set()
        interrupt_thread.join(timeout=1)

    first_capture = capsys.readouterr()
    first_output = first_capture.out
    first_error = first_capture.out

    assert first_exit == 130
    assert live_stderr.prompt_seen.is_set()
    assert "Interrupted: Journey execution was interrupted before it finished." in first_output
    assert "Hint: Run the same command again to resume from saved progress." in first_output
    assert "Loaded support ticket ticket-001" in first_error
    assert INTERRUPT_PROMPT_PREFIX in first_error
    assert "Ctrl-C received. Finishing the active step so Journey can save progress." in first_error
    assert "wait_for_resume_signal" in first_error
    assert "executed attempt=1 duration=" in first_error

    second_exit = main(
        [
            "verify",
            "--reuse-state",
            "--file",
            "docs/resume_journey/resume_journey.py",
        ]
    )
    second_capture = capsys.readouterr()
    second_output = second_capture.out
    second_error = second_capture.out

    assert second_exit == 0
    assert "case_1 resume" in second_error
    assert "load_support_ticket" in second_error
    assert "wait_for_resume_signal" in second_error
    assert "start executed attempt=2" in second_error
    assert "assert_resumed_ticket" in second_error
    assert "executed attempt=1 duration=" in second_error
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
                "verify",
                "--fresh",
                "--file",
                "docs/cloud_webhook_journey/cloud_webhook_journey.py",
            ]
        )
        execute_capture = capsys.readouterr()
        execute_output = execute_capture.out
        execute_logs = execute_capture.out

    assert execute_exit == 0
    assert "docs/cloud_webhook_journey/cloud_webhook_journey.py:cloud_webhook_journey" in execute_output
    assert "send_invoice_payment_and_verify_webhook" in execute_logs
    assert "executed attempt=1 duration=" in execute_logs
    assert "Summary: 1 journey executed, 1 case executed, 0 failed" in execute_output


def test_browser_resume_readme_commands_interrupt_then_resume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    sync_api = pytest.importorskip("playwright.sync_api")
    browser_resume_example = __import__(
        "docs.browser_resume_journey.browser_resume_journey",
        fromlist=["browser_resume_journey"],
    )
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright browser unavailable: {exc}")

    monkeypatch.chdir(_repo_root())
    pause_seconds = configured_pause_seconds(
        browser_resume_example.browser_resume_journey,
        step_label="continue_authenticated_dashboard",
    )
    live_stderr = install_live_stderr(monkeypatch)
    browser_resume_example.reset_demo_state()
    stop_event, interrupt_thread = start_interrupt_on_prompt(
        live_stderr,
        pause_seconds=pause_seconds,
    )

    try:
        try:
            first_exit = main(
                    [
                        "verify",
                        "--reuse-state",
                        "--file",
                        "docs/browser_resume_journey/browser_resume_journey.py",
                ]
            )
        finally:
            stop_event.set()
            interrupt_thread.join(timeout=1)

        first_capture = capsys.readouterr()
        first_output = first_capture.out
        first_error = first_capture.out

        assert first_exit == 130
        assert live_stderr.prompt_seen.is_set()
        assert "Interrupted: Journey execution was interrupted before it finished." in first_output
        assert "Ctrl-C received. Finishing the active step so Journey can save progress." in first_error
        assert "continue_authenticated_dashboard" in first_error
        assert "executed attempt=1 duration=" in first_error
        assert "Signed in and returned JourneyBrowserPage" in first_error
        assert INTERRUPT_PROMPT_PREFIX in first_error

        second_exit = main(
            [
                "verify",
                "--reuse-state",
                "--file",
                "docs/browser_resume_journey/browser_resume_journey.py",
            ]
        )
        second_capture = capsys.readouterr()
        second_output = second_capture.out
        second_error = second_capture.out
    finally:
        browser_resume_example.reset_demo_state()

    assert second_exit == 0
    assert "case_1 resume" in second_error
    assert "continue_authenticated_dashboard" in second_error
    assert "start executed attempt=2" in second_error
    assert "assert_protected_action_complete" in second_error
    assert "executed attempt=1 duration=" in second_error
    assert "The protected action completed." in second_error


def test_fail_fast_readme_commands_show_default_and_fail_fast_modes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(_repo_root())

    default_exit = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/fail_fast_journeys/fail_fast_journeys.py",
        ]
    )
    default_output = capsys.readouterr().out

    assert default_exit == 1
    assert "docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey" in default_output
    assert "Summary: 1 journey executed, 1 case executed, 1 failed" in default_output

    fail_fast_exit = main(
        [
            "verify",
            "--fresh",
            "--file",
            "docs/fail_fast_journeys/fail_fast_journeys.py",
            "--fail-fast",
        ]
    )
    fail_fast_output = capsys.readouterr().out

    assert fail_fast_exit == 1
    assert "docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey" in fail_fast_output
    assert "finish_successfully                       start executed attempt=" not in fail_fast_output
    assert "Summary: 0 journeys executed, 0 cases executed, 1 failed" in fail_fast_output
