"""Tutorial journey showing manual interruption and resume."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from journey import journey, step


def reset_demo_state(*, state_path: str | Path | None = None) -> None:
    """Delete one saved tutorial state file so the demo can start fresh."""

    if state_path is None:
        return
    Path(state_path).unlink(missing_ok=True)


def load_support_ticket() -> dict[str, str]:
    ticket = {
        "ticket_id": "ticket-001",
        "status": "waiting_for_resume",
    }
    _tutorial_note(
        "Loaded support ticket "
        f"{ticket['ticket_id']} and saved it as the result of load_support_ticket(). "
        "The next step will reuse this saved ticket if you resume with --state."
    )
    return ticket


def wait_for_resume_signal(
    ticket: dict[str, str],
    pause_seconds: float,
) -> dict[str, str]:
    _tutorial_note(
        "wait_for_resume_signal() is starting with saved ticket "
        f"{ticket['ticket_id']}. journey resumes at the step boundary, so this step "
        "restarts from the top on resume with the same saved inputs."
    )
    _tutorial_note(
        f"Press Ctrl-C during the next {pause_seconds:.1f} seconds to interrupt after "
        "the earlier step has already been saved. Then rerun the same command with "
        "--state to resume from this step boundary."
    )
    time.sleep(pause_seconds)
    _tutorial_note(
        "No interruption received. wait_for_resume_signal() is returning the saved "
        f"ticket {ticket['ticket_id']} so the journey can continue."
    )
    return ticket


def assert_resumed_ticket(ticket: dict[str, str]) -> bool:
    if ticket.get("status") != "waiting_for_resume":
        raise AssertionError(f"Unexpected ticket status: {ticket.get('status')!r}")
    _tutorial_note(
        "The journey finished. If this run resumed from saved state, "
        "wait_for_resume_signal() restarted with the same saved ticket while "
        "load_support_ticket() was reused from the earlier successful step."
    )
    return True


@journey
def resume_journey() -> None:
    pause_seconds = 2.0
    ticket = step(load_support_ticket)
    resumed_ticket = step(wait_for_resume_signal, ticket, pause_seconds)
    step(assert_resumed_ticket, resumed_ticket)


def _tutorial_note(message: str) -> None:
    print(f"[tutorial] {message}", file=sys.stderr, flush=True)
