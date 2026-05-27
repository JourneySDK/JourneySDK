"""Official HTTP touchpoint helpers."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from journeysdk.session import _require_executing_step
from journeysdk.types import JsonValue


@dataclass(frozen=True)
class HttpResponse:
    """Serializable HTTP response returned by Journey HTTP helpers."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def text(self, encoding: str = "utf-8", *, errors: str = "replace") -> str:
        """Decode the response body as text."""

        return self.body.decode(encoding, errors=errors)

    def json(self) -> JsonValue:
        """Decode the response body as JSON."""

        return json.loads(self.text())


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | str | None = None,
    json_body: JsonValue | None = None,
    timeout: float = 10.0,
) -> HttpResponse:
    """Perform one HTTP request from inside a journey step."""

    normalized_url = _normalize_url("http_request", url)
    normalized_method = _normalize_method("http_request", method)
    normalized_headers = _normalize_headers("http_request", headers)
    normalized_timeout = _normalize_positive_number(
        "http_request",
        "timeout",
        timeout,
    )
    request_headers, request_body = _request_body(
        owner="http_request",
        headers=normalized_headers,
        body=body,
        json_body=json_body,
    )
    _require_executing_step("http_request")
    request = urllib.request.Request(
        normalized_url,
        data=request_body,
        headers=dict(request_headers),
        method=normalized_method,
    )
    try:
        with urllib.request.urlopen(request, timeout=normalized_timeout) as response:
            return _response_from_urlopen(response)
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers=tuple((str(key), str(value)) for key, value in exc.headers.items()),
            body=exc.read(),
        )


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> JsonValue:
    """GET one URL and decode the response body as JSON."""

    return http_request(url, headers=headers, timeout=timeout).json()


def post_json(
    url: str,
    payload: JsonValue,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> JsonValue:
    """POST a JSON payload and decode the response body as JSON."""

    return http_request(
        url,
        method="POST",
        headers=headers,
        json_body=payload,
        timeout=timeout,
    ).json()


def wait_for_http(
    url: str,
    *,
    expected_status: int | Sequence[int] = 200,
    timeout: float = 60.0,
    poll_interval: float = 1.0,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Poll one URL until it returns an expected status code."""

    normalized_statuses = _normalize_expected_statuses(
        "wait_for_http",
        expected_status,
    )
    normalized_timeout = _normalize_nonnegative_number(
        "wait_for_http",
        "timeout",
        timeout,
    )
    normalized_poll_interval = _normalize_positive_number(
        "wait_for_http",
        "poll_interval",
        poll_interval,
    )
    _require_executing_step("wait_for_http")
    deadline = time.monotonic() + normalized_timeout
    last_error: BaseException | None = None
    last_response: HttpResponse | None = None
    while True:
        try:
            response = http_request(
                url,
                method=method,
                headers=headers,
                timeout=min(10.0, max(0.1, normalized_poll_interval)),
            )
            last_response = response
            if response.status in normalized_statuses:
                return response
        except BaseException as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            detail = (
                f"last status {last_response.status}"
                if last_response is not None
                else f"last error {type(last_error).__name__}: {last_error}"
            )
            raise TimeoutError(
                f"Timed out waiting for {url} to return one of "
                f"{sorted(normalized_statuses)} ({detail})."
            )
        time.sleep(normalized_poll_interval)


def _response_from_urlopen(response: object) -> HttpResponse:
    status = getattr(response, "status", None)
    if not isinstance(status, int):
        status = int(getattr(response, "code"))
    headers_obj = getattr(response, "headers", None)
    header_items = headers_obj.items() if headers_obj is not None else ()
    read = getattr(response, "read")
    body = read()
    if not isinstance(body, bytes):
        raise TypeError("http_request(...) expected response.read() to return bytes.")
    return HttpResponse(
        status=status,
        headers=tuple((str(key), str(value)) for key, value in header_items),
        body=body,
    )


def _request_body(
    *,
    owner: str,
    headers: tuple[tuple[str, str], ...],
    body: bytes | str | None,
    json_body: JsonValue | None,
) -> tuple[tuple[tuple[str, str], ...], bytes | None]:
    if body is not None and json_body is not None:
        raise ValueError(f"{owner}(...) accepts either body or json_body, not both.")
    if json_body is not None:
        if not any(key.lower() == "content-type" for key, _ in headers):
            headers += (("Content-Type", "application/json"),)
        return headers, json.dumps(json_body).encode("utf-8")
    if isinstance(body, str):
        return headers, body.encode("utf-8")
    if body is not None and not isinstance(body, bytes):
        raise TypeError(f"{owner}(..., body=...) expects bytes, str, or None.")
    return headers, body


def _normalize_url(owner: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner}(..., url=...) expects a non-blank URL string.")
    return value


def _normalize_method(owner: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner}(..., method=...) expects a non-blank string.")
    return value.strip().upper()


def _normalize_headers(
    owner: str,
    value: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise TypeError(f"{owner}(..., headers=...) expects a mapping or None.")
    headers: list[tuple[str, str]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(
                f"{owner}(..., headers=...) expects string keys and values."
            )
        headers.append((key, item))
    return tuple(headers)


def _normalize_expected_statuses(
    owner: str,
    value: int | Sequence[int],
) -> frozenset[int]:
    if isinstance(value, bool):
        raise TypeError(f"{owner}(..., expected_status=...) expects status integers.")
    if isinstance(value, int):
        statuses = [value]
    elif isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(f"{owner}(..., expected_status=...) expects an int or sequence.")
    else:
        statuses = list(value)
    if not statuses:
        raise ValueError(
            f"{owner}(..., expected_status=...) expects at least one status."
        )
    normalized: list[int] = []
    for status in statuses:
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError(
                f"{owner}(..., expected_status=...) expects status integers."
            )
        if status < 100 or status > 599:
            raise ValueError(
                f"{owner}(..., expected_status=...) expects HTTP status codes."
            )
        normalized.append(status)
    return frozenset(normalized)


def _normalize_nonnegative_number(owner: str, field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{owner}(..., {field}=...) expects a number.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{owner}(..., {field}=...) expects a finite number.")
    if normalized < 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a non-negative number.")
    return normalized


def _normalize_positive_number(owner: str, field: str, value: object) -> float:
    normalized = _normalize_nonnegative_number(owner, field, value)
    if normalized <= 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a positive number.")
    return normalized


__all__ = [
    "HttpResponse",
    "get_json",
    "http_request",
    "post_json",
    "wait_for_http",
]
