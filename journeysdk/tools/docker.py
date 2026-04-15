"""Official Docker Compose snapshot tool."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

_CACHE_ROOT = Path(tempfile.gettempdir()) / "journey-sdk-docker"
_SUPPORTED_CONTAINER_STATES = {"running", "created", "exited"}
_UNSUPPORTED_CONTAINER_STATES = {"paused", "restarting", "removing", "dead"}


@dataclass(frozen=True)
class DockerContainerStatus:
    """Live status for one Compose-managed container."""

    service: str
    container_id: str
    container_name: str
    state: str
    health: str | None
    exit_code: int | None
    image: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class DockerComposeStack:
    """Serializable descriptor for one started Docker Compose project."""

    compose_file: str
    resolved_compose_file: str
    project_name: str
    cache_root: str

    @property
    def statuses(self) -> dict[str, tuple[DockerContainerStatus, ...]]:
        """Return live container statuses grouped by Compose service."""

        return _load_live_statuses(self)

    @property
    def logs(self) -> dict[str, str]:
        """Return live combined logs grouped by Compose service."""

        return _load_live_logs(self)


@dataclass(frozen=True)
class _LiveContainer:
    status: DockerContainerStatus
    container_index: int
    mounts: tuple[dict[str, Any], ...]


def run_docker(
    *,
    compose_file: str | os.PathLike[str] | None = None,
    project_name: str | None = None,
    wait_timeout: int | None = None,
) -> Callable[[], DockerComposeStack]:
    """Return a step callable that starts one local Docker Compose app."""

    normalized_compose_file = _normalize_optional_pathlike(
        owner="run_docker",
        field="compose_file",
        value=compose_file,
    )
    normalized_project_name = _normalize_optional_name(
        owner="run_docker",
        field="project_name",
        value=project_name,
    )
    normalized_wait_timeout = _normalize_optional_positive_int(
        owner="run_docker",
        field="wait_timeout",
        value=wait_timeout,
    )

    def start_stack() -> DockerComposeStack:
        original_compose_path = _resolve_compose_file(normalized_compose_file)
        resolved_project_name = normalized_project_name or _generate_project_name(
            original_compose_path
        )
        project_cache_dir = _project_cache_dir(
            cache_root=_CACHE_ROOT,
            project_name=resolved_project_name,
        )
        project_cache_dir.mkdir(parents=True, exist_ok=True)
        resolved_compose_path = project_cache_dir / "resolved-compose.yml"

        resolved_yaml = _run_cli(
            _compose_command(
                compose_file=original_compose_path,
                project_name=resolved_project_name,
                subcommand=["config", "--format", "yaml"],
            ),
            owner="run_docker",
        )
        resolved_compose_path.write_text(resolved_yaml, encoding="utf-8")

        up_command = ["up", "-d", "--wait"]
        if normalized_wait_timeout is not None:
            up_command.extend(["--wait-timeout", str(normalized_wait_timeout)])
        _run_cli(
            _compose_command(
                compose_file=resolved_compose_path,
                project_name=resolved_project_name,
                subcommand=up_command,
            ),
            owner="run_docker",
        )

        return DockerComposeStack(
            compose_file=str(original_compose_path),
            resolved_compose_file=str(resolved_compose_path),
            project_name=resolved_project_name,
            cache_root=str(_CACHE_ROOT),
        )

    _set_step_metadata(
        start_stack,
        label="run_docker",
        owner="run_docker",
        attrs={},
    )
    return start_stack


def store_docker(
    stack: DockerComposeStack,
    *,
    snapshot_name: str = "default",
) -> None:
    """Store one Docker Compose snapshot for checkpoint replay."""

    validated_stack = _require_stack(stack=stack, owner="store_docker")
    normalized_snapshot_name = _normalize_snapshot_name(
        owner="store_docker",
        value=snapshot_name,
    )
    snapshot_dir = _snapshot_dir(
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
    )
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    (snapshot_dir / "volumes").mkdir(parents=True, exist_ok=True)

    compose_config = _load_compose_config(validated_stack, owner="store_docker")
    live_containers = _load_live_containers(
        validated_stack,
        owner="store_docker",
        include_all=True,
    )
    _require_declared_services_present(
        compose_config=compose_config,
        live_containers=live_containers,
        owner="store_docker",
    )
    _require_single_container_per_service(
        live_containers=live_containers,
        owner="store_docker",
    )
    external_volume_names = _external_volume_names(compose_config)

    container_entries: list[dict[str, Any]] = []
    volume_entries_by_name: dict[str, dict[str, Any]] = {}
    for live_container in live_containers:
        _require_supported_container_state(
            live_container=live_container,
            owner="store_docker",
        )
        snapshot_image = _snapshot_image_ref(
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            service=live_container.status.service,
            container_index=live_container.container_index,
        )
        _run_cli(
            ["docker", "commit", live_container.status.container_id, snapshot_image],
            owner="store_docker",
        )
        container_entries.append(
            {
                "service": live_container.status.service,
                "container_id": live_container.status.container_id,
                "container_name": live_container.status.container_name,
                "container_index": live_container.container_index,
                "state": live_container.status.state,
                "health": live_container.status.health,
                "exit_code": live_container.status.exit_code,
                "image": live_container.status.image,
                "started_at": live_container.status.started_at,
                "finished_at": live_container.status.finished_at,
                "snapshot_image": snapshot_image,
            }
        )
        for mount in live_container.mounts:
            volume_entry = _build_volume_entry(
                stack=validated_stack,
                live_container=live_container,
                mount=mount,
                external_volume_names=external_volume_names,
            )
            if volume_entry is None:
                continue
            existing = volume_entries_by_name.get(volume_entry["volume_name"])
            if existing is not None:
                continue
            backup_dir = snapshot_dir / volume_entry["backup_relpath"]
            backup_dir.mkdir(parents=True, exist_ok=True)
            _copy_container_path_to_local(
                owner="store_docker",
                container_id=live_container.status.container_id,
                container_path=volume_entry["target_path"],
                local_path=backup_dir,
            )
            volume_entries_by_name[volume_entry["volume_name"]] = volume_entry

    manifest = {
        "format": 1,
        "project_name": validated_stack.project_name,
        "compose_file": validated_stack.compose_file,
        "resolved_compose_file": validated_stack.resolved_compose_file,
        "snapshot_name": normalized_snapshot_name,
        "containers": container_entries,
        "volumes": list(volume_entries_by_name.values()),
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def restore_docker(
    stack: DockerComposeStack,
    *,
    snapshot_name: str = "default",
) -> None:
    """Restore one stored Docker Compose snapshot for checkpoint replay."""

    validated_stack = _require_stack(stack=stack, owner="restore_docker")
    normalized_snapshot_name = _normalize_snapshot_name(
        owner="restore_docker",
        value=snapshot_name,
    )
    snapshot_dir = _snapshot_dir(
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
    )
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "restore_docker(...) could not find a stored snapshot manifest at "
            f"'{manifest_path}'."
        )

    manifest = _load_manifest(manifest_path)
    _validate_manifest_matches_stack(
        manifest=manifest,
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
    )
    container_entries = _manifest_container_entries(manifest)
    volume_entries = _manifest_volume_entries(manifest)

    _require_single_container_manifest(
        container_entries=container_entries,
        owner="restore_docker",
    )

    override_path = _write_restore_override(
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
        container_entries=container_entries,
    )

    _run_cli(
        _compose_command(
            compose_file=Path(validated_stack.resolved_compose_file),
            project_name=validated_stack.project_name,
            subcommand=["down", "--remove-orphans"],
        ),
        owner="restore_docker",
    )

    for volume_entry in volume_entries:
        _remove_volume_if_exists(
            owner="restore_docker",
            volume_name=volume_entry["volume_name"],
        )

    _run_cli(
        _compose_multi_file_command(
            compose_files=[
                Path(validated_stack.resolved_compose_file),
                override_path,
            ],
            project_name=validated_stack.project_name,
            subcommand=["create", "--force-recreate"],
        ),
        owner="restore_docker",
    )

    recreated_containers = _load_live_containers(
        validated_stack,
        owner="restore_docker",
        include_all=True,
        extra_compose_files=(override_path,),
    )
    recreated_by_key = {
        (container.status.service, container.container_index): container
        for container in recreated_containers
    }

    for volume_entry in volume_entries:
        key = (volume_entry["service"], volume_entry["container_index"])
        live_container = recreated_by_key.get(key)
        if live_container is None:
            raise RuntimeError(
                "restore_docker(...) could not find the recreated container for "
                f"service '{volume_entry['service']}' index {volume_entry['container_index']}."
            )
        backup_dir = snapshot_dir / volume_entry["backup_relpath"]
        if not backup_dir.exists():
            raise FileNotFoundError(
                "restore_docker(...) could not find the backed-up volume contents at "
                f"'{backup_dir}'."
            )
        _copy_local_path_to_container(
            owner="restore_docker",
            local_path=backup_dir,
            container_id=live_container.status.container_id,
            container_path=volume_entry["target_path"],
        )

    running_services = sorted(
        {
            container_entry["service"]
            for container_entry in container_entries
            if container_entry["state"] == "running"
        }
    )
    if running_services:
        _run_cli(
            _compose_multi_file_command(
                compose_files=[
                    Path(validated_stack.resolved_compose_file),
                    override_path,
                ],
                project_name=validated_stack.project_name,
                subcommand=["start", *running_services],
            ),
            owner="restore_docker",
        )


def _load_live_statuses(stack: DockerComposeStack) -> dict[str, tuple[DockerContainerStatus, ...]]:
    live_containers = _load_live_containers(
        _require_stack(stack=stack, owner="DockerComposeStack.statuses"),
        owner="DockerComposeStack.statuses",
        include_all=True,
    )
    grouped: dict[str, list[DockerContainerStatus]] = defaultdict(list)
    for live_container in live_containers:
        grouped[live_container.status.service].append(live_container.status)
    return {
        service: tuple(statuses)
        for service, statuses in sorted(grouped.items())
    }


def _load_live_logs(stack: DockerComposeStack) -> dict[str, str]:
    validated_stack = _require_stack(stack=stack, owner="DockerComposeStack.logs")
    services = sorted(_load_live_statuses(validated_stack))
    logs: dict[str, str] = {}
    for service in services:
        logs[service] = _run_cli(
            _compose_command(
                compose_file=Path(validated_stack.resolved_compose_file),
                project_name=validated_stack.project_name,
                subcommand=["logs", "--no-color", "--timestamps", service],
            ),
            owner="DockerComposeStack.logs",
        )
    return logs


def _require_stack(*, stack: DockerComposeStack, owner: str) -> DockerComposeStack:
    if not isinstance(stack, DockerComposeStack):
        raise TypeError(f"{owner}(...) expects a DockerComposeStack step result.")
    resolved_compose_path = Path(stack.resolved_compose_file)
    if not resolved_compose_path.exists():
        raise FileNotFoundError(
            f"{owner}(...) could not find the resolved Compose file '{resolved_compose_path}'."
        )
    return stack


def _resolve_compose_file(compose_file: str | None) -> Path:
    candidate = Path(compose_file or "./docker-compose.yml").expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(
            f"run_docker(...) could not find the Compose file '{candidate}'."
        )
    if not candidate.is_file():
        raise FileNotFoundError(
            f"run_docker(...) expected a file at '{candidate}', but it was not a file."
        )
    return candidate


def _normalize_optional_pathlike(
    *,
    owner: str,
    field: str,
    value: object,
) -> str | None:
    if value is None:
        return None
    try:
        normalized = os.fspath(value)
    except TypeError as exc:
        raise TypeError(
            f"{owner}(..., {field}=...) expects a path-like value or None."
        ) from exc
    if not isinstance(normalized, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string path.")
    if not normalized.strip():
        raise ValueError(f"{owner}(..., {field}=...) expects a non-blank path.")
    return normalized


def _normalize_optional_name(
    *,
    owner: str,
    field: str,
    value: object,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string or None.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{owner}(..., {field}=...) expects a non-blank string.")
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError(
            f"{owner}(..., {field}=...) cannot contain path separators or traversal segments."
        )
    return normalized


def _normalize_optional_positive_int(
    *,
    owner: str,
    field: str,
    value: object,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{owner}(..., {field}=...) expects an integer or None.")
    if value <= 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a positive integer.")
    return value


def _normalize_snapshot_name(*, owner: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., snapshot_name=...) expects a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{owner}(..., snapshot_name=...) expects a non-blank string.")
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError(
            f"{owner}(..., snapshot_name=...) cannot contain path separators or traversal segments."
        )
    return normalized


def _generate_project_name(compose_file: Path) -> str:
    return f"journey-{_slugify(compose_file.stem)}-{uuid4().hex[:8]}"


def _project_cache_dir(*, cache_root: Path, project_name: str) -> Path:
    return cache_root / project_name


def _snapshot_dir(*, stack: DockerComposeStack, snapshot_name: str) -> Path:
    return _project_cache_dir(
        cache_root=Path(stack.cache_root),
        project_name=stack.project_name,
    ) / snapshot_name


def _load_compose_config(stack: DockerComposeStack, *, owner: str) -> dict[str, Any]:
    output = _run_cli(
        _compose_command(
            compose_file=Path(stack.resolved_compose_file),
            project_name=stack.project_name,
            subcommand=["config", "--format", "json"],
        ),
        owner=owner,
    )
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{owner}(...) could not parse the resolved Compose config JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{owner}(...) received an invalid Compose config payload.")
    return parsed


def _load_live_containers(
    stack: DockerComposeStack,
    *,
    owner: str,
    include_all: bool,
    extra_compose_files: Sequence[Path] = (),
) -> list[_LiveContainer]:
    subcommand = ["ps"]
    if include_all:
        subcommand.append("-a")
    subcommand.extend(["--format", "json"])
    ps_output = _run_cli(
        _compose_multi_file_command(
            compose_files=[Path(stack.resolved_compose_file), *extra_compose_files],
            project_name=stack.project_name,
            subcommand=subcommand,
        ),
        owner=owner,
    )
    ps_rows = _parse_compose_ps_json(owner=owner, payload=ps_output)
    container_ids = [
        container_id
        for row in ps_rows
        if isinstance((container_id := _lookup(row, "ID", "Id")), str) and container_id
    ]
    if not container_ids:
        return []
    inspect_output = _run_cli(
        ["docker", "inspect", "--type", "container", *container_ids],
        owner=owner,
    )
    inspect_rows = _parse_json_list(owner=owner, payload=inspect_output, label="inspect")
    inspect_by_id: dict[str, dict[str, Any]] = {}
    for row in inspect_rows:
        container_id = row.get("Id")
        if isinstance(container_id, str) and container_id:
            inspect_by_id[container_id] = row

    grouped: dict[str, list[_LiveContainer]] = defaultdict(list)
    for row in ps_rows:
        container_id = _require_string(
            owner=owner,
            label="docker compose ps container id",
            value=_lookup(row, "ID", "Id"),
        )
        inspect_row = inspect_by_id.get(container_id)
        if inspect_row is None:
            raise RuntimeError(
                f"{owner}(...) could not inspect Docker container '{container_id}'."
            )
        service = _service_name_from_rows(ps_row=row, inspect_row=inspect_row, owner=owner)
        container_name = _container_name_from_rows(
            ps_row=row,
            inspect_row=inspect_row,
            owner=owner,
        )
        container_index = _container_index_from_inspect(inspect_row)
        state_block = inspect_row.get("State")
        if not isinstance(state_block, Mapping):
            raise RuntimeError(
                f"{owner}(...) received invalid container state for '{container_name}'."
            )
        health_block = state_block.get("Health")
        health: str | None = None
        if isinstance(health_block, Mapping):
            health_value = health_block.get("Status")
            if isinstance(health_value, str) and health_value:
                health = health_value
        exit_code = state_block.get("ExitCode")
        if not isinstance(exit_code, int):
            exit_code = None
        image: str | None = None
        config_block = inspect_row.get("Config")
        if isinstance(config_block, Mapping):
            image_value = config_block.get("Image")
            if isinstance(image_value, str) and image_value:
                image = image_value
        status = DockerContainerStatus(
            service=service,
            container_id=container_id,
            container_name=container_name,
            state=_require_string(
                owner=owner,
                label=f"container state for '{container_name}'",
                value=state_block.get("Status"),
            ),
            health=health,
            exit_code=exit_code,
            image=image,
            started_at=_optional_string(state_block.get("StartedAt")),
            finished_at=_optional_string(state_block.get("FinishedAt")),
        )
        mounts = inspect_row.get("Mounts")
        if not isinstance(mounts, list):
            raise RuntimeError(
                f"{owner}(...) received invalid mount data for '{container_name}'."
            )
        grouped[service].append(
            _LiveContainer(
                status=status,
                container_index=container_index,
                mounts=tuple(
                    dict(item)
                    for item in mounts
                    if isinstance(item, Mapping)
                ),
            )
        )

    live_containers: list[_LiveContainer] = []
    for service, containers in grouped.items():
        sorted_containers = sorted(
            containers,
            key=lambda item: (item.container_index, item.status.container_name),
        )
        if any(
            container.container_index <= 0
            for container in sorted_containers
        ):
            sorted_containers = [
                _LiveContainer(
                    status=container.status,
                    container_index=index,
                    mounts=container.mounts,
                )
                for index, container in enumerate(sorted_containers, start=1)
            ]
        live_containers.extend(sorted_containers)
    return sorted(
        live_containers,
        key=lambda item: (item.status.service, item.container_index, item.status.container_name),
    )


def _require_declared_services_present(
    *,
    compose_config: Mapping[str, Any],
    live_containers: Sequence[_LiveContainer],
    owner: str,
) -> None:
    service_block = compose_config.get("services")
    if not isinstance(service_block, Mapping):
        raise RuntimeError(f"{owner}(...) received an invalid Compose services block.")
    declared_services = {
        name
        for name in service_block
        if isinstance(name, str) and name
    }
    live_services = {container.status.service for container in live_containers}
    missing = sorted(declared_services - live_services)
    if missing:
        raise RuntimeError(
            f"{owner}(...) could not find live containers for Compose services: {', '.join(missing)}."
        )


def _require_single_container_per_service(
    *,
    live_containers: Sequence[_LiveContainer],
    owner: str,
) -> None:
    counts: dict[str, int] = defaultdict(int)
    for live_container in live_containers:
        counts[live_container.status.service] += 1
    repeated = sorted(service for service, count in counts.items() if count > 1)
    if repeated:
        joined = ", ".join(repeated)
        raise RuntimeError(
            f"{owner}(...) currently supports one container per service to guarantee exact rollback. "
            f"Multi-container services were found for: {joined}."
        )


def _require_supported_container_state(
    *,
    live_container: _LiveContainer,
    owner: str,
) -> None:
    state = live_container.status.state
    if state in _UNSUPPORTED_CONTAINER_STATES:
        raise RuntimeError(
            f"{owner}(...) cannot snapshot service '{live_container.status.service}' while "
            f"container '{live_container.status.container_name}' is {state!r}."
        )
    if state not in _SUPPORTED_CONTAINER_STATES:
        raise RuntimeError(
            f"{owner}(...) does not support restoring container state {state!r} for "
            f"'{live_container.status.container_name}'."
        )


def _external_volume_names(compose_config: Mapping[str, Any]) -> set[str]:
    volume_block = compose_config.get("volumes")
    if not isinstance(volume_block, Mapping):
        return set()
    external_names: set[str] = set()
    for name, details in volume_block.items():
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(details, Mapping):
            continue
        external_field = details.get("external")
        if external_field is True or isinstance(external_field, Mapping):
            external_name = details.get("name")
            if isinstance(external_name, str) and external_name:
                external_names.add(external_name)
            else:
                external_names.add(name)
    return external_names


def _build_volume_entry(
    *,
    stack: DockerComposeStack,
    live_container: _LiveContainer,
    mount: Mapping[str, Any],
    external_volume_names: set[str],
) -> dict[str, Any] | None:
    mount_type = _require_string(
        owner="store_docker",
        label=f"mount type for '{live_container.status.container_name}'",
        value=mount.get("Type"),
    )
    if mount_type != "volume":
        raise RuntimeError(
            "store_docker(...) only supports Docker-managed volumes. "
            f"Container '{live_container.status.container_name}' mounted unsupported type {mount_type!r}."
        )
    volume_name = _require_string(
        owner="store_docker",
        label=f"volume name for '{live_container.status.container_name}'",
        value=mount.get("Name"),
    )
    if volume_name in external_volume_names:
        raise RuntimeError(
            "store_docker(...) cannot guarantee exact restore for external volume "
            f"'{volume_name}'."
        )
    destination = _require_string(
        owner="store_docker",
        label=f"volume destination for '{live_container.status.container_name}'",
        value=mount.get("Destination"),
    )
    if mount.get("RW") is False:
        raise RuntimeError(
            "store_docker(...) cannot guarantee exact restore for read-only volume mount "
            f"'{volume_name}' on container '{live_container.status.container_name}'."
        )
    volume_dir_name = (
        f"volumes/{_slugify(volume_name)}-{abs(hash((stack.project_name, volume_name, destination))) % 10_000_000}"
    )
    return {
        "volume_name": volume_name,
        "service": live_container.status.service,
        "container_index": live_container.container_index,
        "container_name": live_container.status.container_name,
        "target_path": destination,
        "backup_relpath": volume_dir_name,
        "mode": _optional_string(mount.get("Mode")),
    }


def _copy_container_path_to_local(
    *,
    owner: str,
    container_id: str,
    container_path: str,
    local_path: Path,
) -> None:
    local_path.mkdir(parents=True, exist_ok=True)
    _run_cli(
        [
            "docker",
            "cp",
            "-a",
            f"{container_id}:{_cp_source_path(container_path)}",
            str(local_path),
        ],
        owner=owner,
    )


def _copy_local_path_to_container(
    *,
    owner: str,
    local_path: Path,
    container_id: str,
    container_path: str,
) -> None:
    _run_cli(
        [
            "docker",
            "cp",
            "-a",
            _local_cp_source_path(local_path),
            f"{container_id}:{container_path}",
        ],
        owner=owner,
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"restore_docker(...) could not read the snapshot manifest '{path}'."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"restore_docker(...) expected a JSON object in '{path}'."
        )
    return payload


def _validate_manifest_matches_stack(
    *,
    manifest: Mapping[str, Any],
    stack: DockerComposeStack,
    snapshot_name: str,
) -> None:
    if manifest.get("project_name") != stack.project_name:
        raise RuntimeError(
            "restore_docker(...) received a snapshot for a different Compose project."
        )
    if manifest.get("resolved_compose_file") != stack.resolved_compose_file:
        raise RuntimeError(
            "restore_docker(...) received a snapshot for a different resolved Compose file."
        )
    if manifest.get("snapshot_name") != snapshot_name:
        raise RuntimeError(
            "restore_docker(...) received a snapshot manifest for a different snapshot name."
        )


def _manifest_container_entries(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("containers")
    if not isinstance(entries, list) or not all(isinstance(item, Mapping) for item in entries):
        raise RuntimeError(
            "restore_docker(...) received invalid container metadata in the snapshot manifest."
        )
    return [dict(item) for item in entries]


def _manifest_volume_entries(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("volumes")
    if not isinstance(entries, list) or not all(isinstance(item, Mapping) for item in entries):
        raise RuntimeError(
            "restore_docker(...) received invalid volume metadata in the snapshot manifest."
        )
    return [dict(item) for item in entries]


def _require_single_container_manifest(
    *,
    container_entries: Sequence[Mapping[str, Any]],
    owner: str,
) -> None:
    counts: dict[str, int] = defaultdict(int)
    for entry in container_entries:
        service = entry.get("service")
        if isinstance(service, str) and service:
            counts[service] += 1
    repeated = sorted(service for service, count in counts.items() if count > 1)
    if repeated:
        joined = ", ".join(repeated)
        raise RuntimeError(
            f"{owner}(...) cannot restore an exact snapshot for multi-container services: {joined}."
        )


def _write_restore_override(
    *,
    stack: DockerComposeStack,
    snapshot_name: str,
    container_entries: Sequence[Mapping[str, Any]],
) -> Path:
    project_cache_dir = _project_cache_dir(
        cache_root=Path(stack.cache_root),
        project_name=stack.project_name,
    )
    project_cache_dir.mkdir(parents=True, exist_ok=True)
    override_path = project_cache_dir / f"restore-{_slugify(snapshot_name)}.override.yml"
    lines = ["services:"]
    for entry in sorted(
        container_entries,
        key=lambda item: _require_string(
            owner="restore_docker",
            label="service name",
            value=item.get("service"),
        ),
    ):
        service = _require_string(
            owner="restore_docker",
            label="service name",
            value=entry.get("service"),
        )
        image = _require_string(
            owner="restore_docker",
            label=f"snapshot image for '{service}'",
            value=entry.get("snapshot_image"),
        )
        lines.append(f"  {service}:")
        lines.append(f"    image: {_yaml_quote(image)}")
        lines.append("    pull_policy: never")
    override_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return override_path


def _remove_volume_if_exists(*, owner: str, volume_name: str) -> None:
    output = _run_cli(
        ["docker", "volume", "ls", "--format", "json"],
        owner=owner,
    )
    volume_rows = _parse_json_lines(owner=owner, payload=output, label="volume ls")
    if not any(
        isinstance(row, Mapping) and row.get("Name") == volume_name
        for row in volume_rows
    ):
        return
    _run_cli(
        ["docker", "volume", "rm", volume_name],
        owner=owner,
    )


def _parse_compose_ps_json(*, owner: str, payload: str) -> list[dict[str, Any]]:
    if not payload.strip():
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{owner}(...) could not parse `docker compose ps --format json` output."
        ) from exc
    if not isinstance(parsed, list):
        raise RuntimeError(f"{owner}(...) received invalid `docker compose ps` JSON.")
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _parse_json_list(*, owner: str, payload: str, label: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{owner}(...) could not parse Docker {label} JSON.") from exc
    if not isinstance(parsed, list):
        raise RuntimeError(f"{owner}(...) received invalid Docker {label} JSON.")
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _parse_json_lines(*, owner: str, payload: str, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in payload.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{owner}(...) could not parse Docker {label} line JSON."
            ) from exc
        if isinstance(parsed, Mapping):
            rows.append(dict(parsed))
    return rows


def _service_name_from_rows(
    *,
    ps_row: Mapping[str, Any],
    inspect_row: Mapping[str, Any],
    owner: str,
) -> str:
    service = _lookup(ps_row, "Service", "service")
    if isinstance(service, str) and service:
        return service
    config_labels = inspect_row.get("Config")
    if isinstance(config_labels, Mapping):
        labels = config_labels.get("Labels")
        if isinstance(labels, Mapping):
            label_value = labels.get("com.docker.compose.service")
            if isinstance(label_value, str) and label_value:
                return label_value
    raise RuntimeError(f"{owner}(...) could not determine a Compose service name.")


def _container_name_from_rows(
    *,
    ps_row: Mapping[str, Any],
    inspect_row: Mapping[str, Any],
    owner: str,
) -> str:
    name = _lookup(ps_row, "Name", "name")
    if isinstance(name, str) and name:
        return name
    inspect_name = inspect_row.get("Name")
    if isinstance(inspect_name, str) and inspect_name:
        return inspect_name.lstrip("/")
    raise RuntimeError(f"{owner}(...) could not determine a container name.")


def _container_index_from_inspect(inspect_row: Mapping[str, Any]) -> int:
    config_block = inspect_row.get("Config")
    if isinstance(config_block, Mapping):
        labels = config_block.get("Labels")
        if isinstance(labels, Mapping):
            label_value = labels.get("com.docker.compose.container-number")
            if isinstance(label_value, str):
                try:
                    parsed = int(label_value)
                except ValueError:
                    return 0
                if parsed > 0:
                    return parsed
    return 0


def _compose_command(
    *,
    compose_file: Path,
    project_name: str,
    subcommand: Sequence[str],
) -> list[str]:
    return _compose_multi_file_command(
        compose_files=[compose_file],
        project_name=project_name,
        subcommand=subcommand,
    )


def _compose_multi_file_command(
    *,
    compose_files: Sequence[Path],
    project_name: str,
    subcommand: Sequence[str],
) -> list[str]:
    args = ["docker", "compose"]
    for compose_file in compose_files:
        args.extend(["-f", str(compose_file)])
    args.extend(["-p", project_name, *subcommand])
    return args


def _run_cli(args: Sequence[str], *, owner: str) -> str:
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        executable = args[0] if args else "command"
        raise FileNotFoundError(
            f"{owner}(...) requires the '{executable}' CLI, but it was not available."
        ) from exc

    if completed.returncode != 0:
        error_output = completed.stderr.strip() or completed.stdout.strip()
        if not error_output:
            error_output = f"exit code {completed.returncode}"
        raise RuntimeError(
            f"{owner}(...) failed while running `{_display_command(args)}`: {error_output}"
        )
    return completed.stdout


def _lookup(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _require_string(*, owner: str, label: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{owner}(...) expected {label} to be a non-empty string.")
    return value


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _display_command(args: Sequence[str]) -> str:
    return " ".join(str(item) for item in args)


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _snapshot_image_ref(
    *,
    project_name: str,
    snapshot_name: str,
    service: str,
    container_index: int,
) -> str:
    tag = "-".join(
        [
            _slugify(project_name),
            _slugify(snapshot_name),
            _slugify(service),
            str(container_index),
        ]
    )
    return f"journey-sdk-snapshot:{tag}"


def _cp_source_path(container_path: str) -> str:
    if container_path.endswith("/"):
        return f"{container_path}."
    return f"{container_path}/."


def _local_cp_source_path(local_path: Path) -> str:
    path_text = str(local_path)
    if path_text.endswith("/"):
        return f"{path_text}."
    return f"{path_text}/."


def _set_step_metadata(
    fn: Callable[..., Any],
    *,
    label: str,
    owner: str,
    attrs: dict[str, Any],
) -> None:
    fn.__name__ = label
    fn.__qualname__ = f"{owner}.<locals>.{label}"
    for key, value in attrs.items():
        setattr(fn, key, value)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


__all__ = [
    "DockerComposeStack",
    "DockerContainerStatus",
    "restore_docker",
    "run_docker",
    "store_docker",
]
