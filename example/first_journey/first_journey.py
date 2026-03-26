"""Minimal tutorial journey showing one linear path."""

from __future__ import annotations

import journey

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()


def create_customer_profile() -> dict[str, str]:
    profile = {
        "customer_id": "cust-001",
        "email": "alex@example.com",
    }
    EVENTS.append(f"create_customer_profile:{profile['customer_id']}")
    return profile


def send_welcome_message(profile: dict[str, str]) -> dict[str, str]:
    if "customer_id" not in profile or "email" not in profile:
        raise AssertionError(f"Unexpected profile payload: {profile!r}")
    message = {
        "customer_id": profile["customer_id"],
        "channel": "email",
        "status": "sent",
    }
    EVENTS.append(f"send_welcome_message:{message['customer_id']}")
    return message


def assert_welcome_message_sent(message: dict[str, str]) -> bool:
    EVENTS.append(f"assert_welcome_message_sent:{message['customer_id']}")
    if message.get("channel") != "email":
        raise AssertionError(f"Expected channel 'email', got {message.get('channel')!r}.")
    if message.get("status") != "sent":
        raise AssertionError(f"Expected status 'sent', got {message.get('status')!r}.")
    return True


@journey.journey
def first_journey() -> None:
    profile = journey.step(create_customer_profile)
    message = journey.step(send_welcome_message, profile)
    journey.step(assert_welcome_message_sent, message)
