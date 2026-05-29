"""Minimal tutorial journey showing one linear path."""

from __future__ import annotations

from journeysdk import journey, step
from docs._reset_state import reset_default_state

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()
    reset_default_state(__file__)


def create_customer_profile() -> dict[str, str]:
    profile = {
        "customer_id": "cust-001",
        "email": "alex@example.com",
    }
    EVENTS.append(f"create_customer_profile:{profile['customer_id']}")
    return profile


def send_welcome_message_and_verify_delivery(profile: dict[str, str]) -> dict[str, str]:
    if "customer_id" not in profile or "email" not in profile:
        raise AssertionError(f"Unexpected profile payload: {profile!r}")
    message = {
        "customer_id": profile["customer_id"],
        "channel": "email",
        "status": "sent",
    }
    if message.get("channel") != "email":
        raise AssertionError(f"Expected channel 'email', got {message.get('channel')!r}.")
    if message.get("status") != "sent":
        raise AssertionError(f"Expected status 'sent', got {message.get('status')!r}.")
    EVENTS.append(f"send_welcome_message_and_verify_delivery:{message['customer_id']}")
    return message


@journey
def first_journey() -> None:
    profile = step(create_customer_profile)
    step(send_welcome_message_and_verify_delivery, profile)
