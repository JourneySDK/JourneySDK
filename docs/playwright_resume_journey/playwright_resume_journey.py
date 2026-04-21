"""Tutorial journey showing manual interruption with resumable Playwright state."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from journeysdk import journey, step
from journeysdk.tools.playwright import (
    JourneyPlaywrightPage,
    open_page,
)

from docs.playwright_resume_journey._auth_demo import (
    ensure_demo_server,
    reset_demo_port,
    shutdown_demo_server,
)


def reset_demo_state(*, state_path: str | Path | None = None) -> None:
    """Delete one saved tutorial state file and reset the local auth demo."""

    if state_path is not None:
        Path(state_path).unlink(missing_ok=True)
    shutdown_demo_server()
    reset_demo_port()


def login_and_capture_session() -> JourneyPlaywrightPage:
    """Log in to the demo app and capture resumable page state."""

    login_url = f"{ensure_demo_server()}/login"
    page = open_page(login_url)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard")
    page.wait_for_function(
        "() => document.getElementById('auth-state').textContent === 'authenticated'"
    )

    _tutorial_note(
        "Signed in and returned JourneyPlaywrightPage for "
        f"{page.url}. The next step can reopen this authenticated dashboard from "
        "saved state without logging in again."
    )
    return page


def continue_authenticated_dashboard(
    session: JourneyPlaywrightPage,
    pause_seconds: float,
) -> dict[str, str]:
    """Resume the authenticated dashboard and complete the protected action."""

    page = open_page(session)
    auth_state = page.locator("#auth-state").text_content()
    if auth_state != "authenticated":
        raise AssertionError(
            f"Expected an authenticated dashboard, got {auth_state!r}."
        )

    _tutorial_note(
        "continue_authenticated_dashboard() reopened the saved dashboard at "
        f"{session.url}. journey resumes at the step boundary, so this step "
        "restarts from the top on resume with the same saved JourneyPlaywrightPage."
    )
    _tutorial_note(
        f"Press Ctrl-C during the next {pause_seconds:.1f} seconds to interrupt "
        "after the authenticated browser state has already been saved. Then rerun "
        "the same command with --state to reopen this dashboard from the same "
        "saved session."
    )
    time.sleep(pause_seconds)

    page.get_by_role("button", name="Complete protected action").click()
    page.wait_for_function(
        "() => document.getElementById('status').textContent === 'Protected action complete'"
    )
    status_text = page.locator("#status").text_content()

    return {
        "auth_state": auth_state,
        "status": status_text or "",
    }


def assert_protected_action_complete(result: dict[str, str]) -> bool:
    """Confirm that the protected action completed after resume."""

    if result.get("auth_state") != "authenticated":
        raise AssertionError(
            f"Expected auth_state 'authenticated', got {result.get('auth_state')!r}."
        )
    if result.get("status") != "Protected action complete":
        raise AssertionError(
            f"Expected status 'Protected action complete', got {result.get('status')!r}."
        )
    _tutorial_note(
        "The protected action completed. If this run resumed from saved state, "
        "continue_authenticated_dashboard() restarted with the same saved "
        "JourneyPlaywrightPage instead of logging in again."
    )
    return True


@journey
def playwright_resume_journey() -> None:
    pause_seconds = 2.0
    session = step(login_and_capture_session)
    result = step(continue_authenticated_dashboard, session, pause_seconds)
    step(assert_protected_action_complete, result)


def _tutorial_note(message: str) -> None:
    print(f"[tutorial] {message}", file=sys.stderr, flush=True)
