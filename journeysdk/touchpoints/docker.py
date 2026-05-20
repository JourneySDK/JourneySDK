"""Official Docker Compose snapshot touchpoint."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from journeysdk.logger import PrettyLine, get_logger, pretty_row
from journeysdk.rehydration import JourneyRestoreContext, JourneyStoreContext

_CACHE_ROOT = Path(tempfile.gettempdir()) / "journey-sdk-docker"
_CURRENT_SNAPSHOT_FORMAT = 3
_VOLUME_COPY_IMAGE = "debian:bookworm-slim"
_SUPPORTED_CONTAINER_STATES = {"running", "restarting", "created", "exited"}
_STARTED_CONTAINER_STATES = {"running", "restarting"}
_UNSUPPORTED_CONTAINER_STATES = {"paused", "removing", "dead"}
_LOGGER = get_logger("docker")


def _docker_row(detail: object) -> PrettyLine:
    return pretty_row("Docker", detail, indent=8, label_width=27, style="touchpoint")


def _duration_fields(started_at: float) -> dict[str, object]:
    elapsed = time.monotonic() - started_at
    return {
        "duration": f"{elapsed:.3f}s",
        "duration_ms": round(elapsed * 1000, 3),
    }


def _phase_pretty_text(
    message: str,
    *,
    duration: object | None = None,
    detail: object | None = None,
) -> str:
    text = message
    if duration is not None:
        text = f"{text} ({duration})"
    if detail is not None:
        text = f"{text}: {detail}"
    return text


def _pretty_kv(pairs: Sequence[tuple[str, object | None]]) -> str:
    return " ".join(
        f"{key}={_pretty_kv_value(value)}"
        for key, value in pairs
        if value is not None
    )


def _pretty_kv_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(_pretty_kv_value(item) for item in value)
    return str(value)


def _log_snapshot_phase_start(
    *,
    prefix: str,
    phase: str,
    message: str,
    project_name: str,
    snapshot_name: str,
    pretty_detail: object | None = None,
    **fields: object,
) -> float:
    _LOGGER.info(
        f"{prefix}_{phase}_start",
        message,
        pretty=_docker_row(_phase_pretty_text(message, detail=pretty_detail)),
        project=project_name,
        snapshot=snapshot_name,
        phase=phase,
        **fields,
    )
    return time.monotonic()


def _log_snapshot_phase_success(
    *,
    prefix: str,
    phase: str,
    started_at: float,
    message: str,
    project_name: str,
    snapshot_name: str,
    pretty_detail: object | None = None,
    **fields: object,
) -> None:
    duration = _duration_fields(started_at)
    _LOGGER.info(
        f"{prefix}_{phase}_success",
        message,
        pretty=_docker_row(
            _phase_pretty_text(
                message,
                duration=duration["duration"],
                detail=pretty_detail,
            )
        ),
        project=project_name,
        snapshot=snapshot_name,
        phase=phase,
        **duration,
        **fields,
    )


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

    def __store__(self, context: JourneyStoreContext) -> object:
        snapshot_name = _snapshot_name_for_context(context)
        _store_docker_snapshot(
            stack=self,
            snapshot_name=snapshot_name,
            snapshot_root=context.artifact_root,
        )
        return {
            "compose_file": self.compose_file,
            "resolved_compose_file": self.resolved_compose_file,
            "project_name": self.project_name,
            "cache_root": self.cache_root,
            "snapshot_name": snapshot_name,
        }

    @classmethod
    def __restore__(
        cls,
        payload: object,
        context: JourneyRestoreContext,
    ) -> "DockerComposeStack":
        data = _require_stack_payload(payload, owner="DockerComposeStack.__restore__")
        stack = cls(
            compose_file=data["compose_file"],
            resolved_compose_file=data["resolved_compose_file"],
            project_name=data["project_name"],
            cache_root=data["cache_root"],
        )
        _restore_docker_snapshot(
            stack=stack,
            snapshot_name=data["snapshot_name"],
            snapshot_root=context.artifact_root,
        )
        return stack


class RunDockerStep(Protocol):
    def __call__(self) -> DockerComposeStack:
        ...


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
) -> RunDockerStep:
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
        _LOGGER.info(
            "compose_start",
            "starting Docker Compose stack",
            pretty=_docker_row("starting Docker Compose stack"),
            compose_file=original_compose_path,
            project=resolved_project_name,
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
        stack = DockerComposeStack(
            compose_file=str(original_compose_path),
            resolved_compose_file=str(resolved_compose_path),
            project_name=resolved_project_name,
            cache_root=str(_CACHE_ROOT),
        )
        try:
            _run_cli(
                _compose_command(
                    compose_file=resolved_compose_path,
                    project_name=resolved_project_name,
                    subcommand=up_command,
                ),
                owner="run_docker",
            )
        except RuntimeError:
            if not _compose_wait_failure_is_successful_one_shot(stack):
                raise

        _LOGGER.info(
            "compose_success",
            "Docker Compose stack started",
            pretty=_docker_row("Docker Compose stack started"),
            compose_file=stack.compose_file,
            project=stack.project_name,
        )
        return stack

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
    """Store one Docker Compose snapshot for Journey replay."""

    _store_docker_snapshot(
        stack=stack,
        snapshot_name=snapshot_name,
        snapshot_root=None,
    )


def _store_docker_snapshot(
    *,
    stack: DockerComposeStack,
    snapshot_name: str,
    snapshot_root: Path | None,
) -> None:
    store_started_at = time.monotonic()
    validated_stack = _require_stack(stack=stack, owner="store_docker")
    normalized_snapshot_name = _normalize_snapshot_name(
        owner="store_docker",
        value=snapshot_name,
    )
    snapshot_dir = _snapshot_dir(
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
        snapshot_root=snapshot_root,
    )
    snapshot_detail = _pretty_kv(
        [
            ("project", validated_stack.project_name),
            ("snapshot", normalized_snapshot_name),
            ("dir", snapshot_dir),
        ]
    )
    _LOGGER.info(
        "snapshot_store_start",
        "storing Docker Compose snapshot",
        pretty=_docker_row(
            _phase_pretty_text(
                "storing Docker Compose snapshot",
                detail=snapshot_detail,
            )
        ),
        project=validated_stack.project_name,
        snapshot=normalized_snapshot_name,
        snapshot_dir=snapshot_dir,
    )
    prepare_started_at = _log_snapshot_phase_start(
        prefix="snapshot_store",
        phase="prepare_snapshot_dir",
        message="preparing Docker snapshot directory",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv(
            [
                ("dir", snapshot_dir),
                ("replacing", snapshot_dir.exists()),
            ]
        ),
        snapshot_dir=snapshot_dir,
        replacing=snapshot_dir.exists(),
    )
    if snapshot_dir.exists():
        _remove_snapshot_volumes_from_manifest(
            owner="store_docker",
            manifest_path=snapshot_dir / "manifest.json",
        )
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    _log_snapshot_phase_success(
        prefix="snapshot_store",
        phase="prepare_snapshot_dir",
        started_at=prepare_started_at,
        message="prepared Docker snapshot directory",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv([("dir", snapshot_dir)]),
        snapshot_dir=snapshot_dir,
    )

    config_started_at = _log_snapshot_phase_start(
        prefix="snapshot_store",
        phase="compose_config",
        message="loading Docker Compose config",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
    )
    compose_config = _load_compose_config(validated_stack, owner="store_docker")
    _log_snapshot_phase_success(
        prefix="snapshot_store",
        phase="compose_config",
        started_at=config_started_at,
        message="loaded Docker Compose config",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv([("services", _compose_service_count(compose_config))]),
        services=_compose_service_count(compose_config),
    )
    containers_started_at = _log_snapshot_phase_start(
        prefix="snapshot_store",
        phase="live_containers",
        message="loading Docker container metadata",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
    )
    live_containers = _load_live_containers(
        validated_stack,
        owner="store_docker",
        include_all=True,
    )
    _log_snapshot_phase_success(
        prefix="snapshot_store",
        phase="live_containers",
        started_at=containers_started_at,
        message="loaded Docker container metadata",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv([("containers", len(live_containers))]),
        containers=len(live_containers),
    )
    validate_started_at = _log_snapshot_phase_start(
        prefix="snapshot_store",
        phase="validate_snapshot",
        message="validating Docker snapshot inputs",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv([("containers", len(live_containers))]),
        containers=len(live_containers),
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
    _log_snapshot_phase_success(
        prefix="snapshot_store",
        phase="validate_snapshot",
        started_at=validate_started_at,
        message="validated Docker snapshot inputs",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv(
            [
                ("containers", len(live_containers)),
                ("external_volumes", len(external_volume_names)),
            ]
        ),
        containers=len(live_containers),
        external_volumes=len(external_volume_names),
    )

    container_entries: list[dict[str, Any]] = []
    volume_entries_by_name: dict[str, dict[str, Any]] = {}
    for live_container in live_containers:
        _require_supported_container_state(
            live_container=live_container,
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
            }
        )
        for mount in live_container.mounts:
            volume_entry = _build_volume_entry(
                live_container=live_container,
                mount=mount,
                external_volume_names=external_volume_names,
            )
            if volume_entry is None:
                continue
            existing = volume_entries_by_name.get(volume_entry["volume_name"])
            if existing is not None:
                continue
            volume_entry["snapshot_volume_name"] = _snapshot_volume_name(
                project_name=validated_stack.project_name,
                snapshot_name=normalized_snapshot_name,
                source_volume_name=volume_entry["volume_name"],
                target_path=volume_entry["target_path"],
                snapshot_dir=snapshot_dir,
            )
            volume_entries_by_name[volume_entry["volume_name"]] = volume_entry

    volume_entries = list(volume_entries_by_name.values())
    stopped_container_ids: list[str] = []
    try:
        if volume_entries:
            source_volume_names = [entry["volume_name"] for entry in volume_entries]
            stopped_container_ids = _containers_using_volumes(
                owner="store_docker",
                volume_names=source_volume_names,
            )
            _stop_containers(
                owner="store_docker",
                container_ids=stopped_container_ids,
            )
        for volume_entry in volume_entries:
            backup_detail = _pretty_kv(
                [
                    ("volume", volume_entry["volume_name"]),
                    ("service", volume_entry["service"]),
                    ("container", volume_entry["container_name"]),
                    ("from", volume_entry["volume_name"]),
                    ("to", volume_entry["snapshot_volume_name"]),
                ]
            )
            backup_started_at = _log_snapshot_phase_start(
                prefix="snapshot_store",
                phase="volume_backup",
                message="copying Docker volume to snapshot",
                project_name=validated_stack.project_name,
                snapshot_name=normalized_snapshot_name,
                pretty_detail=backup_detail,
                service=volume_entry["service"],
                container=volume_entry["container_name"],
                volume=volume_entry["volume_name"],
                snapshot_volume=volume_entry["snapshot_volume_name"],
                target_path=volume_entry["target_path"],
            )
            _replace_volume_clone(
                owner="store_docker",
                source_volume_name=volume_entry["volume_name"],
                destination_volume_name=volume_entry["snapshot_volume_name"],
                labels={
                    "journeysdk.snapshot": "true",
                    "journeysdk.project": validated_stack.project_name,
                    "journeysdk.snapshot_name": normalized_snapshot_name,
                    "journeysdk.source_volume": volume_entry["volume_name"],
                },
            )
            _log_snapshot_phase_success(
                prefix="snapshot_store",
                phase="volume_backup",
                started_at=backup_started_at,
                message="copied Docker volume to snapshot",
                project_name=validated_stack.project_name,
                snapshot_name=normalized_snapshot_name,
                pretty_detail=backup_detail,
                service=volume_entry["service"],
                container=volume_entry["container_name"],
                volume=volume_entry["volume_name"],
                snapshot_volume=volume_entry["snapshot_volume_name"],
                target_path=volume_entry["target_path"],
            )
    finally:
        _start_containers(
            owner="store_docker",
            container_ids=stopped_container_ids,
        )

    manifest = {
        "format": _CURRENT_SNAPSHOT_FORMAT,
        "project_name": validated_stack.project_name,
        "compose_file": validated_stack.compose_file,
        "resolved_compose_file": validated_stack.resolved_compose_file,
        "snapshot_name": normalized_snapshot_name,
        "containers": container_entries,
        "volumes": volume_entries,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_detail = _pretty_kv(
        [
            ("path", manifest_path),
            ("containers", len(container_entries)),
            ("volumes", len(volume_entries_by_name)),
        ]
    )
    manifest_started_at = _log_snapshot_phase_start(
        prefix="snapshot_store",
        phase="manifest_write",
        message="writing Docker snapshot manifest",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=manifest_detail,
        manifest_path=manifest_path,
        containers=len(container_entries),
        volumes=len(volume_entries_by_name),
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _log_snapshot_phase_success(
        prefix="snapshot_store",
        phase="manifest_write",
        started_at=manifest_started_at,
        message="wrote Docker snapshot manifest",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=manifest_detail,
        manifest_path=manifest_path,
        containers=len(container_entries),
        volumes=len(volume_entries_by_name),
    )
    duration = _duration_fields(store_started_at)
    _LOGGER.info(
        "snapshot_store_success",
        "stored Docker Compose snapshot",
        pretty=_docker_row(
            _phase_pretty_text(
                "stored Docker Compose snapshot",
                duration=duration["duration"],
                detail=_pretty_kv(
                    [
                        ("project", validated_stack.project_name),
                        ("snapshot", normalized_snapshot_name),
                        ("containers", len(container_entries)),
                        ("volumes", len(volume_entries_by_name)),
                    ]
                ),
            )
        ),
        project=validated_stack.project_name,
        snapshot=normalized_snapshot_name,
        containers=len(container_entries),
        volumes=len(volume_entries_by_name),
        **duration,
    )


def restore_docker(
    stack: DockerComposeStack,
    *,
    snapshot_name: str = "default",
) -> None:
    """Restore one stored Docker Compose snapshot for Journey replay."""

    _restore_docker_snapshot(
        stack=stack,
        snapshot_name=snapshot_name,
        snapshot_root=None,
    )


def _restore_docker_snapshot(
    *,
    stack: DockerComposeStack,
    snapshot_name: str,
    snapshot_root: Path | None,
) -> None:
    restore_started_at = time.monotonic()
    validated_stack = _require_stack(stack=stack, owner="restore_docker")
    normalized_snapshot_name = _normalize_snapshot_name(
        owner="restore_docker",
        value=snapshot_name,
    )
    snapshot_dir = _snapshot_dir(
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
        snapshot_root=snapshot_root,
    )
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "restore_docker(...) could not find a stored snapshot manifest at "
            f"'{manifest_path}'."
        )
    _LOGGER.info(
        "snapshot_restore_start",
        "restoring Docker Compose snapshot",
        pretty=_docker_row(
            _phase_pretty_text(
                "restoring Docker Compose snapshot",
                detail=_pretty_kv(
                    [
                        ("project", validated_stack.project_name),
                        ("snapshot", normalized_snapshot_name),
                        ("dir", snapshot_dir),
                    ]
                ),
            )
        ),
        project=validated_stack.project_name,
        snapshot=normalized_snapshot_name,
        snapshot_dir=snapshot_dir,
    )

    manifest_detail = _pretty_kv([("path", manifest_path)])
    manifest_started_at = _log_snapshot_phase_start(
        prefix="snapshot_restore",
        phase="manifest_load",
        message="loading Docker snapshot manifest",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=manifest_detail,
        manifest_path=manifest_path,
    )
    manifest = _load_manifest(manifest_path)
    _validate_manifest_matches_stack(
        manifest=manifest,
        stack=validated_stack,
        snapshot_name=normalized_snapshot_name,
    )
    _manifest_format(manifest)
    container_entries = _manifest_container_entries(manifest)
    volume_entries = _manifest_volume_entries(manifest)

    _require_single_container_manifest(
        container_entries=container_entries,
        owner="restore_docker",
    )
    manifest_loaded_detail = _pretty_kv(
        [
            ("path", manifest_path),
            ("containers", len(container_entries)),
            ("volumes", len(volume_entries)),
        ]
    )
    _log_snapshot_phase_success(
        prefix="snapshot_restore",
        phase="manifest_load",
        started_at=manifest_started_at,
        message="loaded Docker snapshot manifest",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=manifest_loaded_detail,
        manifest_path=manifest_path,
        containers=len(container_entries),
        volumes=len(volume_entries),
    )

    compose_files = [Path(validated_stack.resolved_compose_file)]
    for volume_entry in volume_entries:
        _require_volume_exists(
            owner="restore_docker",
            volume_name=_manifest_volume_snapshot_name(volume_entry),
        )

    compose_down_detail = _pretty_kv([("project", validated_stack.project_name)])
    down_started_at = _log_snapshot_phase_start(
        prefix="snapshot_restore",
        phase="compose_down",
        message="stopping Docker Compose stack",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=compose_down_detail,
    )
    _run_cli(
        _compose_command(
            compose_file=Path(validated_stack.resolved_compose_file),
            project_name=validated_stack.project_name,
            subcommand=["down", "--remove-orphans"],
        ),
        owner="restore_docker",
    )
    _log_snapshot_phase_success(
        prefix="snapshot_restore",
        phase="compose_down",
        started_at=down_started_at,
        message="stopped Docker Compose stack",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=compose_down_detail,
    )

    for volume_entry in volume_entries:
        remove_detail = _pretty_kv(
            [
                ("volume", volume_entry["volume_name"]),
                ("service", volume_entry["service"]),
                ("container", volume_entry["container_name"]),
            ]
        )
        remove_started_at = _log_snapshot_phase_start(
            prefix="snapshot_restore",
            phase="volume_remove",
            message="removing Docker volume before restore",
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            pretty_detail=remove_detail,
            service=volume_entry["service"],
            container=volume_entry["container_name"],
            volume=volume_entry["volume_name"],
        )
        removed = _remove_volume_if_exists(
            owner="restore_docker",
            volume_name=volume_entry["volume_name"],
        )
        _log_snapshot_phase_success(
            prefix="snapshot_restore",
            phase="volume_remove",
            started_at=remove_started_at,
            message="checked Docker volume before restore",
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            pretty_detail=_pretty_kv(
                [
                    ("volume", volume_entry["volume_name"]),
                    ("service", volume_entry["service"]),
                    ("container", volume_entry["container_name"]),
                    ("removed", removed),
                ]
            ),
            service=volume_entry["service"],
            container=volume_entry["container_name"],
            volume=volume_entry["volume_name"],
            removed=removed,
        )

    create_detail = _pretty_kv([("containers", len(container_entries))])
    create_started_at = _log_snapshot_phase_start(
        prefix="snapshot_restore",
        phase="compose_create",
        message="creating Docker Compose containers",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=create_detail,
        containers=len(container_entries),
    )
    _run_cli(
        _compose_multi_file_command(
            compose_files=compose_files,
            project_name=validated_stack.project_name,
            subcommand=["create", "--force-recreate"],
        ),
        owner="restore_docker",
    )
    _log_snapshot_phase_success(
        prefix="snapshot_restore",
        phase="compose_create",
        started_at=create_started_at,
        message="created Docker Compose containers",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=create_detail,
        containers=len(container_entries),
    )

    recreated_started_at = _log_snapshot_phase_start(
        prefix="snapshot_restore",
        phase="recreated_containers",
        message="loading recreated Docker container metadata",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv([("compose_files", len(compose_files))]),
    )
    recreated_containers = _load_live_containers(
        validated_stack,
        owner="restore_docker",
        include_all=True,
        extra_compose_files=tuple(compose_files[1:]),
    )
    _log_snapshot_phase_success(
        prefix="snapshot_restore",
        phase="recreated_containers",
        started_at=recreated_started_at,
        message="loaded recreated Docker container metadata",
        project_name=validated_stack.project_name,
        snapshot_name=normalized_snapshot_name,
        pretty_detail=_pretty_kv([("containers", len(recreated_containers))]),
        containers=len(recreated_containers),
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
        snapshot_volume_name = _manifest_volume_snapshot_name(volume_entry)
        destination_volume_name = _container_volume_name_for_target(
            live_container=live_container,
            target_path=volume_entry["target_path"],
            owner="restore_docker",
        )
        restore_detail = _pretty_kv(
            [
                ("volume", volume_entry["volume_name"]),
                ("service", volume_entry["service"]),
                ("container", live_container.status.container_name),
                ("id", live_container.status.container_id),
                ("from", snapshot_volume_name),
                ("to", destination_volume_name),
                ("target", volume_entry["target_path"]),
            ]
        )
        restore_volume_started_at = _log_snapshot_phase_start(
            prefix="snapshot_restore",
            phase="volume_restore",
            message="restoring Docker volume contents",
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            pretty_detail=restore_detail,
            service=volume_entry["service"],
            container=live_container.status.container_name,
            container_id=live_container.status.container_id,
            volume=volume_entry["volume_name"],
            snapshot_volume=snapshot_volume_name,
            destination_volume=destination_volume_name,
            target_path=volume_entry["target_path"],
        )
        _copy_volume_contents(
            owner="restore_docker",
            source_volume_name=snapshot_volume_name,
            destination_volume_name=destination_volume_name,
        )
        _log_snapshot_phase_success(
            prefix="snapshot_restore",
            phase="volume_restore",
            started_at=restore_volume_started_at,
            message="restored Docker volume contents",
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            pretty_detail=restore_detail,
            service=volume_entry["service"],
            container=live_container.status.container_name,
            container_id=live_container.status.container_id,
            volume=volume_entry["volume_name"],
            snapshot_volume=snapshot_volume_name,
            destination_volume=destination_volume_name,
            target_path=volume_entry["target_path"],
        )

    running_services = sorted(
        {
            container_entry["service"]
            for container_entry in container_entries
            if container_entry["state"] in _STARTED_CONTAINER_STATES
        }
    )
    if running_services:
        start_services_detail = _pretty_kv(
            [
                ("services", running_services),
                ("count", len(running_services)),
            ]
        )
        start_services_started_at = _log_snapshot_phase_start(
            prefix="snapshot_restore",
            phase="start_services",
            message="starting restored Docker Compose services",
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            pretty_detail=start_services_detail,
            services=running_services,
            services_count=len(running_services),
        )
        _run_cli(
            _compose_multi_file_command(
                compose_files=compose_files,
                project_name=validated_stack.project_name,
                subcommand=["start", *running_services],
            ),
            owner="restore_docker",
        )
        _log_snapshot_phase_success(
            prefix="snapshot_restore",
            phase="start_services",
            started_at=start_services_started_at,
            message="started restored Docker Compose services",
            project_name=validated_stack.project_name,
            snapshot_name=normalized_snapshot_name,
            pretty_detail=start_services_detail,
            services=running_services,
            services_count=len(running_services),
        )
    duration = _duration_fields(restore_started_at)
    _LOGGER.info(
        "snapshot_restore_success",
        "restored Docker Compose snapshot",
        pretty=_docker_row(
            _phase_pretty_text(
                "restored Docker Compose snapshot",
                duration=duration["duration"],
                detail=_pretty_kv(
                    [
                        ("project", validated_stack.project_name),
                        ("snapshot", normalized_snapshot_name),
                        ("containers", len(container_entries)),
                        ("volumes", len(volume_entries)),
                    ]
                ),
            )
        ),
        project=validated_stack.project_name,
        snapshot=normalized_snapshot_name,
        containers=len(container_entries),
        volumes=len(volume_entries),
        **duration,
    )


def _load_live_statuses(stack: DockerComposeStack) -> dict[str, tuple[DockerContainerStatus, ...]]:
    _LOGGER.debug(
        "statuses_load_start",
        "loading live Docker Compose statuses",
        project=stack.project_name,
    )
    live_containers = _load_live_containers(
        _require_stack(stack=stack, owner="DockerComposeStack.statuses"),
        owner="DockerComposeStack.statuses",
        include_all=True,
    )
    grouped: dict[str, list[DockerContainerStatus]] = defaultdict(list)
    for live_container in live_containers:
        grouped[live_container.status.service].append(live_container.status)
    statuses = {
        service: tuple(statuses)
        for service, statuses in sorted(grouped.items())
    }
    _LOGGER.debug(
        "statuses_load_success",
        "loaded live Docker Compose statuses",
        project=stack.project_name,
        services=len(statuses),
    )
    return statuses


def _load_live_logs(stack: DockerComposeStack) -> dict[str, str]:
    validated_stack = _require_stack(stack=stack, owner="DockerComposeStack.logs")
    _LOGGER.debug(
        "logs_load_start",
        "loading live Docker Compose logs",
        project=validated_stack.project_name,
    )
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
    _LOGGER.debug(
        "logs_load_success",
        "loaded live Docker Compose logs",
        project=validated_stack.project_name,
        services=len(logs),
    )
    return logs


def _compose_wait_failure_is_successful_one_shot(stack: DockerComposeStack) -> bool:
    try:
        compose_config = _load_compose_config(stack, owner="run_docker")
        live_containers = _load_live_containers(
            stack,
            owner="run_docker",
            include_all=True,
        )
        _require_declared_services_present(
            compose_config=compose_config,
            live_containers=live_containers,
            owner="run_docker",
        )
    except Exception:
        return False

    if not live_containers:
        return False
    return all(
        _container_started_successfully_after_wait_failure(live_container)
        for live_container in live_containers
    )


def _container_started_successfully_after_wait_failure(
    live_container: _LiveContainer,
) -> bool:
    status = live_container.status
    if status.state == "running":
        return status.health in {None, "healthy"}
    if status.state == "exited":
        return status.exit_code == 0
    return False


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


def _snapshot_dir(
    *,
    stack: DockerComposeStack,
    snapshot_name: str,
    snapshot_root: Path | None = None,
) -> Path:
    if snapshot_root is not None:
        return snapshot_root
    return _project_cache_dir(
        cache_root=Path(stack.cache_root),
        project_name=stack.project_name,
    ) / snapshot_name


def _snapshot_name_for_context(context: JourneyStoreContext) -> str:
    return _slugify(f"{context.boundary_kind}-{context.boundary_id}")


def _require_stack_payload(
    payload: object,
    *,
    owner: str,
) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{owner}(...) expects a mapping payload.")
    data = dict(payload)
    required = [
        "compose_file",
        "resolved_compose_file",
        "project_name",
        "cache_root",
        "snapshot_name",
    ]
    result: dict[str, str] = {}
    for key in required:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise TypeError(f"{owner}(...) received invalid payload field {key!r}.")
        result[key] = value
    return result


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


def _compose_service_count(compose_config: Mapping[str, Any]) -> int:
    services = compose_config.get("services")
    if not isinstance(services, Mapping):
        return 0
    return sum(1 for service in services if isinstance(service, str) and service)


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
        inspect_row = _inspect_row_for_container_id(
            owner=owner,
            inspect_by_id=inspect_by_id,
            container_id=container_id,
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


def _inspect_row_for_container_id(
    *,
    owner: str,
    inspect_by_id: Mapping[str, dict[str, Any]],
    container_id: str,
) -> dict[str, Any]:
    inspect_row = inspect_by_id.get(container_id)
    if inspect_row is not None:
        return inspect_row

    matches = [
        row
        for inspected_id, row in inspect_by_id.items()
        if inspected_id.startswith(container_id) or container_id.startswith(inspected_id)
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RuntimeError(
            f"{owner}(...) received ambiguous Docker inspect metadata for "
            f"container '{container_id}'."
        )
    raise RuntimeError(
        f"{owner}(...) could not inspect Docker container '{container_id}'."
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
    live_container: _LiveContainer,
    mount: Mapping[str, Any],
    external_volume_names: set[str],
) -> dict[str, Any] | None:
    mount_type = _require_string(
        owner="store_docker",
        label=f"mount type for '{live_container.status.container_name}'",
        value=mount.get("Type"),
    )
    if mount_type == "bind":
        return None
    if mount_type != "volume":
        raise RuntimeError(
            "store_docker(...) only supports Docker-managed volumes and bind mounts. "
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
    return {
        "volume_name": volume_name,
        "service": live_container.status.service,
        "container_index": live_container.container_index,
        "container_name": live_container.status.container_name,
        "target_path": destination,
        "mode": _optional_string(mount.get("Mode")),
    }


def _snapshot_volume_name(
    *,
    project_name: str,
    snapshot_name: str,
    source_volume_name: str,
    target_path: str,
    snapshot_dir: Path,
) -> str:
    digest = hashlib.sha256(
        "|".join(
            [
                str(snapshot_dir),
                project_name,
                snapshot_name,
                source_volume_name,
                target_path,
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "-".join(
        [
            "journey-snapshot",
            _slugify(project_name)[:32],
            _slugify(snapshot_name)[:32],
            _slugify(source_volume_name)[:48],
            digest,
        ]
    )


def _replace_volume_clone(
    *,
    owner: str,
    source_volume_name: str,
    destination_volume_name: str,
    labels: Mapping[str, str],
) -> None:
    _remove_volume_if_exists(owner=owner, volume_name=destination_volume_name)
    _create_volume(owner=owner, volume_name=destination_volume_name, labels=labels)
    _copy_volume_contents(
        owner=owner,
        source_volume_name=source_volume_name,
        destination_volume_name=destination_volume_name,
    )


def _create_volume(
    *,
    owner: str,
    volume_name: str,
    labels: Mapping[str, str],
) -> None:
    command = ["docker", "volume", "create"]
    for key, value in sorted(labels.items()):
        command.extend(["--label", f"{key}={value}"])
    command.append(volume_name)
    _run_cli(command, owner=owner)


def _copy_volume_contents(
    *,
    owner: str,
    source_volume_name: str,
    destination_volume_name: str,
) -> None:
    _run_cli(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=volume,source={source_volume_name},target=/from,readonly",
            "--mount",
            f"type=volume,source={destination_volume_name},target=/to",
            _VOLUME_COPY_IMAGE,
            "sh",
            "-ec",
            (
                "cp -a --reflink=auto --sparse=always /from/. /to/\n"
                "chown --reference=/from /to\n"
                "chmod --reference=/from /to"
            ),
        ],
        owner=owner,
    )


def _containers_using_volumes(
    *,
    owner: str,
    volume_names: Sequence[str],
) -> list[str]:
    container_ids: list[str] = []
    for volume_name in volume_names:
        output = _run_cli(
            ["docker", "ps", "-q", "--filter", f"volume={volume_name}"],
            owner=owner,
        )
        container_ids.extend(line.strip() for line in output.splitlines() if line.strip())
    return _unique_strings(container_ids)


def _stop_containers(
    *,
    owner: str,
    container_ids: Sequence[str],
) -> None:
    if container_ids:
        _run_cli(["docker", "stop", *container_ids], owner=owner)


def _start_containers(
    *,
    owner: str,
    container_ids: Sequence[str],
) -> None:
    if container_ids:
        _run_cli(["docker", "start", *container_ids], owner=owner)


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _remove_snapshot_volumes_from_manifest(
    *,
    owner: str,
    manifest_path: Path,
) -> None:
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, Mapping):
        return
    for volume_name in _snapshot_volume_names_from_manifest(manifest):
        _remove_volume_if_exists(owner=owner, volume_name=volume_name)


def _snapshot_volume_names_from_manifest(manifest: Mapping[str, Any]) -> list[str]:
    volume_entries = manifest.get("volumes")
    if not isinstance(volume_entries, list):
        return []
    return _unique_strings(
        volume_name
        for volume_entry in volume_entries
        if isinstance(volume_entry, Mapping)
        and isinstance((volume_name := volume_entry.get("snapshot_volume_name")), str)
        and volume_name
    )


def _container_volume_name_for_target(
    *,
    live_container: _LiveContainer,
    target_path: object,
    owner: str,
) -> str:
    target = _require_string(
        owner=owner,
        label=f"volume target path for '{live_container.status.container_name}'",
        value=target_path,
    )
    for mount in live_container.mounts:
        if mount.get("Type") == "volume" and mount.get("Destination") == target:
            return _require_string(
                owner=owner,
                label=(
                    "Docker volume name for "
                    f"'{live_container.status.container_name}' mounted at '{target}'"
                ),
                value=mount.get("Name"),
            )
    raise RuntimeError(
        f"{owner}(...) could not find a Docker volume mounted at '{target}' "
        f"for container '{live_container.status.container_name}'."
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


def _manifest_format(manifest: Mapping[str, Any]) -> int:
    value = manifest.get("format")
    if value != _CURRENT_SNAPSHOT_FORMAT:
        raise RuntimeError(
            "restore_docker(...) received an unsupported Docker snapshot manifest "
            f"format {value!r}. This version only restores format "
            f"{_CURRENT_SNAPSHOT_FORMAT} volume-clone snapshots; regenerate the snapshot."
        )
    return value


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


def _manifest_volume_snapshot_name(entry: Mapping[str, Any]) -> str:
    value = entry.get("snapshot_volume_name")
    return _require_string(
        owner="restore_docker",
        label="volume snapshot_volume_name",
        value=value,
    )


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


def _remove_volume_if_exists(*, owner: str, volume_name: str) -> bool:
    if not _volume_exists(owner=owner, volume_name=volume_name):
        return False
    _run_cli(
        ["docker", "volume", "rm", volume_name],
        owner=owner,
    )
    return True


def _require_volume_exists(*, owner: str, volume_name: str) -> None:
    if not _volume_exists(owner=owner, volume_name=volume_name):
        raise FileNotFoundError(
            f"{owner}(...) could not find Docker snapshot volume '{volume_name}'."
        )


def _volume_exists(*, owner: str, volume_name: str) -> bool:
    output = _run_cli(
        ["docker", "volume", "ls", "--format", "json"],
        owner=owner,
    )
    volume_rows = _parse_json_lines(owner=owner, payload=output, label="volume ls")
    return any(
        isinstance(row, Mapping) and row.get("Name") == volume_name
        for row in volume_rows
    )


def _parse_compose_ps_json(*, owner: str, payload: str) -> list[dict[str, Any]]:
    stripped = payload.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return _parse_json_lines(owner=owner, payload=stripped, label="compose ps")
    if isinstance(parsed, Mapping):
        return [dict(parsed)]
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
    _LOGGER.debug(
        "subprocess_start",
        "running subprocess",
        owner=owner,
        command=_display_command(args),
    )
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        executable = args[0] if args else "command"
        _LOGGER.error(
            "subprocess_missing",
            "required subprocess executable was not available",
            owner=owner,
            executable=executable,
            **_duration_fields(started_at),
        )
        raise FileNotFoundError(
            f"{owner}(...) requires the '{executable}' CLI, but it was not available."
        ) from exc

    if completed.returncode != 0:
        error_output = completed.stderr.strip() or completed.stdout.strip()
        if not error_output:
            error_output = f"exit code {completed.returncode}"
        _LOGGER.error(
            "subprocess_failure",
            "subprocess failed",
            owner=owner,
            command=_display_command(args),
            returncode=completed.returncode,
            **_duration_fields(started_at),
        )
        raise RuntimeError(
            f"{owner}(...) failed while running `{_display_command(args)}`: {error_output}"
        )
    _LOGGER.debug(
        "subprocess_success",
        "subprocess completed",
        owner=owner,
        command=_display_command(args),
        stdout_bytes=len(completed.stdout),
        **_duration_fields(started_at),
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


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


__all__ = [
    "DockerComposeStack",
    "DockerContainerStatus",
    "RunDockerStep",
    "restore_docker",
    "run_docker",
    "store_docker",
]
