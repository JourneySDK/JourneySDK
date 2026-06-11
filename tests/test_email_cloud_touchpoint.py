from __future__ import annotations

from pathlib import Path
import urllib.request

import journeysdk as journey_sdk
import pytest

from journeysdk.errors import CallableExecutionError
from journeysdk.touchpoints._email_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from journeysdk.touchpoints.email import EmailInbox, get_email_inbox, send_email, wait_for_email
from tests._cloud_stub import serve_in_background


def _configure_cloud_env(monkeypatch: pytest.MonkeyPatch, *, api_key: str, base_url: str) -> None:
    monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, api_key)
    monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, base_url)


def resolve_email_inbox() -> EmailInbox:
    return get_email_inbox()


def send_cloud_hello(inbox: EmailInbox) -> dict[str, object]:
    return send_email(
        inbox,
        subject="Cloud hello",
        text_body="Hello",
    )


def receive_cloud_email(inbox: EmailInbox) -> dict[str, object]:
    return wait_for_email(
        inbox,
        subject_contains="Cloud",
        timeout=0.05,
        poll_interval=0.01,
    )


def test_cloud_email_planning_does_not_require_env_or_network(monkeypatch: pytest.MonkeyPatch):
    original_urlopen = urllib.request.urlopen

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the cloud service.")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    def journey():
        inbox = journey_sdk.step(resolve_email_inbox)
        journey_sdk.step(send_cloud_hello, inbox)
        journey_sdk.step(receive_cloud_email, inbox)

    plan = journey_sdk.compile_journey(journey)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
    assert labels == ["resolve_email_inbox", "send_cloud_hello", "receive_cloud_email"]
    monkeypatch.setattr(urllib.request, "urlopen", original_urlopen)


def test_cloud_email_helpers_fail_clearly_when_no_config_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    def journey():
        journey_sdk.step(resolve_email_inbox)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    message = str(exc_info.value)
    assert JOURNEY_CLOUD_API_KEY_ENV in message
    assert JOURNEY_CLOUD_BASE_URL_ENV in message
    assert "JOURNEY_EMAIL_" not in message


def test_cloud_email_touchpoint_uses_default_inbox_and_returns_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        @journey_sdk.journey
        def cloud_email_journey() -> None:
            inbox = journey_sdk.step(resolve_email_inbox)
            journey_sdk.step(send_welcome_email, inbox)
            message = journey_sdk.step(receive_welcome_email, inbox)
            journey_sdk.step(assert_email_message, inbox, message)

        def send_welcome_email(inbox: EmailInbox) -> dict[str, object]:
            return send_email(inbox, subject="Welcome", text_body="Cloud hello")

        def receive_welcome_email(inbox: EmailInbox) -> dict[str, object]:
            return wait_for_email(
                inbox,
                subject_contains="Welcome",
                timeout=0.05,
                poll_interval=0.01,
            )

        def assert_email_message(inbox: EmailInbox, message: dict[str, object]) -> bool:
            assert inbox.transport == "cloud"
            assert message["from_address"] == cloud.default_email_address
            assert message["to"] == [cloud.default_email_address]
            assert message["subject"] == "Welcome"
            assert message["text_body"] == "Cloud hello"
            return True

        report = journey_sdk.execute(cloud_email_journey)

    case_report = report.case_reports[0]
    assert [record.label for record in case_report.records if record.label is not None] == [
        "resolve_email_inbox",
        "send_welcome_email",
        "receive_welcome_email",
        "assert_email_message",
    ]
    inbox = case_report.records[0].result
    receipt = case_report.records[1].result
    message = case_report.records[2].result
    assert inbox.address == cloud.default_email_address
    assert receipt["to"] == [cloud.default_email_address]
    assert receipt["transport"] == "cloud"
    assert message["to"] == [cloud.default_email_address]


def test_cloud_email_journey_supports_targeted_execution(monkeypatch: pytest.MonkeyPatch):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        def assert_message(message: dict[str, object]) -> bool:
            assert message["subject"] == "Targeted"
            return True

        def noop() -> bool:
            return True

        def journey():
            inbox = journey_sdk.step(resolve_email_inbox)
            if journey_sdk.branch(replay_from=inbox):
                journey_sdk.step(send_targeted_email, inbox)
                message = journey_sdk.step(receive_targeted_email, inbox)
                journey_sdk.step(assert_message, message)
            elif journey_sdk.branch(replay_from=inbox):
                journey_sdk.step(noop)

        def send_targeted_email(inbox: EmailInbox) -> dict[str, object]:
            return send_email(inbox, subject="Targeted", text_body="Email body")

        def receive_targeted_email(inbox: EmailInbox) -> dict[str, object]:
            return wait_for_email(
                inbox,
                subject_contains="Targeted",
                timeout=0.05,
                poll_interval=0.01,
            )

        targeted_report = journey_sdk.execute(journey, target_step="assert_message")
        assert len(targeted_report.case_reports) == 1
        assert targeted_report.case_reports[0].stopped_at_label == "assert_message"
        assert targeted_report.case_reports[0].replay_anchor == "resolve_email_inbox"


def test_cloud_email_inbox_handle_survives_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        seen_addresses: list[str] = []
        interrupt_once = {"enabled": True}

        def pause_once(inbox: EmailInbox) -> bool:
            seen_addresses.append(inbox.address)
            if interrupt_once["enabled"]:
                interrupt_once["enabled"] = False
                raise KeyboardInterrupt()
            return True

        def assert_message(message: dict[str, object]) -> bool:
            assert message["subject"] == "Resume"
            return True

        def journey():
            inbox = journey_sdk.step(resolve_email_inbox)
            journey_sdk.step(send_resume_email, inbox)
            journey_sdk.step(pause_once, inbox)
            message = journey_sdk.step(receive_resume_email, inbox)
            journey_sdk.step(assert_message, message)

        def send_resume_email(inbox: EmailInbox) -> dict[str, object]:
            return send_email(inbox, subject="Resume", text_body="Hello")

        def receive_resume_email(inbox: EmailInbox) -> dict[str, object]:
            return wait_for_email(
                inbox,
                subject_contains="Resume",
                timeout=0.05,
                poll_interval=0.01,
            )

        state_file = tmp_path / "journey-cloud-email.state"

        with pytest.raises(KeyboardInterrupt):
            journey_sdk.execute(journey, state=state_file)

        report = journey_sdk.execute(journey, state=state_file)
        record_labels = [
            record.label
            for record in report.case_reports[0].records
            if record.label is not None
        ]
        assert record_labels == [
            "resolve_email_inbox",
            "send_resume_email",
            "pause_once",
            "receive_resume_email",
            "assert_message",
        ]
        assert seen_addresses == [cloud.default_email_address, cloud.default_email_address]
