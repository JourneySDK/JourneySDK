from __future__ import annotations

import imaplib
import os
import smtplib
from email.message import EmailMessage

import journeysdk as journey_sdk
import pytest

from journeysdk.tools._email_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from journeysdk.tools.email import (
    JOURNEY_EMAIL_ADDRESS_ENV,
    JOURNEY_EMAIL_IMAP_HOST_ENV,
    JOURNEY_EMAIL_SMTP_HOST_ENV,
    EmailServerConfig,
    get_email_inbox,
    send_email,
    wait_for_email,
)

DIRECT_SERVER = EmailServerConfig(
    address="qa@example.test",
    from_address="journey@example.test",
    smtp_host="smtp.example.test",
    smtp_port=587,
    smtp_username="journey-user",
    smtp_password="journey-pass",
    smtp_starttls=True,
    imap_host="imap.example.test",
    imap_port=993,
    imap_username="journey-user",
    imap_password="journey-pass",
    imap_ssl=True,
)


class FakeSMTP:
    sent_messages: list[EmailMessage] = []
    logins: list[tuple[str, str]] = []
    starttls_calls = 0

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def ehlo(self) -> tuple[int, bytes]:
        return (250, b"ok")

    def starttls(self) -> tuple[int, bytes]:
        type(self).starttls_calls += 1
        return (220, b"tls")

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        type(self).logins.append((username, password))
        return (235, b"ok")

    def send_message(self, message: EmailMessage) -> None:
        type(self).sent_messages.append(message)

    def quit(self) -> tuple[int, bytes]:
        return (221, b"bye")


class FakeIMAP:
    messages: list[dict[str, object]] = []
    selected_mailbox: str | None = None

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"logged in"])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        del readonly
        type(self).selected_mailbox = mailbox
        return ("OK", [b"1"])

    def search(self, charset: object, criterion: str) -> tuple[str, list[bytes]]:
        del charset
        matches: list[bytes] = []
        for index, item in enumerate(type(self).messages, start=1):
            if item["mailbox"] != type(self).selected_mailbox:
                continue
            if criterion == "UNSEEN" and bool(item["seen"]):
                continue
            matches.append(str(index).encode("ascii"))
        return ("OK", [b" ".join(matches)])

    def fetch(self, message_seq: bytes, parts: str) -> tuple[str, list[object]]:
        del parts
        index = int(message_seq.decode("ascii")) - 1
        payload = type(self).messages[index]["raw"]
        return ("OK", [(b"RFC822", payload)])

    def store(self, message_seq: bytes, mode: str, flags: str) -> tuple[str, list[bytes]]:
        del mode, flags
        index = int(message_seq.decode("ascii")) - 1
        type(self).messages[index]["seen"] = True
        return ("OK", [b"stored"])

    def close(self) -> tuple[str, list[bytes]]:
        return ("OK", [b"closed"])

    def logout(self) -> tuple[str, list[bytes]]:
        return ("BYE", [b"logout"])


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    FakeSMTP.sent_messages = []
    FakeSMTP.logins = []
    FakeSMTP.starttls_calls = 0
    FakeIMAP.messages = []
    FakeIMAP.selected_mailbox = None


def _clear_direct_email_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("JOURNEY_EMAIL_"):
            monkeypatch.delenv(name, raising=False)
    yield
    FakeSMTP.sent_messages = []
    FakeSMTP.logins = []
    FakeSMTP.starttls_calls = 0
    FakeIMAP.messages = []
    FakeIMAP.selected_mailbox = None


def test_direct_email_planning_does_not_touch_smtp_or_imap(monkeypatch: pytest.MonkeyPatch):
    def fail_smtp(*args, **kwargs):
        raise AssertionError("compile_journey() should not send email.")

    def fail_imap(*args, **kwargs):
        raise AssertionError("compile_journey() should not fetch email.")

    monkeypatch.setattr(smtplib, "SMTP", fail_smtp)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fail_imap)
    monkeypatch.setattr(imaplib, "IMAP4", fail_imap)

    def journey():
        inbox = get_email_inbox(server=DIRECT_SERVER)
        journey_sdk.step(inbox)
        journey_sdk.step(send_email(subject="Welcome", text_body="Hello", server=DIRECT_SERVER))
        journey_sdk.step(
            wait_for_email(
                timeout=0.1,
                poll_interval=0.01,
                server=DIRECT_SERVER,
            )
        )

    plan = journey_sdk.compile_journey(journey)
    labels = [node.label for node in plan.case_plans[0].nodes if getattr(node, "label", None)]
    assert labels == ["get_email_inbox", "send_email", "receive_email"]


def test_direct_email_helpers_send_wait_and_mark_messages_seen(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(imaplib, "IMAP4", FakeIMAP)

    inbox = get_email_inbox(server=DIRECT_SERVER)()
    assert inbox.address == DIRECT_SERVER.address
    assert inbox.transport == "direct"
    assert inbox.mailbox == "INBOX"

    receipt = send_email(
        subject="Welcome",
        text_body="Hello from Journey",
        server=DIRECT_SERVER,
    )()

    assert receipt["from_address"] == DIRECT_SERVER.from_address
    assert receipt["to"] == [DIRECT_SERVER.address]
    assert receipt["subject"] == "Welcome"
    assert receipt["transport"] == "direct"
    assert FakeSMTP.logins == [(DIRECT_SERVER.smtp_username, DIRECT_SERVER.smtp_password)]
    assert FakeSMTP.starttls_calls == 1
    assert len(FakeSMTP.sent_messages) == 1

    FakeIMAP.messages.append(
        {
            "mailbox": "INBOX",
            "raw": FakeSMTP.sent_messages[0].as_bytes(),
            "seen": False,
        }
    )

    received = wait_for_email(
        timeout=0.01,
        poll_interval=0.001,
        subject_contains="Wel",
        server=DIRECT_SERVER,
    )()

    assert received["message_id"] == receipt["message_id"]
    assert received["from_address"] == DIRECT_SERVER.from_address
    assert received["to"] == [DIRECT_SERVER.address]
    assert received["subject"] == "Welcome"
    assert received["text_body"] is not None
    assert "Hello from Journey" in received["text_body"]
    assert FakeIMAP.messages[0]["seen"] is True

    with pytest.raises(TimeoutError):
        wait_for_email(
            timeout=0,
            poll_interval=0.001,
            subject_contains="Welcome",
            server=DIRECT_SERVER,
        )()

    log_output = capsys.readouterr().err
    assert "component=email event=inbox_resolve_success" in log_output
    assert "component=email event=email_send_success" in log_output
    assert "component=email event=email_wait_success" in log_output
    assert "component=email event=email_wait_timeout" in log_output
    assert DIRECT_SERVER.smtp_password not in log_output


def test_direct_email_config_error_mentions_direct_and_cloud_paths(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_direct_email_env(monkeypatch)
    monkeypatch.setenv(JOURNEY_EMAIL_ADDRESS_ENV, "qa@example.test")
    monkeypatch.delenv(JOURNEY_EMAIL_SMTP_HOST_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_EMAIL_IMAP_HOST_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        get_email_inbox()()

    message = str(exc_info.value)
    assert JOURNEY_EMAIL_SMTP_HOST_ENV in message
    assert JOURNEY_CLOUD_API_KEY_ENV in message
    assert JOURNEY_CLOUD_BASE_URL_ENV in message
