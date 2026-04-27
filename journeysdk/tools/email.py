"""Official email tool."""

from __future__ import annotations

import imaplib
import os
import smtplib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime
import hashlib
from typing import Any, Literal, Protocol, TypeAlias, TypedDict

from journeysdk.logger import get_logger

from ._email_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
    fetch_next_message,
    get_default_inbox as get_cloud_default_inbox,
    send_message as send_cloud_message,
)

JOURNEY_EMAIL_ADDRESS_ENV = "JOURNEY_EMAIL_ADDRESS"
JOURNEY_EMAIL_FROM_ADDRESS_ENV = "JOURNEY_EMAIL_FROM_ADDRESS"
JOURNEY_EMAIL_SMTP_HOST_ENV = "JOURNEY_EMAIL_SMTP_HOST"
JOURNEY_EMAIL_SMTP_PORT_ENV = "JOURNEY_EMAIL_SMTP_PORT"
JOURNEY_EMAIL_SMTP_USERNAME_ENV = "JOURNEY_EMAIL_SMTP_USERNAME"
JOURNEY_EMAIL_SMTP_PASSWORD_ENV = "JOURNEY_EMAIL_SMTP_PASSWORD"
JOURNEY_EMAIL_SMTP_STARTTLS_ENV = "JOURNEY_EMAIL_SMTP_STARTTLS"
JOURNEY_EMAIL_IMAP_HOST_ENV = "JOURNEY_EMAIL_IMAP_HOST"
JOURNEY_EMAIL_IMAP_PORT_ENV = "JOURNEY_EMAIL_IMAP_PORT"
JOURNEY_EMAIL_IMAP_USERNAME_ENV = "JOURNEY_EMAIL_IMAP_USERNAME"
JOURNEY_EMAIL_IMAP_PASSWORD_ENV = "JOURNEY_EMAIL_IMAP_PASSWORD"
JOURNEY_EMAIL_IMAP_SSL_ENV = "JOURNEY_EMAIL_IMAP_SSL"
JOURNEY_EMAIL_IMAP_MAILBOX_ENV = "JOURNEY_EMAIL_IMAP_MAILBOX"

_DIRECT_EMAIL_REQUIRED_ENV_VARS = (
    JOURNEY_EMAIL_ADDRESS_ENV,
    JOURNEY_EMAIL_SMTP_HOST_ENV,
    JOURNEY_EMAIL_SMTP_PORT_ENV,
    JOURNEY_EMAIL_SMTP_USERNAME_ENV,
    JOURNEY_EMAIL_SMTP_PASSWORD_ENV,
    JOURNEY_EMAIL_SMTP_STARTTLS_ENV,
    JOURNEY_EMAIL_IMAP_HOST_ENV,
    JOURNEY_EMAIL_IMAP_PORT_ENV,
    JOURNEY_EMAIL_IMAP_USERNAME_ENV,
    JOURNEY_EMAIL_IMAP_PASSWORD_ENV,
    JOURNEY_EMAIL_IMAP_SSL_ENV,
)
_LOGGER = get_logger("email")

EmailTransport: TypeAlias = Literal["direct", "cloud"]


class EmailSendReceipt(TypedDict):
    message_id: str
    from_address: str
    to: list[str]
    subject: str
    transport: str


class EmailReceivedMessage(TypedDict):
    message_id: str
    subject: str
    from_address: str
    to: list[str]
    cc: list[str]
    reply_to: str | None
    text_body: str | None
    html_body: str | None
    headers: dict[str, str]
    received_at: str


@dataclass(frozen=True)
class EmailServerConfig:
    """SMTP + IMAP configuration for one inbox."""

    address: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_starttls: bool
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    imap_ssl: bool
    from_address: str | None = None
    imap_mailbox: str = "INBOX"


@dataclass(frozen=True)
class EmailInbox:
    """Descriptor for one default email inbox."""

    address: str
    mailbox: str
    transport: EmailTransport
    api_base_url: str | None


class GetEmailInboxStep(Protocol):
    def __call__(self) -> EmailInbox:
        ...


class SendEmailStep(Protocol):
    def __call__(self, email_inbox: EmailInbox | None = None) -> EmailSendReceipt:
        ...


class WaitForEmailStep(Protocol):
    def __call__(self, email_inbox: EmailInbox | None = None) -> EmailReceivedMessage:
        ...


@dataclass(frozen=True)
class _DirectEnvResolution:
    server: EmailServerConfig | None
    missing_required_env_vars: tuple[str, ...]
    partially_configured: bool


@dataclass(frozen=True)
class _ResolvedTransport:
    transport: EmailTransport
    server: EmailServerConfig | None
    api_base_url: str | None
    address: str | None
    mailbox: str | None


def get_email_inbox(
    *,
    server: EmailServerConfig | None = None,
) -> GetEmailInboxStep:
    """Resolve the active default inbox for direct or cloud execution."""

    validated_server = (
        _validate_server_config(server=server, owner="get_email_inbox")
        if server is not None
        else None
    )

    def acquire_email_inbox() -> EmailInbox:
        _LOGGER.info(
            "inbox_resolve_start",
            "resolving email inbox",
            explicit_server=validated_server is not None,
        )
        if validated_server is not None:
            inbox = _direct_email_inbox(validated_server)
            _LOGGER.info(
                "inbox_resolve_success",
                "resolved direct email inbox",
                transport=inbox.transport,
                address=inbox.address,
                mailbox=inbox.mailbox,
            )
            return inbox

        direct_resolution = _load_direct_server_config_from_env(owner="get_email_inbox")
        if direct_resolution.server is not None:
            inbox = _direct_email_inbox(direct_resolution.server)
            _LOGGER.info(
                "inbox_resolve_success",
                "resolved direct email inbox",
                transport=inbox.transport,
                address=inbox.address,
                mailbox=inbox.mailbox,
            )
            return inbox

        inbox = _load_cloud_email_inbox(
            owner="get_email_inbox",
            direct_resolution=direct_resolution,
            api_base_url=None,
        )
        _LOGGER.info(
            "inbox_resolve_success",
            "resolved cloud email inbox",
            transport=inbox.transport,
            address=inbox.address,
            mailbox=inbox.mailbox,
            api_base_url=inbox.api_base_url,
        )
        return inbox

    _set_step_metadata(
        acquire_email_inbox,
        label="get_email_inbox",
        owner="get_email_inbox",
        attrs={},
    )
    return acquire_email_inbox


def send_email(
    *,
    to: str | Sequence[str] | None = None,
    subject: str,
    text_body: str | None = None,
    html_body: str | None = None,
    from_address: str | None = None,
    server: EmailServerConfig | None = None,
) -> SendEmailStep:
    """Send one email through the active default inbox."""

    validated_server = (
        _validate_server_config(server=server, owner="send_email")
        if server is not None
        else None
    )
    recipients = _normalize_recipient_addresses(
        owner="send_email",
        field="to",
        value=to,
        allow_none=True,
    )
    normalized_subject = _normalize_required_text(
        owner="send_email",
        field="subject",
        value=subject,
    )
    normalized_text_body = _normalize_optional_text(
        owner="send_email",
        field="text_body",
        value=text_body,
    )
    normalized_html_body = _normalize_optional_text(
        owner="send_email",
        field="html_body",
        value=html_body,
    )
    if normalized_text_body is None and normalized_html_body is None:
        raise ValueError(
            "send_email(..., text_body=..., html_body=...) expects at least one body."
        )
    normalized_from_address = _normalize_optional_address(
        owner="send_email",
        field="from_address",
        value=from_address,
    )

    def perform_send(email_inbox: EmailInbox | None = None) -> EmailSendReceipt:
        resolved = _resolve_transport(
            owner="send_email",
            email_inbox=email_inbox,
            server=validated_server,
        )
        resolved = _hydrate_cloud_inbox(
            owner="send_email",
            resolved=resolved,
        )
        _LOGGER.info(
            "email_send_start",
            "sending email",
            transport=resolved.transport,
            address=resolved.address,
            subject=normalized_subject,
        )
        resolved_recipients = recipients
        if resolved_recipients is None:
            if not resolved.address:
                raise RuntimeError(
                    "send_email(..., to=...) omitted a recipient, but no default inbox address was available."
                )
            resolved_recipients = [resolved.address]
        sender = _resolve_sender_address(
            owner="send_email",
            explicit_from_address=normalized_from_address,
            resolved=resolved,
        )
        message_id = _build_message_id(sender)

        if resolved.transport == "direct":
            server_config = _require_direct_server(
                owner="send_email",
                resolved=resolved,
            )
            outbound_message = _build_outbound_message(
                from_address=sender,
                to=resolved_recipients,
                subject=normalized_subject,
                text_body=normalized_text_body,
                html_body=normalized_html_body,
                message_id=message_id,
            )
            _send_smtp_message(server_config=server_config, message=outbound_message)
            receipt = _build_send_receipt(
                message_id=message_id,
                from_address=sender,
                to=resolved_recipients,
                subject=normalized_subject,
                transport="direct",
            )
            _LOGGER.info(
                "email_send_success",
                "email sent",
                transport="direct",
                message_id=receipt["message_id"],
                to_count=len(receipt["to"]),
            )
            return receipt

        payload = {
            "to": resolved_recipients,
            "subject": normalized_subject,
            "text_body": normalized_text_body,
            "html_body": normalized_html_body,
            "from_address": sender,
            "message_id": message_id,
        }
        receipt = send_cloud_message(
            payload=payload,
            api_base_url=resolved.api_base_url,
        )
        validated_receipt = _validate_send_receipt(receipt)
        _LOGGER.info(
            "email_send_success",
            "email sent",
            transport="cloud",
            message_id=validated_receipt["message_id"],
            to_count=len(validated_receipt["to"]),
        )
        return validated_receipt

    _set_step_metadata(
        perform_send,
        label="send_email",
        owner="send_email",
        attrs={},
    )
    return perform_send


def wait_for_email(
    *,
    timeout: float = 1.0,
    poll_interval: float = 0.1,
    subject_contains: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    unread_only: bool = True,
    server: EmailServerConfig | None = None,
) -> WaitForEmailStep:
    """Poll the active inbox until one matching email arrives."""

    validated_server = (
        _validate_server_config(server=server, owner="wait_for_email")
        if server is not None
        else None
    )
    timeout_seconds = _validate_nonnegative_number(
        owner="wait_for_email",
        field="timeout",
        value=timeout,
    )
    poll_interval_seconds = _validate_positive_number(
        owner="wait_for_email",
        field="poll_interval",
        value=poll_interval,
    )
    normalized_subject_filter = _normalize_optional_text(
        owner="wait_for_email",
        field="subject_contains",
        value=subject_contains,
    )
    normalized_from_filter = _normalize_optional_address(
        owner="wait_for_email",
        field="from_address",
        value=from_address,
    )
    normalized_to_filter = _normalize_optional_address(
        owner="wait_for_email",
        field="to_address",
        value=to_address,
    )
    if not isinstance(unread_only, bool):
        raise TypeError("wait_for_email(..., unread_only=...) expects a boolean.")

    def receive_email(email_inbox: EmailInbox | None = None) -> EmailReceivedMessage:
        resolved = _resolve_transport(
            owner="wait_for_email",
            email_inbox=email_inbox,
            server=validated_server,
        )
        _LOGGER.info(
            "email_wait_start",
            "waiting for email",
            transport=resolved.transport,
            address=resolved.address,
            mailbox=resolved.mailbox,
            timeout=timeout_seconds,
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            if resolved.transport == "direct":
                server_config = _require_direct_server(
                    owner="wait_for_email",
                    resolved=resolved,
                )
                payload = _fetch_direct_message(
                    server_config=server_config,
                    mailbox=resolved.mailbox or server_config.imap_mailbox,
                    subject_contains=normalized_subject_filter,
                    from_address=normalized_from_filter,
                    to_address=normalized_to_filter,
                    unread_only=unread_only,
                )
            else:
                payload = fetch_next_message(
                    payload={
                        "subject_contains": normalized_subject_filter,
                        "from_address": normalized_from_filter,
                        "to_address": normalized_to_filter,
                        "unread_only": unread_only,
                    },
                    api_base_url=resolved.api_base_url,
                )

            if payload is not None:
                message = _validate_received_message(payload)
                _LOGGER.info(
                    "email_wait_success",
                    "received email",
                    transport=resolved.transport,
                    message_id=message["message_id"],
                    subject=message["subject"],
                )
                return message

            if time.monotonic() >= deadline:
                descriptor = resolved.address or resolved.mailbox or "the configured inbox"
                _LOGGER.warning(
                    "email_wait_timeout",
                    "timed out waiting for email",
                    transport=resolved.transport,
                    descriptor=descriptor,
                    timeout=timeout_seconds,
                )
                raise TimeoutError(
                    f"Timed out waiting for email in {descriptor} after {timeout} seconds."
                )
            _LOGGER.debug(
                "email_wait_poll_empty",
                "email poll returned no matching message",
                transport=resolved.transport,
                poll_interval=poll_interval_seconds,
            )
            time.sleep(poll_interval_seconds)

    _set_step_metadata(
        receive_email,
        label="receive_email",
        owner="wait_for_email",
        attrs={},
    )
    return receive_email


def _resolve_transport(
    *,
    owner: str,
    email_inbox: EmailInbox | None,
    server: EmailServerConfig | None,
) -> _ResolvedTransport:
    if email_inbox is not None:
        if not isinstance(email_inbox, EmailInbox):
            raise TypeError(f"{owner}(...) expects an EmailInbox step result when provided.")
        if email_inbox.transport == "direct":
            direct_resolution = (
                _DirectEnvResolution(
                    server=server,
                    missing_required_env_vars=(),
                    partially_configured=False,
                )
                if server is not None
                else _load_direct_server_config_from_env(owner=owner)
            )
            direct_server = direct_resolution.server
            if direct_server is None:
                raise _build_missing_configuration_error(
                    owner=owner,
                    direct_resolution=direct_resolution,
                )
            return _ResolvedTransport(
                transport="direct",
                server=direct_server,
                api_base_url=None,
                address=email_inbox.address,
                mailbox=email_inbox.mailbox,
            )
        if email_inbox.transport == "cloud":
            if server is not None:
                raise ValueError(
                    f"{owner}(...) received a cloud EmailInbox, but server=... forces direct mode."
                )
            return _ResolvedTransport(
                transport="cloud",
                server=None,
                api_base_url=email_inbox.api_base_url,
                address=email_inbox.address,
                mailbox=email_inbox.mailbox,
            )
        raise ValueError(
            f"{owner}(...) received an EmailInbox with unknown transport {email_inbox.transport!r}."
        )

    if server is not None:
        return _ResolvedTransport(
            transport="direct",
            server=server,
            api_base_url=None,
            address=server.address,
            mailbox=server.imap_mailbox,
        )

    direct_resolution = _load_direct_server_config_from_env(owner=owner)
    if direct_resolution.server is not None:
        return _ResolvedTransport(
            transport="direct",
            server=direct_resolution.server,
            api_base_url=None,
            address=direct_resolution.server.address,
            mailbox=direct_resolution.server.imap_mailbox,
        )

    return _ResolvedTransport(
        transport="cloud",
        server=None,
        api_base_url=None,
        address=None,
        mailbox=None,
    )


def _load_cloud_email_inbox(
    *,
    owner: str,
    direct_resolution: _DirectEnvResolution,
    api_base_url: str | None,
) -> EmailInbox:
    _LOGGER.info(
        "cloud_inbox_resolve_start",
        "loading cloud email inbox",
        api_base_url=api_base_url,
    )
    try:
        resolved_api_base_url, payload = get_cloud_default_inbox(api_base_url=api_base_url)
    except RuntimeError as exc:
        if _is_missing_cloud_config_error(exc):
            raise _build_missing_configuration_error(
                owner=owner,
                direct_resolution=direct_resolution,
            ) from exc
        raise
    inbox = _payload_to_inbox(
        owner=owner,
        payload=payload,
        api_base_url=resolved_api_base_url,
    )
    _LOGGER.info(
        "cloud_inbox_resolve_success",
        "loaded cloud email inbox",
        address=inbox.address,
        mailbox=inbox.mailbox,
        api_base_url=inbox.api_base_url,
    )
    return inbox


def _validate_server_config(
    *,
    server: EmailServerConfig,
    owner: str,
) -> EmailServerConfig:
    if not isinstance(server, EmailServerConfig):
        raise TypeError(f"{owner}(..., server=...) expects an EmailServerConfig.")
    return EmailServerConfig(
        address=_normalize_required_address(
            owner=owner,
            field="server.address",
            value=server.address,
        ),
        from_address=_normalize_optional_address(
            owner=owner,
            field="server.from_address",
            value=server.from_address,
        ),
        smtp_host=_normalize_required_text(
            owner=owner,
            field="server.smtp_host",
            value=server.smtp_host,
        ),
        smtp_port=_validate_positive_int(
            owner=owner,
            field="server.smtp_port",
            value=server.smtp_port,
        ),
        smtp_username=_normalize_required_text(
            owner=owner,
            field="server.smtp_username",
            value=server.smtp_username,
        ),
        smtp_password=_normalize_required_text(
            owner=owner,
            field="server.smtp_password",
            value=server.smtp_password,
        ),
        smtp_starttls=_validate_bool(
            owner=owner,
            field="server.smtp_starttls",
            value=server.smtp_starttls,
        ),
        imap_host=_normalize_required_text(
            owner=owner,
            field="server.imap_host",
            value=server.imap_host,
        ),
        imap_port=_validate_positive_int(
            owner=owner,
            field="server.imap_port",
            value=server.imap_port,
        ),
        imap_username=_normalize_required_text(
            owner=owner,
            field="server.imap_username",
            value=server.imap_username,
        ),
        imap_password=_normalize_required_text(
            owner=owner,
            field="server.imap_password",
            value=server.imap_password,
        ),
        imap_ssl=_validate_bool(
            owner=owner,
            field="server.imap_ssl",
            value=server.imap_ssl,
        ),
        imap_mailbox=_normalize_mailbox(
            owner=owner,
            field="server.imap_mailbox",
            value=server.imap_mailbox,
        ),
    )


def _load_direct_server_config_from_env(*, owner: str) -> _DirectEnvResolution:
    raw_values = {
        JOURNEY_EMAIL_ADDRESS_ENV: os.environ.get(JOURNEY_EMAIL_ADDRESS_ENV, "").strip(),
        JOURNEY_EMAIL_FROM_ADDRESS_ENV: os.environ.get(
            JOURNEY_EMAIL_FROM_ADDRESS_ENV, ""
        ).strip(),
        JOURNEY_EMAIL_SMTP_HOST_ENV: os.environ.get(JOURNEY_EMAIL_SMTP_HOST_ENV, "").strip(),
        JOURNEY_EMAIL_SMTP_PORT_ENV: os.environ.get(JOURNEY_EMAIL_SMTP_PORT_ENV, "").strip(),
        JOURNEY_EMAIL_SMTP_USERNAME_ENV: os.environ.get(
            JOURNEY_EMAIL_SMTP_USERNAME_ENV, ""
        ).strip(),
        JOURNEY_EMAIL_SMTP_PASSWORD_ENV: os.environ.get(
            JOURNEY_EMAIL_SMTP_PASSWORD_ENV, ""
        ).strip(),
        JOURNEY_EMAIL_SMTP_STARTTLS_ENV: os.environ.get(
            JOURNEY_EMAIL_SMTP_STARTTLS_ENV, ""
        ).strip(),
        JOURNEY_EMAIL_IMAP_HOST_ENV: os.environ.get(JOURNEY_EMAIL_IMAP_HOST_ENV, "").strip(),
        JOURNEY_EMAIL_IMAP_PORT_ENV: os.environ.get(JOURNEY_EMAIL_IMAP_PORT_ENV, "").strip(),
        JOURNEY_EMAIL_IMAP_USERNAME_ENV: os.environ.get(
            JOURNEY_EMAIL_IMAP_USERNAME_ENV, ""
        ).strip(),
        JOURNEY_EMAIL_IMAP_PASSWORD_ENV: os.environ.get(
            JOURNEY_EMAIL_IMAP_PASSWORD_ENV, ""
        ).strip(),
        JOURNEY_EMAIL_IMAP_SSL_ENV: os.environ.get(JOURNEY_EMAIL_IMAP_SSL_ENV, "").strip(),
        JOURNEY_EMAIL_IMAP_MAILBOX_ENV: os.environ.get(
            JOURNEY_EMAIL_IMAP_MAILBOX_ENV, ""
        ).strip(),
    }
    present_env_vars = [name for name, value in raw_values.items() if value]
    if not present_env_vars:
        return _DirectEnvResolution(
            server=None,
            missing_required_env_vars=(),
            partially_configured=False,
        )

    missing_required = tuple(
        name for name in _DIRECT_EMAIL_REQUIRED_ENV_VARS if not raw_values.get(name)
    )
    if missing_required:
        return _DirectEnvResolution(
            server=None,
            missing_required_env_vars=missing_required,
            partially_configured=True,
        )

    try:
        server = EmailServerConfig(
            address=_normalize_required_address(
                owner=owner,
                field=JOURNEY_EMAIL_ADDRESS_ENV,
                value=raw_values[JOURNEY_EMAIL_ADDRESS_ENV],
            ),
            from_address=_normalize_optional_address(
                owner=owner,
                field=JOURNEY_EMAIL_FROM_ADDRESS_ENV,
                value=raw_values[JOURNEY_EMAIL_FROM_ADDRESS_ENV] or None,
            ),
            smtp_host=_normalize_required_text(
                owner=owner,
                field=JOURNEY_EMAIL_SMTP_HOST_ENV,
                value=raw_values[JOURNEY_EMAIL_SMTP_HOST_ENV],
            ),
            smtp_port=_parse_positive_int(
                owner=owner,
                field=JOURNEY_EMAIL_SMTP_PORT_ENV,
                value=raw_values[JOURNEY_EMAIL_SMTP_PORT_ENV],
            ),
            smtp_username=_normalize_required_text(
                owner=owner,
                field=JOURNEY_EMAIL_SMTP_USERNAME_ENV,
                value=raw_values[JOURNEY_EMAIL_SMTP_USERNAME_ENV],
            ),
            smtp_password=_normalize_required_text(
                owner=owner,
                field=JOURNEY_EMAIL_SMTP_PASSWORD_ENV,
                value=raw_values[JOURNEY_EMAIL_SMTP_PASSWORD_ENV],
            ),
            smtp_starttls=_parse_bool(
                owner=owner,
                field=JOURNEY_EMAIL_SMTP_STARTTLS_ENV,
                value=raw_values[JOURNEY_EMAIL_SMTP_STARTTLS_ENV],
            ),
            imap_host=_normalize_required_text(
                owner=owner,
                field=JOURNEY_EMAIL_IMAP_HOST_ENV,
                value=raw_values[JOURNEY_EMAIL_IMAP_HOST_ENV],
            ),
            imap_port=_parse_positive_int(
                owner=owner,
                field=JOURNEY_EMAIL_IMAP_PORT_ENV,
                value=raw_values[JOURNEY_EMAIL_IMAP_PORT_ENV],
            ),
            imap_username=_normalize_required_text(
                owner=owner,
                field=JOURNEY_EMAIL_IMAP_USERNAME_ENV,
                value=raw_values[JOURNEY_EMAIL_IMAP_USERNAME_ENV],
            ),
            imap_password=_normalize_required_text(
                owner=owner,
                field=JOURNEY_EMAIL_IMAP_PASSWORD_ENV,
                value=raw_values[JOURNEY_EMAIL_IMAP_PASSWORD_ENV],
            ),
            imap_ssl=_parse_bool(
                owner=owner,
                field=JOURNEY_EMAIL_IMAP_SSL_ENV,
                value=raw_values[JOURNEY_EMAIL_IMAP_SSL_ENV],
            ),
            imap_mailbox=_normalize_mailbox(
                owner=owner,
                field=JOURNEY_EMAIL_IMAP_MAILBOX_ENV,
                value=raw_values[JOURNEY_EMAIL_IMAP_MAILBOX_ENV] or "INBOX",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc

    return _DirectEnvResolution(
        server=server,
        missing_required_env_vars=(),
        partially_configured=False,
    )


def _send_smtp_message(*, server_config: EmailServerConfig, message: EmailMessage) -> None:
    _LOGGER.debug(
        "smtp_send_start",
        "sending email through SMTP",
        host=server_config.smtp_host,
        port=server_config.smtp_port,
        starttls=server_config.smtp_starttls,
    )
    smtp_client = smtplib.SMTP(server_config.smtp_host, server_config.smtp_port, timeout=5)
    try:
        smtp_client.ehlo()
        if server_config.smtp_starttls:
            smtp_client.starttls()
            smtp_client.ehlo()
        smtp_client.login(server_config.smtp_username, server_config.smtp_password)
        smtp_client.send_message(message)
    except OSError as exc:
        _LOGGER.error(
            "smtp_send_failure",
            "SMTP send failed",
            host=server_config.smtp_host,
            port=server_config.smtp_port,
            error=_format_exception(exc),
        )
        raise RuntimeError(
            f"Could not send email through SMTP server {server_config.smtp_host}:{server_config.smtp_port}: {exc}"
        ) from exc
    finally:
        try:
            smtp_client.quit()
        except OSError:
            pass
    _LOGGER.debug(
        "smtp_send_success",
        "SMTP send completed",
        host=server_config.smtp_host,
        port=server_config.smtp_port,
    )


def _fetch_direct_message(
    *,
    server_config: EmailServerConfig,
    mailbox: str,
    subject_contains: str | None,
    from_address: str | None,
    to_address: str | None,
    unread_only: bool,
) -> EmailReceivedMessage | None:
    _LOGGER.debug(
        "imap_fetch_start",
        "fetching email through IMAP",
        host=server_config.imap_host,
        port=server_config.imap_port,
        mailbox=mailbox,
        unread_only=unread_only,
    )
    imap_client = _build_imap_client(server_config)
    try:
        _require_imap_ok(
            status=imap_client.login(
                server_config.imap_username,
                server_config.imap_password,
            )[0],
            action="login",
        )
        _require_imap_ok(
            status=imap_client.select(mailbox, readonly=False)[0],
            action=f"select mailbox {mailbox!r}",
        )
        search_status, raw_matches = imap_client.search(
            None,
            "UNSEEN" if unread_only else "ALL",
        )
        _require_imap_ok(status=search_status, action="search mailbox")
        if not raw_matches:
            _LOGGER.debug(
                "imap_fetch_empty",
                "IMAP search returned no messages",
                mailbox=mailbox,
            )
            return None

        message_ids = raw_matches[0].split()
        for message_seq in message_ids:
            fetch_status, fetched = imap_client.fetch(message_seq, "(RFC822)")
            _require_imap_ok(status=fetch_status, action=f"fetch email {message_seq!r}")
            raw_message = _extract_imap_message_bytes(fetched)
            parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
            normalized = _normalize_received_message_from_message(parsed, raw_message=raw_message)
            if not _message_matches(
                message=normalized,
                subject_contains=subject_contains,
                from_address=from_address,
                to_address=to_address,
            ):
                continue
            store_status, _ = imap_client.store(message_seq, "+FLAGS", "(\\Seen)")
            _require_imap_ok(status=store_status, action=f"mark email {message_seq!r} as seen")
            _LOGGER.debug(
                "imap_fetch_success",
                "matched email through IMAP",
                mailbox=mailbox,
                message_id=normalized["message_id"],
                subject=normalized["subject"],
            )
            return normalized
        _LOGGER.debug(
            "imap_fetch_no_match",
            "IMAP messages did not match filters",
            mailbox=mailbox,
            candidates=len(message_ids),
        )
        return None
    except (imaplib.IMAP4.error, OSError) as exc:
        _LOGGER.error(
            "imap_fetch_failure",
            "IMAP fetch failed",
            host=server_config.imap_host,
            port=server_config.imap_port,
            mailbox=mailbox,
            error=_format_exception(exc),
        )
        raise RuntimeError(
            f"Could not fetch email through IMAP server {server_config.imap_host}:{server_config.imap_port}: {exc}"
        ) from exc
    finally:
        try:
            imap_client.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            imap_client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _build_imap_client(server_config: EmailServerConfig) -> imaplib.IMAP4:
    if server_config.imap_ssl:
        return imaplib.IMAP4_SSL(
            server_config.imap_host,
            server_config.imap_port,
        )
    return imaplib.IMAP4(
        server_config.imap_host,
        server_config.imap_port,
    )


def _extract_imap_message_bytes(fetched: Any) -> bytes:
    if not isinstance(fetched, list):
        raise RuntimeError("IMAP fetch did not return a valid message payload.")
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("IMAP fetch did not include raw message bytes.")


def _build_outbound_message(
    *,
    from_address: str,
    to: list[str],
    subject: str,
    text_body: str | None,
    html_body: str | None,
    message_id: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message["Message-Id"] = message_id
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    if text_body is not None:
        message.set_content(text_body)
        if html_body is not None:
            message.add_alternative(html_body, subtype="html")
    else:
        message.add_alternative(html_body or "", subtype="html")
    return message


def _normalize_received_message_from_message(
    message: EmailMessage,
    *,
    raw_message: bytes | None = None,
) -> EmailReceivedMessage:
    text_body, html_body = _extract_bodies(message)
    received_at = _received_at_from_message(message)
    from_address = _first_address(message, "From") or "unknown@example.test"
    message_id = str(message.get("Message-Id", "")).strip() or _synthetic_message_id(
        from_address=from_address,
        raw_message=raw_message,
    )
    return {
        "message_id": message_id,
        "subject": str(message.get("Subject", "")),
        "from_address": from_address,
        "to": _addresses_for_header(message, "To"),
        "cc": _addresses_for_header(message, "Cc"),
        "reply_to": _first_address(message, "Reply-To"),
        "text_body": text_body,
        "html_body": html_body,
        "headers": {
            name.lower(): value
            for name, value in message.items()
        },
        "received_at": received_at,
    }


def _extract_bodies(message: EmailMessage) -> tuple[str | None, str | None]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == "attachment":
                continue
            try:
                content = part.get_content()
            except LookupError:
                continue
            if not isinstance(content, str):
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                text_parts.append(content)
            elif content_type == "text/html":
                html_parts.append(content)
    else:
        try:
            content = message.get_content()
        except LookupError:
            content = None
        if isinstance(content, str):
            if message.get_content_type() == "text/html":
                html_parts.append(content)
            else:
                text_parts.append(content)

    text_body = "\n".join(text_parts).strip() or None
    html_body = "\n".join(html_parts).strip() or None
    return text_body, html_body


def _received_at_from_message(message: EmailMessage) -> str:
    raw_date = str(message.get("Date", "")).strip()
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError, IndexError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _message_matches(
    *,
    message: EmailReceivedMessage,
    subject_contains: str | None,
    from_address: str | None,
    to_address: str | None,
) -> bool:
    if subject_contains is not None:
        subject = str(message.get("subject", ""))
        if subject_contains not in subject:
            return False
    if from_address is not None and message.get("from_address") != from_address:
        return False
    if to_address is not None and to_address not in list(message.get("to", [])):
        return False
    return True


def _build_message_id(from_address: str) -> str:
    domain = from_address.split("@", 1)[1] if "@" in from_address else None
    return make_msgid(domain=domain)


def _synthetic_message_id(*, from_address: str, raw_message: bytes | None) -> str:
    domain = from_address.split("@", 1)[1] if "@" in from_address else "journey.local"
    if raw_message:
        digest = hashlib.sha256(raw_message).hexdigest()[:24]
        return f"<journey-{digest}@{domain}>"
    return _build_message_id(from_address)


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _resolve_sender_address(
    *,
    owner: str,
    explicit_from_address: str | None,
    resolved: _ResolvedTransport,
) -> str:
    if explicit_from_address is not None:
        return explicit_from_address
    if resolved.transport == "direct":
        server_config = _require_direct_server(owner=owner, resolved=resolved)
        if server_config.from_address is not None:
            return server_config.from_address
        return server_config.address
    if resolved.address is not None:
        return resolved.address
    hydrated = _hydrate_cloud_inbox(
        owner=owner,
        resolved=resolved,
    )
    if hydrated.address is None:
        raise RuntimeError(f"{owner}(...) could not resolve a cloud inbox address.")
    return hydrated.address


def _build_missing_configuration_error(
    *,
    owner: str,
    direct_resolution: _DirectEnvResolution,
) -> RuntimeError:
    direct_requirements = ", ".join(_DIRECT_EMAIL_REQUIRED_ENV_VARS)
    message = (
        f"{owner}(...) could not resolve email transport. "
        "Configure direct email by passing server=EmailServerConfig(...) or by setting "
        f"{direct_requirements}. "
        "Alternatively configure Journey Cloud by setting "
        f"{JOURNEY_CLOUD_API_KEY_ENV} and {JOURNEY_CLOUD_BASE_URL_ENV}."
    )
    if direct_resolution.partially_configured and direct_resolution.missing_required_env_vars:
        missing = ", ".join(direct_resolution.missing_required_env_vars)
        message += f" The current direct email environment is missing: {missing}."
    return RuntimeError(message)


def _payload_to_inbox(
    *,
    owner: str,
    payload: Mapping[str, object],
    api_base_url: str,
) -> EmailInbox:
    address = payload.get("address")
    mailbox = payload.get("mailbox")
    transport = payload.get("transport")
    if not isinstance(address, str) or not address:
        raise RuntimeError(f"Journey cloud returned an invalid inbox address for {owner}(...).")
    if not isinstance(mailbox, str) or not mailbox:
        raise RuntimeError(f"Journey cloud returned an invalid inbox mailbox for {owner}(...).")
    if transport != "cloud":
        raise RuntimeError("Journey cloud returned an unexpected inbox transport.")
    return EmailInbox(
        address=address,
        mailbox=mailbox,
        transport="cloud",
        api_base_url=api_base_url,
    )


def _validate_send_receipt(receipt: Mapping[str, object]) -> EmailSendReceipt:
    message_id = receipt.get("message_id")
    from_address = receipt.get("from_address")
    subject = receipt.get("subject")
    transport = receipt.get("transport")
    to = receipt.get("to")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(from_address, str)
        or not from_address
        or not isinstance(subject, str)
        or not isinstance(transport, str)
        or not isinstance(to, list)
    ):
        raise RuntimeError("Journey cloud returned an invalid email receipt.")
    if any(not isinstance(item, str) or not item for item in to):
        raise RuntimeError("Journey cloud returned an invalid email recipient list.")
    recipients = list(to)
    return {
        "message_id": message_id,
        "from_address": from_address,
        "to": recipients,
        "subject": subject,
        "transport": transport,
    }


def _validate_received_message(payload: Mapping[str, object]) -> EmailReceivedMessage:
    required_string_fields = ("message_id", "subject", "from_address", "received_at")
    for field in required_string_fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Email payload is missing a valid {field!r}.")

    address_lists: dict[str, list[str]] = {}
    for field in ("to", "cc"):
        values = payload.get(field)
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item for item in values
        ):
            raise RuntimeError(f"Email payload is missing a valid {field!r} list.")
        address_lists[field] = list(values)

    reply_to = payload.get("reply_to")
    if reply_to is not None and (not isinstance(reply_to, str) or not reply_to):
        raise RuntimeError("Email payload has an invalid 'reply_to' value.")

    for field in ("text_body", "html_body"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise RuntimeError(f"Email payload has an invalid {field!r} value.")

    headers = payload.get("headers")
    if not isinstance(headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers.items()
    ):
        raise RuntimeError("Email payload has invalid headers.")
    normalized_headers = {
        key: value
        for key, value in headers.items()
        if isinstance(key, str) and isinstance(value, str)
    }

    return {
        "message_id": payload["message_id"],
        "subject": payload["subject"],
        "from_address": payload["from_address"],
        "to": address_lists["to"],
        "cc": address_lists["cc"],
        "reply_to": payload.get("reply_to"),
        "text_body": payload.get("text_body"),
        "html_body": payload.get("html_body"),
        "headers": normalized_headers,
        "received_at": payload["received_at"],
    }


def _build_send_receipt(
    *,
    message_id: str,
    from_address: str,
    to: list[str],
    subject: str,
    transport: str,
) -> EmailSendReceipt:
    return {
        "message_id": message_id,
        "from_address": from_address,
        "to": to,
        "subject": subject,
        "transport": transport,
    }


def _hydrate_cloud_inbox(
    *,
    owner: str,
    resolved: _ResolvedTransport,
) -> _ResolvedTransport:
    if resolved.transport != "cloud" or resolved.address is not None:
        return resolved
    inbox = _load_cloud_email_inbox(
        owner=owner,
        direct_resolution=_DirectEnvResolution(
            server=None,
            missing_required_env_vars=(),
            partially_configured=False,
        ),
        api_base_url=resolved.api_base_url,
    )
    return _ResolvedTransport(
        transport="cloud",
        server=None,
        api_base_url=inbox.api_base_url,
        address=inbox.address,
        mailbox=inbox.mailbox,
    )


def _direct_email_inbox(server_config: EmailServerConfig) -> EmailInbox:
    return EmailInbox(
        address=server_config.address,
        mailbox=server_config.imap_mailbox,
        transport="direct",
        api_base_url=None,
    )


def _require_direct_server(
    *,
    owner: str,
    resolved: _ResolvedTransport,
) -> EmailServerConfig:
    if resolved.server is None:
        raise RuntimeError(f"{owner}(...) could not resolve a direct email server.")
    return resolved.server


def _is_missing_cloud_config_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return (
        JOURNEY_CLOUD_API_KEY_ENV in message
        or JOURNEY_CLOUD_BASE_URL_ENV in message
    )


def _set_step_metadata(
    fn: object,
    *,
    label: str,
    owner: str,
    attrs: Mapping[str, object],
) -> None:
    setattr(fn, "__name__", label)
    setattr(fn, "__qualname__", f"{owner}.<locals>.{label}")
    for key, value in attrs.items():
        setattr(fn, key, value)


def _require_imap_ok(*, status: str, action: str) -> None:
    if status != "OK":
        raise RuntimeError(f"IMAP could not {action}.")


def _validate_nonnegative_number(*, owner: str, field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{owner}(..., {field}=...) expects a number.")
    if float(value) < 0:
        raise ValueError(f"{owner}(..., {field}=...) expects zero or more seconds.")
    return float(value)


def _validate_positive_number(*, owner: str, field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{owner}(..., {field}=...) expects a number.")
    if float(value) <= 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a positive number.")
    return float(value)


def _validate_positive_int(*, owner: str, field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{owner}(..., {field}=...) expects an integer.")
    if value <= 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a positive integer.")
    return value


def _validate_bool(*, owner: str, field: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{owner}(..., {field}=...) expects a boolean.")
    return value


def _parse_positive_int(*, owner: str, field: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{owner}(..., {field}=...) expects an integer.") from exc
    if parsed <= 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a positive integer.")
    return parsed


def _parse_bool(*, owner: str, field: str, value: str) -> bool:
    normalized = value.strip().lower()
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    if normalized in truthy:
        return True
    if normalized in falsy:
        return False
    raise ValueError(
        f"{owner}(..., {field}=...) expects one of true/false/1/0/yes/no/on/off."
    )


def _normalize_required_text(*, owner: str, field: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{owner}(..., {field}=...) expects a non-blank string.")
    return normalized


def _normalize_optional_text(*, owner: str, field: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string or None.")
    return value


def _normalize_required_address(*, owner: str, field: str, value: object) -> str:
    normalized = _normalize_required_text(owner=owner, field=field, value=value)
    if "@" not in normalized:
        raise ValueError(f"{owner}(..., {field}=...) expects an email address.")
    return normalized


def _normalize_optional_address(*, owner: str, field: str, value: object) -> str | None:
    if value is None:
        return None
    return _normalize_required_address(owner=owner, field=field, value=value)


def _normalize_mailbox(*, owner: str, field: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string mailbox.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{owner}(..., {field}=...) expects a non-blank mailbox.")
    return normalized


def _normalize_recipient_addresses(
    *,
    owner: str,
    field: str,
    value: object,
    allow_none: bool,
) -> list[str] | None:
    if value is None:
        return None if allow_none else []
    if isinstance(value, str):
        return [_normalize_required_address(owner=owner, field=field, value=value)]
    if not isinstance(value, Sequence):
        raise TypeError(f"{owner}(..., {field}=...) expects a string or sequence of strings.")

    recipients: list[str] = []
    for item in value:
        recipients.append(_normalize_required_address(owner=owner, field=field, value=item))
    if not recipients:
        raise ValueError(f"{owner}(..., {field}=...) expects at least one recipient.")
    return recipients


def _addresses_for_header(message: EmailMessage, header: str) -> list[str]:
    return [
        address
        for _, address in getaddresses(message.get_all(header, []))
        if address
    ]


def _first_address(message: EmailMessage, header: str) -> str | None:
    addresses = _addresses_for_header(message, header)
    if not addresses:
        return None
    return addresses[0]


__all__ = [
    "EmailReceivedMessage",
    "EmailInbox",
    "EmailSendReceipt",
    "EmailServerConfig",
    "EmailTransport",
    "GetEmailInboxStep",
    "SendEmailStep",
    "WaitForEmailStep",
    "get_email_inbox",
    "send_email",
    "wait_for_email",
]
