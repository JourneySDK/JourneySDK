"""Tutorial journey showing how to use the cloud-hosted email touchpoint."""

from __future__ import annotations

from docs._reset_state import reset_default_state
from journeysdk import journey, step
from journeysdk.touchpoints.email import get_email_inbox, send_email, wait_for_email

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()
    reset_default_state(__file__)


def assert_welcome_email(
    inbox,
    receipt: dict[str, object],
    message: dict[str, object],
) -> bool:
    if receipt.get("subject") != "Welcome to Journey":
        raise AssertionError(f"Expected receipt subject 'Welcome to Journey', got {receipt.get('subject')!r}.")
    if message.get("subject") != "Welcome to Journey":
        raise AssertionError(f"Expected message subject 'Welcome to Journey', got {message.get('subject')!r}.")
    if message.get("from_address") != inbox.address:
        raise AssertionError(f"Expected sender {inbox.address!r}, got {message.get('from_address')!r}.")
    if message.get("to") != [inbox.address]:
        raise AssertionError(f"Expected recipient [{inbox.address!r}], got {message.get('to')!r}.")
    if message.get("text_body") != "Hello from Journey Cloud":
        raise AssertionError(f"Unexpected email body: {message.get('text_body')!r}")

    EVENTS.append("assert_welcome_email")
    return True


def send_welcome_email_and_verify_delivery() -> bool:
    inbox = get_email_inbox()
    receipt = send_email(
        inbox,
        subject="Welcome to Journey",
        text_body="Hello from Journey Cloud",
    )
    message = wait_for_email(
        inbox,
        subject_contains="Welcome",
        timeout=60,
        poll_interval=0.5,
    )
    return assert_welcome_email(inbox, receipt, message)


@journey
def cloud_email_journey() -> None:
    step(send_welcome_email_and_verify_delivery)
