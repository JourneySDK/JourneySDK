"""Tutorial journeys for discovery, --journey, and --json."""

from __future__ import annotations

from journeysdk import journey, step

EVENTS: list[str] = []


def reset_demo_state() -> None:
    EVENTS.clear()


def load_welcome_email_job() -> dict[str, str]:
    job = {
        "job_id": "welcome-001",
        "template": "welcome_email",
    }
    EVENTS.append(f"load_welcome_email_job:{job['job_id']}")
    return job


def assert_welcome_email_job(job: dict[str, str]) -> bool:
    EVENTS.append(f"assert_welcome_email_job:{job['job_id']}")
    if job.get("template") != "welcome_email":
        raise AssertionError(f"Unexpected template: {job.get('template')!r}")
    return True


def load_invoice_reminder() -> dict[str, str]:
    reminder = {
        "reminder_id": "invoice-001",
        "channel": "email",
    }
    EVENTS.append(f"load_invoice_reminder:{reminder['reminder_id']}")
    return reminder


def assert_invoice_reminder(reminder: dict[str, str]) -> bool:
    EVENTS.append(f"assert_invoice_reminder:{reminder['reminder_id']}")
    if reminder.get("channel") != "email":
        raise AssertionError(f"Unexpected channel: {reminder.get('channel')!r}")
    return True


@journey
def welcome_email_journey() -> None:
    job = step(load_welcome_email_job)
    step(assert_welcome_email_job, job)


@journey
def invoice_reminder_journey() -> None:
    reminder = step(load_invoice_reminder)
    step(assert_invoice_reminder, reminder)
