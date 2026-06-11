"""Official Docker Compose snapshot touchpoint."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Literal
from urllib.parse import urlunsplit
from uuid import uuid4

from journeysdk.logger import JourneyLogLevel, PrettyLine, get_logger, pretty_row
from journeysdk.rehydration import JourneyRestoreContext, JourneyStoreContext
from journeysdk.session import _allocate_log_artifact, _register_case_exit_object

_CACHE_ROOT = Path(tempfile.gettempdir()) / "journey-sdk-docker"
_CURRENT_SNAPSHOT_FORMAT = 4
_VOLUME_COPY_IMAGE = "debian:bookworm-slim"
_DOCKER_PULL_POLICIES = {"always", "missing", "never"}
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
    _LOGGER.debug(
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
    _LOGGER.debug(
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
class DockerLogMatcher:
    """Regex matcher for one Docker service log line."""

    service_name: str
    message: str
    timeout: float = 60.0
    poll_interval: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service_name",
            _normalize_regex_pattern(
                owner="DockerLogMatcher",
                field="service_name",
                value=self.service_name,
            ),
        )
        object.__setattr__(
            self,
            "message",
            _normalize_regex_pattern(
                owner="DockerLogMatcher",
                field="message",
                value=self.message,
            ),
        )
        object.__setattr__(
            self,
            "timeout",
            _normalize_nonnegative_number(
                owner="DockerLogMatcher",
                field="timeout",
                value=self.timeout,
            ),
        )
        object.__setattr__(
            self,
            "poll_interval",
            _normalize_positive_number(
                owner="DockerLogMatcher",
                field="poll_interval",
                value=self.poll_interval,
            ),
        )


@dataclass(frozen=True)
class DockerLogMatch:
    """Matched Docker log line metadata."""

    service: str
    container_id: str
    container_name: str
    line: str


@dataclass(frozen=True)
class DockerHttpCheck:
    """HTTP readiness check for one Docker Compose service."""

    service_name: str
    port: int
    path: str = "/"
    scheme: str = "http"
    expected_status: int = 200
    timeout: float = 60.0
    poll_interval: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service_name",
            _normalize_service_name(
                owner="DockerHttpCheck",
                field="service_name",
                value=self.service_name,
            ),
        )
        object.__setattr__(
            self,
            "port",
            _normalize_positive_int(
                owner="DockerHttpCheck",
                field="port",
                value=self.port,
            ),
        )
        object.__setattr__(
            self,
            "path",
            _normalize_http_path(
                owner="DockerHttpCheck",
                field="path",
                value=self.path,
            ),
        )
        object.__setattr__(
            self,
            "scheme",
            _normalize_http_scheme(
                owner="DockerHttpCheck",
                field="scheme",
                value=self.scheme,
            ),
        )
        object.__setattr__(
            self,
            "expected_status",
            _normalize_http_status(
                owner="DockerHttpCheck",
                field="expected_status",
                value=self.expected_status,
            ),
        )
        object.__setattr__(
            self,
            "timeout",
            _normalize_nonnegative_number(
                owner="DockerHttpCheck",
                field="timeout",
                value=self.timeout,
            ),
        )
        object.__setattr__(
            self,
            "poll_interval",
            _normalize_positive_number(
                owner="DockerHttpCheck",
                field="poll_interval",
                value=self.poll_interval,
            ),
        )


@dataclass(frozen=True)
class DockerComposeStack:
    """Serializable descriptor for one started Docker Compose project."""

    compose_file: str
    resolved_compose_file: str
    project_name: str
    cache_root: str
    wait_timeout: int | None = None
    log_matchers: tuple[DockerLogMatcher, ...] = ()
    http_checks: tuple[DockerHttpCheck, ...] = ()
    log_artifact_metadata: object | None = None

    @property
    def statuses(self) -> dict[str, tuple[DockerContainerStatus, ...]]:
        """Return live container statuses grouped by Compose service."""

        return _load_live_statuses(self)

    @property
    def logs(self) -> dict[str, str]:
        """Return live combined logs grouped by Compose service."""

        return _load_live_logs(self)

    def wait_for_log(
        self,
        *,
        service_name: str,
        message: str,
        timeout: float = 60.0,
        poll_interval: float = 0.25,
        since: datetime | str = "now",
    ) -> DockerLogMatch:
        """Wait until a matching raw Docker log line appears."""

        matcher = DockerLogMatcher(
            service_name=service_name,
            message=message,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        return _wait_for_docker_log_match(
            stack=self,
            matcher=matcher,
            since=_normalize_docker_logs_since(since),
            owner="DockerComposeStack.wait_for_log",
        )

    def service_url(
        self,
        service_name: str,
        port: int,
        *,
        path: str = "",
        scheme: str = "http",
    ) -> str:
        """Return the host URL for one published Compose service port."""

        normalized_service_name = _normalize_service_name(
            owner="DockerComposeStack.service_url",
            field="service_name",
            value=service_name,
        )
        normalized_port = _normalize_positive_int(
            owner="DockerComposeStack.service_url",
            field="port",
            value=port,
        )
        normalized_path = _normalize_http_path(
            owner="DockerComposeStack.service_url",
            field="path",
            value=path,
            allow_empty=True,
        )
        normalized_scheme = _normalize_http_scheme(
            owner="DockerComposeStack.service_url",
            field="scheme",
            value=scheme,
        )
        endpoint = _run_cli(
            _compose_command(
                compose_file=Path(self.resolved_compose_file),
                project_name=self.project_name,
                subcommand=["port", normalized_service_name, str(normalized_port)],
            ),
            owner="DockerComposeStack.service_url",
        )
        host, host_port = _parse_compose_port_endpoint(
            endpoint,
            owner="DockerComposeStack.service_url",
        )
        return urlunsplit(
            (
                normalized_scheme,
                f"{host}:{host_port}",
                normalized_path,
                "",
                "",
            )
        )

    def __case_exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop Compose containers owned by this case while preserving volumes."""

        if getattr(self, "_case_exit_closed", False):
            return
        object.__setattr__(self, "_case_exit_closed", True)
        _capture_docker_compose_logs(
            self,
            owner="DockerComposeStack.__case_exit__",
            status="interrupted" if exc_type is not None else "success",
        )
        _down_docker_stack(
            self,
            owner="DockerComposeStack.__case_exit__",
        )

    def __store__(self, context: JourneyStoreContext) -> object:
        snapshot_name = _snapshot_name_for_context(context)
        _store_docker_snapshot(
            stack=self,
            snapshot_name=snapshot_name,
            snapshot_root=_context_snapshot_root(context),
        )
        return {
            "compose_file": self.compose_file,
            "resolved_compose_file": self.resolved_compose_file,
            "project_name": self.project_name,
            "cache_root": self.cache_root,
            "snapshot_name": snapshot_name,
            "wait_timeout": self.wait_timeout,
            "log_matchers": [
                _log_matcher_payload(matcher)
                for matcher in self.log_matchers
            ],
            "http_checks": [
                _http_check_payload(check)
                for check in self.http_checks
            ],
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
            wait_timeout=data["wait_timeout"],
            log_matchers=data["log_matchers"],
            http_checks=data["http_checks"],
        )
        _restore_docker_snapshot(
            stack=stack,
            snapshot_name=data["snapshot_name"],
            snapshot_root=_context_snapshot_root(context),
        )
        return stack


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
    wait_for_logs: Sequence[DockerLogMatcher] = (),
    wait_for_http: Sequence[DockerHttpCheck] = (),
    build: bool = False,
    pull_policy: Literal["always", "missing", "never"] | None = None,
) -> DockerComposeStack:
    """Start one local Docker Compose app."""

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
    normalized_log_matchers = _normalize_log_matcher_sequence(
        owner="run_docker",
        field="wait_for_logs",
        value=wait_for_logs,
    )
    normalized_http_checks = _normalize_http_check_sequence(
        owner="run_docker",
        field="wait_for_http",
        value=wait_for_http,
    )
    normalized_build = _normalize_bool(
        owner="run_docker",
        field="build",
        value=build,
    )
    normalized_pull_policy = _normalize_optional_pull_policy(
        owner="run_docker",
        field="pull_policy",
        value=pull_policy,
    )

    compose_started_at = time.monotonic()
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
        wait_timeout=normalized_wait_timeout,
        build=normalized_build,
        pull_policy=normalized_pull_policy,
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
    if normalized_build:
        up_command.append("--build")
    if normalized_pull_policy is not None:
        up_command.extend(["--pull", normalized_pull_policy])
    if normalized_wait_timeout is not None:
        up_command.extend(["--wait-timeout", str(normalized_wait_timeout)])
    log_artifact_metadata = _allocate_log_artifact(
        "run_docker",
        kind="docker_compose_logs",
        touchpoint="docker",
        source=resolved_project_name,
        suffix=".docker.log",
        content_type="text/plain",
    )
    stack = DockerComposeStack(
        compose_file=str(original_compose_path),
        resolved_compose_file=str(resolved_compose_path),
        project_name=resolved_project_name,
        cache_root=str(_CACHE_ROOT),
        wait_timeout=normalized_wait_timeout,
        log_matchers=normalized_log_matchers,
        http_checks=normalized_http_checks,
        log_artifact_metadata=log_artifact_metadata,
    )
    _register_case_exit_object("run_docker", stack)
    log_since = _docker_logs_since_timestamp()
    try:
        _run_cli(
            _compose_command(
                compose_file=resolved_compose_path,
                project_name=resolved_project_name,
                subcommand=up_command,
            ),
            owner="run_docker",
            failure_level="debug",
        )
    except RuntimeError as exc:
        if not _compose_wait_failure_is_successful_one_shot(stack):
            _capture_docker_compose_logs(
                stack,
                owner="run_docker",
                status="failed",
            )
            _LOGGER.error(
                "compose_failure",
                "Docker Compose stack failed",
                pretty="Docker Compose stack failed",
                compose_file=stack.compose_file,
                resolved_compose_file=stack.resolved_compose_file,
                project=stack.project_name,
                wait_timeout=normalized_wait_timeout,
                error=str(exc),
                **_duration_fields(compose_started_at),
            )
            raise

    _wait_for_docker_log_matchers(
        stack=stack,
        matchers=normalized_log_matchers,
        since=log_since,
        since_container_start=True,
        owner="run_docker",
    )
    _wait_for_docker_http_checks(
        stack=stack,
        checks=normalized_http_checks,
        owner="run_docker",
    )

    duration = _duration_fields(compose_started_at)
    _LOGGER.info(
        "compose_success",
        "Docker Compose stack started",
        pretty=_docker_row(
            _phase_pretty_text(
                "Docker Compose stack started",
                duration=duration["duration"],
                detail=_pretty_kv(
                    [
                        ("project", stack.project_name),
                        ("compose", stack.compose_file),
                        ("resolved", stack.resolved_compose_file),
                        ("wait_timeout", normalized_wait_timeout),
                        ("build", normalized_build),
                        ("pull_policy", normalized_pull_policy),
                    ]
                )
            ),
        ),
        compose_file=stack.compose_file,
        resolved_compose_file=stack.resolved_compose_file,
        project=stack.project_name,
        wait_timeout=normalized_wait_timeout,
        build=normalized_build,
        pull_policy=normalized_pull_policy,
        **duration,
    )
    return stack


def _down_docker_stack(
    stack: DockerComposeStack,
    *,
    owner: str,
) -> None:
    down_started_at = time.monotonic()
    validated_stack = _require_stack(stack=stack, owner=owner)
    pretty_detail = _pretty_kv(
        [
            ("project", validated_stack.project_name),
            ("compose", validated_stack.resolved_compose_file),
        ]
    )
    _LOGGER.info(
        "compose_down_start",
        "stopping Docker Compose stack",
        pretty=_docker_row(
            _phase_pretty_text(
                "stopping Docker Compose stack",
                detail=pretty_detail,
            )
        ),
        compose_file=validated_stack.compose_file,
        resolved_compose_file=validated_stack.resolved_compose_file,
        project=validated_stack.project_name,
    )
    try:
        _run_cli(
            _compose_command(
                compose_file=Path(validated_stack.resolved_compose_file),
                project_name=validated_stack.project_name,
                subcommand=["down", "--remove-orphans"],
            ),
            owner=owner,
        )
    except BaseException as exc:
        _LOGGER.error(
            "compose_down_failure",
            "Docker Compose stack cleanup failed",
            pretty=_docker_row(
                _phase_pretty_text(
                    "Docker Compose stack cleanup failed",
                    duration=_duration_fields(down_started_at)["duration"],
                    detail=pretty_detail,
                )
            ),
            compose_file=validated_stack.compose_file,
            resolved_compose_file=validated_stack.resolved_compose_file,
            project=validated_stack.project_name,
            error=str(exc),
            **_duration_fields(down_started_at),
        )
        raise
    duration = _duration_fields(down_started_at)
    _LOGGER.info(
        "compose_down_success",
        "Docker Compose stack stopped",
        pretty=_docker_row(
            _phase_pretty_text(
                "Docker Compose stack stopped",
                duration=duration["duration"],
                detail=pretty_detail,
            )
        ),
        compose_file=validated_stack.compose_file,
        resolved_compose_file=validated_stack.resolved_compose_file,
        project=validated_stack.project_name,
        **duration,
    )


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
    _LOGGER.info(
        "snapshot_store_start",
        "storing Docker Compose snapshot",
        pretty=_docker_row("storing Docker Compose snapshot"),
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
            volume_entry["backup_relpath"] = _volume_snapshot_payload_relpath(
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
            snapshot_payload_detail = _pretty_kv(
                [
                    ("volume", volume_entry["volume_name"]),
                    ("service", volume_entry["service"]),
                    ("container", volume_entry["container_name"]),
                    ("from", volume_entry["volume_name"]),
                    ("to", volume_entry["backup_relpath"]),
                ]
            )
            snapshot_payload_started_at = _log_snapshot_phase_start(
                prefix="snapshot_store",
                phase="volume_payload",
                message="copying Docker volume to snapshot",
                project_name=validated_stack.project_name,
                snapshot_name=normalized_snapshot_name,
                pretty_detail=snapshot_payload_detail,
                service=volume_entry["service"],
                container=volume_entry["container_name"],
                volume=volume_entry["volume_name"],
                backup_relpath=volume_entry["backup_relpath"],
                target_path=volume_entry["target_path"],
            )
            snapshot_payload_dir = _manifest_volume_snapshot_payload_dir(
                snapshot_dir=snapshot_dir,
                volume_entry=volume_entry,
                owner="store_docker",
            )
            snapshot_payload_dir.mkdir(parents=True, exist_ok=False)
            _copy_volume_to_directory(
                owner="store_docker",
                source_volume_name=volume_entry["volume_name"],
                destination_dir=snapshot_payload_dir,
            )
            _log_snapshot_phase_success(
                prefix="snapshot_store",
                phase="volume_payload",
                started_at=snapshot_payload_started_at,
                message="copied Docker volume to snapshot",
                project_name=validated_stack.project_name,
                snapshot_name=normalized_snapshot_name,
                pretty_detail=snapshot_payload_detail,
                service=volume_entry["service"],
                container=volume_entry["container_name"],
                volume=volume_entry["volume_name"],
                backup_relpath=volume_entry["backup_relpath"],
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
                        ("dir", snapshot_dir),
                        ("manifest", manifest_path),
                        ("containers", len(container_entries)),
                        ("volumes", len(volume_entries_by_name)),
                    ]
                ),
            )
        ),
        project=validated_stack.project_name,
        snapshot=normalized_snapshot_name,
        snapshot_dir=snapshot_dir,
        manifest_path=manifest_path,
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
        pretty=_docker_row("restoring Docker Compose snapshot"),
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
        snapshot_payload_dir = _manifest_volume_snapshot_payload_dir(
            snapshot_dir=snapshot_dir,
            volume_entry=volume_entry,
            owner="restore_docker",
        )
        _require_snapshot_payload_dir_exists(
            owner="restore_docker",
            snapshot_payload_dir=snapshot_payload_dir,
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
        snapshot_payload_dir = _manifest_volume_snapshot_payload_dir(
            snapshot_dir=snapshot_dir,
            volume_entry=volume_entry,
            owner="restore_docker",
        )
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
                ("from", volume_entry["backup_relpath"]),
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
            backup_relpath=volume_entry["backup_relpath"],
            destination_volume=destination_volume_name,
            target_path=volume_entry["target_path"],
        )
        _copy_directory_to_volume(
            owner="restore_docker",
            source_dir=snapshot_payload_dir,
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
            backup_relpath=volume_entry["backup_relpath"],
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
        up_command = ["up", "-d", "--wait", "--no-recreate", "--no-deps"]
        if validated_stack.wait_timeout is not None:
            up_command.extend(["--wait-timeout", str(validated_stack.wait_timeout)])
        up_command.extend(running_services)
        log_since = _docker_logs_since_timestamp()
        _run_cli(
            _compose_multi_file_command(
                compose_files=compose_files,
                project_name=validated_stack.project_name,
                subcommand=up_command,
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
        _wait_for_docker_log_matchers(
            stack=validated_stack,
            matchers=validated_stack.log_matchers,
            since=log_since,
            owner="restore_docker",
            service_names=running_services,
        )
        _wait_for_docker_http_checks(
            stack=validated_stack,
            checks=validated_stack.http_checks,
            owner="restore_docker",
            service_names=running_services,
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
                        ("dir", snapshot_dir),
                        ("manifest", manifest_path),
                        ("containers", len(container_entries)),
                        ("volumes", len(volume_entries)),
                        ("services", running_services),
                    ]
                ),
            )
        ),
        project=validated_stack.project_name,
        snapshot=normalized_snapshot_name,
        snapshot_dir=snapshot_dir,
        manifest_path=manifest_path,
        containers=len(container_entries),
        volumes=len(volume_entries),
        services=running_services,
        services_count=len(running_services),
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


def _capture_docker_compose_logs(
    stack: DockerComposeStack,
    *,
    owner: str,
    status: str,
) -> None:
    metadata = getattr(stack, "log_artifact_metadata", None)
    if metadata is None:
        return
    try:
        logs = _load_live_logs(stack)
    except BaseException as exc:
        _LOGGER.warning(
            "logs_capture_failure",
            "could not capture Docker Compose logs",
            owner=owner,
            project=stack.project_name,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    base_path = Path(getattr(metadata, "path"))
    for service, text in logs.items():
        service_slug = _safe_artifact_segment(service)
        path = base_path.with_name(f"{base_path.stem}-{service_slug}{base_path.suffix}")
        manifest_path = path.with_name(f"{path.stem}.manifest.json")
        _write_docker_log_artifact(
            metadata=metadata,
            service=service,
            path=path,
            manifest_path=manifest_path,
            text=text,
            status=status,
        )


def _write_docker_log_artifact(
    *,
    metadata: object,
    service: str,
    path: Path,
    manifest_path: Path,
    text: str,
    status: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    line_count = len(text.splitlines())
    manifest = {
        "format": "journey.log_artifact",
        "version": 1,
        "kind": getattr(metadata, "kind"),
        "touchpoint": getattr(metadata, "touchpoint"),
        "source": service,
        "content_type": getattr(metadata, "content_type"),
        "status": status,
        "started_at": None,
        "stopped_at": _utc_timestamp(),
        "run_id": getattr(metadata, "run_id"),
        "sequence": getattr(metadata, "sequence"),
        "artifact_key": f"{getattr(metadata, 'key')}-{service}",
        "journey_id": getattr(metadata, "journey_id"),
        "function_ref": getattr(metadata, "function_ref"),
        "case_id": getattr(metadata, "case_id"),
        "branch_env": getattr(metadata, "branch_env"),
        "step_id": getattr(metadata, "step_id"),
        "step_label": getattr(metadata, "step_label"),
        "step_name": getattr(metadata, "step_name"),
        "node_index": getattr(metadata, "node_index"),
        "attempt": getattr(metadata, "attempt"),
        "path": str(path),
        "line_count": line_count,
        "byte_count": path.stat().st_size,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_artifact_segment(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.+-]+", "-", value).strip("-._")
    return (slug or "service")[:96]


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _wait_for_docker_log_match(
    *,
    stack: DockerComposeStack,
    matcher: DockerLogMatcher,
    since: str | None,
    owner: str,
) -> DockerLogMatch:
    matches = _wait_for_docker_log_matchers(
        stack=stack,
        matchers=(matcher,),
        since=since,
        owner=owner,
    )
    if not matches:
        raise RuntimeError(f"{owner}(...) did not run any Docker log matchers.")
    return matches[0]


def _wait_for_docker_log_matchers(
    *,
    stack: DockerComposeStack,
    matchers: Sequence[DockerLogMatcher],
    since: str | None,
    owner: str,
    service_names: Sequence[str] | None = None,
    since_container_start: bool = False,
) -> tuple[DockerLogMatch, ...]:
    normalized_matchers = _normalize_log_matcher_sequence(
        owner=owner,
        field="matchers",
        value=matchers,
    )
    if not normalized_matchers:
        return ()

    validated_stack = _require_stack(stack=stack, owner=owner)
    compose_config = _load_compose_config(validated_stack, owner=owner)
    declared_services = _declared_service_names(compose_config, owner=owner)
    service_filter = set(service_names) if service_names is not None else None
    results: list[DockerLogMatch] = []
    for matcher in normalized_matchers:
        match = _wait_for_one_docker_log_matcher(
            stack=validated_stack,
            matcher=matcher,
            declared_services=declared_services,
            service_filter=service_filter,
            since=since,
            owner=owner,
            since_container_start=since_container_start,
        )
        if match is not None:
            results.append(match)
    return tuple(results)


def _wait_for_one_docker_log_matcher(
    *,
    stack: DockerComposeStack,
    matcher: DockerLogMatcher,
    declared_services: Sequence[str],
    service_filter: set[str] | None,
    since: str | None,
    owner: str,
    since_container_start: bool,
) -> DockerLogMatch | None:
    service_pattern = re.compile(matcher.service_name)
    message_pattern = re.compile(matcher.message)
    matched_services = [
        service for service in declared_services if service_pattern.search(service)
    ]
    if not matched_services:
        raise RuntimeError(
            f"{owner}(...) could not find a declared Compose service matching "
            f"{matcher.service_name!r}."
        )

    eligible_services = matched_services
    if service_filter is not None:
        eligible_services = [
            service for service in matched_services if service in service_filter
        ]
        if not eligible_services:
            _LOGGER.debug(
                "log_wait_skipped",
                "skipping Docker log wait for services not started in this restore",
                owner=owner,
                project=stack.project_name,
                service_name_pattern=matcher.service_name,
                matched_services=matched_services,
                started_services=sorted(service_filter),
            )
            return None

    _LOGGER.debug(
        "log_wait_start",
        "waiting for Docker log line",
        owner=owner,
        project=stack.project_name,
        service_name_pattern=matcher.service_name,
        log_message_pattern=matcher.message,
        services=eligible_services,
        timeout=matcher.timeout,
        since=since,
        since_container_start=since_container_start,
    )
    started_at = time.monotonic()
    deadline = started_at + matcher.timeout
    attempts = 0
    while True:
        attempts += 1
        live_containers = _load_live_containers(
            stack,
            owner=owner,
            include_all=True,
        )
        for live_container in live_containers:
            status = live_container.status
            if status.service not in eligible_services:
                continue
            log_since = _docker_log_since_for_container(
                status=status,
                fallback_since=since,
                since_container_start=since_container_start,
            )
            output = _load_raw_container_logs(
                container_id=status.container_id,
                since=log_since,
                owner=owner,
            )
            for line in output.splitlines():
                if message_pattern.search(line):
                    duration = _duration_fields(started_at)
                    match = DockerLogMatch(
                        service=status.service,
                        container_id=status.container_id,
                        container_name=status.container_name,
                        line=line,
                    )
                    _LOGGER.debug(
                        "log_wait_success",
                        "matched Docker log line",
                        owner=owner,
                        project=stack.project_name,
                        service=status.service,
                        container_id=status.container_id,
                        container_name=status.container_name,
                        service_name_pattern=matcher.service_name,
                        log_message_pattern=matcher.message,
                        attempts=attempts,
                        **duration,
                    )
                    return match

        now = time.monotonic()
        if now >= deadline:
            _LOGGER.warning(
                "log_wait_timeout",
                "timed out waiting for Docker log line",
                owner=owner,
                project=stack.project_name,
                service_name_pattern=matcher.service_name,
                log_message_pattern=matcher.message,
                services=eligible_services,
                timeout=matcher.timeout,
                attempts=attempts,
                **_duration_fields(started_at),
            )
            raise TimeoutError(
                f"{owner}(...) timed out after {matcher.timeout:g}s waiting for "
                f"Docker log message {matcher.message!r} from service pattern "
                f"{matcher.service_name!r}."
            )
        time.sleep(min(matcher.poll_interval, deadline - now))


def _docker_log_since_for_container(
    *,
    status: DockerContainerStatus,
    fallback_since: str | None,
    since_container_start: bool,
) -> str | None:
    if not since_container_start:
        return fallback_since
    started_at = status.started_at
    if started_at is None:
        return fallback_since
    normalized = started_at.strip()
    if not normalized or normalized.startswith("0001-01-01T00:00:00"):
        return fallback_since
    return normalized


def _load_raw_container_logs(
    *,
    container_id: str,
    since: str | None,
    owner: str,
) -> str:
    command = ["docker", "logs"]
    if since is not None:
        command.extend(["--since", since])
    command.append(container_id)
    return _run_cli(command, owner=owner)


def _wait_for_docker_http_checks(
    *,
    stack: DockerComposeStack,
    checks: Sequence[DockerHttpCheck],
    owner: str,
    service_names: Sequence[str] | None = None,
) -> None:
    normalized_checks = _normalize_http_check_sequence(
        owner=owner,
        field="checks",
        value=checks,
    )
    if not normalized_checks:
        return

    validated_stack = _require_stack(stack=stack, owner=owner)
    service_filter = set(service_names) if service_names is not None else None
    for check in normalized_checks:
        if service_filter is not None and check.service_name not in service_filter:
            _LOGGER.debug(
                "http_wait_skipped",
                "skipping Docker HTTP wait for service not started in this restore",
                owner=owner,
                project=validated_stack.project_name,
                service=check.service_name,
                started_services=sorted(service_filter),
            )
            continue
        _wait_for_one_docker_http_check(
            stack=validated_stack,
            check=check,
            owner=owner,
        )


def _wait_for_one_docker_http_check(
    *,
    stack: DockerComposeStack,
    check: DockerHttpCheck,
    owner: str,
) -> None:
    _LOGGER.debug(
        "http_wait_start",
        "waiting for Docker HTTP endpoint",
        owner=owner,
        project=stack.project_name,
        service=check.service_name,
        port=check.port,
        path=check.path,
        expected_status=check.expected_status,
        timeout=check.timeout,
    )
    started_at = time.monotonic()
    deadline = started_at + check.timeout
    attempts = 0
    last_detail = "<no attempts>"
    while True:
        attempts += 1
        try:
            url = stack.service_url(
                check.service_name,
                check.port,
                path=check.path,
                scheme=check.scheme,
            )
            status = _http_status(url, timeout=min(10.0, max(0.1, check.poll_interval)))
            last_detail = f"status {status}"
            if status == check.expected_status:
                _LOGGER.debug(
                    "http_wait_success",
                    "Docker HTTP endpoint is ready",
                    owner=owner,
                    project=stack.project_name,
                    service=check.service_name,
                    port=check.port,
                    url=url,
                    attempts=attempts,
                    **_duration_fields(started_at),
                )
                return
        except BaseException as exc:
            last_detail = f"{type(exc).__name__}: {exc}"

        now = time.monotonic()
        if now >= deadline:
            _LOGGER.warning(
                "http_wait_timeout",
                "timed out waiting for Docker HTTP endpoint",
                owner=owner,
                project=stack.project_name,
                service=check.service_name,
                port=check.port,
                path=check.path,
                expected_status=check.expected_status,
                attempts=attempts,
                last_detail=last_detail,
                **_duration_fields(started_at),
            )
            raise TimeoutError(
                f"{owner}(...) timed out after {check.timeout:g}s waiting for "
                f"{check.service_name}:{check.port}{check.path} to return "
                f"HTTP {check.expected_status}; {last_detail}."
            )
        time.sleep(min(check.poll_interval, deadline - now))


def _http_status(url: str, *, timeout: float) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if isinstance(status, int):
                return status
            return int(getattr(response, "code"))
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _parse_compose_port_endpoint(endpoint: str, *, owner: str) -> tuple[str, str]:
    first_line = next(
        (line.strip() for line in endpoint.splitlines() if line.strip()),
        "",
    )
    if not first_line:
        raise RuntimeError(f"{owner}(...) could not resolve a published Docker port.")
    if first_line.startswith("["):
        closing = first_line.find("]")
        if closing == -1 or closing + 1 >= len(first_line) or first_line[closing + 1] != ":":
            raise RuntimeError(
                f"{owner}(...) could not parse Docker port endpoint {first_line!r}."
            )
        host = first_line[1:closing]
        port = first_line[closing + 2 :]
    else:
        if ":" not in first_line:
            raise RuntimeError(
                f"{owner}(...) could not parse Docker port endpoint {first_line!r}."
            )
        host, port = first_line.rsplit(":", 1)
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    if not port.isdigit():
        raise RuntimeError(
            f"{owner}(...) could not parse Docker port endpoint {first_line!r}."
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host, port


def _declared_service_names(
    compose_config: Mapping[str, Any],
    *,
    owner: str,
) -> tuple[str, ...]:
    services = compose_config.get("services")
    if not isinstance(services, Mapping):
        raise RuntimeError(f"{owner}(...) received an invalid Compose services block.")
    return tuple(
        service for service in services if isinstance(service, str) and service
    )


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


def _normalize_bool(
    *,
    owner: str,
    field: str,
    value: object,
) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{owner}(..., {field}=...) expects a boolean.")
    return value


def _normalize_optional_pull_policy(
    *,
    owner: str,
    field: str,
    value: object,
) -> Literal["always", "missing", "never"] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(
            f"{owner}(..., {field}=...) expects 'always', 'missing', 'never', or None."
        )
    normalized = value.strip().lower()
    if normalized not in _DOCKER_PULL_POLICIES:
        raise ValueError(
            f"{owner}(..., {field}=...) expects 'always', 'missing', 'never', or None."
        )
    if normalized == "always":
        return "always"
    if normalized == "missing":
        return "missing"
    return "never"


def _normalize_positive_int(
    *,
    owner: str,
    field: str,
    value: object,
) -> int:
    normalized = _normalize_optional_positive_int(
        owner=owner,
        field=field,
        value=value,
    )
    if normalized is None:
        raise TypeError(f"{owner}(..., {field}=...) expects a positive integer.")
    return normalized


def _normalize_nonnegative_number(
    *,
    owner: str,
    field: str,
    value: object,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{owner}(..., {field}=...) expects a number.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{owner}(..., {field}=...) expects a finite number.")
    if normalized < 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a non-negative number.")
    return normalized


def _normalize_positive_number(
    *,
    owner: str,
    field: str,
    value: object,
) -> float:
    normalized = _normalize_nonnegative_number(
        owner=owner,
        field=field,
        value=value,
    )
    if normalized <= 0:
        raise ValueError(f"{owner}(..., {field}=...) expects a positive number.")
    return normalized


def _normalize_regex_pattern(
    *,
    owner: str,
    field: str,
    value: object,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a regex string.")
    if not value.strip():
        raise ValueError(f"{owner}(..., {field}=...) expects a non-blank regex string.")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(
            f"{owner}(..., {field}=...) received an invalid regex: {exc}."
        ) from exc
    return value


def _normalize_service_name(
    *,
    owner: str,
    field: str,
    value: object,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{owner}(..., {field}=...) expects a non-blank string.")
    return normalized


def _normalize_http_path(
    *,
    owner: str,
    field: str,
    value: object,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string.")
    if value == "" and allow_empty:
        return ""
    if not value.startswith("/"):
        raise ValueError(f"{owner}(..., {field}=...) expects a path starting with '/'.")
    return value


def _normalize_http_scheme(
    *,
    owner: str,
    field: str,
    value: object,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner}(..., {field}=...) expects a string.")
    normalized = value.strip().lower()
    if normalized not in {"http", "https"}:
        raise ValueError(f"{owner}(..., {field}=...) expects 'http' or 'https'.")
    return normalized


def _normalize_http_status(
    *,
    owner: str,
    field: str,
    value: object,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{owner}(..., {field}=...) expects an integer.")
    if value < 100 or value > 599:
        raise ValueError(f"{owner}(..., {field}=...) expects an HTTP status code.")
    return value


def _normalize_log_matcher_sequence(
    *,
    owner: str,
    field: str,
    value: object,
) -> tuple[DockerLogMatcher, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(
            f"{owner}(..., {field}=...) expects a sequence of DockerLogMatcher values."
        )
    matchers: list[DockerLogMatcher] = []
    for index, matcher in enumerate(value):
        if not isinstance(matcher, DockerLogMatcher):
            raise TypeError(
                f"{owner}(..., {field}=...) expects DockerLogMatcher values; "
                f"item {index} was {type(matcher).__name__}."
            )
        matchers.append(matcher)
    return tuple(matchers)


def _normalize_http_check_sequence(
    *,
    owner: str,
    field: str,
    value: object,
) -> tuple[DockerHttpCheck, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(
            f"{owner}(..., {field}=...) expects a sequence of DockerHttpCheck values."
        )
    checks: list[DockerHttpCheck] = []
    for index, check in enumerate(value):
        if not isinstance(check, DockerHttpCheck):
            raise TypeError(
                f"{owner}(..., {field}=...) expects DockerHttpCheck values; "
                f"item {index} was {type(check).__name__}."
            )
        checks.append(check)
    return tuple(checks)


def _normalize_docker_logs_since(value: datetime | str) -> str | None:
    if isinstance(value, datetime):
        return _datetime_to_docker_timestamp(value)
    if not isinstance(value, str):
        raise TypeError(
            "DockerComposeStack.wait_for_log(..., since=...) expects a string or datetime."
        )
    normalized = value.strip()
    if normalized == "now":
        return _docker_logs_since_timestamp()
    if normalized == "all":
        return None
    if not normalized:
        raise ValueError(
            "DockerComposeStack.wait_for_log(..., since=...) expects a non-blank string."
        )
    return normalized


def _docker_logs_since_timestamp() -> str:
    return _datetime_to_docker_timestamp(datetime.now(UTC))


def _datetime_to_docker_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _log_matcher_payload(matcher: DockerLogMatcher) -> dict[str, object]:
    return {
        "service_name": matcher.service_name,
        "message": matcher.message,
        "timeout": matcher.timeout,
        "poll_interval": matcher.poll_interval,
    }


def _http_check_payload(check: DockerHttpCheck) -> dict[str, object]:
    return {
        "service_name": check.service_name,
        "port": check.port,
        "path": check.path,
        "scheme": check.scheme,
        "expected_status": check.expected_status,
        "timeout": check.timeout,
        "poll_interval": check.poll_interval,
    }


def _require_log_matchers_payload(
    value: object,
    *,
    owner: str,
) -> tuple[DockerLogMatcher, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(
            f"{owner}(...) received invalid payload field 'log_matchers'."
        )
    matchers: list[DockerLogMatcher] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(
                f"{owner}(...) received invalid log_matchers payload item {index}."
            )
        matchers.append(
            DockerLogMatcher(
                service_name=item.get("service_name"),
                message=item.get("message"),
                timeout=item.get("timeout", 60.0),
                poll_interval=item.get("poll_interval", 0.25),
            )
        )
    return tuple(matchers)


def _require_http_checks_payload(
    value: object,
    *,
    owner: str,
) -> tuple[DockerHttpCheck, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(
            f"{owner}(...) received invalid payload field 'http_checks'."
        )
    checks: list[DockerHttpCheck] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(
                f"{owner}(...) received invalid http_checks payload item {index}."
            )
        checks.append(
            DockerHttpCheck(
                service_name=item.get("service_name"),
                port=item.get("port"),
                path=item.get("path", "/"),
                scheme=item.get("scheme", "http"),
                expected_status=item.get("expected_status", 200),
                timeout=item.get("timeout", 60.0),
                poll_interval=item.get("poll_interval", 0.25),
            )
        )
    return tuple(checks)


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
    return (
        Path(stack.compose_file).resolve().parent
        / ".journey"
        / "docker"
        / _slugify(stack.project_name)
        / snapshot_name
    )


def _context_snapshot_root(
    context: JourneyStoreContext | JourneyRestoreContext,
) -> Path:
    return context.persistent_artifact_root or context.artifact_root


def _snapshot_name_for_context(context: JourneyStoreContext) -> str:
    return _slugify(f"{context.boundary_kind}-{context.boundary_id}")


def _require_stack_payload(
    payload: object,
    *,
    owner: str,
) -> dict[str, Any]:
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
    result: dict[str, Any] = {}
    for key in required:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise TypeError(f"{owner}(...) received invalid payload field {key!r}.")
        result[key] = value
    wait_timeout = data.get("wait_timeout")
    if wait_timeout is not None:
        wait_timeout = _normalize_optional_positive_int(
            owner=owner,
            field="wait_timeout",
            value=wait_timeout,
        )
    result["wait_timeout"] = wait_timeout
    result["log_matchers"] = _require_log_matchers_payload(
        data.get("log_matchers", ()),
        owner=owner,
    )
    result["http_checks"] = _require_http_checks_payload(
        data.get("http_checks", ()),
        owner=owner,
    )
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
    for requested_id, row in zip(container_ids, inspect_rows):
        if requested_id:
            inspect_by_id[requested_id] = row
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


def _volume_snapshot_payload_relpath(
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
    payload_dir_name = f"{_slugify(source_volume_name)[:64]}-{digest}"
    return (Path("volumes") / payload_dir_name).as_posix()


def _copy_volume_to_directory(
    *,
    owner: str,
    source_volume_name: str,
    destination_dir: Path,
) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_mount, destination_path = _bind_mount_for_path(
        destination_dir,
        target="/to-root",
    )
    _copy_snapshot_contents(
        owner=owner,
        source_mount=f"type=volume,source={source_volume_name},target=/from,readonly",
        destination_mount=destination_mount,
        source_path="/from",
        destination_path=destination_path,
    )


def _copy_directory_to_volume(
    *,
    owner: str,
    source_dir: Path,
    destination_volume_name: str,
) -> None:
    source_mount, source_path = _bind_mount_for_path(
        source_dir,
        target="/from-root",
        readonly=True,
    )
    _copy_snapshot_contents(
        owner=owner,
        source_mount=source_mount,
        destination_mount=f"type=volume,source={destination_volume_name},target=/to",
        source_path=source_path,
        destination_path="/to",
    )


def _bind_mount_for_path(
    host_path: Path,
    *,
    target: str,
    readonly: bool = False,
) -> tuple[str, str]:
    resolved = host_path.resolve()
    mount_source = resolved
    relative_parts: tuple[str, ...] = ()
    parts = resolved.parts
    for index, part in enumerate(parts):
        if part.startswith(".") and index > 0:
            mount_source = Path(*parts[:index])
            relative_parts = tuple(parts[index:])
            break
    container_path = PurePosixPath(target).joinpath(*relative_parts).as_posix()
    mount_parts = [
        "type=bind",
        f"source={mount_source}",
        f"target={target}",
    ]
    if readonly:
        mount_parts.append("readonly")
    return ",".join(mount_parts), container_path


def _copy_snapshot_contents(
    *,
    owner: str,
    source_mount: str,
    destination_mount: str,
    source_path: str,
    destination_path: str,
) -> None:
    _run_cli(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            source_mount,
            "--mount",
            destination_mount,
            _VOLUME_COPY_IMAGE,
            "sh",
            "-ec",
            'mkdir -p "$2" && cp -a --reflink=auto --sparse=always "$1"/. "$2"/',
            "copy",
            source_path,
            destination_path,
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


def _manifest_volume_snapshot_payload_dir(
    *,
    snapshot_dir: Path,
    volume_entry: Mapping[str, Any],
    owner: str,
) -> Path:
    relpath = _require_string(
        owner=owner,
        label="volume backup_relpath",
        value=volume_entry.get("backup_relpath"),
    )
    if "\\" in relpath:
        raise RuntimeError(
            f"{owner}(...) received invalid volume backup_relpath "
            "in the snapshot manifest."
        )
    parsed = PurePosixPath(relpath)
    if parsed.is_absolute() or not parsed.parts:
        raise RuntimeError(
            f"{owner}(...) received invalid volume backup_relpath "
            "in the snapshot manifest."
        )
    if any(part in {"", ".", ".."} for part in parsed.parts):
        raise RuntimeError(
            f"{owner}(...) received invalid volume backup_relpath "
            "in the snapshot manifest."
        )
    root = snapshot_dir.resolve()
    snapshot_payload_dir = root.joinpath(*parsed.parts).resolve()
    if snapshot_payload_dir != root and root not in snapshot_payload_dir.parents:
        raise RuntimeError(
            f"{owner}(...) received invalid volume backup_relpath "
            "in the snapshot manifest."
        )
    return snapshot_payload_dir


def _require_snapshot_payload_dir_exists(
    *,
    owner: str,
    snapshot_payload_dir: Path,
) -> None:
    if not snapshot_payload_dir.exists():
        raise FileNotFoundError(
            f"{owner}(...) could not find Docker snapshot volume payload directory "
            f"'{snapshot_payload_dir}'."
        )
    if not snapshot_payload_dir.is_dir():
        raise FileNotFoundError(
            f"{owner}(...) expected Docker snapshot volume payload path "
            f"'{snapshot_payload_dir}' to be a directory."
        )


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
            f"{_CURRENT_SNAPSHOT_FORMAT} filesystem-backed snapshots; "
            "regenerate the snapshot."
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


def _run_cli(
    args: Sequence[str],
    *,
    owner: str,
    failure_level: JourneyLogLevel = "error",
) -> str:
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
        log_failure = _LOGGER.debug if failure_level == "debug" else _LOGGER.error
        log_failure(
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


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


__all__ = [
    "DockerComposeStack",
    "DockerContainerStatus",
    "DockerHttpCheck",
    "DockerLogMatch",
    "DockerLogMatcher",
    "restore_docker",
    "run_docker",
    "store_docker",
]
