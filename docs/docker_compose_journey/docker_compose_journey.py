"""Tutorial journey showing Docker Compose checkpoint-started branches."""

from __future__ import annotations

from pathlib import Path

from journey import branch, checkpoint, journey, step
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


def capture_stack_summary(stack: DockerComposeStack) -> dict[str, str]:
    """Read one small serializable summary from the running Docker stack."""

    app_statuses = stack.statuses.get("app")
    if not app_statuses:
        raise AssertionError("Expected one 'app' service in Docker Compose statuses.")
    app_status = app_statuses[0]
    app_logs = stack.logs.get("app", "")
    return {
        "state": app_status.state,
        "logs": app_logs,
    }


def assert_running_branch(summary: dict[str, str]) -> bool:
    """Confirm that the rewound branch still sees a running app service."""

    if summary.get("state") != "running":
        raise AssertionError(
            f"Expected app state 'running', got {summary.get('state')!r}."
        )
    return True


def assert_boot_logs_branch(summary: dict[str, str]) -> bool:
    """Confirm that the rewound branch still sees the boot log line."""

    logs = summary.get("logs", "")
    if "Journey docker demo ready" not in logs:
        raise AssertionError(
            "Expected app logs to contain the Docker demo boot message."
        )
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
    after_boot = checkpoint(
        stack,
        store=store_docker,
        restore=restore_docker,
        snapshot_name="after_boot",
    )
    summary = step(capture_stack_summary, stack)
    if branch(start_from=after_boot):
        step(assert_running_branch, summary)
    elif branch(start_from=after_boot):
        step(assert_boot_logs_branch, summary)
