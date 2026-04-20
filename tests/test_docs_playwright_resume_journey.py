from __future__ import annotations

import importlib
from pathlib import Path

import journeysdk as journey
import pytest
from journeysdk.models import StepNode

pytest.importorskip("playwright.sync_api")

playwright_resume_module = importlib.import_module(
    "docs.playwright_resume_journey.playwright_resume_journey"
)

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


def _require_playwright_browser() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright browser unavailable: {exc}")


def test_playwright_resume_example_compiles_with_explicit_playwright_dependency():
    reloaded = importlib.reload(playwright_resume_module)
    first_plan = journey.compile_journey(reloaded.playwright_resume_journey)
    second_plan = journey.compile_journey(reloaded.playwright_resume_journey)

    assert _case_labels(first_plan) == [
        [
            "login_and_capture_session",
            "continue_authenticated_dashboard",
            "assert_protected_action_complete",
        ]
    ]
    assert _case_labels(second_plan) == [
        [
            "login_and_capture_session",
            "continue_authenticated_dashboard",
            "assert_protected_action_complete",
        ]
    ]


def test_playwright_resume_example_runs_and_resumes_authenticated_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _require_playwright_browser()

    state_file = tmp_path / "journey.state"
    pause_seconds = configured_pause_seconds(
        playwright_resume_module.playwright_resume_journey,
        step_label="continue_authenticated_dashboard",
    )
    live_stderr = install_live_stderr(monkeypatch)
    playwright_resume_module.reset_demo_state(state_path=state_file)
    stop_event, interrupt_thread = start_interrupt_on_prompt(
        live_stderr,
        pause_seconds=pause_seconds,
    )

    try:
        try:
            with pytest.raises(KeyboardInterrupt):
                journey.execute(
                    playwright_resume_module.playwright_resume_journey,
                    state=state_file,
                )
        finally:
            stop_event.set()
            interrupt_thread.join(timeout=1)

        first_capture = capsys.readouterr()

        assert state_file.exists()
        assert live_stderr.prompt_seen.is_set()
        assert "Signed in and captured PlaywrightPageState" in first_capture.err
        assert INTERRUPT_PROMPT_PREFIX in first_capture.err

        report = journey.execute(
            playwright_resume_module.playwright_resume_journey,
            state=state_file,
        )
        second_capture = capsys.readouterr()
    finally:
        playwright_resume_module.reset_demo_state(state_path=state_file)

    assert [record.label for record in report.case_reports[0].records if record.label is not None] == [
        "login_and_capture_session",
        "continue_authenticated_dashboard",
        "assert_protected_action_complete",
    ]
    assert report.case_reports[0].records[0].result.url.endswith("/dashboard")
    assert report.case_reports[0].records[1].result == {
        "auth_state": "authenticated",
        "status": "Protected action complete",
    }
    assert report.case_reports[0].records[2].result is True
    assert "The protected action completed." in second_capture.err
