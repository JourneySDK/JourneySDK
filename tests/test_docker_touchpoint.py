from __future__ import annotations

import json
import os
import pickle
import subprocess
import urllib.error
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

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


def _build_stack(
    tmp_path: Path,
    *,
    project_name: str = "demo",
    wait_timeout: int | None = None,
    log_matchers: tuple[journey_docker.DockerLogMatcher, ...] = (),
    http_checks: tuple[journey_docker.DockerHttpCheck, ...] = (),
) -> journey_docker.DockerComposeStack:
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
        wait_timeout=wait_timeout,
        log_matchers=log_matchers,
        http_checks=http_checks,
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
    exit_code: int = 0,
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
            "ExitCode": exit_code,
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
        container_logs: dict[str, str] | None = None,
        published_ports: dict[tuple[str, int], str] | None = None,
        compose_ps_json_lines: bool = False,
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
                        image="demo-web:latest",
                    )
                ]
            )
        }
        self.logs_by_service = logs_by_service or {
            "web": "web-1  | 2026-04-14T10:00:00Z booted\n",
        }
        self.container_logs = container_logs or {
            "web-container-1": "2026-04-14T10:00:00Z booted\n",
            "web-container-2": "2026-04-14T10:00:00Z restored\n",
        }
        self.published_ports = published_ports or {
            ("web", 8000): "0.0.0.0:5050\n",
        }
        self.compose_ps_json_lines = compose_ps_json_lines
        self.volume_names = {"demo_demo_data"}
        self.volume_labels: dict[str, dict[str, str]] = {}
        self.phase = "live"
        self.commands: list[tuple[str, list[str]]] = []
        self.volume_copies: list[tuple[str, str]] = []
        self.stopped_container_ids: list[str] = []
        self.started_container_ids: list[str] = []

    def __call__(
        self,
        args: object,
        *,
        owner: str,
        failure_level: object | None = None,
    ) -> str:
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
            if self.compose_ps_json_lines:
                return "\n".join(json.dumps(row) for row in rows)
            return json.dumps(rows)

        if args[:2] == ["docker", "inspect"]:
            container_ids = args[4:]
            rows = self.restored_inspect_rows if self.phase == "restored" else self.live_inspect_rows
            selected_rows = []
            for container_id in container_ids:
                row = rows.get(container_id)
                if row is None:
                    matches = [
                        candidate
                        for full_id, candidate in rows.items()
                        if full_id.startswith(container_id)
                    ]
                    if len(matches) != 1:
                        raise KeyError(container_id)
                    row = matches[0]
                selected_rows.append(row)
            return json.dumps(selected_rows)

        if args[:2] == ["docker", "compose"] and "logs" in args:
            service = args[-1]
            return self.logs_by_service[service]

        if args[:2] == ["docker", "compose"] and "port" in args:
            service = args[-2]
            port = int(args[-1])
            return self.published_ports[(service, port)]

        if args[:2] == ["docker", "logs"]:
            container_id = args[-1]
            return self.container_logs.get(container_id, "")

        if args[:2] == ["docker", "commit"]:
            return f"sha256:{args[-1]}"

        if args[:3] == ["docker", "run", "--rm"]:
            mount_values = [
                args[index + 1]
                for index, item in enumerate(args)
                if item == "--mount"
            ]
            mounts = [_mount_parts(value) for value in mount_values]
            source = _host_path_for_container_path(args[-2], mounts)
            destination = _host_path_for_container_path(args[-1], mounts)
            self.volume_copies.append((source, destination))
            return ""

        if args[:3] == ["docker", "ps", "-q"]:
            volume_filter = args[args.index("--filter") + 1]
            _, volume_name = volume_filter.split("=", 1)
            rows = self.live_inspect_rows if self.phase != "restored" else self.restored_inspect_rows
            container_ids = [
                container_id
                for container_id, row in rows.items()
                if _inspect_row_uses_volume(row, volume_name)
            ]
            return "\n".join(container_ids) + ("\n" if container_ids else "")

        if args[:2] == ["docker", "stop"]:
            self.stopped_container_ids.extend(args[2:])
            return "\n".join(args[2:])

        if args[:2] == ["docker", "start"] and args[:3] != ["docker", "start", "-a"]:
            self.started_container_ids.extend(args[2:])
            return "\n".join(args[2:])

        if args[:3] == ["docker", "volume", "ls"]:
            return "\n".join(
                json.dumps({"Name": name})
                for name in sorted(self.volume_names)
            )

        if args[:3] == ["docker", "volume", "create"]:
            volume_name = args[-1]
            labels: dict[str, str] = {}
            for index, item in enumerate(args):
                if item == "--label":
                    key, value = args[index + 1].split("=", 1)
                    labels[key] = value
            self.volume_names.add(volume_name)
            self.volume_labels[volume_name] = labels
            return volume_name + "\n"

        if args[:3] == ["docker", "volume", "rm"]:
            self.volume_names.discard(args[3])
            self.volume_labels.pop(args[3], None)
            return args[3]

        if args[:2] == ["docker", "compose"] and "down" in args:
            self.phase = "down"
            return ""

        if args[:2] == ["docker", "compose"] and "create" in args:
            self.phase = "restored"
            self.volume_names.add("demo_demo_data")
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


def _docker_jsonl_records(output: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if record.get("component") == "docker":
            records.append(record)
    return records


class _FakeHttpResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = BytesIO(b"")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


def _fake_urlopen_statuses(statuses: list[int]):
    calls: list[tuple[str, float | None]] = []

    def fake_urlopen(url: str, *, timeout: float | None = None):
        calls.append((url, timeout))
        status = statuses.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(
                url,
                status,
                "error",
                hdrs={},
                fp=BytesIO(b""),
            )
        return _FakeHttpResponse(status)

    fake_urlopen.calls = calls  # type: ignore[attr-defined]
    return fake_urlopen


def _mount_parts(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(",") if "=" in part)


def _host_path_for_container_path(
    container_path: str,
    mounts: list[dict[str, str]],
) -> str:
    for mount in sorted(
        mounts,
        key=lambda item: len(item.get("target", "")),
        reverse=True,
    ):
        target = mount.get("target")
        if target is None:
            continue
        if container_path != target and not container_path.startswith(f"{target}/"):
            continue
        source = mount["source"]
        if mount.get("type") == "volume":
            return source
        relpath = container_path[len(target) :].lstrip("/")
        if not relpath:
            return source
        return str(Path(source) / relpath)
    raise AssertionError(f"Container path is not mounted: {container_path}")


def _inspect_row_uses_volume(row: dict[str, object], volume_name: str) -> bool:
    mounts = row.get("Mounts")
    if not isinstance(mounts, list):
        return False
    return any(
        isinstance(mount, dict)
        and mount.get("Type") == "volume"
        and mount.get("Name") == volume_name
        for mount in mounts
    )


def _write_restore_snapshot(
    stack: journey_docker.DockerComposeStack,
    *,
    snapshot_name: str = "after_boot",
) -> Path:
    snapshot_dir = journey_docker._snapshot_dir(
        stack=stack,
        snapshot_name=snapshot_name,
    )
    snapshot_payload_dir = snapshot_dir / "volumes" / "demo-demo-data"
    snapshot_payload_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": 4,
        "project_name": stack.project_name,
        "compose_file": stack.compose_file,
        "resolved_compose_file": stack.resolved_compose_file,
        "snapshot_name": snapshot_name,
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
    return snapshot_dir


def test_run_docker_validates_arguments(tmp_path: Path):
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
    with pytest.raises(TypeError):
        journey_docker.run_docker(wait_for_logs=[object()])
    with pytest.raises(ValueError):
        journey_docker.DockerLogMatcher(service_name="[", message="ready")
    with pytest.raises(ValueError):
        journey_docker.DockerLogMatcher(service_name="web", message="[")
    with pytest.raises(ValueError):
        journey_docker.DockerLogMatcher(service_name="web", message="ready", timeout=-1)
    with pytest.raises(ValueError):
        journey_docker.DockerLogMatcher(
            service_name="web",
            message="ready",
            poll_interval=0,
        )
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

    def start_stack() -> journey_docker.DockerComposeStack:
        return journey_docker.run_docker(compose_file=compose_file)

    def journey():
        stack = journey_sdk.step(start_stack)
        journey_sdk.step(lambda current: current.project_name, stack)

    plan = journey_sdk.compile_journey(journey)

    monkeypatch.setattr(journey_docker.subprocess, "run", original_run)
    assert _record_labels(plan) == ["start_stack", "<lambda>"]


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
    )

    resolved_path = Path(stack.resolved_compose_file)
    assert stack.compose_file == str(compose_file.resolve())
    assert stack.project_name == "demo-project"
    assert stack.wait_timeout == 15
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


def test_docker_stack_case_exit_stops_compose_without_removing_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    stack.__case_exit__(None, None, None)
    stack.__case_exit__(None, None, None)

    down_commands = [
        command
        for owner, command in runtime.commands
        if owner == "DockerComposeStack.__case_exit__" and "down" in command
    ]
    assert down_commands == [
        [
            "docker",
            "compose",
            "-f",
            stack.resolved_compose_file,
            "-p",
            stack.project_name,
            "down",
            "--remove-orphans",
        ]
    ]
    assert "--volumes" not in down_commands[0]


def test_docker_stack_case_exit_captures_service_logs_before_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    metadata = SimpleNamespace(
        path=tmp_path / ".journey" / "logs" / "docker.log",
        manifest_path=tmp_path / ".journey" / "logs" / "docker.manifest.json",
        kind="docker_compose_logs",
        touchpoint="docker",
        source="demo",
        content_type="text/plain",
        run_id="run123",
        sequence=1,
        key="0001-case_1-start_services-docker-demo-attempt-1-run-run123",
        journey_id="demo_journey",
        function_ref="module:demo_journey",
        case_id="case_1",
        branch_env={},
        step_id="node_1",
        step_label="start_services",
        step_name="start_services",
        node_index=0,
        attempt=1,
    )
    object.__setattr__(stack, "log_artifact_metadata", metadata)
    runtime = _FakeDockerRuntime(
        logs_by_service={
            "web": "web-1  | 2026-04-14T10:00:00Z endpoint unreachable\n",
        }
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    stack.__case_exit__(None, None, None)

    log_path = tmp_path / ".journey" / "logs" / "docker-web.log"
    manifest_path = tmp_path / ".journey" / "logs" / "docker-web.manifest.json"
    assert "endpoint unreachable" in log_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "journey.log_artifact"
    assert manifest["kind"] == "docker_compose_logs"
    assert manifest["touchpoint"] == "docker"
    assert manifest["source"] == "web"
    assert manifest["case_id"] == "case_1"


def test_run_docker_waits_for_configured_raw_container_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    matcher = journey_docker.DockerLogMatcher(
        service_name=r"^we.",
        message=r"server\s+ready",
        timeout=1,
        poll_interval=0.01,
    )
    runtime = _FakeDockerRuntime(
        container_logs={
            "web-container-1": "booting\nserver ready\n",
        },
    )
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_for_logs=[matcher],
    )

    assert stack.log_matchers == (matcher,)
    docker_logs_commands = [
        command
        for owner, command in runtime.commands
        if owner == "run_docker" and command[:2] == ["docker", "logs"]
    ]
    assert len(docker_logs_commands) == 1
    assert docker_logs_commands[0][:3] == ["docker", "logs", "--since"]
    assert docker_logs_commands[0][-1] == "web-container-1"


def test_run_docker_log_wait_filters_services_by_regex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime(
        compose_config={
            "services": {
                "web": {"image": "demo-web:latest"},
                "worker": {"image": "demo-worker:latest"},
            },
            "volumes": {"demo_data": {}},
        },
        live_ps_rows=[
            _ps_row("web-container-1", "demo-web-1", "web"),
            _ps_row("worker-container-1", "demo-worker-1", "worker"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            ),
            _inspect_row(
                container_id="worker-container-1",
                name="demo-worker-1",
                service="worker",
                image="demo-worker:latest",
                mounts=[],
            ),
        ],
        container_logs={
            "web-container-1": "server ready\n",
            "worker-container-1": "worker ready\n",
        },
    )
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_for_logs=[
            journey_docker.DockerLogMatcher(
                service_name=r"worker$",
                message=r"worker\s+ready",
                timeout=1,
                poll_interval=0.01,
            )
        ],
    )

    docker_logs_commands = [
        command
        for owner, command in runtime.commands
        if owner == "run_docker" and command[:2] == ["docker", "logs"]
    ]
    assert [command[-1] for command in docker_logs_commands] == ["worker-container-1"]


def test_run_docker_log_wait_times_out_when_message_never_appears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime(container_logs={"web-container-1": "booting\n"})
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    with pytest.raises(TimeoutError) as exc_info:
        journey_docker.run_docker(
            compose_file=compose_file,
            project_name="demo-project",
            wait_for_logs=[
                journey_docker.DockerLogMatcher(
                    service_name=r"web",
                    message=r"server ready",
                    timeout=0,
                    poll_interval=0.01,
                )
            ],
        )

    assert "timed out" in str(exc_info.value)


def test_run_docker_log_wait_requires_matching_declared_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    with pytest.raises(RuntimeError) as exc_info:
        journey_docker.run_docker(
            compose_file=compose_file,
            project_name="demo-project",
            wait_for_logs=[
                journey_docker.DockerLogMatcher(
                    service_name=r"api",
                    message=r"ready",
                    timeout=0,
                    poll_interval=0.01,
                )
            ],
        )

    assert "declared Compose service" in str(exc_info.value)
    assert not any(
        command[:2] == ["docker", "logs"]
        for _, command in runtime.commands
    )


def test_docker_stack_service_url_resolves_compose_published_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        published_ports={("web", 8000): "0.0.0.0:54321\n"},
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    url = stack.service_url("web", 8000, path="/healthz")

    assert url == "http://127.0.0.1:54321/healthz"
    assert runtime.commands[-1] == (
        "DockerComposeStack.service_url",
        [
            "docker",
            "compose",
            "-f",
            stack.resolved_compose_file,
            "-p",
            stack.project_name,
            "port",
            "web",
            "8000",
        ],
    )


def test_run_docker_waits_for_configured_http_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    check = journey_docker.DockerHttpCheck(
        service_name="web",
        port=8000,
        path="/healthz",
        timeout=1,
        poll_interval=0.01,
    )
    runtime = _FakeDockerRuntime(
        published_ports={("web", 8000): "0.0.0.0:5050\n"},
    )
    fake_urlopen = _fake_urlopen_statuses([503, 200])
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)
    monkeypatch.setattr(journey_docker.urllib.request, "urlopen", fake_urlopen)

    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_for_http=[check],
    )

    assert stack.http_checks == (check,)
    assert fake_urlopen.calls == [  # type: ignore[attr-defined]
        ("http://127.0.0.1:5050/healthz", 0.1),
        ("http://127.0.0.1:5050/healthz", 0.1),
    ]


def test_run_docker_http_check_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime()
    fake_urlopen = _fake_urlopen_statuses([503])
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)
    monkeypatch.setattr(journey_docker.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TimeoutError) as exc_info:
        journey_docker.run_docker(
            compose_file=compose_file,
            project_name="demo-project",
            wait_for_http=[
                journey_docker.DockerHttpCheck(
                    service_name="web",
                    port=8000,
                    path="/healthz",
                    timeout=0,
                    poll_interval=0.01,
                )
            ],
        )

    assert "timed out" in str(exc_info.value)


def test_run_docker_validates_http_checks(tmp_path: Path):
    compose_file = _write_compose_file(tmp_path)
    with pytest.raises(TypeError):
        journey_docker.run_docker(compose_file=compose_file, wait_for_http=[object()])
    with pytest.raises(TypeError):
        journey_docker.DockerHttpCheck(service_name="web", port="8000")
    with pytest.raises(ValueError):
        journey_docker.DockerHttpCheck(service_name="web", port=8000, path="healthz")


@pytest.mark.parametrize(
    ("since", "expected_since"),
    [
        ("all", None),
        ("2026-04-14T10:00:00Z", "2026-04-14T10:00:00Z"),
        (
            datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
            "2026-04-14T10:00:00.000000Z",
        ),
    ],
)
def test_docker_stack_wait_for_log_supports_since_variants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    since: datetime | str,
    expected_since: str | None,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        container_logs={
            "web-container-1": "booting\nserver ready\n",
        },
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    match = stack.wait_for_log(
        service_name=r"web",
        message=r"server\s+ready",
        timeout=1,
        poll_interval=0.01,
        since=since,
    )

    assert match == journey_docker.DockerLogMatch(
        service="web",
        container_id="web-container-1",
        container_name="demo-web-1",
        line="server ready",
    )
    docker_logs_command = next(
        command
        for owner, command in runtime.commands
        if owner == "DockerComposeStack.wait_for_log"
        and command[:2] == ["docker", "logs"]
    )
    if expected_since is None:
        assert docker_logs_command == ["docker", "logs", "web-container-1"]
    else:
        assert docker_logs_command == [
            "docker",
            "logs",
            "--since",
            expected_since,
            "web-container-1",
        ]


def test_docker_stack_store_payload_preserves_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    matcher = journey_docker.DockerLogMatcher(
        service_name=r"web",
        message=r"ready",
        timeout=12,
        poll_interval=0.5,
    )
    http_check = journey_docker.DockerHttpCheck(
        service_name="web",
        port=8000,
        path="/healthz",
        timeout=9,
        poll_interval=0.25,
    )
    stack = _build_stack(
        tmp_path,
        wait_timeout=42,
        log_matchers=(matcher,),
        http_checks=(http_check,),
    )
    snapshots: list[dict[str, object]] = []

    def fake_store_docker_snapshot(**kwargs: object) -> None:
        snapshots.append(dict(kwargs))

    monkeypatch.setattr(
        journey_docker,
        "_store_docker_snapshot",
        fake_store_docker_snapshot,
    )

    payload = stack.__store__(
        journey_sdk.JourneyStoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        )
    )
    assert isinstance(payload, dict)
    assert payload["wait_timeout"] == 42
    assert payload["log_matchers"] == [
        {
            "service_name": r"web",
            "message": r"ready",
            "timeout": 12.0,
            "poll_interval": 0.5,
        }
    ]
    assert payload["http_checks"] == [
        {
            "service_name": "web",
            "port": 8000,
            "path": "/healthz",
            "scheme": "http",
            "expected_status": 200,
            "timeout": 9.0,
            "poll_interval": 0.25,
        }
    ]
    assert snapshots[0]["stack"] is stack

    legacy_payload = dict(payload)
    del legacy_payload["wait_timeout"]
    del legacy_payload["log_matchers"]
    del legacy_payload["http_checks"]
    restored_payload = journey_docker._require_stack_payload(
        legacy_payload,
        owner="DockerComposeStack.__restore__",
    )
    assert restored_payload["wait_timeout"] is None
    assert restored_payload["log_matchers"] == ()
    assert restored_payload["http_checks"] == ()

    restored_payload_with_matcher = journey_docker._require_stack_payload(
        payload,
        owner="DockerComposeStack.__restore__",
    )
    assert restored_payload_with_matcher["log_matchers"] == (matcher,)
    assert restored_payload_with_matcher["http_checks"] == (http_check,)

    restored_snapshots: list[dict[str, object]] = []

    def fake_restore_docker_snapshot(**kwargs: object) -> None:
        restored_snapshots.append(dict(kwargs))

    monkeypatch.setattr(
        journey_docker,
        "_restore_docker_snapshot",
        fake_restore_docker_snapshot,
    )

    restored_stack = journey_docker.DockerComposeStack.__restore__(
        payload,
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )
    assert restored_stack.log_matchers == (matcher,)
    assert restored_stack.http_checks == (http_check,)
    assert restored_snapshots[0]["stack"] == restored_stack


def test_run_docker_accepts_wait_failure_when_one_shot_service_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime(
        compose_config={
            "services": {
                "setup": {"image": "demo-setup:latest"},
                "web": {"image": "demo-web:latest"},
            },
            "volumes": {"demo_data": {}},
        },
        compose_config_yaml=(
            "services:\n"
            "  setup:\n"
            "    image: demo-setup:latest\n"
            "  web:\n"
            "    image: demo-web:latest\n"
        ),
        live_ps_rows=[
            _ps_row("setup-container-1", "demo-setup-1", "setup"),
            _ps_row("web-container-1", "demo-web-1", "web"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id="setup-container-1",
                name="demo-setup-1",
                service="setup",
                image="demo-setup:latest",
                state="exited",
                health=None,
            ),
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            ),
        ],
    )

    def run_cli(
        args: object,
        *,
        owner: str,
        failure_level: object | None = None,
    ) -> str:
        output = runtime(args, owner=owner, failure_level=failure_level)
        if isinstance(args, list) and args[:2] == ["docker", "compose"] and "up" in args:
            assert failure_level == "debug"
            raise RuntimeError("container demo-setup-1 exited (0)")
        return output

    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", run_cli)

    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_timeout=15,
    )

    assert stack.project_name == "demo-project"


def test_run_docker_cleans_up_partial_stack_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    runtime = _FakeDockerRuntime(
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
                state="exited",
                exit_code=1,
                health=None,
            )
        ],
    )

    def run_cli(
        args: object,
        *,
        owner: str,
        failure_level: object | None = None,
    ) -> str:
        output = runtime(args, owner=owner, failure_level=failure_level)
        if isinstance(args, list) and args[:2] == ["docker", "compose"] and "up" in args:
            raise RuntimeError("container demo-web-1 exited (1)")
        return output

    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", run_cli)

    def start_docker_stack() -> journey_docker.DockerComposeStack:
        return journey_docker.run_docker(
            compose_file=compose_file,
            project_name="demo-project",
            wait_timeout=15,
        )

    def journey() -> None:
        journey_sdk.step(start_docker_stack)

    with pytest.raises(journey_sdk.CallableExecutionError, match="exited"):
        journey_sdk.execute(journey, no_state=True)

    assert any(
        owner == "DockerComposeStack.__case_exit__"
        and command[-2:] == ["down", "--remove-orphans"]
        and "--volumes" not in command
        for owner, command in runtime.commands
    )


def test_run_docker_log_wait_uses_container_start_after_accepted_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    compose_file = _write_compose_file(tmp_path)
    matcher = journey_docker.DockerLogMatcher(
        service_name=r"web",
        message=r"server\s+ready",
        timeout=0,
        poll_interval=0.01,
    )
    runtime = _FakeDockerRuntime(
        compose_config={
            "services": {
                "setup": {"image": "demo-setup:latest"},
                "web": {"image": "demo-web:latest"},
            },
            "volumes": {"demo_data": {}},
        },
        compose_config_yaml=(
            "services:\n"
            "  setup:\n"
            "    image: demo-setup:latest\n"
            "  web:\n"
            "    image: demo-web:latest\n"
        ),
        live_ps_rows=[
            _ps_row("setup-container-1", "demo-setup-1", "setup"),
            _ps_row("web-container-1", "demo-web-1", "web"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id="setup-container-1",
                name="demo-setup-1",
                service="setup",
                image="demo-setup:latest",
                state="exited",
                health=None,
            ),
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            ),
        ],
    )

    def run_cli(
        args: object,
        *,
        owner: str,
        failure_level: object | None = None,
    ) -> str:
        assert isinstance(args, list)
        if args[:2] == ["docker", "logs"]:
            runtime.commands.append((owner, list(args)))
            if args == [
                "docker",
                "logs",
                "--since",
                "2026-04-14T10:00:00Z",
                "web-container-1",
            ]:
                return "server ready\n"
            return ""
        output = runtime(args, owner=owner, failure_level=failure_level)
        if args[:2] == ["docker", "compose"] and "up" in args:
            raise RuntimeError("container demo-setup-1 exited (0)")
        return output

    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", run_cli)

    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_timeout=15,
        wait_for_logs=[matcher],
    )

    docker_logs_commands = [
        command
        for owner, command in runtime.commands
        if owner == "run_docker" and command[:2] == ["docker", "logs"]
    ]
    assert stack.log_matchers == (matcher,)
    assert docker_logs_commands == [
        [
            "docker",
            "logs",
            "--since",
            "2026-04-14T10:00:00Z",
            "web-container-1",
        ]
    ]


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


def test_docker_run_cli_can_downgrade_subprocess_failure_to_debug(
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
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom\n")

    monkeypatch.setattr(journey_docker.subprocess, "run", fake_run)

    configure_logging("info")
    try:
        with pytest.raises(RuntimeError):
            journey_docker._run_cli(
                ["docker", "compose", "up"],
                owner="test",
                failure_level="debug",
            )
    finally:
        configure_logging("info")

    assert "subprocess failed" not in capsys.readouterr().out

    configure_logging("debug")
    try:
        with pytest.raises(RuntimeError):
            journey_docker._run_cli(
                ["docker", "compose", "up"],
                owner="test",
                failure_level="debug",
            )
    finally:
        configure_logging("info")

    assert "Debug: docker:subprocess_failure" in capsys.readouterr().out


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


def test_store_docker_accepts_compose_ps_json_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()

    def run_cli(args: object, *, owner: str) -> str:
        output = runtime(args, owner=owner)
        if isinstance(args, list) and args[:2] == ["docker", "compose"] and "ps" in args:
            rows = json.loads(output)
            return "\n".join(json.dumps(row) for row in rows) + "\n"
        return output

    monkeypatch.setattr(journey_docker, "_run_cli", run_cli)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["containers"][0]["container_id"] == "web-container-1"


def test_store_docker_matches_abbreviated_compose_ps_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    full_container_id = "13fb39470b3b9dc76db53057f52029e388cb8a47f1271a64b07d7b84ac0f88c2"
    runtime = _FakeDockerRuntime(
        live_ps_rows=[
            _ps_row(full_container_id[:12], "demo-web-1", "web"),
        ],
        live_inspect_rows=[
            _inspect_row(
                container_id=full_container_id,
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            ),
        ],
    )

    def run_cli(args: object, *, owner: str) -> str:
        if isinstance(args, list) and args[:2] == ["docker", "inspect"]:
            return json.dumps(list(runtime.live_inspect_rows.values()))
        return runtime(args, owner=owner)

    monkeypatch.setattr(journey_docker, "_run_cli", run_cli)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["containers"][0]["container_id"] == full_container_id[:12]


def test_docker_stack_statuses_accept_compose_ps_json_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(compose_ps_json_lines=True)
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    statuses = stack.statuses

    assert statuses["web"][0].container_id == "web-container-1"


def test_docker_stack_statuses_accept_short_compose_container_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    short_id = "abc123def456"
    full_id = f"{short_id}7890abcdef7890abcdef7890abcdef7890abcdef7890abcdef"
    runtime = _FakeDockerRuntime(
        live_ps_rows=[_ps_row(short_id, "demo-web-1", "web")],
        live_inspect_rows=[
            _inspect_row(
                container_id=full_id,
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
            )
        ],
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    statuses = stack.statuses

    assert statuses["web"][0].container_id == short_id
    assert statuses["web"][0].container_name == "demo-web-1"


def test_docker_compose_stack_is_pickle_serializable(tmp_path: Path):
    stack = _build_stack(tmp_path)
    restored = pickle.loads(pickle.dumps(stack))
    assert restored == stack


def test_store_docker_creates_manifest_and_volume_snapshot_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))

    volume_entry = manifest["volumes"][0]
    assert manifest["project_name"] == "demo"
    assert manifest["snapshot_name"] == "after_boot"
    assert manifest["format"] == 4
    assert snapshot_dir == (
        Path(stack.compose_file).parent / ".journey" / "docker" / "demo" / "after_boot"
    )
    assert "snapshot_image" not in manifest["containers"][0]
    assert volume_entry["volume_name"] == "demo_demo_data"
    assert "snapshot_volume_name" not in volume_entry
    assert volume_entry["backup_relpath"].startswith("volumes/demo_demo_data-")
    snapshot_payload_dir = snapshot_dir / volume_entry["backup_relpath"]
    assert snapshot_payload_dir.is_dir()
    assert snapshot_payload_dir.is_relative_to(snapshot_dir)
    assert not any(
        command[:3] == ["docker", "volume", "create"]
        for _, command in runtime.commands
    )
    assert not any(
        command[:2] == ["docker", "commit"]
        for _, command in runtime.commands
    )
    assert any(
        command[:3] == ["docker", "run", "--rm"]
        and any(
            "cp -a --reflink=auto --sparse=always" in item
            for item in command
        )
        for _, command in runtime.commands
    )
    assert ("demo_demo_data", str(snapshot_payload_dir)) in runtime.volume_copies
    assert runtime.stopped_container_ids == ["web-container-1"]
    assert runtime.started_container_ids == ["web-container-1"]
    assert not any(
        command[:2] == ["docker", "cp"]
        for _, command in runtime.commands
    )


def test_store_docker_replaces_filesystem_snapshot_without_extra_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")
    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    snapshot_payload_dir = snapshot_dir / manifest["volumes"][0]["backup_relpath"]
    stale_file = snapshot_payload_dir / "stale.txt"
    stale_file.write_text("old", encoding="utf-8")

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    snapshot_payload_dir = snapshot_dir / manifest["volumes"][0]["backup_relpath"]
    assert snapshot_payload_dir.is_dir()
    assert not stale_file.exists()
    assert runtime.volume_names == {"demo_demo_data"}
    assert not any(
        command[:3] == ["docker", "volume", "create"]
        for _, command in runtime.commands
    )
    assert runtime.volume_copies.count(("demo_demo_data", str(snapshot_payload_dir))) == 2


def test_store_docker_removes_replaced_snapshot_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(
            {
                "format": 3,
                "volumes": [{"snapshot_volume_name": "old_demo_snapshot"}],
            }
        ),
        encoding="utf-8",
    )
    runtime = _FakeDockerRuntime()
    runtime.volume_names.add("old_demo_snapshot")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    assert any(
        command == ["docker", "volume", "rm", "old_demo_snapshot"]
        for _, command in runtime.commands
    )
    assert "old_demo_snapshot" not in runtime.volume_names


def test_store_docker_dedupes_multiple_mounts_of_one_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
                mounts=[
                    {
                        "Type": "volume",
                        "Name": "demo_demo_data",
                        "Destination": "/data",
                        "Mode": "",
                        "RW": True,
                    },
                    {
                        "Type": "volume",
                        "Name": "demo_demo_data",
                        "Destination": "/data-again",
                        "Mode": "",
                        "RW": True,
                    },
                ],
            )
        ],
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [volume["volume_name"] for volume in manifest["volumes"]] == ["demo_demo_data"]
    assert len(runtime.volume_copies) == 1


def test_store_docker_info_logs_compact_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("info", output_format="jsonl")
    try:
        journey_docker.store_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    records = _docker_jsonl_records(capsys.readouterr().out)
    records_by_event = {record["event"]: record for record in records}
    assert "snapshot_store_start" in records_by_event
    assert "snapshot_store_success" in records_by_event
    assert "snapshot_store_volume_payload_success" not in records_by_event
    assert "snapshot_store_manifest_write_success" not in records_by_event
    success = records_by_event["snapshot_store_success"]
    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    assert success["level"] == "INFO"
    assert isinstance(success["duration_ms"], int | float)
    assert success["snapshot_dir"] == str(snapshot_dir)
    assert success["manifest_path"] == str(snapshot_dir / "manifest.json")
    assert success["containers"] == 1
    assert success["volumes"] == 1


def test_store_docker_debug_logs_snapshot_phase_timings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("debug", output_format="jsonl")
    try:
        journey_docker.store_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    records = _docker_jsonl_records(capsys.readouterr().out)
    records_by_event = {record["event"]: record for record in records}
    for event in [
        "snapshot_store_prepare_snapshot_dir_success",
        "snapshot_store_compose_config_success",
        "snapshot_store_live_containers_success",
        "snapshot_store_validate_snapshot_success",
        "snapshot_store_manifest_write_success",
    ]:
        assert event in records_by_event
        assert records_by_event[event]["level"] == "DEBUG"
        assert isinstance(records_by_event[event]["duration_ms"], int | float)

    assert records_by_event["snapshot_store_success"]["level"] == "INFO"
    assert "snapshot_store_container_commit_success" not in records_by_event

    volume_record = next(
        record
        for record in records
        if record["event"] == "snapshot_store_volume_payload_success"
    )
    assert volume_record["level"] == "DEBUG"
    assert volume_record["volume"] == "demo_demo_data"
    assert volume_record["target_path"] == "/data"
    assert isinstance(volume_record["duration_ms"], int | float)


def test_store_docker_jsonl_info_hides_snapshot_phase_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("info", output_format="jsonl")
    try:
        journey_docker.store_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    events = {
        record["event"] for record in _docker_jsonl_records(capsys.readouterr().out)
    }
    assert "snapshot_store_success" in events
    assert "snapshot_store_volume_payload_success" not in events
    assert "snapshot_store_manifest_write_success" not in events


def test_store_docker_pretty_logs_compact_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("info", output_format="pretty")
    try:
        journey_docker.store_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    output = capsys.readouterr().out
    assert "committing Docker container snapshot" not in output
    assert "copying Docker volume to snapshot" not in output
    assert "writing Docker snapshot manifest" not in output
    assert ".tar" not in output
    assert "stored Docker Compose snapshot" in output
    assert "manifest=" in output
    assert "containers=1 volumes=1" in output


def test_store_docker_debug_pretty_logs_identify_containers_and_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("debug", output_format="pretty")
    try:
        journey_docker.store_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    output = capsys.readouterr().out
    assert (
        "copying Docker volume to snapshot: volume=demo_demo_data service=web "
        "container=demo-web-1 from=demo_demo_data to=volumes/demo_demo_data-"
    ) in output
    assert "writing Docker snapshot manifest" in output
    assert "stored Docker Compose snapshot" in output


def test_store_docker_ignores_bind_mounts_and_snapshots_managed_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
                mounts=[
                    {
                        "Type": "bind",
                        "Source": "/host/config.json",
                        "Destination": "/app/config.json",
                        "Mode": "ro",
                        "RW": False,
                    },
                    {
                        "Type": "volume",
                        "Name": "demo_demo_data",
                        "Destination": "/data",
                        "Mode": "",
                        "RW": True,
                    },
                ],
            )
        ],
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")

    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [volume["volume_name"] for volume in manifest["volumes"]] == ["demo_demo_data"]
    assert all(
        command[:2] != ["docker", "create"] or "/app/config.json" not in command
        for _, command in runtime.commands
    )


@pytest.mark.parametrize(
    ("mount_type", "state", "external", "read_write", "message"),
    [
        ("tmpfs", "running", False, True, "unsupported type"),
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


def test_restarting_containers_are_treated_as_started_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    runtime = _FakeDockerRuntime(
        live_inspect_rows=[
            _inspect_row(
                container_id="web-container-1",
                name="demo-web-1",
                service="web",
                image="demo-web:latest",
                state="restarting",
                mounts=[],
            )
        ],
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.store_docker(stack, snapshot_name="after_boot")
    journey_docker.restore_docker(stack, snapshot_name="after_boot")

    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["containers"][0]["state"] == "restarting"
    assert manifest["volumes"] == []
    assert any(
        command == [
            "docker",
            "compose",
            "-f",
            stack.resolved_compose_file,
            "-p",
            stack.project_name,
            "up",
            "-d",
            "--wait",
            "--no-recreate",
            "--no-deps",
            "web",
        ]
        for _, command in runtime.commands
    )


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


def test_restore_docker_recreates_stack_and_restores_snapshot_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path, wait_timeout=42)
    snapshot_dir = _write_restore_snapshot(stack)
    snapshot_payload_dir = snapshot_dir / "volumes" / "demo-demo-data"

    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.restore_docker(stack, snapshot_name="after_boot")

    override_path = Path(stack.cache_root) / stack.project_name / "restore-after_boot.override.yml"
    assert not override_path.exists()
    assert (str(snapshot_payload_dir), "demo_demo_data") in runtime.volume_copies
    assert any(
        command[:3] == ["docker", "run", "--rm"]
        and any(
            "cp -a --reflink=auto --sparse=always" in item
            for item in command
        )
        for _, command in runtime.commands
    )
    assert not any(
        command[:2] == ["docker", "cp"]
        for _, command in runtime.commands
    )
    assert any(
        command == [
            "docker",
            "compose",
            "-f",
            stack.resolved_compose_file,
            "-p",
            stack.project_name,
            "up",
            "-d",
            "--wait",
            "--no-recreate",
            "--no-deps",
            "--wait-timeout",
            "42",
            "web",
        ]
        for _, command in runtime.commands
    )
    assert not any(
        str(override_path) in command
        for _, command in runtime.commands
    )


def test_restore_docker_rejects_legacy_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    snapshot_payload_dir = snapshot_dir / "volumes" / "demo-demo-data"
    snapshot_payload_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_payload_dir / "snapshot.txt").write_text("saved", encoding="utf-8")
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

    with pytest.raises(RuntimeError) as exc_info:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")

    assert "only restores format 4 filesystem-backed snapshots" in str(exc_info.value)


def test_restore_docker_rejects_unsafe_backup_relpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    snapshot_dir = _write_restore_snapshot(stack)
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["volumes"][0]["backup_relpath"] = "../escape"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    with pytest.raises(RuntimeError) as exc_info:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")

    assert "invalid volume backup_relpath" in str(exc_info.value)
    assert not any("down" in command for _, command in runtime.commands)


def test_restore_docker_info_logs_compact_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    _write_restore_snapshot(stack)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("info", output_format="jsonl")
    try:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    records = _docker_jsonl_records(capsys.readouterr().out)
    records_by_event = {record["event"]: record for record in records}
    assert "snapshot_restore_start" in records_by_event
    assert "snapshot_restore_success" in records_by_event
    assert "snapshot_restore_volume_remove_success" not in records_by_event
    assert "snapshot_restore_volume_restore_success" not in records_by_event
    assert "snapshot_restore_start_services_success" not in records_by_event
    success = records_by_event["snapshot_restore_success"]
    snapshot_dir = journey_docker._snapshot_dir(stack=stack, snapshot_name="after_boot")
    assert success["level"] == "INFO"
    assert isinstance(success["duration_ms"], int | float)
    assert success["snapshot_dir"] == str(snapshot_dir)
    assert success["manifest_path"] == str(snapshot_dir / "manifest.json")
    assert success["containers"] == 1
    assert success["volumes"] == 1
    assert success["services"] == ["web"]


def test_restore_docker_debug_logs_snapshot_phase_timings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    _write_restore_snapshot(stack)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("debug", output_format="jsonl")
    try:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    records = _docker_jsonl_records(capsys.readouterr().out)
    records_by_event = {record["event"]: record for record in records}
    for event in [
        "snapshot_restore_manifest_load_success",
        "snapshot_restore_compose_down_success",
        "snapshot_restore_volume_remove_success",
        "snapshot_restore_compose_create_success",
        "snapshot_restore_recreated_containers_success",
        "snapshot_restore_volume_restore_success",
        "snapshot_restore_start_services_success",
    ]:
        assert event in records_by_event
        assert records_by_event[event]["level"] == "DEBUG"
        assert isinstance(records_by_event[event]["duration_ms"], int | float)

    assert records_by_event["snapshot_restore_success"]["level"] == "INFO"
    assert "snapshot_restore_override_write_success" not in records_by_event
    assert records_by_event["snapshot_restore_volume_remove_success"]["removed"] is True
    assert records_by_event["snapshot_restore_start_services_success"]["services"] == ["web"]
    assert records_by_event["snapshot_restore_volume_restore_success"]["volume"] == (
        "demo_demo_data"
    )


def test_restore_docker_pretty_logs_compact_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    _write_restore_snapshot(stack)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("info", output_format="pretty")
    try:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    output = capsys.readouterr().out
    assert "removing Docker volume before restore" not in output
    assert "checked Docker volume before restore" not in output
    assert "restoring Docker volume contents" not in output
    assert "starting restored Docker Compose services" not in output
    assert "restored Docker Compose snapshot" in output
    assert "manifest=" in output
    assert "containers=1 volumes=1 services=web" in output


def test_restore_docker_debug_pretty_logs_identify_volumes_and_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    stack = _build_stack(tmp_path)
    _write_restore_snapshot(stack)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    configure_logging("debug", output_format="pretty")
    try:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")
    finally:
        configure_logging("info")

    output = capsys.readouterr().out
    assert (
        "removing Docker volume before restore: volume=demo_demo_data service=web "
        "container=demo-web-1"
    ) in output
    assert (
        "checked Docker volume before restore" in output
        and "volume=demo_demo_data service=web container=demo-web-1 removed=true" in output
    )
    assert (
        "restoring Docker volume contents: volume=demo_demo_data service=web "
        "container=demo-web-1 id=web-container-2 from=volumes/demo-demo-data "
        "to=demo_demo_data target=/data"
    ) in output
    assert "starting restored Docker Compose services: services=web count=1" in output


def test_restore_docker_waits_for_configured_raw_container_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(
        tmp_path,
        log_matchers=(
            journey_docker.DockerLogMatcher(
                service_name=r"web",
                message=r"restored\s+ready",
                timeout=1,
                poll_interval=0.01,
            ),
        ),
    )
    _write_restore_snapshot(stack)
    runtime = _FakeDockerRuntime(
        container_logs={
            "web-container-1": "old ready\n",
            "web-container-2": "restored ready\n",
        },
    )
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.restore_docker(stack, snapshot_name="after_boot")

    docker_logs_commands = [
        command
        for owner, command in runtime.commands
        if owner == "restore_docker" and command[:2] == ["docker", "logs"]
    ]
    assert len(docker_logs_commands) == 1
    assert docker_logs_commands[0][:3] == ["docker", "logs", "--since"]
    assert docker_logs_commands[0][-1] == "web-container-2"


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
                image="demo-app:latest",
                mounts=[],
            ),
            _inspect_row(
                container_id="db-container-2",
                name="demo-db-1",
                service="db",
                image="postgres:16-alpine",
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

    def start_docker_stack() -> journey_docker.DockerComposeStack:
        return journey_docker.run_docker(
            compose_file=compose_file,
            project_name="demo",
        )

    def journey():
        stack = journey_sdk.step(start_docker_stack)
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
            "start_docker_stack",
            "assert_stack_ready",
            "capture_baseline_state",
            "increment_counter",
            "assert_increment_branch",
        ],
        [
            "start_docker_stack",
            "assert_stack_ready",
            "capture_baseline_state",
            "read_counter_state",
            "assert_restored_counter_branch",
        ],
    ]
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "store_docker" and command[:2] == ["docker", "commit"]
    ) == 0
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "store_docker"
        and command[:3] == ["docker", "run", "--rm"]
        and any("cp -a --reflink=auto --sparse=always" in item for item in command)
    ) == 2
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "restore_docker" and "down" in command
    ) == 1
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "DockerComposeStack.__case_exit__"
        and command[-2:] == ["down", "--remove-orphans"]
    ) == 2


def test_execute_no_state_stores_docker_snapshot_under_journey_dot_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    journey_dir = tmp_path / "journey"
    journey_dir.mkdir()
    compose_file = _write_compose_file(tmp_path)
    journey_file = journey_dir / "flow.py"
    source = """
def capture_anchor(stack):
    return "anchor"


def first_branch(stack):
    EVENTS.append("first")
    return True


def second_branch(stack):
    EVENTS.append("second")
    return True


def start_docker_stack():
    return journey_docker.run_docker(
        compose_file=COMPOSE_FILE,
        project_name="demo",
    )


def flow():
    stack = journey_sdk.step(start_docker_stack)
    anchor = journey_sdk.step(capture_anchor, stack)
    if journey_sdk.branch(start_from=anchor):
        journey_sdk.step(first_branch, stack)
    elif journey_sdk.branch(start_from=anchor):
        journey_sdk.step(second_branch, stack)
"""
    journey_file.write_text(source, encoding="utf-8")
    events: list[str] = []
    namespace = {
        "COMPOSE_FILE": compose_file,
        "EVENTS": events,
        "journey_docker": journey_docker,
        "journey_sdk": journey_sdk,
    }
    exec(compile(source, str(journey_file), "exec"), namespace)
    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    report = journey_sdk.execute(namespace["flow"], no_state=True)

    artifact_root = journey_dir / ".journey" / "state.json.artifacts"
    manifest_paths = list(artifact_root.rglob("manifest.json"))
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]
    assert events == ["first", "second"]
    assert not (journey_dir / ".journey" / "state.json").exists()
    assert len(manifest_paths) == 1
    assert manifest_paths[0].is_relative_to(artifact_root)
    assert any(
        source == "demo_demo_data" and Path(destination).is_relative_to(artifact_root)
        for source, destination in runtime.volume_copies
    )
    assert any(
        Path(source).is_relative_to(artifact_root) and destination == "demo_demo_data"
        for source, destination in runtime.volume_copies
    )
    bind_mounts = [
        command[index + 1]
        for _, command in runtime.commands
        for index, item in enumerate(command)
        if item == "--mount" and "type=bind" in command[index + 1]
    ]
    assert all("/.journey/" not in _mount_parts(mount)["source"] for mount in bind_mounts)
    assert any(
        _mount_parts(mount)["source"] == str(journey_dir.resolve())
        and _mount_parts(mount)["target"] == "/to-root"
        for mount in bind_mounts
    )
    assert any(
        _mount_parts(mount)["source"] == str(journey_dir.resolve())
        and _mount_parts(mount)["target"] == "/from-root"
        for mount in bind_mounts
    )


def test_journey_resume_reruns_docker_retry_anchor_without_snapshotting_args(
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

    def start_docker_stack() -> journey_docker.DockerComposeStack:
        return journey_docker.run_docker(
            compose_file=compose_file,
            project_name="demo",
        )

    def journey():
        stack = journey_sdk.step(start_docker_stack)
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
        "start_docker_stack",
        "poll",
        "finish",
    ]
    assert not list((tmp_path / "journey.state.artifacts").rglob("manifest.json"))
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "restore_docker" and "down" in command
    ) == 0


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
    )
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
                "--volumes",
                "--remove-orphans",
            ],
            owner="test_real_docker_smoke_round_trips_one_compose_snapshot",
        )
