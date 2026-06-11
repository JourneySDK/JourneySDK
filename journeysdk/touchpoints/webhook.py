"""Official cloud-hosted webhook touchpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass

from journeysdk.logger import PrettyLine, get_logger, pretty_row
from journeysdk.session import _require_executing_step
from ._webhook_shared import (
    WebhookHeaders,
    WebhookQuery,
    WebhookRequestPayload,
    normalize_path,
)

from ._webhook_cloud import create_webhook_endpoint, fetch_next_request

_LOGGER = get_logger("webhook")


def _webhook_row(detail: object) -> PrettyLine:
    return pretty_row("Webhook", detail, indent=8, label_width=27, style="touchpoint")


@dataclass(frozen=True)
class CloudWebhookEndpoint:
    """Descriptor for one Journey Cloud webhook endpoint."""

    endpoint_id: str
    path: str
    url: str
    api_base_url: str


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
        raise ValueError(
            f"{owner}(..., {field}=...) expects a positive number."
        )
    return float(value)


def get_webhook_endpoint(*, path: str) -> CloudWebhookEndpoint:
    """Acquire one Journey Cloud-hosted webhook endpoint and return it as a step value."""

    _require_executing_step("get_webhook_endpoint")
    normalized_path = normalize_path(path, owner="get_webhook_endpoint")
    _LOGGER.info(
        "cloud_endpoint_create_start",
        "creating cloud webhook endpoint",
        pretty=_webhook_row("creating cloud webhook endpoint"),
        path=normalized_path,
    )
    api_base_url, payload = create_webhook_endpoint(path=normalized_path)
    endpoint_id = payload.get("endpoint_id")
    public_url = payload.get("url")
    response_path = payload.get("path")
    if not isinstance(endpoint_id, str) or not endpoint_id:
        raise RuntimeError("Journey cloud returned an endpoint without an endpoint_id.")
    if not isinstance(public_url, str) or not public_url:
        raise RuntimeError("Journey cloud returned an endpoint without a public URL.")
    if response_path != normalized_path:
        raise RuntimeError(
            "Journey cloud returned an endpoint for a different webhook path than requested."
        )
    endpoint = CloudWebhookEndpoint(
        endpoint_id=endpoint_id,
        path=normalized_path,
        url=public_url,
        api_base_url=api_base_url,
    )
    _LOGGER.info(
        "cloud_endpoint_create_success",
        "created cloud webhook endpoint",
        pretty=_webhook_row("created cloud webhook endpoint"),
        path=endpoint.path,
        endpoint_id=endpoint.endpoint_id,
        url=endpoint.url,
    )
    return endpoint


def wait_for_webhook_request(
    endpoint: CloudWebhookEndpoint,
    *,
    path: str | None = None,
    timeout: float = 1.0,
    poll_interval: float = 0.1,
) -> WebhookRequestPayload:
    """Poll one Journey Cloud-hosted webhook endpoint for the next received request."""

    _require_executing_step("wait_for_webhook_request")
    if not isinstance(endpoint, CloudWebhookEndpoint):
        raise TypeError(
            "wait_for_webhook_request(...) expects a CloudWebhookEndpoint step value."
        )
    timeout_seconds = _validate_nonnegative_number(
        owner="wait_for_webhook_request",
        field="timeout",
        value=timeout,
    )
    poll_interval_seconds = _validate_positive_number(
        owner="wait_for_webhook_request",
        field="poll_interval",
        value=poll_interval,
    )
    normalized_path = (
        endpoint.path if path is None else normalize_path(path, owner="wait_for_webhook_request")
    )
    if endpoint.path != normalized_path:
        raise ValueError(
            "wait_for_webhook_request(...) received a CloudWebhookEndpoint for "
            f"{endpoint.path!r}, expected {normalized_path!r}."
        )

    deadline = time.monotonic() + timeout_seconds
    _LOGGER.info(
        "cloud_webhook_wait_start",
        "waiting for cloud webhook request",
        pretty=_webhook_row("waiting for cloud webhook request"),
        path=normalized_path,
        endpoint_id=endpoint.endpoint_id,
        timeout=timeout_seconds,
    )
    while True:
        payload = fetch_next_request(
            endpoint_id=endpoint.endpoint_id,
            api_base_url=endpoint.api_base_url,
        )
        if payload is not None:
            if payload.get("path") != normalized_path:
                raise RuntimeError(
                    "Journey cloud returned a webhook payload for a different path than requested."
                )
            _LOGGER.info(
                "cloud_webhook_wait_success",
                "received cloud webhook request",
                pretty=_webhook_row("received cloud webhook request"),
                path=normalized_path,
                endpoint_id=endpoint.endpoint_id,
                method=payload.get("method"),
            )
            return payload

        if time.monotonic() >= deadline:
            _LOGGER.warning(
                "cloud_webhook_wait_timeout",
                "timed out waiting for cloud webhook request",
                pretty="Webhook timed out waiting for request",
                path=normalized_path,
                endpoint_id=endpoint.endpoint_id,
                timeout=timeout_seconds,
            )
            raise TimeoutError(
                f"Timed out waiting for a webhook on {endpoint.url} after {timeout} seconds."
            )
        _LOGGER.debug(
            "cloud_webhook_wait_poll_empty",
            "cloud webhook poll returned no request",
            path=normalized_path,
            endpoint_id=endpoint.endpoint_id,
            poll_interval=poll_interval_seconds,
        )
        time.sleep(poll_interval_seconds)


__all__ = [
    "CloudWebhookEndpoint",
    "WebhookHeaders",
    "WebhookQuery",
    "WebhookRequestPayload",
    "get_webhook_endpoint",
    "wait_for_webhook_request",
]
