# Docker Touchpoint Reference

Use the Docker touchpoint when a journey needs a local Docker Compose app as durable test infrastructure. Read this
reference before writing Docker-backed journeys.

## Public API

- `run_docker(compose_file=None, project_name=None, wait_timeout=None, wait_for_logs=(), wait_for_http=()) -> DockerComposeStack`
- `DockerComposeStack`: rehydratable handle for one Compose project.
- `DockerComposeStack.statuses`: live `DockerContainerStatus` values grouped by service.
- `DockerComposeStack.logs`: live combined logs grouped by service.
- `DockerComposeStack.wait_for_log(service_name, message, timeout=60.0, poll_interval=0.25, since="now") -> DockerLogMatch`
- `DockerComposeStack.service_url(service_name, port, path="", scheme="http") -> str`
- `DockerContainerStatus`: service, container id/name, state, health, exit code, image, started/finished timestamps.
- `DockerLogMatcher(service_name, message, timeout=60.0, poll_interval=0.25)`: regex wait used by `run_docker`.
- `DockerLogMatch`: service, container id/name, matched log line.
- `DockerHttpCheck(service_name, port, path="/", scheme="http", expected_status=200, timeout=60.0, poll_interval=0.25)`: readiness wait used by `run_docker`.
- `store_docker(stack, snapshot_name)` and `restore_docker(stack, snapshot_name)`: advanced explicit snapshot helpers.

## Authoring Pattern

Wrap `run_docker(...)` in one named journey step when starting the app is a meaningful durable boundary. Keep Compose
file details, service names, readiness checks, and host port lookup in helpers, not in the `@journey` function.

```python
from journeysdk.touchpoints.docker import DockerHttpCheck, run_docker


def start_app_with_docker():
    stack = run_docker(
        compose_file="docker-compose.yml",
        project_name="checkout-journey",
        wait_timeout=120,
        wait_for_http=[
            DockerHttpCheck(service_name="web", port=8000, path="/healthz"),
        ],
    )
    return DemoApp(stack=stack, base_url=stack.service_url("web", 8000))
```

Use `DockerLogMatcher` when the app exposes readiness in logs:

```python
run_docker(
    compose_file="docker-compose.yml",
    wait_for_logs=[
        DockerLogMatcher(service_name=r"^web$", message=r"server ready", timeout=60),
    ],
)
```

## Replay, lifecycle, and cleanup

`DockerComposeStack` implements Journey rehydration. When a later `branch(start_from=...)` or `retry_from=...` boundary
needs to replay Docker state, Journey stores and restores Docker-managed volume contents. Keep durable app state in
Docker-managed volumes when branch replay matters.

Journey stops the Compose project at case exit with `docker compose down --remove-orphans` and preserves volumes by
default. Snapshot payloads live under `.journey` with other Journey state.

## Limits

Docker snapshots restore Docker-managed volumes only. Bind mounts are treated as external host state and are not copied
or restored. External volumes, read-only volume mounts, unsupported mount types, and multi-container services are
rejected to keep rollback exact.

Do not hand-roll subprocess calls, `docker compose port`, PID handling, sleeps, or raw HTTP polling in journey specs.
Use `run_docker`, `DockerHttpCheck`, `DockerLogMatcher`, `service_url`, and project helpers.
