from __future__ import annotations

import os
from pathlib import Path
import urllib.request

import journeysdk as journey_sdk
import pytest

from journeysdk.tools._email_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from journeysdk.tools.email import EmailInbox, get_email_inbox, send_email, wait_for_email
from tests._cloud_stub import serve_in_background


def _configure_cloud_env(monkeypatch: pytest.MonkeyPatch, *, api_key: str, base_url: str) -> None:
    _clear_direct_email_env(monkeypatch)
    monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, api_key)
    monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, base_url)


def _clear_direct_email_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("JOURNEY_EMAIL_"):
            monkeypatch.delenv(name, raising=False)


def test_cloud_email_planning_does_not_require_env_or_network(monkeypatch: pytest.MonkeyPatch):
    original_urlopen = urllib.request.urlopen

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the cloud service.")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    _clear_direct_email_env(monkeypatch)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    def journey():
        inbox = get_email_inbox()
        handle = journey_sdk.step(inbox)
        journey_sdk.step(send_email(subject="Cloud hello", text_body="Hello"), handle)
        journey_sdk.step(
            wait_for_email(
                subject_contains="Cloud",
                timeout=0.05,
                poll_interval=0.01,
            ),
            handle,
        )

    plan = journey_sdk.compile_journey(journey)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
    assert labels == ["get_email_inbox", "send_email", "receive_email"]
    monkeypatch.setattr(urllib.request, "urlopen", original_urlopen)


def test_cloud_email_helpers_fail_clearly_when_no_config_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_direct_email_env(monkeypatch)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        get_email_inbox()()

    message = str(exc_info.value)
    assert "JOURNEY_EMAIL_ADDRESS" in message
    assert JOURNEY_CLOUD_API_KEY_ENV in message
    assert JOURNEY_CLOUD_BASE_URL_ENV in message


def test_cloud_email_tool_uses_default_inbox_and_returns_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    with serve_in_background() as cloud:
        _configure_cloud_env(monkeypatch, api_key=cloud.api_key, base_url=cloud.base_url)

        @journey_sdk.journey
        def cloud_email_journey() -> None:
            inbox = journey_sdk.step(get_email_inbox())
            journey_sdk.step(send_email(subject="Welcome", text_body="Cloud hello"))
            message = journey_sdk.step(
                wait_for_email(
                    subject_contains="Welcome",
                    timeout=0.05,
                    poll_interval=0.01,
                ),
                inbox,
            )
            journey_sdk.step(assert_email_message, inbox, message)

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
        "get_email_inbox",
        "send_email",
        "receive_email",
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
            inbox = journey_sdk.step(get_email_inbox())
            if journey_sdk.branch(start_from=inbox):
                journey_sdk.step(
                    send_email(subject="Targeted", text_body="Email body"),
                    inbox,
                )
                message = journey_sdk.step(
                    wait_for_email(
                        subject_contains="Targeted",
                        timeout=0.05,
                        poll_interval=0.01,
                    ),
                    inbox,
                )
                journey_sdk.step(assert_message, message)
            elif journey_sdk.branch(start_from=inbox):
                journey_sdk.step(noop)

        targeted_report = journey_sdk.execute(journey, step="assert_message")
        assert len(targeted_report.case_reports) == 1
        assert targeted_report.case_reports[0].stopped_at_label == "assert_message"
        assert targeted_report.case_reports[0].replay_anchor == "get_email_inbox"


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
            inbox = journey_sdk.step(get_email_inbox())
            journey_sdk.step(send_email(subject="Resume", text_body="Hello"), inbox)
            journey_sdk.step(pause_once, inbox)
            message = journey_sdk.step(
                wait_for_email(
                    subject_contains="Resume",
                    timeout=0.05,
                    poll_interval=0.01,
                ),
                inbox,
            )
            journey_sdk.step(assert_message, message)

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
            "get_email_inbox",
            "send_email",
            "pause_once",
            "receive_email",
            "assert_message",
        ]
        assert seen_addresses == [cloud.default_email_address, cloud.default_email_address]
