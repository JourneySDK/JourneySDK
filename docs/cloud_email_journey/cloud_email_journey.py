"""Tutorial journey showing how to use the cloud-hosted email tool."""

from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.email import get_email_inbox, send_email, wait_for_email

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()


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


@journey
def cloud_email_journey() -> None:
    inbox = step(get_email_inbox())
    receipt = step(
        send_email(
            subject="Welcome to Journey",
            text_body="Hello from Journey Cloud",
        )
    )
    message = step(
        wait_for_email(
            subject_contains="Welcome",
            timeout=0.05,
            poll_interval=0.01,
        ),
        inbox,
    )
    step(assert_welcome_email, inbox, receipt, message)
