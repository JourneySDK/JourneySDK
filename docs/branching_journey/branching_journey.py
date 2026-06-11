"""Tutorial journey showing step-anchored branch selection."""

from __future__ import annotations

from journeysdk import branch, journey, step
from docs._reset_state import reset_default_state

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()
    reset_default_state(__file__)


def load_signup_request() -> dict[str, str]:
    request = {
        "request_id": "signup-001",
        "email": "new-user@example.com",
    }
    EVENTS.append(f"load_signup_request:{request['request_id']}")
    return request


def classify_signup_request(request: dict[str, str]) -> dict[str, str]:
    classified = {
        "request_id": request["request_id"],
        "decision_basis": "branch_demo",
    }
    EVENTS.append(f"classify_signup_request:{classified['request_id']}")
    return classified


def approve_fast_track_signup(classified: dict[str, str]) -> bool:
    EVENTS.append(f"approve_fast_track_signup:{classified['request_id']}")
    if classified.get("decision_basis") != "branch_demo":
        raise AssertionError(f"Unexpected decision basis: {classified.get('decision_basis')!r}")
    return True


def queue_manual_review_signup(classified: dict[str, str]) -> bool:
    EVENTS.append(f"queue_manual_review_signup:{classified['request_id']}")
    if classified.get("decision_basis") != "branch_demo":
        raise AssertionError(f"Unexpected decision basis: {classified.get('decision_basis')!r}")
    return True


@journey
def branching_journey() -> None:
    signup_request = step(load_signup_request)
    classified = step(classify_signup_request, signup_request)

    if branch():
        step(approve_fast_track_signup, classified)
    elif branch(replay_from=classified):
        step(queue_manual_review_signup, classified)
