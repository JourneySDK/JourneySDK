from __future__ import annotations

import json
import os
import pickle
import subprocess
from pathlib import Path

import journeysdk as journey_sdk
import pytest

from journeysdk.models import StepNode
from journeysdk.logger import configure_logging
from journeysdk.touchpoints import docker as journey_docker


def _write_compose_file(tmp_path: Path) -> Path:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "services:\n"
        "  web:\n"
        "    image: demo-web:latest\n"
        "    volumes:\n"
        "      - demo_data:/data\n"
        "volumes:\n"
        "  demo_data: {}\n",
        encoding="utf-8",
    )
    return compose_file


def _build_stack(tmp_path: Path, *, project_name: str = "demo") -> journey_docker.DockerComposeStack:
    compose_file = _write_compose_file(tmp_path).resolve()
    cache_root = tmp_path / "cache"
    resolved_compose_file = cache_root / project_name / "resolved-compose.yml"
    resolved_compose_file.parent.mkdir(parents=True, exist_ok=True)
    resolved_compose_file.write_text(
        "services:\n  web:\n    image: demo-web:latest\n",
        encoding="utf-8",
    )
    return journey_docker.DockerComposeStack(
        compose_file=str(compose_file),
        resolved_compose_file=str(resolved_compose_file),
        project_name=project_name,
        cache_root=str(cache_root),
    )


def _ps_row(container_id: str, name: str, service: str) -> dict[str, str]:
    return {
        "ID": container_id,
        "Name": name,
        "Service": service,
    }


def _inspect_row(
    *,
    container_id: str,
    name: str,
    service: str,
    image: str,
    state: str = "running",
    volume_name: str = "demo_demo_data",
    destination: str = "/data",
    mount_type: str = "volume",
    read_write: bool = True,
    health: str | None = "healthy",
    mounts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    health_block: dict[str, str] | None = None
    if health is not None:
        health_block = {"Status": health}
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Config": {
            "Image": image,
            "Labels": {
                "com.docker.compose.service": service,
                "com.docker.compose.container-number": "1",
            },
        },
        "State": {
            "Status": state,
            "ExitCode": 0,
            "StartedAt": "2026-04-14T10:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": health_block,
        },
        "Mounts": mounts
        if mounts is not None
        else [
            {
                "Type": mount_type,
                "Name": volume_name,
                "Destination": destination,
                "Mode": "",
                "RW": read_write,
            }
        ],
    }


class _FakeDockerRuntime:
    def __init__(
        self,
        *,
        compose_config: dict[str, object] | None = None,
        compose_config_yaml: str | None = None,
        live_ps_rows: list[dict[str, str]] | None = None,
        live_inspect_rows: list[dict[str, object]] | None = None,
        restored_ps_rows: list[dict[str, str]] | None = None,
        restored_inspect_rows: list[dict[str, object]] | None = None,
        logs_by_service: dict[str, str] | None = None,
    ) -> None:
        self.compose_config = compose_config or {
            "services": {
                "web": {"image": "demo-web:latest"},
            },
            "volumes": {
                "demo_data": {},
            },
        }
        self.compose_config_yaml = (
            compose_config_yaml
            or "services:\n  web:\n    image: demo-web:latest\n"
        )
        self.live_ps_rows = live_ps_rows or [
            _ps_row("web-container-1", "demo-web-1", "web"),
        ]
        self.live_inspect_rows = {
            row["Id"]: row
            for row in (
                live_inspect_rows
                or [
                    _inspect_row(
                        container_id="web-container-1",
                        name="demo-web-1",
                        service="web",
                        image="demo-web:latest",
                    )
                ]
            )
        }
        self.restored_ps_rows = restored_ps_rows or [
            _ps_row("web-container-2", "demo-web-1", "web"),
        ]
        self.restored_inspect_rows = {
            row["Id"]: row
            for row in (
                restored_inspect_rows
                or [
                    _inspect_row(
                        container_id="web-container-2",
                        name="demo-web-1",
                        service="web",
                        image="journey-sdk-snapshot:demo-after-boot-web-1",
                    )
                ]
            )
        }
        self.logs_by_service = logs_by_service or {
            "web": "web-1  | 2026-04-14T10:00:00Z booted\n",
        }
        self.volume_names = {"demo_demo_data"}
        self.phase = "live"
        self.commands: list[tuple[str, list[str]]] = []
        self.restored_copies: list[tuple[str, str]] = []

    def __call__(self, args: object, *, owner: str) -> str:
        assert isinstance(args, list)
        self.commands.append((owner, list(args)))

        if args[:2] == ["docker", "compose"] and "config" in args:
            format_value = args[args.index("--format") + 1]
            if format_value == "yaml":
                return self.compose_config_yaml
            if format_value == "json":
                return json.dumps(self.compose_config)

        if args[:2] == ["docker", "compose"] and "ps" in args:
            rows = self.restored_ps_rows if self.phase == "restored" else self.live_ps_rows
            return json.dumps(rows)

        if args[:2] == ["docker", "inspect"]:
            container_ids = args[4:]
            rows = self.restored_inspect_rows if self.phase == "restored" else self.live_inspect_rows
            return json.dumps([rows[container_id] for container_id in container_ids])

        if args[:2] == ["docker", "compose"] and "logs" in args:
            service = args[-1]
            return self.logs_by_service[service]

        if args[:2] == ["docker", "commit"]:
            return f"sha256:{args[-1]}"

        if args[:3] == ["docker", "cp", "-a"]:
            source = args[3]
            destination = args[4]
            if source.startswith("web-container-") or source.startswith("db-container-"):
                backup_dir = Path(destination)
                backup_dir.mkdir(parents=True, exist_ok=True)
                (backup_dir / "snapshot.txt").write_text(
                    f"saved from {source}",
                    encoding="utf-8",
                )
            else:
                self.restored_copies.append((source, destination))
            return ""

        if args[:3] == ["docker", "volume", "ls"]:
            return "\n".join(
                json.dumps({"Name": name})
                for name in sorted(self.volume_names)
            )

        if args[:3] == ["docker", "volume", "rm"]:
            self.volume_names.discard(args[3])
            return args[3]

        if args[:2] == ["docker", "compose"] and "down" in args:
            self.phase = "down"
            return ""

        if args[:2] == ["docker", "compose"] and "create" in args:
            self.phase = "restored"
            return ""

        if args[:2] == ["docker", "compose"] and ("up" in args or "start" in args):
            return ""

        raise AssertionError(f"Unexpected Docker command: {args}")


def _record_labels(plan: journey_sdk.JourneyPlan) -> list[str]:
    return [
        node.label
        for node in plan.case_plans[0].nodes
        if isinstance(node, StepNode) and node.label is not None
    ]


def test_run_docker_validates_factory_arguments(tmp_path: Path):
    with pytest.raises(TypeError):
        journey_docker.run_docker(compose_file=object())
    with pytest.raises(ValueError):
        journey_docker.run_docker(project_name=" ")
    with pytest.raises(ValueError):
        journey_docker.run_docker(project_name="../unsafe")
    with pytest.raises(TypeError):
        journey_docker.run_docker(wait_timeout="5")
    with pytest.raises(ValueError):
        journey_docker.run_docker(wait_timeout=0)
    with pytest.raises(ValueError):
        journey_docker.store_docker(
            _build_stack(tmp_path),
            snapshot_name="../bad",
        )


def test_run_docker_planning_does_not_touch_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    compose_file = _write_compose_file(tmp_path)
    original_run = journey_docker.subprocess.run

    def fail_run(*args, **kwargs):
        raise AssertionError("compile_journey() should not call Docker.")

    monkeypatch.setattr(journey_docker.subprocess, "run", fail_run)

    def journey():
        stack = journey_sdk.step(journey_docker.run_docker(compose_file=compose_file))
        journey_sdk.step(lambda current: current.project_name, stack)

    plan = journey_sdk.compile_journey(journey)

    monkeypatch.setattr(journey_docker.subprocess, "run", original_run)
    assert _record_labels(plan) == ["run_docker", "<lambda>"]


def test_run_docker_executes_compose_config_and_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_timeout=15,
    )()

    resolved_path = Path(stack.resolved_compose_file)
    assert stack.compose_file == str(compose_file.resolve())
    assert stack.project_name == "demo-project"
    assert resolved_path.exists()
    assert resolved_path.read_text(encoding="utf-8") == (
        "services:\n  web:\n    image: demo-web:latest\n"
    )
    assert runtime.commands == [
        (
            "run_docker",
            [
                "docker",
                "compose",
                "-f",
                str(compose_file.resolve()),
                "-p",
                "demo-project",
                "config",
                "--format",
                "yaml",
            ],
        ),
        (
            "run_docker",
            [
                "docker",
                "compose",
                "-f",
                str(resolved_path),
                "-p",
                "demo-project",
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "15",
            ],
        ),
    ]
    log_output = capsys.readouterr().out
    assert "Docker" in log_output
    assert "starting Docker Compose stack" in log_output
    assert "Docker Compose stack started" in log_output


def test_docker_run_cli_logs_subprocess_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    def fake_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(journey_docker.subprocess, "run", fake_run)

    configure_logging("debug")
    try:
        assert journey_docker._run_cli(["docker", "ps"], owner="test") == "ok\n"
    finally:
        configure_logging("info")

    log_output = capsys.readouterr().out
    assert "Debug: docker:subprocess_start" in log_output
    assert "Debug: docker:subprocess_success" in log_output


def test_docker_stack_statuses_and_logs_use_live_docker_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        compose_config={
            "services": {
                "db": {"image": "demo-db:latest"},
                "web": {"image": "demo-web:latest"},
            },
            "volumes": {"demo_data": {}},
        },
        live_ps_rows=[
            _ps_row("web-container-1", "demo-web-1", "web"),
            _ps_row("db-container-1", "demo-db-1", "db"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            ),
            _inspect_row(
                container_id="db-container-1",
                name="demo-db-1",
                service="db",
                image="demo-db:latest",
                health=None,
            ),
        ],
        logs_by_service={
            "db": "db-1  | 2026-04-14T10:00:00Z ready\n",
            "web": "web-1  | 2026-04-14T10:00:01Z listening\n",
        },
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    statuses = stack.statuses
    logs = stack.logs

    assert list(statuses) == ["db", "web"]
    assert statuses["web"][0] == journey_docker.DockerContainerStatus(
        service="web",
        container_id="web-container-1",
        container_name="demo-web-1",
        state="running",
        health="healthy",
        exit_code=0,
        image="demo-web:latest",
        started_at="2026-04-14T10:00:00Z",
        finished_at="0001-01-01T00:00:00Z",
    )
    assert statuses["db"][0].health is None
    assert logs == {
        "db": "db-1  | 2026-04-14T10:00:00Z ready\n",
        "web": "web-1  | 2026-04-14T10:00:01Z listening\n",
    }


def test_docker_compose_stack_is_pickle_serializable(tmp_path: Path):
    stack = _build_stack(tmp_path)
    restored = pickle.loads(pickle.dumps(stack))
    assert restored == stack


def test_store_docker_creates_manifest_and_volume_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["project_name"] == "demo"
    assert manifest["snapshot_name"] == "after_boot"
    assert manifest["containers"][0]["snapshot_image"].startswith("journey-sdk-snapshot:")
    assert manifest["volumes"][0]["volume_name"] == "demo_demo_data"
    assert (snapshot_dir / manifest["volumes"][0]["backup_relpath"] / "snapshot.txt").exists()
    assert any(
        command[:3] == ["docker", "commit", "web-container-1"]
        for _, command in runtime.commands
    )
    assert any(
        command[:3] == ["docker", "cp", "-a"] and command[3].startswith("web-container-1:/data")
        for _, command in runtime.commands
    )


@pytest.mark.parametrize(
    ("mount_type", "state", "external", "read_write", "message"),
    [
        ("bind", "running", False, True, "Docker-managed volumes"),
        ("volume", "paused", False, True, "cannot snapshot"),
        ("volume", "running", True, True, "external volume"),
        ("volume", "running", False, False, "read-only volume mount"),
    ],
)
def test_store_docker_rejects_unsupported_snapshot_shapes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mount_type: str,
    state: str,
    external: bool,
    read_write: bool,
    message: str,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        compose_config={
            "services": {"web": {"image": "demo-web:latest"}},
            "volumes": {"demo_data": {"external": True}} if external else {"demo_data": {}},
        },
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
                state=state,
                volume_name="demo_data" if external else "demo_demo_data",
                mount_type=mount_type,
                read_write=read_write,
            )
        ],
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    with pytest.raises(RuntimeError) as exc_info:
        journey_docker.store_docker(stack, snapshot_name="after_boot")

    assert message in str(exc_info.value)


def test_store_docker_rejects_multi_container_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        live_ps_rows=[
            _ps_row("web-container-1", "demo-web-1", "web"),
            _ps_row("web-container-2", "demo-web-2", "web"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            ),
            _inspect_row(
                container_id="web-container-2",
                name="demo-web-2",
                service="web",
                image="demo-web:latest",
            ),
        ],
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    with pytest.raises(RuntimeError) as exc_info:
        journey_docker.store_docker(stack, snapshot_name="after_boot")

    assert "one container per service" in str(exc_info.value)


def test_restore_docker_recreates_stack_and_restores_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
    backup_dir = snapshot_dir / "volumes" / "demo-demo-data"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "snapshot.txt").write_text("saved", encoding="utf-8")
    manifest = {
        "format": 1,
        "project_name": stack.project_name,
        "compose_file": stack.compose_file,
        "resolved_compose_file": stack.resolved_compose_file,
        "snapshot_name": "after_boot",
        "containers": [
            {
                "service": "web",
                "container_id": "web-container-1",
                "container_name": "demo-web-1",
                "container_index": 1,
                "state": "running",
                "health": "healthy",
                "exit_code": 0,
                "image": "demo-web:latest",
                "started_at": "2026-04-14T10:00:00Z",
                "finished_at": "0001-01-01T00:00:00Z",
                "snapshot_image": "journey-sdk-snapshot:demo-after-boot-web-1",
            }
        ],
        "volumes": [
            {
                "volume_name": "demo_demo_data",
                "service": "web",
                "container_index": 1,
                "container_name": "demo-web-1",
                "target_path": "/data",
                "backup_relpath": "volumes/demo-demo-data",
                "mode": "",
            }
        ],
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.restore_docker(stack, snapshot_name="after_boot")

    override_path = Path(stack.cache_root) / stack.project_name / "restore-after_boot.override.yml"
    assert override_path.exists()
    assert "journey-sdk-snapshot:demo-after-boot-web-1" in override_path.read_text(
        encoding="utf-8"
    )
    assert runtime.restored_copies == [
        (str(backup_dir) + "/.", "web-container-2:/data"),
    ]
    assert any(
        command[:6] == [
            "docker",
            "compose",
            "-f",
            stack.resolved_compose_file,
            "-f",
            str(override_path),
        ] and "start" in command
        for _, command in runtime.commands
    )


def test_restore_docker_requires_existing_snapshot(tmp_path: Path):
    stack = _build_stack(tmp_path)

    with pytest.raises(FileNotFoundError) as exc_info:
        journey_docker.restore_docker(stack, snapshot_name="missing")

    assert "could not find a stored snapshot manifest" in str(exc_info.value)


def test_execute_step_started_branches_restore_docker_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime(
        compose_config={
            "services": {
                "app": {"image": "demo-app:latest"},
                "db": {"image": "postgres:16-alpine"},
            },
            "volumes": {"db_data": {}},
        },
        compose_config_yaml=(
            "services:\n"
            "  app:\n"
            "    image: demo-app:latest\n"
            "  db:\n"
            "    image: postgres:16-alpine\n"
            "volumes:\n"
            "  db_data: {}\n"
        ),
        live_ps_rows=[
            _ps_row("app-container-1", "demo-app-1", "app"),
            _ps_row("db-container-1", "demo-db-1", "db"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id="app-container-1",
                name="demo-app-1",
                service="app",
                image="demo-app:latest",
                mounts=[],
            ),
            _inspect_row(
                container_id="db-container-1",
                name="demo-db-1",
                service="db",
                image="postgres:16-alpine",
                volume_name="demo_db_data",
                destination="/var/lib/postgresql/data",
            ),
        ],
        restored_ps_rows=[
            _ps_row("app-container-2", "demo-app-1", "app"),
            _ps_row("db-container-2", "demo-db-1", "db"),
        ],
        restored_inspect_rows=[
            _inspect_row(
                container_id="app-container-2",
                name="demo-app-1",
                service="app",
                image="journey-sdk-snapshot:demo-cp-1-app-1",
                mounts=[],
            ),
            _inspect_row(
                container_id="db-container-2",
                name="demo-db-1",
                service="db",
                image="journey-sdk-snapshot:demo-cp-1-db-1",
                volume_name="demo_db_data",
                destination="/var/lib/postgresql/data",
            ),
        ],
        logs_by_service={
            "app": "app-1  | 2026-04-14T10:00:01Z listening\n",
            "db": "db-1  | 2026-04-14T10:00:00Z ready\n",
        },
    )
    runtime.volume_names = {"demo_db_data"}
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)
    events: list[str] = []
    counter = {"value": 0}

    def assert_stack_ready(stack: journey_docker.DockerComposeStack) -> bool:
        app_status = stack.statuses["app"][0]
        db_status = stack.statuses["db"][0]
        events.append(f"ready_{app_status.container_id}_{db_status.container_id}")
        assert app_status.state == "running"
        assert app_status.health == "healthy"
        assert db_status.state == "running"
        assert db_status.health == "healthy"
        return True

    def capture_baseline_state(
        stack: journey_docker.DockerComposeStack,
    ) -> dict[str, object]:
        app_status = stack.statuses["app"][0]
        db_status = stack.statuses["db"][0]
        events.append(f"baseline_{counter['value']}_{app_status.container_id}")
        return {
            "count": counter["value"],
            "app_state": app_status.state,
            "db_health": db_status.health,
        }

    def increment_counter(
        stack: journey_docker.DockerComposeStack,
    ) -> dict[str, int]:
        before = counter["value"]
        counter["value"] += 1
        events.append(f"increment_{before}_{counter['value']}")
        return {"before": before, "after": counter["value"]}

    def assert_increment_branch(
        baseline: dict[str, object],
        incremented: dict[str, int],
    ) -> bool:
        events.append(f"assert_increment_{incremented['after']}")
        assert incremented["before"] == baseline["count"]
        assert incremented["after"] == baseline["count"] + 1
        return True

    def read_counter_state(
        stack: journey_docker.DockerComposeStack,
    ) -> dict[str, int]:
        app_status = stack.statuses["app"][0]
        current_count = 0 if app_status.container_id.endswith("-2") else counter["value"]
        events.append(f"read_{current_count}_{app_status.container_id}")
        return {"count": current_count}

    def assert_restored_counter_branch(
        baseline: dict[str, object],
        current: dict[str, int],
    ) -> bool:
        events.append(f"assert_restored_{current['count']}")
        assert baseline["count"] == 0
        assert current["count"] == baseline["count"]
        return True

    def journey():
        stack = journey_sdk.step(
            journey_docker.run_docker(
                compose_file=compose_file,
                project_name="demo",
            )
        )
        journey_sdk.step(assert_stack_ready, stack)
        baseline = journey_sdk.step(capture_baseline_state, stack)
        if journey_sdk.branch(start_from=baseline):
            incremented = journey_sdk.step(increment_counter, stack)
            journey_sdk.step(assert_increment_branch, baseline, incremented)
        elif journey_sdk.branch(start_from=baseline):
            current = journey_sdk.step(read_counter_state, stack)
            journey_sdk.step(assert_restored_counter_branch, baseline, current)

    report = journey_sdk.execute(journey)

    assert events == [
        "ready_app-container-1_db-container-1",
        "baseline_0_app-container-1",
        "increment_0_1",
        "assert_increment_1",
        "read_0_app-container-2",
        "assert_restored_0",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert [
        [record.label for record in case.records if record.label is not None]
        for case in report.case_reports
    ] == [
        [
            "run_docker",
            "assert_stack_ready",
            "capture_baseline_state",
            "increment_counter",
            "assert_increment_branch",
        ],
        [
            "run_docker",
            "assert_stack_ready",
            "capture_baseline_state",
            "read_counter_state",
            "assert_restored_counter_branch",
        ],
    ]
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "store_docker" and "commit" in command
    ) >= 2
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "restore_docker" and "down" in command
    ) == 1


def test_journey_resume_restores_docker_snapshot_with_saved_step_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)
    state_file = tmp_path / "journey.state"
    attempts = {"count": 0}

    def poll() -> bool:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise KeyboardInterrupt()
        return True

    def finish() -> bool:
        return True

    def journey():
        stack = journey_sdk.step(
            journey_docker.run_docker(
                compose_file=compose_file,
                project_name="demo",
            )
        )
        journey_sdk.step(
            poll,
            retry=1,
            retry_delay=0,
            retry_from=stack,
        )
        journey_sdk.step(finish)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert [record.label for record in report.case_reports[0].records if record.label is not None] == [
        "run_docker",
        "poll",
        "finish",
    ]
    assert list((tmp_path / "journey.state.artifacts").rglob("manifest.json"))
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "restore_docker" and "down" in command
    ) >= 1


@pytest.mark.skipif(
    os.environ.get("JOURNEY_RUN_DOCKER_SMOKE") != "1",
    reason="Set JOURNEY_RUN_DOCKER_SMOKE=1 to run the real Docker smoke test.",
)
def test_real_docker_smoke_round_trips_one_compose_snapshot(tmp_path: Path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "services:\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        "    command: ['sh', '-c', 'echo seeded >/data/state.txt && sleep 300']\n"
        "    volumes:\n"
        "      - app_data:/data\n"
        "volumes:\n"
        "  app_data: {}\n",
        encoding="utf-8",
    )
    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name=f"journey-smoke-{os.getpid()}",
        wait_timeout=30,
    )()
    try:
        assert stack.statuses["app"][0].state == "running"
        journey_docker.store_docker(stack, snapshot_name="smoke")
        container_id = stack.statuses["app"][0].container_id
        journey_docker._run_cli(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-c",
                "echo mutated >/data/state.txt",
            ],
            owner="test_real_docker_smoke_round_trips_one_compose_snapshot",
        )
        journey_docker.restore_docker(stack, snapshot_name="smoke")
        restored_container_id = stack.statuses["app"][0].container_id
        contents = journey_docker._run_cli(
            [
                "docker",
                "exec",
                restored_container_id,
                "cat",
                "/data/state.txt",
            ],
            owner="test_real_docker_smoke_round_trips_one_compose_snapshot",
        )
        assert contents.strip() == "seeded"
    finally:
        journey_docker._run_cli(
            [
                "docker",
                "compose",
                "-f",
                stack.resolved_compose_file,
                "-p",
                stack.project_name,
                "down",
                "--remove-orphans",
            ],
            owner="test_real_docker_smoke_round_trips_one_compose_snapshot",
        )
