"""Internal helpers for webhook touchpointing."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Protocol, TypeAlias, TypedDict, cast
from urllib.parse import parse_qs

from journeysdk.types import JsonValue


class _HeaderCollection(Protocol):
    def items(self) -> Iterable[tuple[object, object]]:
        ...


WebhookQuery: TypeAlias = dict[str, list[str]]
WebhookHeaders: TypeAlias = dict[str, str]


class WebhookRequestPayload(TypedDict):
    method: str
    url: str
    path: str
    query: WebhookQuery
    headers: WebhookHeaders
    body_text: str | None
    body_json: JsonValue
    body_base64: str
    received_at: str


def normalize_path(path: str, *, owner: str) -> str:
    """Validate and normalize a webhook path."""

    if not isinstance(path, str):
        raise TypeError(f"{owner}(..., path=...) expects a string path.")
    normalized = "/" + path.lstrip("/")
    if normalized == "/":
        raise ValueError(f"{owner}(..., path=...) expects a non-root path.")
    return normalized


def sanitize_label_fragment(path: str) -> str:
    """Convert a path to a stable step-label fragment."""

    letters = [char if char.isalnum() else "_" for char in path.strip("/")]
    collapsed = "".join(letters).strip("_")
    return collapsed or "root"


def build_step_label(*, prefix: str, path: str) -> str:
    """Build a step label from a path and prefix."""

    return f"{prefix}{sanitize_label_fragment(path)}"


def _lower_headers(raw_headers: _HeaderCollection) -> WebhookHeaders:
    return {
        str(name).lower(): str(value)
        for name, value in raw_headers.items()
    }


def _body_text(raw_body: bytes) -> str | None:
    try:
        return raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _body_json(*, headers: WebhookHeaders, text: str | None) -> JsonValue:
    content_type = headers.get("content-type", "")
    if "application/json" not in content_type or text is None:
        return None
    try:
        return cast(JsonValue, json.loads(text))
    except json.JSONDecodeError:
        return None


def build_request_payload(
    *,
    method: str,
    url: str,
    path: str,
    query_string: str,
    headers: _HeaderCollection,
    raw_body: bytes,
    received_at: datetime | None = None,
) -> WebhookRequestPayload:
    """Build the normalized webhook request payload returned by webhook helpers."""

    lowered_headers = _lower_headers(headers)
    text = _body_text(raw_body)
    moment = received_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return {
        "method": method,
        "url": url,
        "path": path,
        "query": parse_qs(query_string, keep_blank_values=True),
        "headers": lowered_headers,
        "body_text": text,
        "body_json": _body_json(headers=lowered_headers, text=text),
        "body_base64": base64.b64encode(raw_body).decode("ascii"),
        "received_at": moment.isoformat(),
    }
