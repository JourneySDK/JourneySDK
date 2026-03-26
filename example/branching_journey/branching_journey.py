"""Tutorial journey showing checkpoint and branch selection."""

from __future__ import annotations

import journey

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()


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


def assert_fast_track_path(classified: dict[str, str]) -> bool:
    EVENTS.append(f"assert_fast_track_path:{classified['request_id']}")
    if classified.get("decision_basis") != "branch_demo":
        raise AssertionError(f"Unexpected decision basis: {classified.get('decision_basis')!r}")
    return True


def assert_manual_review_path(classified: dict[str, str]) -> bool:
    EVENTS.append(f"assert_manual_review_path:{classified['request_id']}")
    if classified.get("decision_basis") != "branch_demo":
        raise AssertionError(f"Unexpected decision basis: {classified.get('decision_basis')!r}")
    return True


@journey.journey
def branching_journey() -> None:
    signup_request = journey.step(load_signup_request)
    classified = journey.step(classify_signup_request, signup_request)

    after_classification = journey.checkpoint()
    fast_track = journey.branch()
    manual_review = journey.branch(start_from=after_classification)
    selected = journey.checkpoint(branches=[fast_track, manual_review])

    if selected.is_(fast_track):
        journey.step(assert_fast_track_path, classified)
    elif selected.is_(manual_review):
        journey.step(assert_manual_review_path, classified)
