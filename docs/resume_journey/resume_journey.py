"""Tutorial journey showing manual interruption and resume."""

from __future__ import annotations

import time
from pathlib import Path

from journeysdk import journey, step
from docs._reset_state import reset_default_state
from journeysdk.logger import get_logger

_LOGGER = get_logger("tutorial")


def reset_demo_state(*, state_path: str | Path | None = None) -> None:
    """Delete one saved tutorial state file so the demo can start fresh."""

    if state_path is None:
        reset_default_state(__file__)
        return
    Path(state_path).unlink(missing_ok=True)


def load_support_ticket() -> dict[str, str]:
    ticket = {
        "ticket_id": "ticket-001",
        "status": "waiting_for_resume",
    }
    _tutorial_note(
        "Loaded support ticket "
        f"{ticket['ticket_id']}. This demo has no explicit replay boundary, so "
        "interrupted runs start this case again from the beginning."
    )
    return ticket


def wait_for_resume_signal(
    ticket: dict[str, str],
    pause_seconds: float,
) -> dict[str, str]:
    _tutorial_note(
        "wait_for_resume_signal() is starting with ticket "
        f"{ticket['ticket_id']}. This journey has no explicit replay boundary, so "
        "a forceful interruption restarts from the case beginning."
    )
    _tutorial_note(
        f"Press Ctrl-C once during the next {pause_seconds:.1f} seconds to stop "
        "gracefully after this step reaches post-exit. Press Ctrl-C a second time "
        "to stop now; Journey will rerun from the nearest explicit replay boundary."
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
        "The journey finished. This demo has no explicit replay boundary, so an "
        "interrupted run restarts the case from the beginning. Add "
        "branch(replay_from=...) or positive retry when a step value should be saved "
        "and reused."
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
