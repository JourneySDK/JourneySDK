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
        self.volume_names = {"demo_demo_data", "demo_demo_data_snapshot"}
        self.volume_labels: dict[str, dict[str, str]] = {}
        self.phase = "live"
        self.commands: list[tuple[str, list[str]]] = []
        self.volume_copies: list[tuple[str, str]] = []
        self.stopped_container_ids: list[str] = []
        self.started_container_ids: list[str] = []

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

        if args[:3] == ["docker", "run", "--rm"]:
            mount_values = [
                args[index + 1]
                for index, item in enumerate(args)
                if item == "--mount"
            ]
            mounts = [_mount_parts(value) for value in mount_values]
            source = next(
                mount["source"]
                for mount in mounts
                if mount.get("target") == "/from"
            )
            destination = next(
                mount["source"]
                for mount in mounts
                if mount.get("target") == "/to"
            )
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


def _mount_parts(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(",") if "=" in part)


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
    snapshot_dir = Path(stack.cache_root) / stack.project_name / snapshot_name
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": 3,
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
                "snapshot_volume_name": "demo_demo_data_snapshot",
                "mode": "",
            }
        ],
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return snapshot_dir


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

    def run_cli(args: object, *, owner: str) -> str:
        output = runtime(args, owner=owner)
        if isinstance(args, list) and args[:2] == ["docker", "compose"] and "up" in args:
            raise RuntimeError("container demo-setup-1 exited (0)")
        return output

    monkeypatch.setattr(journey_docker, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(journey_docker, "_run_cli", run_cli)

    stack = journey_docker.run_docker(
        compose_file=compose_file,
        project_name="demo-project",
        wait_timeout=15,
    )()

    assert stack.project_name == "demo-project"


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

    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
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

    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["containers"][0]["container_id"] == full_container_id[:12]


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

    volume_entry = manifest["volumes"][0]
    assert manifest["project_name"] == "demo"
    assert manifest["snapshot_name"] == "after_boot"
    assert manifest["format"] == 3
    assert "snapshot_image" not in manifest["containers"][0]
    assert volume_entry["volume_name"] == "demo_demo_data"
    assert volume_entry["snapshot_volume_name"].startswith("journey-snapshot-demo-")
    assert volume_entry["snapshot_volume_name"] in runtime.volume_names
    assert not any(
        command[:2] == ["docker", "commit"]
        for _, command in runtime.commands
    )
    assert any(
        command[:3] == ["docker", "run", "--rm"]
        and any(
            "cp -a --reflink=auto --sparse=always /from/. /to/" in item
            for item in command
        )
        for _, command in runtime.commands
    )
    assert ("demo_demo_data", volume_entry["snapshot_volume_name"]) in runtime.volume_copies
    assert runtime.stopped_container_ids == ["web-container-1"]
    assert runtime.started_container_ids == ["web-container-1"]
    assert not any(
        command[:2] == ["docker", "cp"]
        for _, command in runtime.commands
    )


def test_store_docker_removes_replaced_snapshot_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
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


def test_store_docker_logs_snapshot_phase_timings(
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
    for event in [
        "snapshot_store_prepare_snapshot_dir_success",
        "snapshot_store_compose_config_success",
        "snapshot_store_live_containers_success",
        "snapshot_store_validate_snapshot_success",
        "snapshot_store_manifest_write_success",
        "snapshot_store_success",
    ]:
        assert event in records_by_event
        assert isinstance(records_by_event[event]["duration_ms"], int | float)

    assert "snapshot_store_container_commit_success" not in records_by_event

    volume_record = next(
        record
        for record in records
        if record["event"] == "snapshot_store_volume_backup_success"
    )
    assert volume_record["volume"] == "demo_demo_data"
    assert volume_record["target_path"] == "/data"
    assert isinstance(volume_record["duration_ms"], int | float)


def test_store_docker_pretty_logs_identify_containers_and_volumes(
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
    assert (
        "copying Docker volume to snapshot: volume=demo_demo_data service=web "
        "container=demo-web-1 from=demo_demo_data to=journey-snapshot-demo-"
    ) in output
    assert ".tar" not in output
    assert "stored Docker Compose snapshot" in output
    assert "containers=1 volumes=1" in output


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

    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
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

    snapshot_dir = Path(stack.cache_root) / stack.project_name / "after_boot"
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
            "start",
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


def test_restore_docker_recreates_stack_and_restores_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    stack = _build_stack(tmp_path)
    _write_restore_snapshot(stack)

    runtime = _FakeDockerRuntime()
    monkeypatch.setattr(journey_docker, "_run_cli", runtime)

    journey_docker.restore_docker(stack, snapshot_name="after_boot")

    override_path = Path(stack.cache_root) / stack.project_name / "restore-after_boot.override.yml"
    assert not override_path.exists()
    assert ("demo_demo_data_snapshot", "demo_demo_data") in runtime.volume_copies
    assert any(
        command[:3] == ["docker", "run", "--rm"]
        and any(
            "cp -a --reflink=auto --sparse=always /from/. /to/" in item
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
            "start",
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

    with pytest.raises(RuntimeError) as exc_info:
        journey_docker.restore_docker(stack, snapshot_name="after_boot")

    assert "only restores format 3 volume-clone snapshots" in str(exc_info.value)


def test_restore_docker_logs_snapshot_phase_timings(
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
    for event in [
        "snapshot_restore_manifest_load_success",
        "snapshot_restore_compose_down_success",
        "snapshot_restore_volume_remove_success",
        "snapshot_restore_compose_create_success",
        "snapshot_restore_recreated_containers_success",
        "snapshot_restore_volume_restore_success",
        "snapshot_restore_start_services_success",
        "snapshot_restore_success",
    ]:
        assert event in records_by_event
        assert isinstance(records_by_event[event]["duration_ms"], int | float)

    assert "snapshot_restore_override_write_success" not in records_by_event
    assert records_by_event["snapshot_restore_volume_remove_success"]["removed"] is True
    assert records_by_event["snapshot_restore_start_services_success"]["services"] == ["web"]
    assert records_by_event["snapshot_restore_volume_restore_success"]["volume"] == (
        "demo_demo_data"
    )


def test_restore_docker_pretty_logs_identify_volumes_and_services(
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
        "container=demo-web-1 id=web-container-2 from=demo_demo_data_snapshot "
        "to=demo_demo_data target=/data"
    ) in output
    assert "starting restored Docker Compose services: services=web count=1" in output


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
        if owner == "store_docker" and command[:2] == ["docker", "commit"]
    ) == 0
    assert sum(
        1
        for owner, command in runtime.commands
        if owner == "store_docker"
        and command[:3] == ["docker", "run", "--rm"]
        and any("cp -a --reflink=auto --sparse=always" in item for item in command)
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
