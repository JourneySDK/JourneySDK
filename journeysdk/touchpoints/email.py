"""Official cloud-hosted email touchpoint."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import make_msgid
from typing import Literal, TypeAlias, TypedDict

from journeysdk.logger import PrettyLine, get_logger, pretty_row
from journeysdk.session import _require_executing_step

from ._email_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
    fetch_next_message,
    get_default_inbox as get_cloud_default_inbox,
    send_message as send_cloud_message,
)

_LOGGER = get_logger("email")


def _email_row(detail: object) -> PrettyLine:
    return pretty_row("Email", detail, indent=8, label_width=27, style="touchpoint")

EmailTransport: TypeAlias = Literal["cloud"]


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
class EmailInbox:
    """Descriptor for the Journey Cloud-hosted default email inbox."""

    address: str
    mailbox: str
    transport: EmailTransport
    api_base_url: str


def get_email_inbox() -> EmailInbox:
    """Resolve the active Journey Cloud-hosted default inbox."""

    _require_executing_step("get_email_inbox")
    _LOGGER.info(
        "inbox_resolve_start",
        "resolving cloud email inbox",
        pretty=_email_row("resolving cloud email inbox"),
    )
    inbox = _load_cloud_email_inbox(owner="get_email_inbox", api_base_url=None)
    _LOGGER.info(
        "inbox_resolve_success",
        "resolved cloud email inbox",
        pretty=_email_row("resolved cloud email inbox"),
        transport=inbox.transport,
        address=inbox.address,
        mailbox=inbox.mailbox,
        api_base_url=inbox.api_base_url,
    )
    return inbox


def send_email(
    email_inbox: EmailInbox | None = None,
    *,
    to: str | Sequence[str] | None = None,
    subject: str,
    text_body: str | None = None,
    html_body: str | None = None,
    from_address: str | None = None,
) -> EmailSendReceipt:
    """Send one email through Journey Cloud."""

    _require_executing_step("send_email")
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
    inbox = _resolve_cloud_inbox(owner="send_email", email_inbox=email_inbox)
    _LOGGER.info(
        "email_send_start",
        "sending email",
        pretty=_email_row("sending email"),
        transport=inbox.transport,
        address=inbox.address,
        subject=normalized_subject,
    )
    resolved_recipients = recipients or [inbox.address]
    sender = normalized_from_address or inbox.address
    message_id = _build_message_id(sender)
    receipt = send_cloud_message(
        payload={
            "to": resolved_recipients,
            "subject": normalized_subject,
            "text_body": normalized_text_body,
            "html_body": normalized_html_body,
            "from_address": sender,
            "message_id": message_id,
        },
        api_base_url=inbox.api_base_url,
    )
    validated_receipt = _validate_send_receipt(receipt)
    _LOGGER.info(
        "email_send_success",
        "email sent",
        pretty=_email_row("email sent"),
        transport="cloud",
        message_id=validated_receipt["message_id"],
        to_count=len(validated_receipt["to"]),
    )
    return validated_receipt


def wait_for_email(
    email_inbox: EmailInbox | None = None,
    *,
    timeout: float = 1.0,
    poll_interval: float = 0.1,
    subject_contains: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    unread_only: bool = True,
) -> EmailReceivedMessage:
    """Poll the Journey Cloud-hosted inbox until one matching email arrives."""

    _require_executing_step("wait_for_email")
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
    inbox = _resolve_cloud_inbox(owner="wait_for_email", email_inbox=email_inbox)
    _LOGGER.info(
        "email_wait_start",
        "waiting for email",
        pretty=_email_row("waiting for email"),
        transport=inbox.transport,
        address=inbox.address,
        mailbox=inbox.mailbox,
        timeout=timeout_seconds,
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        payload = fetch_next_message(
            payload={
                "subject_contains": normalized_subject_filter,
                "from_address": normalized_from_filter,
                "to_address": normalized_to_filter,
                "unread_only": unread_only,
            },
            api_base_url=inbox.api_base_url,
        )
        if payload is not None:
            message = _validate_received_message(payload)
            _LOGGER.info(
                "email_wait_success",
                "received email",
                pretty=_email_row("received email"),
                transport=inbox.transport,
                message_id=message["message_id"],
                subject=message["subject"],
            )
            return message

        if time.monotonic() >= deadline:
            descriptor = inbox.address or inbox.mailbox
            _LOGGER.warning(
                "email_wait_timeout",
                "timed out waiting for email",
                pretty="Email timed out waiting for message",
                transport=inbox.transport,
                descriptor=descriptor,
                timeout=timeout_seconds,
            )
            raise TimeoutError(
                f"Timed out waiting for email in {descriptor} after {timeout} seconds."
            )
        _LOGGER.debug(
            "email_wait_poll_empty",
            "email poll returned no matching message",
            transport=inbox.transport,
            poll_interval=poll_interval_seconds,
        )
        time.sleep(poll_interval_seconds)


def _resolve_cloud_inbox(
    *,
    owner: str,
    email_inbox: EmailInbox | None,
) -> EmailInbox:
    if email_inbox is None:
        return _load_cloud_email_inbox(owner=owner, api_base_url=None)
    if not isinstance(email_inbox, EmailInbox):
        raise TypeError(f"{owner}(...) expects an EmailInbox step result when provided.")
    if email_inbox.transport != "cloud":
        raise ValueError(
            f"{owner}(...) received an EmailInbox with unknown transport "
            f"{email_inbox.transport!r}."
        )
    return email_inbox


def _load_cloud_email_inbox(
    *,
    owner: str,
    api_base_url: str | None,
) -> EmailInbox:
    _LOGGER.info(
        "cloud_inbox_resolve_start",
        "loading cloud email inbox",
        pretty=_email_row("loading cloud email inbox"),
        api_base_url=api_base_url,
    )
    try:
        resolved_api_base_url, payload = get_cloud_default_inbox(api_base_url=api_base_url)
    except RuntimeError as exc:
        if _is_missing_cloud_config_error(exc):
            raise _build_missing_configuration_error(owner=owner) from exc
        raise
    inbox = _payload_to_inbox(
        owner=owner,
        payload=payload,
        api_base_url=resolved_api_base_url,
    )
    _LOGGER.info(
        "cloud_inbox_resolve_success",
        "loaded cloud email inbox",
        pretty=_email_row("loaded cloud email inbox"),
        address=inbox.address,
        mailbox=inbox.mailbox,
        api_base_url=inbox.api_base_url,
    )
    return inbox


def _build_missing_configuration_error(*, owner: str) -> RuntimeError:
    return RuntimeError(
        f"{owner}(...) requires Journey Cloud email hosting. Set "
        f"{JOURNEY_CLOUD_API_KEY_ENV} and {JOURNEY_CLOUD_BASE_URL_ENV} before "
        "executing the step."
    )


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
        or transport != "cloud"
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
        "transport": "cloud",
    }


def _validate_received_message(payload: Mapping[str, object]) -> EmailReceivedMessage:
    message_id = payload.get("message_id")
    subject = payload.get("subject")
    from_address = payload.get("from_address")
    received_at = payload.get("received_at")
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError("Email payload is missing a valid 'message_id'.")
    if not isinstance(subject, str) or not subject:
        raise RuntimeError("Email payload is missing a valid 'subject'.")
    if not isinstance(from_address, str) or not from_address:
        raise RuntimeError("Email payload is missing a valid 'from_address'.")
    if not isinstance(received_at, str) or not received_at:
        raise RuntimeError("Email payload is missing a valid 'received_at'.")

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

    text_body = payload.get("text_body")
    html_body = payload.get("html_body")
    if text_body is not None and not isinstance(text_body, str):
        raise RuntimeError("Email payload has an invalid 'text_body' value.")
    if html_body is not None and not isinstance(html_body, str):
        raise RuntimeError("Email payload has an invalid 'html_body' value.")

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
        "message_id": message_id,
        "subject": subject,
        "from_address": from_address,
        "to": address_lists["to"],
        "cc": address_lists["cc"],
        "reply_to": reply_to,
        "text_body": text_body,
        "html_body": html_body,
        "headers": normalized_headers,
        "received_at": received_at,
    }


def _build_message_id(from_address: str) -> str:
    domain = from_address.split("@", 1)[1] if "@" in from_address else None
    return make_msgid(domain=domain)


def _is_missing_cloud_config_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return (
        JOURNEY_CLOUD_API_KEY_ENV in message
        or JOURNEY_CLOUD_BASE_URL_ENV in message
    )


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


__all__ = [
    "EmailReceivedMessage",
    "EmailInbox",
    "EmailSendReceipt",
    "EmailTransport",
    "get_email_inbox",
    "send_email",
    "wait_for_email",
]
