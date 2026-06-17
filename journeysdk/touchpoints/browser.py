"""Official browser page-state touchpoint."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from time import sleep
from types import TracebackType
from typing import Any, Literal, TypedDict, cast

from journeysdk.logger import PrettyLine, PrettyStyle, get_logger, pretty_row
from journeysdk.rehydration import JourneyRestoreContext, JourneyStoreContext
from journeysdk.session import (
    _allocate_log_artifact,
    _allocate_browser_recording,
    _get_step_side_outputs,
    _register_step_exit_object,
    _register_step_side_output,
    _require_executing_step,
)
from journeysdk.executor import (
    _is_step_forced_interrupt_requested,
    _register_forced_step_interrupt_callback,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage
from playwright.sync_api import sync_playwright


class BrowserCookie(TypedDict, total=False):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: Literal["Strict", "Lax", "None"]


_STORAGE_SCRIPT = """
() => {
    const items = {};
    for (let index = 0; index < window.localStorage.length; index += 1) {
        const key = window.localStorage.key(index);
        if (key !== null) {
            items[key] = window.localStorage.getItem(key) ?? "";
        }
    }
    return items;
}
"""

_REHYDRATE_STORAGE_SCRIPT = """
(items) => {
    window.localStorage.clear();
    for (const [key, value] of Object.entries(items)) {
        window.localStorage.setItem(key, value);
    }
}
"""

_SUPPORTED_BROWSERS = {"chromium", "firefox", "webkit"}
_BROWSER_PAGE_SIDE_OUTPUT = "browser_page"
_BROWSER_INSTALL_LOCK = Lock()
_NAVIGATION_RETRY_ATTEMPTS = 5
_NAVIGATION_RETRY_DELAY_SECONDS = 0.25
_TRANSIENT_NAVIGATION_ERROR_FRAGMENTS = (
    "err_connection_reset",
    "err_connection_refused",
    "err_empty_response",
    "err_connection_closed",
    "connection reset",
    "connection refused",
    "empty response",
    "connection closed",
    "ns_error_net_reset",
    "ns_error_connection_refused",
    "ns_binding_aborted",
    "navigation interrupted",
)
_LOGGER = get_logger("browser")


def _browser_row(detail: object, *, style: PrettyStyle = "touchpoint") -> PrettyLine:
    return pretty_row("Browser", detail, indent=8, label_width=27, style=style)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _BrowserRecording:
    metadata: Any
    browser: str
    headless: bool
    initial_url: str
    trace_path: Path
    video_path: Path
    manifest_path: Path
    video_temp_dir: Path
    started_at: str = field(default_factory=_utc_timestamp)
    stopped_at: str | None = None
    final_url: str | None = None
    status: str = "running"
    trace_started: bool = False
    trace_saved: bool = False
    video_saved: bool = False
    errors: list[str] = field(default_factory=list)
    _video: object | None = None

    @property
    def sequence(self) -> int:
        return cast(int, getattr(self.metadata, "sequence"))

    @property
    def key(self) -> str:
        return cast(str, getattr(self.metadata, "key"))

    def start_trace(self, context: object) -> None:
        try:
            tracing = getattr(context, "tracing")
            start = getattr(tracing, "start")
            start(
                title=self._trace_title(),
                screenshots=True,
                snapshots=True,
                sources=True,
            )
            self.trace_started = True
            self.write_manifest()
            _LOGGER.info(
                "recording_start",
                "browser recording started",
                pretty=_browser_row(f"recording {self.key}"),
                **self._log_fields(),
            )
        except BaseException as exc:
            self._record_failure("trace_start", exc)
            self.write_manifest()
            self._log_failure(
                "recording_start_failure",
                "browser recording failed to start",
                exc,
            )

    def capture_video(self, page: PlaywrightPage) -> None:
        try:
            self._video = getattr(page, "_journey_test_video", None)
            if self._video is None:
                self._video = getattr(page, "video", None)
        except BaseException as exc:
            self._record_failure("video_capture", exc)

    def stop_trace(self, context: object | None) -> None:
        if not self.trace_started or context is None:
            return
        try:
            tracing = getattr(context, "tracing")
            stop = getattr(tracing, "stop")
            stop(path=self.trace_path)
            self.trace_saved = True
        except BaseException as exc:
            self._record_failure("trace_stop", exc)
            self._log_failure(
                "recording_trace_stop_failure",
                "browser recording trace failed to stop",
                exc,
            )

    def save_video(self) -> None:
        video = self._video
        if video is None:
            return
        try:
            save_as = getattr(video, "save_as")
            save_as(self.video_path)
            self.video_saved = True
        except BaseException as exc:
            self._record_failure("video_save", exc)
            self._log_failure(
                "recording_video_save_failure",
                "browser recording video failed to save",
                exc,
            )

    def finish(self, *, status: str, final_url: str | None) -> None:
        self.status = status
        self.final_url = final_url
        self.stopped_at = _utc_timestamp()
        self.write_manifest()
        _LOGGER.info(
            "recording_success" if not self.errors else "recording_partial",
            "browser recording finalized",
            pretty=_browser_row(
                (
                    f"recording {'saved' if not self.errors else 'partial'} "
                    f"{self.key} trace={self.trace_path} "
                    f"video={self.video_path if self.video_saved else '<not saved>'} "
                    f"manifest={self.manifest_path}"
                ),
                style="touchpoint" if not self.errors else "warning",
            ),
            status=self.status,
            errors=len(self.errors),
            trace_saved=self.trace_saved,
            video_saved=self.video_saved,
            **self._log_fields(),
        )
        self.cleanup()

    def abort(self, *, status: str, final_url: str | None, reason: str) -> None:
        self.status = status
        self.final_url = final_url
        self.stopped_at = _utc_timestamp()
        self.errors.append(reason)
        self.write_manifest()
        _LOGGER.warning(
            "recording_interrupted",
            "browser recording interrupted before it could be finalized",
            pretty=_browser_row(
                f"recording interrupted {self.key} manifest={self.manifest_path}",
                style="warning",
            ),
            status=self.status,
            reason=reason,
            **self._log_fields(),
        )
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.video_temp_dir, ignore_errors=True)

    def write_manifest(self) -> None:
        try:
            self.manifest_path.write_text(
                json.dumps(self._manifest_payload(), indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        except BaseException as exc:
            self._log_failure(
                "recording_manifest_failure",
                "browser recording manifest failed to write",
                exc,
            )

    def _record_failure(self, action: str, exc: BaseException) -> None:
        self.errors.append(f"{action}: {_format_exception(exc)}")

    def _log_failure(self, event: str, message: str, exc: BaseException) -> None:
        _LOGGER.warning(
            event,
            message,
            pretty=_browser_row(
                f"{message}: {_format_exception(exc)}",
                style="warning",
            ),
            error=_format_exception(exc),
            **self._log_fields(),
        )

    def _trace_title(self) -> str:
        return (
            f"Journey {getattr(self.metadata, 'case_id')} "
            f"{getattr(self.metadata, 'step_name')} "
            f"attempt {getattr(self.metadata, 'attempt')} "
            f"context {getattr(self.metadata, 'context_index')}"
        )

    def _log_fields(self) -> dict[str, object]:
        return {
            "recording_sequence": self.sequence,
            "recording_key": self.key,
            "recording_dir": str(self.manifest_path.parent),
            "trace_path": str(self.trace_path),
            "video_path": str(self.video_path),
            "manifest_path": str(self.manifest_path),
            "run_id": getattr(self.metadata, "run_id"),
            "case": getattr(self.metadata, "case_id"),
            "step": getattr(self.metadata, "step_name"),
            "attempt": getattr(self.metadata, "attempt"),
        }

    def _manifest_payload(self) -> dict[str, object]:
        return {
            "format": "journey.log_artifact",
            "version": 1,
            "kind": "browser_recording",
            "touchpoint": "browser",
            "source": "page",
            "content_type": "application/vnd.journey.browser-recording",
            "status": self.status,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "run_id": getattr(self.metadata, "run_id"),
            "sequence": self.sequence,
            "artifact_key": self.key,
            "recording_key": self.key,
            "journey_id": getattr(self.metadata, "journey_id"),
            "function_ref": getattr(self.metadata, "function_ref"),
            "case_id": getattr(self.metadata, "case_id"),
            "branch_env": getattr(self.metadata, "branch_env"),
            "step_id": getattr(self.metadata, "step_id"),
            "step_label": getattr(self.metadata, "step_label"),
            "step_name": getattr(self.metadata, "step_name"),
            "node_index": getattr(self.metadata, "node_index"),
            "attempt": getattr(self.metadata, "attempt"),
            "context_index": getattr(self.metadata, "context_index"),
            "browser": self.browser,
            "headless": self.headless,
            "path": str(self.trace_path),
            "initial_url": self.initial_url,
            "final_url": self.final_url,
            "trace_path": str(self.trace_path),
            "video_path": str(self.video_path),
            "trace_saved": self.trace_saved,
            "video_saved": self.video_saved,
            "errors": list(self.errors),
            "show_trace": f"playwright show-trace {self.trace_path}",
        }


@dataclass
class _BrowserEventLog:
    metadata: Any
    path: Path
    manifest_path: Path
    started_at: str = field(default_factory=_utc_timestamp)
    stopped_at: str | None = None
    status: str = "running"
    event_count: int = 0
    errors: list[str] = field(default_factory=list)

    def attach(self, page: PlaywrightPage) -> None:
        self.write_manifest()
        on = getattr(page, "on", None)
        if not callable(on):
            return
        on("console", self._on_console)
        on("pageerror", self._on_page_error)
        on("requestfailed", self._on_request_failed)
        on("response", self._on_response)

    def finish(self, *, status: str) -> None:
        self.status = status
        self.stopped_at = _utc_timestamp()
        self.write_manifest()

    def abort(self, *, status: str, reason: str) -> None:
        self.status = status
        self.stopped_at = _utc_timestamp()
        self.errors.append(reason)
        self.write_manifest()

    def write_manifest(self) -> None:
        try:
            self.manifest_path.write_text(
                json.dumps(self._manifest_payload(), indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        except BaseException as exc:
            _LOGGER.warning(
                "browser_event_log_manifest_failure",
                "browser event log manifest failed to write",
                pretty=False,
                error=_format_exception(exc),
            )

    def _write_event(self, event: str, **fields: object) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "time": _utc_timestamp(),
                "event": event,
                **fields,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(payload, ensure_ascii=True, default=str, separators=(",", ":"))
                    + "\n"
                )
            self.event_count += 1
        except BaseException as exc:
            self.errors.append(f"{event}: {_format_exception(exc)}")

    def _on_console(self, message: object) -> None:
        location = getattr(message, "location", None)
        self._write_event(
            "console",
            type=getattr(message, "type", None),
            text=getattr(message, "text", None),
            location=location if isinstance(location, dict) else None,
        )

    def _on_page_error(self, error: object) -> None:
        self._write_event("pageerror", error=str(error))

    def _on_request_failed(self, request: object) -> None:
        failure = None
        failure_value = getattr(request, "failure", None)
        if callable(failure_value):
            try:
                failure = failure_value()
            except BaseException:
                failure = None
        else:
            failure = failure_value
        self._write_event(
            "requestfailed",
            method=getattr(request, "method", None),
            url=getattr(request, "url", None),
            resource_type=getattr(request, "resource_type", None),
            failure=str(failure) if failure is not None else None,
        )

    def _on_response(self, response: object) -> None:
        request = getattr(response, "request", None)
        self._write_event(
            "response",
            status=getattr(response, "status", None),
            url=getattr(response, "url", None),
            method=getattr(request, "method", None) if request is not None else None,
        )

    def _manifest_payload(self) -> dict[str, object]:
        return {
            "format": "journey.log_artifact",
            "version": 1,
            "kind": getattr(self.metadata, "kind"),
            "touchpoint": getattr(self.metadata, "touchpoint"),
            "source": getattr(self.metadata, "source"),
            "content_type": getattr(self.metadata, "content_type"),
            "status": self.status,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "run_id": getattr(self.metadata, "run_id"),
            "sequence": getattr(self.metadata, "sequence"),
            "artifact_key": getattr(self.metadata, "key"),
            "journey_id": getattr(self.metadata, "journey_id"),
            "function_ref": getattr(self.metadata, "function_ref"),
            "case_id": getattr(self.metadata, "case_id"),
            "branch_env": getattr(self.metadata, "branch_env"),
            "step_id": getattr(self.metadata, "step_id"),
            "step_label": getattr(self.metadata, "step_label"),
            "step_name": getattr(self.metadata, "step_name"),
            "node_index": getattr(self.metadata, "node_index"),
            "attempt": getattr(self.metadata, "attempt"),
            "path": str(self.path),
            "recording_key": getattr(self.metadata, "recording_key"),
            "line_count": self.event_count,
            "byte_count": self.path.stat().st_size if self.path.exists() else 0,
            "errors": list(self.errors),
        }


def _prepare_browser_recording(
    *,
    browser: str,
    headless: bool,
    initial_url: str,
) -> _BrowserRecording | None:
    metadata = _allocate_browser_recording("open_page")
    if metadata is None:
        return None
    try:
        root = Path(getattr(metadata, "root"))
        root.mkdir(parents=True, exist_ok=True)
        stem = str(getattr(metadata, "stem"))
        video_temp_dir = Path(tempfile.mkdtemp(prefix="journey-browser-video."))
        return _BrowserRecording(
            metadata=metadata,
            browser=browser,
            headless=headless,
            initial_url=initial_url,
            trace_path=root / f"{stem}.trace.zip",
            video_path=root / f"{stem}.webm",
            manifest_path=root / f"{stem}.manifest.json",
            video_temp_dir=video_temp_dir,
        )
    except BaseException as exc:
        _LOGGER.warning(
            "recording_prepare_failure",
            "browser recording could not be prepared",
            pretty=_browser_row(
                f"browser recording could not be prepared: {_format_exception(exc)}",
                style="warning",
            ),
            error=_format_exception(exc),
        )
        return None


def _prepare_browser_event_log(
    *,
    recording_key: str | None,
) -> _BrowserEventLog | None:
    metadata = _allocate_log_artifact(
        "open_page",
        kind="browser_event_log",
        touchpoint="browser",
        source="page",
        suffix=".browser-events.jsonl",
        content_type="application/jsonl",
        recording_key=recording_key,
    )
    if metadata is None:
        return None
    try:
        return _BrowserEventLog(
            metadata=metadata,
            path=Path(getattr(metadata, "path")),
            manifest_path=Path(getattr(metadata, "manifest_path")),
        )
    except BaseException as exc:
        _LOGGER.warning(
            "browser_event_log_prepare_failure",
            "browser event log could not be prepared",
            pretty=False,
            error=_format_exception(exc),
        )
        return None


@dataclass(frozen=True)
class _PageSnapshot:
    url: str
    cookies: tuple[BrowserCookie, ...]
    local_storage: tuple[tuple[str, str], ...]

    @classmethod
    def from_url(cls, url: str) -> _PageSnapshot:
        return cls.from_payload(
            {
                "url": url,
                "cookies": [],
                "local_storage": {},
            }
        )

    @classmethod
    def from_live_page(cls, page: PlaywrightPage) -> _PageSnapshot:
        return cls.from_payload(
            {
                "url": page.url,
                "cookies": list(page.context.cookies()),
                "local_storage": page.evaluate(_STORAGE_SCRIPT),
            }
        )

    @classmethod
    def from_payload(cls, payload: object) -> _PageSnapshot:
        if not isinstance(payload, dict):
            raise TypeError("JourneyBrowserPage state expects a dictionary payload.")
        if set(payload) != {"url", "cookies", "local_storage"}:
            raise ValueError(
                "JourneyBrowserPage state expects exactly 'url', 'cookies', and 'local_storage'."
            )

        url = payload["url"]
        if not isinstance(url, str) or not url:
            raise TypeError("JourneyBrowserPage state url must be a non-empty string.")

        cookies = payload["cookies"]
        if not isinstance(cookies, (list, tuple)):
            raise TypeError(
                "JourneyBrowserPage state cookies must be a list of cookie objects."
            )
        normalized_cookies: list[BrowserCookie] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise TypeError(
                    "JourneyBrowserPage state cookies must contain only cookie dictionaries."
                )
            normalized_cookie: dict[str, object] = {}
            for key, value in cookie.items():
                if not isinstance(key, str):
                    raise TypeError(
                        "JourneyBrowserPage state cookie keys must be strings."
                    )
                normalized_cookie[key] = value
            normalized_cookies.append(cast(BrowserCookie, normalized_cookie))

        local_storage = payload["local_storage"]
        if not isinstance(local_storage, dict):
            raise TypeError(
                "JourneyBrowserPage state local_storage must be a dictionary of strings."
            )
        normalized_local_storage: dict[str, str] = {}
        for key, value in local_storage.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(
                    "JourneyBrowserPage state local_storage must contain only string keys and values."
                )
            normalized_local_storage[key] = value

        return cls(
            url=url,
            cookies=tuple(normalized_cookies),
            local_storage=tuple(sorted(normalized_local_storage.items())),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "url": self.url,
            "cookies": [dict(cookie) for cookie in self.cookies],
            "local_storage": self.local_storage_dict(),
        }

    def local_storage_dict(self) -> dict[str, str]:
        return dict(self.local_storage)


class JourneyBrowserPage(PlaywrightPage):
    """Browser page wrapper that can be saved and reopened by Journey."""

    def __init__(
        self,
        impl_obj: object | None = None,
        *,
        snapshot: _PageSnapshot | None = None,
    ) -> None:
        if impl_obj is None:
            if snapshot is None:
                raise TypeError(
                    "JourneyBrowserPage needs either a live browser page or saved state."
                )
            self._impl_obj = None
            self._loop = None
            self._dispatcher_fiber = None
            self._journey_step_closed = True
        else:
            super().__init__(impl_obj)
            self._journey_step_closed = False
        self._journey_snapshot = snapshot
        self._journey_context = None
        self._journey_browser = None
        self._journey_manager = None
        self._journey_exit_started = False
        self._journey_recording: _BrowserRecording | None = None
        self._journey_event_log: _BrowserEventLog | None = None
        self._journey_forced_interrupt_cleanup_started = False
        self._journey_forced_interrupt_unregister: Callable[[], None] | None = None

    @classmethod
    def _from_snapshot(cls, snapshot: _PageSnapshot) -> JourneyBrowserPage:
        return cls(snapshot=snapshot)

    @classmethod
    def _restore_pickle(cls, payload: object) -> JourneyBrowserPage:
        return cls._from_snapshot(_PageSnapshot.from_payload(payload))

    @property
    def url(self) -> str:
        if self._is_live:
            return cast(str, super().url)
        return self._snapshot_for_storage().url

    @property
    def _is_live(self) -> bool:
        return (
            not bool(getattr(self, "_journey_step_closed", True))
            and getattr(self, "_impl_obj", None) is not None
        )

    def __repr__(self) -> str:
        live = "live" if self._is_live else "saved"
        return f"JourneyBrowserPage(url={self.url!r}, state={live})"

    def _attach_live_page(
        self,
        page: PlaywrightPage,
        *,
        fallback_snapshot: _PageSnapshot,
    ) -> None:
        impl_obj = getattr(page, "_impl_obj", None)
        if impl_obj is None:
            raise TypeError("open_page(...) expected Playwright to return a sync Page.")
        PlaywrightPage.__init__(self, impl_obj)
        self._journey_snapshot = fallback_snapshot
        self._journey_step_closed = False

    def _set_step_resources(
        self,
        *,
        manager: object | None = None,
        browser: object | None = None,
        context: object | None = None,
        recording: _BrowserRecording | None = None,
        event_log: _BrowserEventLog | None = None,
    ) -> None:
        if manager is not None:
            self._journey_manager = manager
        if browser is not None:
            self._journey_browser = browser
        if context is not None:
            self._journey_context = context
        if recording is not None:
            self._journey_recording = recording
        if event_log is not None:
            self._journey_event_log = event_log
        self._ensure_forced_interrupt_callback()

    def _ensure_forced_interrupt_callback(self) -> None:
        if self._journey_forced_interrupt_unregister is not None:
            return
        self._journey_forced_interrupt_unregister = (
            _register_forced_step_interrupt_callback(
                "browser page",
                self._force_close_after_forced_interrupt,
            )
        )

    def _unregister_forced_interrupt_callback(self) -> None:
        unregister = self._journey_forced_interrupt_unregister
        self._journey_forced_interrupt_unregister = None
        if unregister is not None:
            unregister()

    def _force_close_after_forced_interrupt(self) -> None:
        self._journey_forced_interrupt_cleanup_started = True
        self._abort_recording(
            status="interrupted",
            reason="forced interrupt requested",
        )
        self._abort_event_log(
            status="interrupted",
            reason="forced interrupt requested",
        )
        _suppress_playwright_callback_future_noise(self._journey_manager)
        _kill_playwright_driver_process(self._journey_manager)
        self._mark_step_closed()

    def _is_forced_interrupt_cleanup_started(self) -> bool:
        return bool(getattr(self, "_journey_forced_interrupt_cleanup_started", False))

    def _snapshot_for_storage(self) -> _PageSnapshot:
        if self._is_live:
            self._journey_snapshot = _PageSnapshot.from_live_page(self)
        if self._journey_snapshot is None:
            raise RuntimeError("JourneyBrowserPage has no saved page state.")
        return self._journey_snapshot

    def _mark_step_closed(self) -> None:
        self._journey_step_closed = True

    def _abort_recording(self, *, status: str, reason: str) -> None:
        recording = self._journey_recording
        if recording is None:
            return
        self._journey_recording = None
        recording.abort(status=status, final_url=self._safe_recording_url(), reason=reason)

    def _abort_event_log(self, *, status: str, reason: str) -> None:
        event_log = self._journey_event_log
        if event_log is None:
            return
        self._journey_event_log = None
        event_log.abort(status=status, reason=reason)

    def _safe_recording_url(self) -> str | None:
        try:
            return self.url
        except BaseException:
            snapshot = getattr(self, "_journey_snapshot", None)
            if isinstance(snapshot, _PageSnapshot):
                return snapshot.url
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close browser resources owned by this step-scoped page."""

        self._close_step_resources(
            exc_type,
            exc,
            traceback,
            capture_snapshot=True,
        )

    def _close_after_failed_open(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close_step_resources(
            exc_type,
            exc,
            traceback,
            capture_snapshot=False,
        )

    def _close_step_resources(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        *,
        capture_snapshot: bool,
    ) -> None:
        if self._journey_exit_started:
            return
        self._journey_exit_started = True
        self._unregister_forced_interrupt_callback()
        cleanup_url = self._cleanup_url(prefer_live=capture_snapshot)
        was_live = self._is_live
        recording = self._journey_recording
        event_log = self._journey_event_log
        _LOGGER.debug(
            "page_cleanup_start",
            "cleaning up browser page resources",
            url=cleanup_url,
            live=was_live,
        )
        failures: list[BaseException] = []
        forced_interrupted = _is_step_forced_interrupt_requested()
        interrupted = (
            exc_type is not None and issubclass(exc_type, KeyboardInterrupt)
        ) or forced_interrupted

        if forced_interrupted:
            self._journey_forced_interrupt_cleanup_started = True
            self._abort_recording(
                status="interrupted",
                reason="forced interrupt requested",
            )
            self._abort_event_log(
                status="interrupted",
                reason="forced interrupt requested",
            )
            if self._is_live:
                self._mark_step_closed()
            _suppress_playwright_callback_future_noise(self._journey_manager)
            _kill_playwright_driver_process(self._journey_manager)
            _LOGGER.debug(
                "page_cleanup_forced_interrupt",
                "skipped normal browser cleanup after forced interrupt",
                url=cleanup_url,
                live=was_live,
            )
            return

        if recording is not None and self._is_live:
            recording.capture_video(self)

        if self._is_live:
            if capture_snapshot and not interrupted:
                try:
                    self._snapshot_for_storage()
                except BaseException as snapshot_exc:  # pragma: no cover - surfaced through executor
                    failures.append(snapshot_exc)
                finally:
                    self._mark_step_closed()
            else:
                self._mark_step_closed()

        if recording is not None:
            recording.stop_trace(self._journey_context)

        context_close_failed = False
        context_close = getattr(self._journey_context, "close", None)
        if callable(context_close):
            try:
                context_close()
            except BaseException as close_exc:  # pragma: no cover - environment dependent
                context_close_failed = True
                if interrupted:
                    _LOGGER.debug(
                        "page_cleanup_interrupt_close_failure",
                        "ignored browser cleanup failure during interrupt",
                        url=cleanup_url,
                        error=_format_exception(close_exc),
                    )
                else:
                    failures.append(close_exc)

        if recording is not None and not context_close_failed:
            recording.save_video()

        browser_close = getattr(self._journey_browser, "close", None)
        if callable(browser_close):
            try:
                browser_close()
            except BaseException as close_exc:  # pragma: no cover - environment dependent
                if interrupted:
                    _LOGGER.debug(
                        "page_cleanup_interrupt_close_failure",
                        "ignored browser cleanup failure during interrupt",
                        url=cleanup_url,
                        error=_format_exception(close_exc),
                    )
                else:
                    failures.append(close_exc)

        manager_exit = getattr(self._journey_manager, "__exit__", None)
        if callable(manager_exit):
            try:
                if interrupted:
                    _suppress_playwright_callback_future_noise(self._journey_manager)
                manager_exit(exc_type, exc, traceback)
            except BaseException as manager_exc:  # pragma: no cover - environment dependent
                if interrupted:
                    _LOGGER.debug(
                        "page_cleanup_interrupt_manager_failure",
                        "ignored browser manager cleanup failure during interrupt",
                        url=cleanup_url,
                        error=_format_exception(manager_exc),
                    )
                else:
                    failures.append(manager_exc)

        if recording is not None:
            recording_status = (
                "interrupted"
                if interrupted
                else "failed"
                if exc_type is not None or failures
                else "success"
            )
            recording.finish(
                status=recording_status,
                final_url=self._safe_recording_url() or cleanup_url,
            )
            self._journey_recording = None

        if event_log is not None:
            event_log_status = (
                "interrupted"
                if interrupted
                else "failed"
                if exc_type is not None or failures
                else "success"
            )
            event_log.finish(status=event_log_status)
            self._journey_event_log = None

        if failures:
            _LOGGER.error(
                "page_cleanup_failure",
                "browser page cleanup failed",
                url=cleanup_url,
                failures=len(failures),
            )
            raise RuntimeError(_cleanup_failure_message(failures))
        _LOGGER.debug(
            "page_cleanup_success",
            "browser page resources cleaned up",
            url=cleanup_url,
        )

    def _cleanup_url(self, *, prefer_live: bool) -> str:
        if prefer_live:
            try:
                return self.url
            except Exception:  # pragma: no cover - environment dependent
                pass
        if self._journey_snapshot is not None:
            return self._journey_snapshot.url
        return "<unknown>"

    def __store__(self, context: JourneyStoreContext) -> object:
        """Store the current page state for Journey replay."""

        return self._snapshot_for_storage().to_payload()

    def __dev__(self, context: object) -> object:
        """Contribute browser page guidance when ``journey dev`` pauses."""

        from journeysdk.dev import browser_dev_contribution

        return browser_dev_contribution(self, context=context)

    @classmethod
    def __restore__(
        cls,
        payload: object,
        context: JourneyRestoreContext,
    ) -> JourneyBrowserPage:
        """Restore a saved page handle for explicit reopening in a later step."""

        return cls._from_snapshot(_PageSnapshot.from_payload(payload))

    def __reduce__(self) -> tuple[object, tuple[object]]:
        return (type(self)._restore_pickle, (self._snapshot_for_storage().to_payload(),))


def open_page(
    page_or_url: JourneyBrowserPage | str,
    *,
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
    headless: bool = True,
) -> JourneyBrowserPage:
    """Open a fresh browser page from a URL or saved Journey page."""

    if not isinstance(page_or_url, (JourneyBrowserPage, str)):
        raise TypeError("open_page(...) expects a URL string or JourneyBrowserPage.")
    _require_executing_step("open_page")
    if browser not in _SUPPORTED_BROWSERS:
        raise ValueError(
            "open_page(..., browser=...) expects 'chromium', 'firefox', or 'webkit'."
        )

    snapshot = _snapshot_from_open_page_input(page_or_url)
    local_storage = snapshot.local_storage_dict()
    page = JourneyBrowserPage._from_snapshot(snapshot)
    _LOGGER.info(
        "open_page_start",
        "opening browser page",
        pretty=_browser_row(
            f"opening {browser} {snapshot.url}"
            + (" headless=false" if headless is False else "")
        ),
        url=snapshot.url,
        browser=browser,
        headless=headless,
    )

    try:
        manager = sync_playwright()
        page._set_step_resources(manager=manager)
        playwright = manager.__enter__()
        browser_type = getattr(playwright, browser)
        launched_browser = _launch_browser_with_auto_install(
            browser_type,
            browser=browser,
            headless=headless,
        )
        page._set_step_resources(browser=launched_browser)
        recording = _prepare_browser_recording(
            browser=browser,
            headless=headless,
            initial_url=snapshot.url,
        )
        if recording is not None:
            page._set_step_resources(recording=recording)
        event_log = _prepare_browser_event_log(
            recording_key=recording.key if recording is not None else None,
        )
        if event_log is not None:
            page._set_step_resources(event_log=event_log)
        context_kwargs: dict[str, object] = {}
        if recording is not None:
            context_kwargs["record_video_dir"] = recording.video_temp_dir
        context = launched_browser.new_context(**context_kwargs)
        page._set_step_resources(context=context)
        if recording is not None:
            recording.start_trace(context)
        if snapshot.cookies:
            context.add_cookies([dict(cookie) for cookie in snapshot.cookies])
        native_page = context.new_page()
        if event_log is not None:
            event_log.attach(cast(PlaywrightPage, native_page))
        page._attach_live_page(
            cast(PlaywrightPage, native_page),
            fallback_snapshot=snapshot,
        )
        _retry_navigation(lambda: page.goto(snapshot.url, wait_until="load"))
        if local_storage:
            page.evaluate(_REHYDRATE_STORAGE_SCRIPT, local_storage)
            _retry_navigation(lambda: page.reload(wait_until="load"))
        _register_step_exit_object("open_page", page)
        _register_step_side_output("open_page", _BROWSER_PAGE_SIDE_OUTPUT, page)
        _LOGGER.info(
            "open_page_success",
            "browser page opened",
            pretty=_browser_row(f"opened {browser} {page.url}"),
            url=page.url,
            browser=browser,
        )
        return page
    except KeyboardInterrupt as exc:
        _LOGGER.warning(
            "open_page_interrupted",
            "browser page opening was interrupted",
            pretty=False,
            url=snapshot.url,
            browser=browser,
        )
        _close_open_page_after_interrupt(page, exc)
        raise
    except Exception as exc:
        if _is_forced_interrupt_playwright_error(page, exc):
            _LOGGER.warning(
                "open_page_interrupted_after_signal",
                "browser page aborted after Ctrl-C",
                pretty=False,
                url=snapshot.url,
                browser=browser,
                error=_format_exception(exc),
            )
            interrupt = KeyboardInterrupt()
            _close_open_page_after_interrupt(page, interrupt)
            raise interrupt from exc
        _LOGGER.error(
            "open_page_failure",
            "failed to open browser page",
            pretty=f"Browser failed to open {browser} {snapshot.url}: {_format_exception(exc)}",
            url=snapshot.url,
            browser=browser,
            error=_format_exception(exc),
        )
        try:
            page._close_after_failed_open(type(exc), exc, exc.__traceback__)
        except BaseException as cleanup_exc:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(str(cleanup_exc))
        raise
    except BaseException as exc:
        try:
            page.__exit__(type(exc), exc, exc.__traceback__)
        except BaseException as cleanup_exc:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(str(cleanup_exc))
        raise


def _snapshot_from_open_page_input(
    page_or_url: JourneyBrowserPage | str,
) -> _PageSnapshot:
    if isinstance(page_or_url, JourneyBrowserPage):
        return page_or_url._snapshot_for_storage()
    return _PageSnapshot.from_url(page_or_url)


def ensure_browser_installed(
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
) -> None:
    """Install the requested browser runtime in the current environment if needed."""

    if browser not in _SUPPORTED_BROWSERS:
        raise ValueError(
            "ensure_browser_installed(..., browser=...) expects "
            "'chromium', 'firefox', or 'webkit'."
        )

    _LOGGER.info(
        "browser_install_check_start",
        "checking browser installation",
        pretty=_browser_row(f"checking {browser} installation"),
        browser=browser,
    )
    with sync_playwright() as playwright:
        _ensure_browser_type_installed(
            getattr(playwright, browser),
            browser=browser,
        )
    _LOGGER.info(
        "browser_install_check_success",
        "browser installation is available",
        pretty=_browser_row(f"{browser} installation available"),
        browser=browser,
    )


def _launch_browser_with_auto_install(
    browser_type: object,
    *,
    browser: Literal["chromium", "firefox", "webkit"],
    headless: bool,
) -> object:
    attempted_install = _ensure_browser_type_installed(
        browser_type,
        browser=browser,
    )
    launch = getattr(browser_type, "launch")
    try:
        return launch(headless=headless, handle_sigint=False)
    except BaseException as exc:
        if attempted_install or not _looks_like_missing_browser_error(exc):
            raise
        _install_playwright_browser(browser)
        return launch(headless=headless, handle_sigint=False)


def browser_pages_from_step_result(step_result: object) -> tuple[JourneyBrowserPage, ...]:
    """Return browser pages opened by the step that produced ``step_result``."""

    outputs = _get_step_side_outputs(
        "browser_pages_from_step_result",
        step_result,
        _BROWSER_PAGE_SIDE_OUTPUT,
    )
    pages = tuple(
        output for output in outputs if isinstance(output, JourneyBrowserPage)
    )
    if not pages:
        raise TypeError(
            "The selected step did not return or open a JourneyBrowserPage. "
            "Use a step that calls open_page(...) or returns JourneyBrowserPage."
        )
    return pages


def browser_page_from_step_result(
    step_result: object,
    *,
    index: int = -1,
) -> JourneyBrowserPage:
    """Return one browser page opened by the step that produced ``step_result``."""

    pages = browser_pages_from_step_result(step_result)
    try:
        return pages[index]
    except IndexError as exc:
        raise IndexError(
            f"Step result has {len(pages)} browser page side output(s); index {index} is out of range."
        ) from exc


def _close_open_page_after_interrupt(
    page: JourneyBrowserPage,
    exc: KeyboardInterrupt,
) -> None:
    try:
        page.__exit__(KeyboardInterrupt, exc, exc.__traceback__)
    except BaseException as cleanup_exc:
        _LOGGER.debug(
            "open_page_interrupt_cleanup_failure",
            "ignored browser cleanup failure during interrupt",
            error=_format_exception(cleanup_exc),
        )


def _is_forced_interrupt_playwright_error(
    page: JourneyBrowserPage,
    exc: BaseException,
) -> bool:
    """Classify only Playwright errors caused by Journey-owned forced cleanup."""

    return page._is_forced_interrupt_cleanup_started() and isinstance(
        exc,
        PlaywrightError,
    )


def _suppress_playwright_callback_future_noise(manager: object) -> None:
    connection = getattr(manager, "_connection", None)
    callbacks = getattr(connection, "_callbacks", None)
    if not isinstance(callbacks, dict):
        return
    for callback in tuple(callbacks.values()):
        future = getattr(callback, "future", None)
        add_done_callback = getattr(future, "add_done_callback", None)
        if not callable(add_done_callback):
            continue
        add_done_callback(_retrieve_future_exception)


def _retrieve_future_exception(future: object) -> None:
    exception = getattr(future, "exception", None)
    if not callable(exception):
        return
    try:
        exception()
    except BaseException:
        return


def _kill_playwright_driver_process(manager: object) -> None:
    connection = getattr(manager, "_connection", None)
    transport = getattr(connection, "_transport", None)
    process = getattr(transport, "_proc", None)
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return
    returncode = getattr(process, "returncode", None)
    if returncode is not None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        _LOGGER.debug(
            "playwright_driver_forced_interrupt_kill",
            "sent SIGTERM to Playwright driver after forced interrupt",
            pid=pid,
        )
    except ProcessLookupError:
        return
    except BaseException as exc:  # pragma: no cover - platform dependent
        _LOGGER.debug(
            "playwright_driver_forced_interrupt_kill_failure",
            "failed to terminate Playwright driver after forced interrupt",
            pid=pid,
            error=_format_exception(exc),
        )


def _ensure_browser_type_installed(
    browser_type: object,
    *,
    browser: Literal["chromium", "firefox", "webkit"],
) -> bool:
    executable_path = _browser_executable_path(browser_type)
    if executable_path is None or executable_path.exists():
        _LOGGER.debug(
            "browser_install_check_skip",
            "browser executable is already available",
            browser=browser,
            executable_path=executable_path,
        )
        return False
    _install_playwright_browser(
        browser,
        executable_path=executable_path,
    )
    return True


def _browser_executable_path(browser_type: object) -> Path | None:
    executable_path = getattr(browser_type, "executable_path", None)
    if not isinstance(executable_path, str) or not executable_path:
        return None
    return Path(executable_path)


def _install_playwright_browser(
    browser: Literal["chromium", "firefox", "webkit"],
    *,
    executable_path: Path | None = None,
) -> None:
    command = [sys.executable, "-m", "playwright", "install", browser]
    with _BROWSER_INSTALL_LOCK:
        if executable_path is not None and executable_path.exists():
            return
        _LOGGER.info(
            "browser_install_start",
            "installing browser runtime",
            pretty=_browser_row(f"installing {browser}"),
            browser=browser,
            command=" ".join(command),
            executable_path=executable_path,
        )
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            _LOGGER.error(
                "browser_install_failure",
                "browser installation failed",
                pretty=f"Browser failed to install {browser}",
                browser=browser,
                returncode=result.returncode,
            )
            raise RuntimeError(
                f"Journey SDK could not automatically install the Playwright {browser!r} browser. "
                "Retry after restoring network access, or run the Playwright installer manually "
                f"in the same environment: {' '.join(command)}"
            )
        if executable_path is not None and not executable_path.exists():
            _LOGGER.error(
                "browser_install_missing_executable",
                "browser install completed but executable is missing",
                pretty=f"Browser installed {browser}, but the executable is missing",
                browser=browser,
                executable_path=executable_path,
            )
            raise RuntimeError(
                f"Journey SDK installed the Playwright {browser!r} browser, "
                f"but the executable is still missing at {executable_path}."
            )
        _LOGGER.info(
            "browser_install_success",
            "browser installed",
            pretty=_browser_row(f"installed {browser}"),
            browser=browser,
            executable_path=executable_path,
        )


def _looks_like_missing_browser_error(exc: BaseException) -> bool:
    message = str(exc)
    return (
        "Executable doesn't exist" in message
        or "Please run the following command to download new browsers" in message
    )


def _retry_navigation(action: Callable[[], object]) -> object:
    for attempt in range(1, _NAVIGATION_RETRY_ATTEMPTS + 1):
        try:
            return action()
        except Exception as exc:
            if (
                attempt == _NAVIGATION_RETRY_ATTEMPTS
                or not _looks_like_transient_navigation_error(exc)
            ):
                raise
            _LOGGER.debug(
                "open_page_navigation_retry",
                "retrying transient browser navigation failure",
                attempt=attempt,
                max_attempts=_NAVIGATION_RETRY_ATTEMPTS,
                error=_format_exception(exc),
            )
            sleep(_NAVIGATION_RETRY_DELAY_SECONDS)
    raise AssertionError("_retry_navigation loop should always return or raise.")


def _looks_like_transient_navigation_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in _TRANSIENT_NAVIGATION_ERROR_FRAGMENTS
    )


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _cleanup_failure_message(failures: list[BaseException]) -> str:
    if len(failures) == 1:
        failure = failures[0]
        return (
            "browser page cleanup failed: "
            f"{type(failure).__name__}: {failure}"
        )
    joined = "; ".join(
        f"{type(failure).__name__}: {failure}"
        for failure in failures
    )
    return f"{len(failures)} browser cleanup actions failed: {joined}"


__all__ = [
    "BrowserCookie",
    "JourneyBrowserPage",
    "browser_page_from_step_result",
    "browser_pages_from_step_result",
    "ensure_browser_installed",
    "open_page",
]
