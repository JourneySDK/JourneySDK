"""Tutorial journey showing manual interruption and resume."""

from __future__ import annotations

import time
from pathlib import Path

from journeysdk import journey, step
from journeysdk.logger import get_logger

_LOGGER = get_logger("tutorial")


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
        f"Press Ctrl-C once during the next {pause_seconds:.1f} seconds to stop "
        "gracefully after this step reaches post-exit. Press Ctrl-C a second time "
        "to stop now; Journey will rerun this step later from saved inputs."
    )
    time.sleep(pause_seconds)
    _tutorial_note(
        "wait_for_resume_signal() reached the step boundary and is returning the "
        f"saved ticket {ticket['ticket_id']} so Journey can save or continue."
    )
    return ticket


def assert_resumed_ticket(ticket: dict[str, str]) -> bool:
    if ticket.get("status") != "waiting_for_resume":
        raise AssertionError(f"Unexpected ticket status: {ticket.get('status')!r}")
    _tutorial_note(
        "The journey finished. After a graceful Ctrl-C, saved completed steps "
        "are reused. After a forceful Ctrl-C, wait_for_resume_signal() restarts "
        "with the same saved ticket while load_support_ticket() is reused."
    )
    return True


@journey
def resume_journey() -> None:
    pause_seconds = 2.0
    ticket = step(load_support_ticket)
    resumed_ticket = step(wait_for_resume_signal, ticket, pause_seconds)
    step(assert_resumed_ticket, resumed_ticket)


def _tutorial_note(message: str) -> None:
    _LOGGER.info("tutorial_note", message)
