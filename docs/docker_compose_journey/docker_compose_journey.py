"""Tutorial journey showing Docker Compose app snapshots."""

from __future__ import annotations

from pathlib import Path

from journey import checkpoint, journey, step
from journey.tools.docker import (
    DockerComposeStack,
    restore_docker,
    run_docker,
    store_docker,
)

_COMPOSE_FILE = Path(__file__).with_name("docker-compose.yml")


def assert_stack_running(stack: DockerComposeStack) -> bool:
    """Confirm that the demo Compose app is up before snapshotting it."""

    app_statuses = stack.statuses.get("app")
    if not app_statuses:
        raise AssertionError("Expected one 'app' service in Docker Compose statuses.")
    app_status = app_statuses[0]
    if app_status.state != "running":
        raise AssertionError(
            f"Expected app service to be running, got {app_status.state!r}."
        )
    return True


def assert_boot_logs(stack: DockerComposeStack) -> bool:
    """Confirm that the boot log line is still visible after restore."""

    app_logs = stack.logs.get("app", "")
    if "Journey docker demo ready" not in app_logs:
        raise AssertionError(
            "Expected app logs to contain the Docker demo boot message."
        )
    return True


def assert_stack_is_still_running(stack: DockerComposeStack) -> bool:
    """Confirm that later steps can keep using the restored stack descriptor."""

    app_statuses = stack.statuses.get("app")
    if not app_statuses or app_statuses[0].state != "running":
        raise AssertionError("Expected app service to still be running.")
    return True


@journey
def docker_compose_journey() -> None:
    stack = step(
        run_docker(
            compose_file=_COMPOSE_FILE,
            project_name="journey-docker-docs",
        )
    )
    step(assert_stack_running, stack)
    checkpoint(
        stack,
        store=store_docker,
        restore=restore_docker,
        snapshot_name="after_boot",
    )
    step(assert_boot_logs, stack)
    step(assert_stack_is_still_running, stack)
