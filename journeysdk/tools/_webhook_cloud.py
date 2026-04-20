"""Private cloud client for the official webhook tool."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import cast

from journeysdk.types import JsonObject

from ._webhook_shared import WebhookRequestPayload

JOURNEY_CLOUD_API_KEY_ENV = "JOURNEY_CLOUD_API_KEY"
JOURNEY_CLOUD_BASE_URL_ENV = "JOURNEY_CLOUD_BASE_URL"


@dataclass(frozen=True)
class _CloudWebhookConfig:
    api_key: str
    api_base_url: str


def load_cloud_config(*, api_base_url: str | None = None) -> _CloudWebhookConfig:
    """Load the journey cloud API key and base URL from the environment."""

    api_key = os.environ.get(JOURNEY_CLOUD_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(
            "Journey cloud webhook access requires JOURNEY_CLOUD_API_KEY to be set."
        )

    raw_base_url = api_base_url
    if raw_base_url is None:
        raw_base_url = os.environ.get(JOURNEY_CLOUD_BASE_URL_ENV, "").strip()
    if not raw_base_url:
        raise RuntimeError(
            "Journey cloud webhook access requires JOURNEY_CLOUD_BASE_URL to be set."
        )

    return _CloudWebhookConfig(
        api_key=api_key,
        api_base_url=raw_base_url.rstrip("/"),
    )


def create_webhook_endpoint(*, path: str) -> tuple[str, JsonObject]:
    """Create one cloud-hosted webhook endpoint."""

    config = load_cloud_config()
    payload = _request_json(
        config=config,
        method="POST",
        route="/v1/webhook-endpoints",
        payload={"path": path},
        allow_no_content=False,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Journey cloud returned an invalid endpoint response.")
    return config.api_base_url, payload


def fetch_next_request(
    *,
    endpoint_id: str,
    api_base_url: str | None,
) -> WebhookRequestPayload | None:
    """Fetch one queued webhook request from the cloud service."""

    config = load_cloud_config(api_base_url=api_base_url)
    payload = _request_json(
        config=config,
        method="POST",
        route=f"/v1/webhook-endpoints/{endpoint_id}/requests/next",
        payload={},
        allow_no_content=True,
    )
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeError("Journey cloud returned an invalid webhook request payload.")
    return cast(WebhookRequestPayload, payload)


def _request_json(
    *,
    config: _CloudWebhookConfig,
    method: str,
    route: str,
    payload: dict[str, object],
    allow_no_content: bool,
) -> JsonObject | None:
    url = f"{config.api_base_url}{route}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read())
        raise RuntimeError(
            f"Journey cloud request to {url} failed with status {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach the journey cloud service at {config.api_base_url}: {exc.reason}"
        ) from exc

    if status == 204 and allow_no_content:
        return None

    if not raw_body:
        return {}

    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Journey cloud returned a non-JSON response for {url}."
        ) from exc

    if not isinstance(decoded, dict):
        raise RuntimeError(
            f"Journey cloud returned an unexpected response payload for {url}."
        )
    return cast(JsonObject, decoded)


def _error_detail(raw_body: bytes) -> str:
    if not raw_body:
        return "no response body"
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw_body.decode("utf-8", errors="replace")
    if isinstance(payload, dict) and "error" in payload:
        return str(payload["error"])
    return json.dumps(payload, sort_keys=True)
