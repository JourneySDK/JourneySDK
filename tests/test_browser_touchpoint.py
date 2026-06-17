from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import MethodType

import journeysdk as journey_sdk
import pytest
from journeysdk.errors import CallableExecutionError, InvalidBranchUsageError

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

from journeysdk import executor as journey_executor
from journeysdk.logger import configure_logging
from journeysdk.touchpoints import browser as journey_browser


def _browser_recording_root() -> Path:
    return Path(__file__).parent / ".journey" / "logs"


def _state_payload(
    *,
    url: str = "http://example.test/dashboard",
    cookies: list[dict[str, object]] | None = None,
    local_storage: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "url": url,
        "cookies": cookies or [],
        "local_storage": local_storage or {},
    }


class _FakeNativePage:
    def __init__(self, context: object) -> None:
        self.context = context


class _FakePageImpl:
    def __init__(self, page: journey_browser.JourneyBrowserPage) -> None:
        self._page = page

    @property
    def url(self) -> str:
        return self._page._fake_url


class _FakeTracing:
    def __init__(self, events: list[object], *, record_events: bool) -> None:
        self._events = events
        self._record_events = record_events
        self.started = False

    def start(
        self,
        *,
        title: str,
        screenshots: bool,
        snapshots: bool,
        sources: bool,
    ) -> None:
        self.started = True
        if self._record_events:
            self._events.append(
                ("trace_start", title, screenshots, snapshots, sources)
            )

    def stop(self, *, path: Path) -> None:
        if self._record_events:
            self._events.append(("trace_stop", Path(path).name))
        Path(path).write_text("fake trace", encoding="utf-8")


class _FakeVideo:
    def __init__(self, events: list[object], *, record_events: bool) -> None:
        self._events = events
        self._record_events = record_events

    def save_as(self, path: Path) -> None:
        if self._record_events:
            self._events.append(("video_save", Path(path).name))
        Path(path).write_text("fake video", encoding="utf-8")


def _attach_fake_live_page(
    page: journey_browser.JourneyBrowserPage,
    native_page: object,
    *,
    events: list[object],
    fallback_snapshot: object,
    initial_url: str = "about:blank",
    goto_errors: list[BaseException] | None = None,
    reload_errors: list[BaseException] | None = None,
    fail_goto: BaseException | None = None,
    force_cleanup_before_goto_failure: bool = False,
) -> None:
    context = getattr(native_page, "context")
    pending_goto_errors = list(goto_errors or [])
    pending_reload_errors = list(reload_errors or [])
    page._journey_snapshot = fallback_snapshot
    page._journey_step_closed = False
    page._fake_context = context
    page._fake_url = initial_url
    page._fake_local_storage = {}
    page._impl_obj = _FakePageImpl(page)
    page._journey_test_video = getattr(context, "video", None)

    def goto(self, url: str, *, wait_until: str) -> None:
        if fail_goto is not None:
            events.append(("goto", url, wait_until))
            if force_cleanup_before_goto_failure:
                self._force_close_after_forced_interrupt()
            raise fail_goto
        events.append(("goto", url, wait_until))
        if pending_goto_errors:
            raise pending_goto_errors.pop(0)
        self._fake_url = url

    def evaluate(
        self,
        script: str,
        items: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        if items is None:
            assert "window.localStorage" in script
            events.append(("capture_storage", self._fake_url))
            return dict(self._fake_local_storage)
        assert "window.localStorage.clear" in script
        self._fake_local_storage = dict(items)
        events.append(("evaluate", dict(items)))
        return None

    def reload(self, *, wait_until: str) -> None:
        events.append(("reload", wait_until))
        if pending_reload_errors:
            raise pending_reload_errors.pop(0)

    def snapshot_for_storage(self):
        if not self._is_live and self._journey_snapshot is not None:
            return self._journey_snapshot
        events.append(("capture_state", self._fake_url))
        self._journey_snapshot = journey_browser._PageSnapshot.from_payload(
            _state_payload(
                url=self._fake_url,
                cookies=self._fake_context.cookies(),
                local_storage=self._fake_local_storage,
            )
        )
        return self._journey_snapshot

    page.goto = MethodType(goto, page)
    page.evaluate = MethodType(evaluate, page)
    page.reload = MethodType(reload, page)
    page._snapshot_for_storage = MethodType(snapshot_for_storage, page)


class _FakeContext:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_close: BaseException | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._fail_close = fail_close
        self.page = _FakeNativePage(self)
        self._cookies: list[dict[str, object]] = []
        self.tracing = _FakeTracing(
            events,
            record_events=record_recording_events,
        )
        self.video: _FakeVideo | None = None
        self._record_recording_events = record_recording_events

    def configure_recording(self, *, record_video_dir: object | None = None) -> None:
        if record_video_dir is None:
            self.video = None
            return
        if self._record_recording_events:
            self._events.append(("record_video_dir", Path(record_video_dir).name))
        self.video = _FakeVideo(
            self._events,
            record_events=self._record_recording_events,
        )

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self._cookies = list(cookies)
        self._events.append(("add_cookies", list(cookies)))

    def new_page(self) -> _FakeNativePage:
        self._events.append("new_page")
        if self._fail_new_page:
            raise RuntimeError("new page failed")
        return self.page

    def cookies(self) -> list[dict[str, object]]:
        return list(self._cookies)

    def close(self) -> None:
        self._events.append("context_close")
        if self._fail_close is not None:
            raise self._fail_close


class _FakeBrowser:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_close: BaseException | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_close = fail_close
        self.context = _FakeContext(
            events,
            fail_new_page=fail_new_page,
            fail_close=fail_context_close,
            record_recording_events=record_recording_events,
        )

    def new_context(self, **kwargs: object) -> _FakeContext:
        self._events.append("new_context")
        self.context.configure_recording(
            record_video_dir=kwargs.get("record_video_dir"),
        )
        return self.context

    def close(self) -> None:
        self._events.append("browser_close")
        if self._fail_close is not None:
            raise self._fail_close


class _FakeBrowserType:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_browser_close: BaseException | None = None,
        executable_path: str | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._fail_context_close = fail_context_close
        self._fail_browser_close = fail_browser_close
        self.executable_path = executable_path
        self._record_recording_events = record_recording_events

    def launch(self, *, headless: bool, handle_sigint: bool) -> _FakeBrowser:
        self._events.append(("launch", headless, handle_sigint))
        return _FakeBrowser(
            self._events,
            fail_new_page=self._fail_new_page,
            fail_context_close=self._fail_context_close,
            fail_close=self._fail_browser_close,
            record_recording_events=self._record_recording_events,
        )


class _FakePlaywright:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_browser_close: BaseException | None = None,
        executable_path: str | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self.chromium = _FakeBrowserType(
            events,
            fail_new_page=fail_new_page,
            fail_context_close=fail_context_close,
            fail_browser_close=fail_browser_close,
            executable_path=executable_path,
            record_recording_events=record_recording_events,
        )


class _FakeManager:
    def __init__(
        self,
        events: list[object],
        *,
        fail_new_page: bool = False,
        fail_context_close: BaseException | None = None,
        fail_browser_close: BaseException | None = None,
        executable_path: str | None = None,
        record_recording_events: bool = False,
    ) -> None:
        self._events = events
        self._fail_new_page = fail_new_page
        self._fail_context_close = fail_context_close
        self._fail_browser_close = fail_browser_close
        self._executable_path = executable_path
        self._record_recording_events = record_recording_events

    def __enter__(self) -> _FakePlaywright:
        self._events.append("playwright_enter")
        return _FakePlaywright(
            self._events,
            fail_new_page=self._fail_new_page,
            fail_context_close=self._fail_context_close,
            fail_browser_close=self._fail_browser_close,
            executable_path=self._executable_path,
            record_recording_events=self._record_recording_events,
        )

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._events.append("playwright_exit")
        return False


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    *,
    fail_new_page: bool = False,
    fail_goto: BaseException | None = None,
    force_cleanup_before_goto_failure: bool = False,
    fail_context_close: BaseException | None = None,
    fail_browser_close: BaseException | None = None,
    executable_path: str | None = None,
    goto_errors: list[BaseException] | None = None,
    reload_errors: list[BaseException] | None = None,
    record_recording_events: bool = False,
) -> None:
    def attach_live_page(
        self: journey_browser.JourneyBrowserPage,
        native_page: object,
        *,
        fallback_snapshot: object,
    ) -> None:
        _attach_fake_live_page(
            self,
            native_page,
            events=events,
            fallback_snapshot=fallback_snapshot,
            goto_errors=goto_errors,
            reload_errors=reload_errors,
            fail_goto=fail_goto,
            force_cleanup_before_goto_failure=force_cleanup_before_goto_failure,
        )

    monkeypatch.setattr(
        journey_browser,
        "sync_playwright",
        lambda: _FakeManager(
            events,
            fail_new_page=fail_new_page,
            fail_context_close=fail_context_close,
            fail_browser_close=fail_browser_close,
            executable_path=executable_path,
            record_recording_events=record_recording_events,
        ),
    )
    monkeypatch.setattr(
        journey_browser.JourneyBrowserPage,
        "_attach_live_page",
        attach_live_page,
    )




def test_journey_browser_page_round_trips_rehydration_payload(tmp_path: Path):
    assert issubclass(journey_browser.JourneyBrowserPage, PlaywrightPage)

    state = _state_payload(
        cookies=[
            {
                "name": "journey_session",
                "value": "demo-session",
                "domain": "example.test",
                "path": "/",
            }
        ],
        local_storage={
            "journey_session_token": "demo-token",
        },
    )
    page = journey_browser.JourneyBrowserPage.__restore__(
        state,
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    payload = page.__store__(
        journey_sdk.JourneyStoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        )
    )
    restored = journey_browser.JourneyBrowserPage.__restore__(
        payload,
        journey_sdk.JourneyRestoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    assert isinstance(payload, dict)
    assert isinstance(restored, journey_browser.JourneyBrowserPage)
    assert restored.url == "http://example.test/dashboard"
    assert restored.__store__(
        journey_sdk.JourneyStoreContext(
            artifact_root=tmp_path,
            boundary_kind="binding",
            boundary_id="step:n_1",
        )
    ) == state


def test_open_page_opens_url_string_and_cleans_returned_page(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def open_login() -> journey_browser.JourneyBrowserPage:
        page = journey_browser.open_page("http://example.test/login")
        assert isinstance(page, journey_browser.JourneyBrowserPage)
        events.append("inside")
        return page

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "inside",
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]
    log_output = capsys.readouterr().out
    assert "Browser" in log_output
    assert "opening chromium http://example.test/login" in log_output
    assert "opened chromium http://example.test/login" in log_output


def test_open_page_cleans_unreturned_page_at_step_exit(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def assert_login() -> bool:
        page = journey_browser.open_page("http://example.test/login")
        assert isinstance(page, journey_browser.JourneyBrowserPage)
        events.append("inside")
        return True

    def journey():
        journey_sdk.step(assert_login)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "inside",
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_side_output_recovers_from_non_page_step_result_after_branch_replay(
    tmp_path,
    monkeypatch,
):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def prepare_workspace() -> dict[str, str]:
        page = journey_browser.open_page("http://example.test/setup")
        page.goto("http://example.test/checkout", wait_until="load")
        return {"workspace_id": "w-1"}

    def recover_first_path(workspace: dict[str, str]) -> bool:
        page = journey_browser.browser_page_from_step_result(workspace)
        events.append(("first_path", page.url))
        return True

    def recover_second_path(workspace: dict[str, str]) -> bool:
        page = journey_browser.browser_page_from_step_result(workspace)
        events.append(("second_path", page.url))
        return True

    def journey():
        workspace = journey_sdk.step(prepare_workspace)
        if journey_sdk.branch(replay_from=workspace):
            journey_sdk.step(recover_first_path, workspace)
        elif journey_sdk.branch(replay_from=workspace):
            journey_sdk.step(recover_second_path, workspace)

    report = journey_sdk.execute(journey, state=tmp_path / "journey.state")

    assert len(report.case_reports) == 2
    assert events.count(("goto", "http://example.test/setup", "load")) == 1
    assert ("capture_state", "http://example.test/checkout") in events
    assert ("first_path", "http://example.test/checkout") in events
    assert ("second_path", "http://example.test/checkout") in events


def test_open_page_cleans_unreturned_page_when_step_fails(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def assert_login() -> bool:
        journey_browser.open_page("http://example.test/login")
        raise RuntimeError("assertion failed")

    def journey():
        journey_sdk.step(assert_login)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "assertion failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_retries_transient_initial_navigation_failure(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        goto_errors=[
            RuntimeError("JourneyBrowserPage.goto: net::ERR_CONNECTION_RESET"),
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        ("sleep", journey_browser._NAVIGATION_RETRY_DELAY_SECONDS),
        ("goto", "http://example.test/login", "load"),
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_does_not_retry_non_transient_navigation_failure(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        goto_errors=[
            RuntimeError("JourneyBrowserPage.goto: certificate verify failed"),
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "certificate verify failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_repeated_transient_navigation_failure_skips_snapshot_cleanup(
    monkeypatch,
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        goto_errors=[
            RuntimeError("JourneyBrowserPage.goto: net::ERR_CONNECTION_RESET")
            for _ in range(journey_browser._NAVIGATION_RETRY_ATTEMPTS)
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    message = str(exc_info.value)
    assert "ERR_CONNECTION_RESET" in message
    assert "browser page cleanup failed" not in message
    assert "localStorage" not in message
    assert ("capture_state", "about:blank") not in events
    assert not any(event == ("capture_storage", "about:blank") for event in events)
    assert events.count(("goto", "http://example.test/login", "load")) == (
        journey_browser._NAVIGATION_RETRY_ATTEMPTS
    )
    assert events[-3:] == ["context_close", "browser_close", "playwright_exit"]


def test_open_page_retries_transient_reload_failure_after_local_storage(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        reload_errors=[
            RuntimeError("JourneyBrowserPage.reload: net::ERR_EMPTY_RESPONSE"),
        ],
    )
    monkeypatch.setattr(
        journey_browser,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )
    saved_page = journey_browser.JourneyBrowserPage.__restore__(
        _state_payload(local_storage={"journey_session_token": "demo-token"}),
        journey_sdk.JourneyRestoreContext(
            artifact_root=Path("."),
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    def open_dashboard() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page(saved_page)

    def journey():
        journey_sdk.step(open_dashboard)

    journey_sdk.execute(journey)

    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/dashboard", "load"),
        ("evaluate", {"journey_session_token": "demo-token"}),
        ("reload", "load"),
        ("sleep", journey_browser._NAVIGATION_RETRY_DELAY_SECONDS),
        ("reload", "load"),
        ("capture_state", "http://example.test/dashboard"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_rehydrates_in_expected_order_and_cleans_nested_page(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    saved_page = journey_browser.JourneyBrowserPage.__restore__(
        _state_payload(
            cookies=[
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
            local_storage={"journey_session_token": "demo-token"},
        ),
        journey_sdk.JourneyRestoreContext(
            artifact_root=Path("."),
            boundary_kind="binding",
            boundary_id="step:n_1",
        ),
    )

    def open_dashboard() -> dict[str, object]:
        page = journey_browser.open_page(saved_page, headless=False)
        assert isinstance(page, journey_browser.JourneyBrowserPage)
        events.append("inside")
        return {"page": page}

    def journey():
        journey_sdk.step(open_dashboard)

    journey_sdk.execute(journey, no_state=True)

    assert events == [
        "playwright_enter",
        ("launch", False, False),
        "new_context",
        (
            "add_cookies",
            [
                {
                    "name": "journey_session",
                    "value": "demo-session",
                    "domain": "example.test",
                    "path": "/",
                }
            ],
        ),
        "new_page",
        ("goto", "http://example.test/dashboard", "load"),
        ("evaluate", {"journey_session_token": "demo-token"}),
        ("reload", "load"),
        "inside",
        ("capture_state", "http://example.test/dashboard"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_installs_missing_browser_automatically(monkeypatch, tmp_path: Path):
    events: list[object] = []
    executable_path = tmp_path / "ms-playwright" / "chromium"
    _install_fake_playwright(
        monkeypatch,
        events,
        executable_path=str(executable_path),
    )
    install_commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        install_commands.append(command)
        executable_path.parent.mkdir(parents=True, exist_ok=True)
        executable_path.write_text("installed", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(journey_browser.subprocess, "run", fake_run)

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True)

    assert install_commands == [
        [sys.executable, "-m", "playwright", "install", "chromium"]
    ]
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        ("capture_state", "http://example.test/login"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_cleans_partial_allocations_on_failure(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events, fail_new_page=True)

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    assert "new page failed" in str(exc_info.value)
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_records_trace_video_and_manifest_by_default(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    configure_logging("info", output_format="jsonl")
    try:
        journey_sdk.execute(journey, no_state=True)
    finally:
        configure_logging("info", output_format="pretty")

    root = _browser_recording_root()
    manifests = [
        path
        for path in sorted(root.glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    traces = sorted(root.glob("*.trace.zip"))
    videos = sorted(root.glob("*.webm"))

    assert len(manifests) == 1
    assert len(traces) == 1
    assert len(videos) == 1
    assert manifests[0].name.startswith(
        "0001-case_1-open_login-attempt-1-context-1-run-"
    )
    assert traces[0].stem.startswith("0001-case_1-open_login-attempt-1-context-1-run-")
    assert videos[0].stem.startswith("0001-case_1-open_login-attempt-1-context-1-run-")

    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["sequence"] == 1
    assert payload["case_id"] == "case_1"
    assert payload["step_name"] == "open_login"
    assert payload["attempt"] == 1
    assert payload["context_index"] == 1
    assert payload["initial_url"] == "http://example.test/login"
    assert payload["final_url"] == "http://example.test/login"
    assert payload["trace_path"] == str(traces[0])
    assert payload["video_path"] == str(videos[0])
    assert payload["trace_saved"] is True
    assert payload["video_saved"] is True
    assert payload["show_trace"] == f"playwright show-trace {traces[0]}"

    recording_events = [event[0] for event in events if isinstance(event, tuple)]
    assert "trace_start" in recording_events
    assert "trace_stop" in recording_events
    assert "video_save" in recording_events
    video_save_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "video_save"
    )
    assert events.index("context_close") < video_save_index
    assert video_save_index < events.index("browser_close")
    assert video_save_index < events.index("playwright_exit")

    log_records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    recording_logs = [
        record
        for record in log_records
        if record["component"] == "browser"
        and record["event"] == "recording_success"
    ]
    assert len(recording_logs) == 1
    assert recording_logs[0]["recording_sequence"] == 1
    assert recording_logs[0]["recording_key"] == payload["recording_key"]
    assert recording_logs[0]["trace_path"] == str(traces[0])
    assert recording_logs[0]["video_path"] == str(videos[0])
    assert recording_logs[0]["manifest_path"] == str(manifests[0])


def test_execute_cleans_previous_browser_recordings_before_run(monkeypatch):
    root = _browser_recording_root()
    root.mkdir(parents=True)
    old_manifest = root / "0001-case_1-old-attempt-1-context-1-run-old.manifest.json"
    old_manifest.write_text("{}", encoding="utf-8")

    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True)

    manifests = [
        path
        for path in sorted(root.glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert not old_manifest.exists()
    assert len(manifests) == 1
    assert manifests[0].name.startswith(
        "0001-case_1-open_login-attempt-1-context-1-run-"
    )


def test_open_page_records_flat_incrementing_files_for_multiple_contexts(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_two_pages() -> tuple[journey_browser.JourneyBrowserPage, journey_browser.JourneyBrowserPage]:
        first = journey_browser.open_page("http://example.test/one")
        second = journey_browser.open_page("http://example.test/two")
        return first, second

    def journey():
        journey_sdk.step(open_two_pages)

    journey_sdk.execute(journey, no_state=True)

    manifests = [
        path
        for path in sorted(_browser_recording_root().glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert [path.name[:4] for path in manifests] == ["0001", "0002"]
    assert all(path.parent == _browser_recording_root() for path in manifests)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in manifests
    ]
    assert [payload["context_index"] for payload in payloads] == [1, 2]
    assert [payload["sequence"] for payload in payloads] == [1, 2]
    assert all(payload["step_name"] == "open_two_pages" for payload in payloads)
    assert all(payload["status"] == "success" for payload in payloads)


def test_execute_can_disable_browser_recording(monkeypatch):
    root = _browser_recording_root()
    root.mkdir(parents=True)
    (root / "old.manifest.json").write_text("{}", encoding="utf-8")

    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def open_login() -> journey_browser.JourneyBrowserPage:
        return journey_browser.open_page("http://example.test/login")

    def journey():
        journey_sdk.step(open_login)

    journey_sdk.execute(journey, no_state=True, no_browser_recording=True)

    recording_manifests = [
        path
        for path in _browser_recording_root().glob("*.manifest.json")
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert recording_manifests == []
    assert not any(
        isinstance(event, tuple) and event[0] in {"trace_start", "trace_stop", "video_save"}
        for event in events
    )


def test_open_page_finalizes_recording_when_step_fails(monkeypatch):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        record_recording_events=True,
    )

    def assert_login() -> bool:
        journey_browser.open_page("http://example.test/login")
        raise RuntimeError("assertion failed")

    def journey():
        journey_sdk.step(assert_login)

    with pytest.raises(CallableExecutionError):
        journey_sdk.execute(journey, no_state=True)

    manifests = [
        path
        for path in sorted(_browser_recording_root().glob("*.manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "browser_recording"
    ]
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["step_name"] == "assert_login"
    assert payload["trace_saved"] is True
    assert payload["video_saved"] is True


def test_open_page_keyboard_interrupt_is_not_reported_as_browser_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=KeyboardInterrupt(),
        fail_context_close=RuntimeError("TargetClosedError: context closed"),
        fail_browser_close=RuntimeError("TargetClosedError: browser closed"),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "Browser failed to open" not in output
    assert "browser page cleanup failed" not in output
    assert "TargetClosedError" not in output
    assert events == [
        "playwright_enter",
        ("launch", True, False),
        "new_context",
        "new_page",
        ("goto", "http://example.test/login", "load"),
        "context_close",
        "browser_close",
        "playwright_exit",
    ]


def test_open_page_playwright_abort_after_forced_interrupt_is_keyboard_interrupt(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "JourneyBrowserPage.goto: net::ERR_ABORTED; maybe frame was detached?"
        ),
        force_cleanup_before_goto_failure=True,
    )

    class ForcedInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            raise KeyboardInterrupt()

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(ForcedInterruptController()):
        with pytest.raises(KeyboardInterrupt):
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "Browser failed to open" not in output
    assert "failed after" not in output
    assert ("capture_state", "about:blank") not in events


def test_open_page_forced_interrupt_without_cleanup_keeps_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "JourneyBrowserPage.goto: net::ERR_ABORTED; maybe frame was detached?"
        ),
    )

    class ForcedInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(ForcedInterruptController()):
        with pytest.raises(CallableExecutionError) as exc_info:
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "net::ERR_ABORTED" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_open_page_pending_interrupt_keeps_real_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "JourneyBrowserPage.goto: net::ERR_ABORTED; maybe frame was detached?"
        ),
    )

    class PendingInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return True

        def is_step_forced_interrupt_requested(self) -> bool:
            return False

        def raise_if_interrupted_after_step(self) -> None:
            raise KeyboardInterrupt()

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(PendingInterruptController()):
        with pytest.raises(CallableExecutionError) as exc_info:
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "net::ERR_ABORTED" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_open_page_no_interrupt_keeps_target_closed_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=PlaywrightError(
            "TargetClosedError: Target page, context or browser has been closed"
        ),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "TargetClosedError" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_open_page_forced_interrupt_keeps_non_playwright_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=RuntimeError(
            "TargetClosedError: application-level failure that is not Playwright"
        ),
        force_cleanup_before_goto_failure=True,
    )

    class ForcedInterruptController:
        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with journey_executor._use_step_interrupt_controller(ForcedInterruptController()):
        with pytest.raises(CallableExecutionError) as exc_info:
            journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "application-level failure" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output


def test_browser_interrupt_cleanup_marks_playwright_callback_futures_observed():
    class FakeFuture:
        def __init__(self) -> None:
            self.callbacks: list[object] = []
            self.retrieved = False

        def add_done_callback(self, callback: object) -> None:
            self.callbacks.append(callback)

        def exception(self) -> RuntimeError:
            self.retrieved = True
            return RuntimeError("TargetClosedError: browser closed")

    class FakeCallback:
        def __init__(self, future: FakeFuture) -> None:
            self.future = future

    class FakeConnection:
        def __init__(self, future: FakeFuture) -> None:
            self._callbacks = {"pending": FakeCallback(future)}

    class FakeManager:
        def __init__(self, future: FakeFuture) -> None:
            self._connection = FakeConnection(future)

    future = FakeFuture()
    journey_browser._suppress_playwright_callback_future_noise(FakeManager(future))

    assert len(future.callbacks) == 1
    callback = future.callbacks[0]
    assert callable(callback)
    callback(future)
    assert future.retrieved


def test_browser_registers_forced_interrupt_cleanup_that_stops_playwright_driver(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeController:
        def __init__(self) -> None:
            self.callbacks: list[Callable[[], None]] = []

        def on_step_lifecycle_phase(self, phase: str | None) -> None:
            pass

        def is_step_interrupt_pending(self) -> bool:
            return False

        def is_step_forced_interrupt_requested(self) -> bool:
            return True

        def raise_if_interrupted_after_step(self) -> None:
            return None

        def register_forced_interrupt_callback(
            self,
            name: str,
            callback: Callable[[], None],
        ) -> Callable[[], None]:
            self.callbacks.append(callback)
            return lambda: None

    class FakeProcess:
        pid = 12345
        returncode = None

    class FakeTransport:
        _proc = FakeProcess()

    class FakeConnection:
        _transport = FakeTransport()
        _callbacks: dict[str, object] = {}

    class FakeManager:
        _connection = FakeConnection()

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        journey_browser.os,
        "kill",
        lambda pid, sig: killed.append((pid, sig)),
    )

    controller = FakeController()
    with journey_executor._use_step_interrupt_controller(controller):
        page = journey_browser.JourneyBrowserPage._from_snapshot(
            journey_browser._PageSnapshot.from_url("https://example.test/")
        )
        page._set_step_resources(manager=FakeManager())

    assert len(controller.callbacks) == 1
    controller.callbacks[0]()
    assert killed == [(12345, journey_browser.signal.SIGTERM)]
    assert page._is_forced_interrupt_cleanup_started()




def test_open_page_real_navigation_failure_remains_browser_failure(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    events: list[object] = []
    _install_fake_playwright(
        monkeypatch,
        events,
        fail_goto=RuntimeError("navigation failed"),
    )

    def open_fails() -> bool:
        journey_browser.open_page("http://example.test/login")
        return True

    def journey():
        journey_sdk.step(open_fails)

    with pytest.raises(CallableExecutionError) as exc_info:
        journey_sdk.execute(journey)

    output = capsys.readouterr().out
    assert "navigation failed" in str(exc_info.value)
    assert "Browser failed to open chromium http://example.test/login" in output
    assert ("capture_state", "about:blank") not in events


def test_ensure_browser_installed_reports_automatic_install_failure(
    monkeypatch,
    tmp_path: Path,
):
    events: list[object] = []
    executable_path = tmp_path / "ms-playwright" / "chromium"
    _install_fake_playwright(
        monkeypatch,
        events,
        executable_path=str(executable_path),
    )

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(journey_browser.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="could not automatically install"):
        journey_browser.ensure_browser_installed()

    assert events == [
        "playwright_enter",
        "playwright_exit",
    ]


def test_open_page_rejects_outside_step():
    with pytest.raises(InvalidBranchUsageError):
        journey_browser.open_page("http://example.test/login")


def test_open_page_rejects_unsupported_input_type():
    with pytest.raises(TypeError, match="URL string or JourneyBrowserPage"):
        journey_browser.open_page(object())


def test_journey_browser_page_rejects_legacy_json_payload(tmp_path: Path):
    with pytest.raises(TypeError, match="dictionary payload"):
        journey_browser.JourneyBrowserPage.__restore__(
            '{"url":"http://example.test"}',
            journey_sdk.JourneyRestoreContext(
                artifact_root=tmp_path,
                boundary_kind="binding",
                boundary_id="step:n_1",
            ),
        )


def test_execute_resume_rehydrates_saved_journey_browser_page(tmp_path, monkeypatch):
    state_file = tmp_path / "journey.state"
    attempts = {"count": 0}
    events: list[object] = []
    _install_fake_playwright(monkeypatch, events)

    def login() -> journey_browser.JourneyBrowserPage:
        page = journey_browser.open_page("http://example.test/login")
        page.goto("http://example.test/dashboard", wait_until="load")
        return page

    def continue_from_page(
        saved_page: journey_browser.JourneyBrowserPage,
    ) -> journey_browser.JourneyBrowserPage:
        attempts["count"] += 1
        events.append(f"continue:{attempts['count']}:{saved_page.url}")
        if attempts["count"] == 1:
            raise KeyboardInterrupt()
        return journey_browser.open_page(saved_page)

    def assert_page(
        saved_page: journey_browser.JourneyBrowserPage,
    ) -> journey_browser.JourneyBrowserPage:
        page = journey_browser.open_page(saved_page)
        events.append(f"assert:{page.url}")
        return page

    def journey():
        page = journey_sdk.step(login)
        continued = journey_sdk.step(continue_from_page, page)
        journey_sdk.step(assert_page, continued)

    with pytest.raises(KeyboardInterrupt):
        journey_sdk.execute(journey, state=state_file)

    report = journey_sdk.execute(journey, state=state_file)

    assert [
        record.label
        for record in report.case_reports[0].records
        if record.label is not None
    ] == [
        "login",
        "continue_from_page",
        "assert_page",
    ]
    assert "continue:1:http://example.test/dashboard" in events
    assert "continue:2:http://example.test/dashboard" in events
    assert "assert:http://example.test/dashboard" in events
