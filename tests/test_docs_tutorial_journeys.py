from __future__ import annotations

from pathlib import Path

import journeysdk as journey
import pytest
from journeysdk.errors import CallableExecutionError
from journeysdk.models import StepNode

import docs.branching_journey as branching_docs
import docs.cloud_webhook_journey as cloud_webhook_docs
import docs.fail_fast_journeys as fail_fast_docs
import docs.first_journey as first_docs
import docs.resume_journey as resume_docs
import docs.retry_journey as retry_docs
import docs.selection_journeys as selection_docs
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


def _case_labels(plan: journey.JourneyPlan) -> list[list[str]]:
    return [
        [
            node.label
            for node in case_plan.nodes
            if isinstance(node, StepNode) and node.label is not None
        ]
        for case_plan in plan.case_plans
    ]


def test_first_journey_compiles_and_executes_in_order():
    first_docs.reset_demo_state()

    plan = journey.compile_journey(first_docs.first_journey)

    assert _case_labels(plan) == [
        [
            "create_customer_profile",
            "send_welcome_message",
            "assert_welcome_message_sent",
        ]
    ]

    report = journey.execute(first_docs.first_journey)

    assert [case.case_id for case in report.case_reports] == ["case_1"]
    assert first_docs.EVENTS == [
        "create_customer_profile:cust-001",
        "send_welcome_message:cust-001",
        "assert_welcome_message_sent:cust-001",
    ]


def test_selection_journeys_compile_and_execute_independently():
    selection_docs.reset_demo_state()

    welcome_plan = journey.compile_journey(selection_docs.welcome_email_journey)
    invoice_plan = journey.compile_journey(selection_docs.invoice_reminder_journey)

    assert _case_labels(welcome_plan) == [
        [
            "load_welcome_email_job",
            "assert_welcome_email_job",
        ]
    ]
    assert _case_labels(invoice_plan) == [
        [
            "load_invoice_reminder",
            "assert_invoice_reminder",
        ]
    ]

    welcome_report = journey.execute(selection_docs.welcome_email_journey)
    invoice_report = journey.execute(selection_docs.invoice_reminder_journey)

    assert [case.case_id for case in welcome_report.case_reports] == ["case_1"]
    assert [case.case_id for case in invoice_report.case_reports] == ["case_1"]


def test_branching_journey_compiles_and_targets_the_manual_path():
    branching_docs.reset_demo_state()

    plan = journey.compile_journey(branching_docs.branching_journey)

    assert _case_labels(plan) == [
        [
            "load_signup_request",
            "classify_signup_request",
            "assert_fast_track_path",
        ],
        [
            "load_signup_request",
            "classify_signup_request",
            "assert_manual_review_path",
        ],
    ]

    report = journey.execute(
        branching_docs.branching_journey,
        step="assert_manual_review_path",
    )

    assert len(report.case_reports) == 1
    assert report.case_reports[0].stopped_at_label == "assert_manual_review_path"
    assert report.case_reports[0].replay_anchor == "cp_1"
    assert branching_docs.EVENTS == [
        "load_signup_request:signup-001",
        "classify_signup_request:signup-001",
        "assert_manual_review_path:signup-001",
    ]


def test_retry_examples_show_same_step_step_anchor_and_checkpoint_anchor():
    retry_docs.reset_demo_state()
    journey.execute(retry_docs.retry_current_step_journey)
    assert retry_docs.EVENTS == [
        "prepare_same_step_demo",
        "wait_for_same_step:1",
        "wait_for_same_step:2",
    ]

    retry_docs.reset_demo_state()
    journey.execute(retry_docs.retry_from_step_result_journey)
    assert retry_docs.EVENTS == [
        "issue_report_request:report-1",
        "wait_for_report:report-1",
        "issue_report_request:report-2",
        "wait_for_report:report-2",
        "assert_report_ready:report-2",
    ]

    retry_docs.reset_demo_state()
    journey.execute(retry_docs.retry_from_checkpoint_journey)
    assert retry_docs.EVENTS == [
        "load_status_request:status-001",
        "refresh_status_cache:1",
        "wait_for_checkpoint_retry:status-001:refresh_1:attempt_1",
        "refresh_status_cache:2",
        "wait_for_checkpoint_retry:status-001:refresh_2:attempt_2",
        "assert_checkpoint_retry_ready:status-001",
    ]


def test_resume_example_resumes_from_saved_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
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
        with pytest.raises(KeyboardInterrupt):
            journey.execute(resume_docs.resume_journey, state=state_file)
    finally:
        stop_event.set()
        interrupt_thread.join(timeout=1)

    first_capture = capsys.readouterr()

    assert state_file.exists()
    assert live_stderr.prompt_seen.is_set()
    assert "Loaded support ticket ticket-001" in first_capture.err
    assert INTERRUPT_PROMPT_PREFIX in first_capture.err

    report = journey.execute(resume_docs.resume_journey, state=state_file)
    second_capture = capsys.readouterr()

    assert [record.label for record in report.case_reports[0].records if record.label is not None] == [
        "load_support_ticket",
        "wait_for_resume_signal",
        "assert_resumed_ticket",
    ]
    assert report.case_reports[0].records[0].result == {
        "ticket_id": "ticket-001",
        "status": "waiting_for_resume",
    }
    assert report.case_reports[0].records[1].result == {
        "ticket_id": "ticket-001",
        "status": "waiting_for_resume",
    }
    assert report.case_reports[0].records[2].result is True
    assert "The journey finished." in second_capture.err


def test_cloud_webhook_example_compiles_and_executes(
    monkeypatch: pytest.MonkeyPatch,
):
    with serve_in_background() as cloud:
        monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, cloud.api_key)
        monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, cloud.base_url)
        cloud_webhook_docs.reset_demo_state()

        plan = journey.compile_journey(cloud_webhook_docs.cloud_webhook_journey)

        assert _case_labels(plan) == [
            [
                "get_webhook_invoice_paid",
                "send_invoice_paid_webhook_later",
                "receive_webhook_invoice_paid",
                "assert_invoice_paid_webhook",
            ]
        ]

        report = journey.execute(cloud_webhook_docs.cloud_webhook_journey)

    assert [case.case_id for case in report.case_reports] == ["case_1"]
    assert cloud_webhook_docs.EVENTS[-1] == "assert_invoice_paid_webhook"


def test_fail_fast_examples_include_one_success_and_one_expected_failure():
    report = journey.execute(fail_fast_docs.good_demo_journey)

    assert [case.case_id for case in report.case_reports] == ["case_1"]

    with pytest.raises(CallableExecutionError) as exc_info:
        journey.execute(fail_fast_docs.broken_demo_journey)

    assert "expected tutorial failure" in str(exc_info.value)
