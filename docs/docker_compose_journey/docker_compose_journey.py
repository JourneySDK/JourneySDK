"""Tutorial journey showing Docker Compose step-anchor rewind with app+db state."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
import urllib.request

from journeysdk import branch, journey, step
from journeysdk.touchpoints.docker import (
    DockerComposeStack,
    DockerContainerStatus,
    run_docker,
)

_COMPOSE_FILE = Path(__file__).with_name("docker-compose.yml")
_DEFAULT_APP_PORT = 18080


def _app_base_url() -> str:
    port = os.environ.get("JOURNEY_DOCKER_DOCS_PORT", str(_DEFAULT_APP_PORT)).strip()
    return f"http://127.0.0.1:{port or _DEFAULT_APP_PORT}"


def _fetch_json(path: str, *, method: str = "GET") -> dict[str, object]:
    request = urllib.request.Request(
        f"{_app_base_url()}{path}",
        method=method,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw_body = response.read().decode("utf-8")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Expected JSON from Docker demo endpoint {path!r}, got: {raw_body!r}."
        ) from exc
    if not isinstance(payload, dict):
        raise AssertionError(
            f"Expected a JSON object from Docker demo endpoint {path!r}, got {type(payload).__name__}."
        )
    return payload


def _require_service_status(
    stack: DockerComposeStack,
    service: str,
) -> DockerContainerStatus:
    statuses = stack.statuses.get(service)
    if not statuses:
        raise AssertionError(
            f"Expected one {service!r} service in Docker Compose statuses."
        )
    if len(statuses) != 1:
        raise AssertionError(
            f"Expected exactly one {service!r} container, got {len(statuses)}."
        )
    return statuses[0]


def _assert_running_healthy(
    status: DockerContainerStatus,
    *,
    service: str,
) -> None:
    if status.state != "running":
        raise AssertionError(
            f"Expected {service!r} to be running, got {status.state!r}."
        )
    if status.health != "healthy":
        raise AssertionError(
            f"Expected {service!r} to be healthy, got {status.health!r}."
        )


def _require_int_field(
    payload: Mapping[str, object],
    *,
    field: str,
    owner: str,
) -> int:
    value = payload.get(field)
    if not isinstance(value, int):
        raise AssertionError(
            f"{owner} expected integer field {field!r}, got {value!r}."
        )
    return value


def _require_string_field(
    payload: Mapping[str, object],
    *,
    field: str,
    owner: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise AssertionError(
            f"{owner} expected non-empty string field {field!r}, got {value!r}."
        )
    return value


def _parse_counter_payload(
    payload: Mapping[str, object],
    *,
    owner: str,
) -> dict[str, object]:
    return {
        "count": _require_int_field(payload, field="count", owner=owner),
        "database": _require_string_field(payload, field="database", owner=owner),
    }


def _parse_increment_payload(
    payload: Mapping[str, object],
    *,
    owner: str,
) -> dict[str, int]:
    return {
        "before": _require_int_field(payload, field="before", owner=owner),
        "after": _require_int_field(payload, field="after", owner=owner),
    }


def assert_stack_ready(stack: DockerComposeStack) -> bool:
    """Confirm that the demo app and database are healthy before snapshotting."""

    app_status = _require_service_status(stack, "app")
    db_status = _require_service_status(stack, "db")
    _assert_running_healthy(app_status, service="app")
    _assert_running_healthy(db_status, service="db")

    health_payload = _fetch_json("/health")
    status = _require_string_field(
        health_payload,
        field="status",
        owner="assert_stack_ready",
    )
    database = _require_string_field(
        health_payload,
        field="database",
        owner="assert_stack_ready",
    )
    if status != "ok" or database != "ready":
        raise AssertionError(
            "Expected Docker demo /health endpoint to report status='ok' and database='ready'."
        )
    return True


def capture_baseline_state(stack: DockerComposeStack) -> dict[str, object]:
    """Read one small serializable baseline payload from the running app."""

    app_status = _require_service_status(stack, "app")
    db_status = _require_service_status(stack, "db")
    counter_payload = _parse_counter_payload(
        _fetch_json("/counter"),
        owner="capture_baseline_state",
    )
    return {
        "count": counter_payload["count"],
        "database": counter_payload["database"],
        "app_state": app_status.state,
        "db_health": db_status.health,
    }


def increment_counter(stack: DockerComposeStack) -> dict[str, int]:
    """Mutate the app+db state in one branch."""

    return _parse_increment_payload(
        _fetch_json("/counter/increment", method="POST"),
        owner="increment_counter",
    )


def read_counter_state(stack: DockerComposeStack) -> dict[str, object]:
    """Read the counter after Journey restores the saved Docker snapshot."""

    return _parse_counter_payload(
        _fetch_json("/counter"),
        owner="read_counter_state",
    )


def assert_increment_branch(
    baseline: Mapping[str, object],
    incremented: Mapping[str, object],
) -> bool:
    """Confirm that the mutation branch changed the database-backed counter."""

    baseline_count = _require_int_field(
        baseline,
        field="count",
        owner="assert_increment_branch",
    )
    before = _require_int_field(
        incremented,
        field="before",
        owner="assert_increment_branch",
    )
    after = _require_int_field(
        incremented,
        field="after",
        owner="assert_increment_branch",
    )
    if before != baseline_count or after != baseline_count + 1:
        raise AssertionError(
            f"Expected counter transition {baseline_count}->{baseline_count + 1}, got {before}->{after}."
        )
    return True


def assert_restored_counter_branch(
    baseline: Mapping[str, object],
    restored: Mapping[str, object],
) -> bool:
    """Confirm that the rewind branch sees the original baseline state again."""

    baseline_count = _require_int_field(
        baseline,
        field="count",
        owner="assert_restored_counter_branch",
    )
    restored_count = _require_int_field(
        restored,
        field="count",
        owner="assert_restored_counter_branch",
    )
    if restored_count != baseline_count:
        raise AssertionError(
            f"Expected restored counter {baseline_count}, got {restored_count}."
        )
    return True


@journey
def docker_compose_journey() -> None:
    stack = step(
        run_docker(
            compose_file=_COMPOSE_FILE,
            project_name="journey-docker-docs",
            wait_timeout=60,
        )
    )
    step(assert_stack_ready, stack)
    baseline = step(capture_baseline_state, stack)
    if branch(start_from=baseline):
        incremented = step(increment_counter, stack)
        step(assert_increment_branch, baseline, incremented)
    elif branch(start_from=baseline):
        restored = step(read_counter_state, stack)
        step(assert_restored_counter_branch, baseline, restored)
