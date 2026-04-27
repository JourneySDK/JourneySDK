"""Official webhook tool."""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from journeysdk.logger import get_logger
from journeysdk.session import get_session
from ._webhook_shared import (
    WebhookHeaders,
    WebhookQuery,
    WebhookRequestPayload,
    build_step_label,
    normalize_path as normalize_cloud_path,
)

from ._webhook_cloud import create_webhook_endpoint, fetch_next_request
from ._webhook_local import (
    build_poll_url,
    build_public_url,
    ensure_local_host,
    normalize_path,
)

_LOGGER = get_logger("webhook")


@dataclass(frozen=True)
class CloudWebhookEndpoint:
    """Descriptor for one journey cloud webhook endpoint."""

    endpoint_id: str
    path: str
    url: str
    api_base_url: str


class LocalWebhookStep(Protocol):
    url: str
    path: str
    port: int

    def __call__(self) -> WebhookRequestPayload:
        ...


class CloudWebhookEndpointStep(Protocol):
    def __call__(self) -> CloudWebhookEndpoint:
        ...


class CloudWebhookRequestStep(Protocol):
    def __call__(self, endpoint: CloudWebhookEndpoint) -> WebhookRequestPayload:
        ...


def _guard_duplicate_registration(*, session: object, port: int, path: str) -> None:
    epoch = getattr(session, "_journey_webhook_epoch", 0)
    seen_by_epoch = getattr(session, "_journey_webhook_seen_by_epoch", None)
    if seen_by_epoch is None:
        seen_by_epoch = {}
        setattr(session, "_journey_webhook_seen_by_epoch", seen_by_epoch)

    seen = seen_by_epoch.setdefault(epoch, set())
    descriptor = (port, path)
    if descriptor in seen:
        raise ValueError(
            f"host_webhook_endpoint(...) registered the same endpoint twice: http://localhost:{port}{path}"
        )
    seen.add(descriptor)

    stale_epochs = [item for item in seen_by_epoch if item != epoch]
    for stale_epoch in stale_epochs:
        seen_by_epoch.pop(stale_epoch, None)


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


def host_webhook_endpoint(
    *,
    port: int,
    path: str,
    timeout: float = 1.0,
    poll_interval: float = 0.1,
) -> LocalWebhookStep:
    """Host a local webhook endpoint and return a step callable that polls it."""

    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("host_webhook_endpoint(..., port=...) expects an integer port.")
    if port <= 0:
        raise ValueError("host_webhook_endpoint(..., port=...) expects a positive port.")
    timeout_seconds = _validate_nonnegative_number(
        owner="host_webhook_endpoint",
        field="timeout",
        value=timeout,
    )
    poll_interval_seconds = _validate_positive_number(
        owner="host_webhook_endpoint",
        field="poll_interval",
        value=poll_interval,
    )

    normalized_path = normalize_path(path)
    public_url = build_public_url(port=port, path=normalized_path)
    poll_url = build_poll_url(port=port, path=normalized_path)
    label = build_step_label(prefix="receive_webhook_", path=normalized_path)

    session = get_session()
    if session is not None:
        _guard_duplicate_registration(
            session=session,
            port=port,
            path=normalized_path,
        )
    if getattr(session, "mode", None) == "run":
        _LOGGER.info(
            "local_host_start",
            "ensuring local webhook host",
            port=port,
            path=normalized_path,
            url=public_url,
        )
        ensure_local_host(port=port, path=normalized_path)
        _LOGGER.info(
            "local_host_success",
            "local webhook host is ready",
            port=port,
            path=normalized_path,
            url=public_url,
        )

    def receive_webhook() -> WebhookRequestPayload:
        _LOGGER.info(
            "webhook_wait_start",
            "waiting for local webhook request",
            path=normalized_path,
            url=public_url,
            timeout=timeout_seconds,
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            with urllib.request.urlopen(poll_url, timeout=max(timeout_seconds, 0.1)) as response:
                if response.status == 200:
                    payload = response.read()
                    decoded = cast(
                        WebhookRequestPayload,
                        json.loads(payload.decode("utf-8")),
                    )
                    _LOGGER.info(
                        "webhook_wait_success",
                        "received local webhook request",
                        path=normalized_path,
                        method=decoded.get("method"),
                    )
                    return decoded
                if response.status != 204:
                    raise RuntimeError(
                        f"Unexpected webhook poll response {response.status} for {public_url}."
                    )

            if time.monotonic() >= deadline:
                _LOGGER.warning(
                    "webhook_wait_timeout",
                    "timed out waiting for local webhook request",
                    path=normalized_path,
                    url=public_url,
                    timeout=timeout_seconds,
                )
                raise TimeoutError(
                    f"Timed out waiting for a webhook on {public_url} after {timeout} seconds."
                )
            _LOGGER.debug(
                "webhook_wait_poll_empty",
                "local webhook poll returned no request",
                path=normalized_path,
                poll_interval=poll_interval_seconds,
            )
            time.sleep(poll_interval_seconds)

    _set_step_metadata(
        receive_webhook,
        label=label,
        owner="host_webhook_endpoint",
        attrs={
            "url": public_url,
            "path": normalized_path,
            "port": port,
        },
    )
    return cast(LocalWebhookStep, receive_webhook)


def get_webhook_endpoint(*, path: str) -> CloudWebhookEndpointStep:
    """Acquire one cloud-hosted webhook endpoint and return it as a step value."""

    normalized_path = normalize_cloud_path(path, owner="get_webhook_endpoint")
    label = build_step_label(prefix="get_webhook_", path=normalized_path)

    def acquire_webhook_endpoint() -> CloudWebhookEndpoint:
        _LOGGER.info(
            "cloud_endpoint_create_start",
            "creating cloud webhook endpoint",
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
            path=endpoint.path,
            endpoint_id=endpoint.endpoint_id,
            url=endpoint.url,
        )
        return endpoint

    _set_step_metadata(
        acquire_webhook_endpoint,
        label=label,
        owner="get_webhook_endpoint",
        attrs={"path": normalized_path},
    )
    return acquire_webhook_endpoint


def wait_for_webhook_request(
    *,
    path: str,
    timeout: float = 1.0,
    poll_interval: float = 0.1,
) -> CloudWebhookRequestStep:
    """Poll one cloud-hosted webhook endpoint for the next received request."""

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
    normalized_path = normalize_cloud_path(path, owner="wait_for_webhook_request")
    label = build_step_label(prefix="receive_webhook_", path=normalized_path)

    def receive_webhook(endpoint: CloudWebhookEndpoint) -> WebhookRequestPayload:
        if not isinstance(endpoint, CloudWebhookEndpoint):
            raise TypeError(
                "wait_for_webhook_request(...) expects a CloudWebhookEndpoint step result."
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
                    path=normalized_path,
                    endpoint_id=endpoint.endpoint_id,
                    method=payload.get("method"),
                )
                return payload

            if time.monotonic() >= deadline:
                _LOGGER.warning(
                    "cloud_webhook_wait_timeout",
                    "timed out waiting for cloud webhook request",
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

    _set_step_metadata(
        receive_webhook,
        label=label,
        owner="wait_for_webhook_request",
        attrs={"path": normalized_path},
    )
    return receive_webhook


__all__ = [
    "CloudWebhookEndpointStep",
    "CloudWebhookRequestStep",
    "CloudWebhookEndpoint",
    "LocalWebhookStep",
    "WebhookHeaders",
    "WebhookQuery",
    "WebhookRequestPayload",
    "get_webhook_endpoint",
    "host_webhook_endpoint",
    "wait_for_webhook_request",
]
