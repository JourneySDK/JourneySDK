"""Official Playwright page-state tool."""

from __future__ import annotations

from collections.abc import Mapping
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Literal, TypedDict, cast

from journeysdk.logger import PrettyLine, PrettyStyle, get_logger, pretty_row
from journeysdk.rehydration import JourneyRestoreContext, JourneyStoreContext
from journeysdk.session import _require_executing_step
from playwright.sync_api import Page as PlaywrightPage
from playwright.sync_api import sync_playwright

from ._playwright_prompt import (
    prompt_page,
)


class PlaywrightCookie(TypedDict, total=False):
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
_PLAYWRIGHT_INSTALL_LOCK = Lock()
_LOGGER = get_logger("playwright")


def _browser_row(detail: object, *, style: PrettyStyle = "tool") -> PrettyLine:
    return pretty_row("Browser", detail, indent=8, label_width=27, style=style)


@dataclass(frozen=True)
class _PageSnapshot:
    url: str
    cookies: tuple[PlaywrightCookie, ...]
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
            raise TypeError("JourneyPlaywrightPage state expects a dictionary payload.")
        if set(payload) != {"url", "cookies", "local_storage"}:
            raise ValueError(
                "JourneyPlaywrightPage state expects exactly 'url', 'cookies', and 'local_storage'."
            )

        url = payload["url"]
        if not isinstance(url, str) or not url:
            raise TypeError("JourneyPlaywrightPage state url must be a non-empty string.")

        cookies = payload["cookies"]
        if not isinstance(cookies, (list, tuple)):
            raise TypeError(
                "JourneyPlaywrightPage state cookies must be a list of cookie objects."
            )
        normalized_cookies: list[PlaywrightCookie] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise TypeError(
                    "JourneyPlaywrightPage state cookies must contain only cookie dictionaries."
                )
            normalized_cookie: dict[str, object] = {}
            for key, value in cookie.items():
                if not isinstance(key, str):
                    raise TypeError(
                        "JourneyPlaywrightPage state cookie keys must be strings."
                    )
                normalized_cookie[key] = value
            normalized_cookies.append(cast(PlaywrightCookie, normalized_cookie))

        local_storage = payload["local_storage"]
        if not isinstance(local_storage, dict):
            raise TypeError(
                "JourneyPlaywrightPage state local_storage must be a dictionary of strings."
            )
        normalized_local_storage: dict[str, str] = {}
        for key, value in local_storage.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(
                    "JourneyPlaywrightPage state local_storage must contain only string keys and values."
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


class JourneyPlaywrightPage(PlaywrightPage):
    """Playwright page wrapper that can be saved and reopened by Journey."""

    def __init__(
        self,
        impl_obj: object | None = None,
        *,
        snapshot: _PageSnapshot | None = None,
    ) -> None:
        if impl_obj is None:
            if snapshot is None:
                raise TypeError(
                    "JourneyPlaywrightPage needs either a live Playwright page or saved state."
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

    @classmethod
    def _from_snapshot(cls, snapshot: _PageSnapshot) -> JourneyPlaywrightPage:
        return cls(snapshot=snapshot)

    @classmethod
    def _restore_pickle(cls, payload: object) -> JourneyPlaywrightPage:
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
        return f"JourneyPlaywrightPage(url={self.url!r}, state={live})"

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
    ) -> None:
        if manager is not None:
            self._journey_manager = manager
        if browser is not None:
            self._journey_browser = browser
        if context is not None:
            self._journey_context = context

    def _snapshot_for_storage(self) -> _PageSnapshot:
        if self._is_live:
            self._journey_snapshot = _PageSnapshot.from_live_page(self)
        if self._journey_snapshot is None:
            raise RuntimeError("JourneyPlaywrightPage has no saved page state.")
        return self._journey_snapshot

    def _mark_step_closed(self) -> None:
        self._journey_step_closed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close Playwright resources owned by this step-scoped page."""

        if self._journey_exit_started:
            return
        self._journey_exit_started = True
        cleanup_url = self.url
        was_live = self._is_live
        _LOGGER.debug(
            "page_cleanup_start",
            "cleaning up Playwright page resources",
            url=cleanup_url,
            live=was_live,
        )
        failures: list[BaseException] = []

        if self._is_live:
            try:
                self._snapshot_for_storage()
            except BaseException as snapshot_exc:  # pragma: no cover - surfaced through executor
                failures.append(snapshot_exc)
            finally:
                self._mark_step_closed()

        for resource in (
            self._journey_context,
            self._journey_browser,
        ):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as close_exc:  # pragma: no cover - environment dependent
                    failures.append(close_exc)

        manager_exit = getattr(self._journey_manager, "__exit__", None)
        if callable(manager_exit):
            try:
                manager_exit(exc_type, exc, traceback)
            except BaseException as manager_exc:  # pragma: no cover - environment dependent
                failures.append(manager_exc)

        if failures:
            _LOGGER.error(
                "page_cleanup_failure",
                "Playwright page cleanup failed",
                url=cleanup_url,
                failures=len(failures),
            )
            raise RuntimeError(_cleanup_failure_message(failures))
        _LOGGER.debug(
            "page_cleanup_success",
            "Playwright page resources cleaned up",
            url=cleanup_url,
        )

    def __store__(self, context: JourneyStoreContext) -> object:
        """Store the current page state for Journey replay."""

        del context
        return self._snapshot_for_storage().to_payload()

    @classmethod
    def __restore__(
        cls,
        payload: object,
        context: JourneyRestoreContext,
    ) -> JourneyPlaywrightPage:
        """Restore a saved page handle for explicit reopening in a later step."""

        del context
        return cls._from_snapshot(_PageSnapshot.from_payload(payload))

    def __reduce__(self) -> tuple[object, tuple[object]]:
        return (type(self)._restore_pickle, (self._snapshot_for_storage().to_payload(),))

    def prompt(
        self,
        instruction: str,
        *,
        model: str | None = None,
        max_steps: int = 15,
        action_timeout_seconds: float = 5.0,
        memory: str | None = None,
        output: Mapping[str, str | Mapping[str, object]] | None = None,
    ) -> str | dict[str, object]:
        """Use an LLM to inspect and interact with the live page.

        Args:
            instruction: Natural-language task for the prompt loop.
            model: Optional LangChain model identifier, such as
                `openai:gpt-4.1-mini` or `anthropic:claude-sonnet-4-5`.
            max_steps: Maximum generated Python snippets before failing.
            action_timeout_seconds: Timeout passed to generated Playwright actions.
            memory: Optional named prompt memory stored as `[memory].memory.json`.
            output: Optional structured-output fields. String values are field
                descriptions for string output fields; mapping values are
                JSON-schema property fragments. When omitted, the return value is
                plain text.
        """

        if not self._is_live:
            raise RuntimeError(
                "JourneyPlaywrightPage.prompt(...) requires a live Playwright page. "
                "Call open_page(saved_page) first."
            )
        return prompt_page(
            self,
            instruction=instruction,
            model=model,
            max_steps=max_steps,
            action_timeout_seconds=action_timeout_seconds,
            memory=memory,
            output=output,
        )


def open_page(
    page_or_url: JourneyPlaywrightPage | str,
    *,
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
    headless: bool = True,
) -> JourneyPlaywrightPage:
    """Open a fresh Playwright page from a URL or saved Journey page."""

    if not isinstance(page_or_url, (JourneyPlaywrightPage, str)):
        raise TypeError("open_page(...) expects a URL string or JourneyPlaywrightPage.")
    _require_executing_step("open_page")
    if browser not in _SUPPORTED_BROWSERS:
        raise ValueError(
            "open_page(..., browser=...) expects 'chromium', 'firefox', or 'webkit'."
        )

    snapshot = _snapshot_from_open_page_input(page_or_url)
    local_storage = snapshot.local_storage_dict()
    page = JourneyPlaywrightPage._from_snapshot(snapshot)
    _LOGGER.info(
        "open_page_start",
        "opening Playwright page",
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
        context = launched_browser.new_context()
        page._set_step_resources(context=context)
        if snapshot.cookies:
            context.add_cookies([dict(cookie) for cookie in snapshot.cookies])
        native_page = context.new_page()
        page._attach_live_page(
            cast(PlaywrightPage, native_page),
            fallback_snapshot=snapshot,
        )
        page.goto(snapshot.url, wait_until="load")
        if local_storage:
            page.evaluate(_REHYDRATE_STORAGE_SCRIPT, local_storage)
            page.reload(wait_until="load")
        _LOGGER.info(
            "open_page_success",
            "Playwright page opened",
            pretty=_browser_row(f"opened {browser} {page.url}"),
            url=page.url,
            browser=browser,
        )
        return page
    except BaseException as exc:
        _LOGGER.error(
            "open_page_failure",
            "failed to open Playwright page",
            pretty=f"Browser failed to open {browser} {snapshot.url}: {_format_exception(exc)}",
            url=snapshot.url,
            browser=browser,
            error=_format_exception(exc),
        )
        try:
            page.__exit__(type(exc), exc, exc.__traceback__)
        except BaseException as cleanup_exc:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(str(cleanup_exc))
        raise


def _snapshot_from_open_page_input(
    page_or_url: JourneyPlaywrightPage | str,
) -> _PageSnapshot:
    if isinstance(page_or_url, JourneyPlaywrightPage):
        return page_or_url._snapshot_for_storage()
    return _PageSnapshot.from_url(page_or_url)


def ensure_browser_installed(
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
) -> None:
    """Install the requested Playwright browser in the current environment if needed."""

    if browser not in _SUPPORTED_BROWSERS:
        raise ValueError(
            "ensure_browser_installed(..., browser=...) expects "
            "'chromium', 'firefox', or 'webkit'."
        )

    _LOGGER.info(
        "browser_install_check_start",
        "checking Playwright browser installation",
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
        "Playwright browser installation is available",
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
        return launch(headless=headless)
    except BaseException as exc:
        if attempted_install or not _looks_like_missing_browser_error(exc):
            raise
        _install_playwright_browser(browser)
        return launch(headless=headless)


def _ensure_browser_type_installed(
    browser_type: object,
    *,
    browser: Literal["chromium", "firefox", "webkit"],
) -> bool:
    executable_path = _browser_executable_path(browser_type)
    if executable_path is None or executable_path.exists():
        _LOGGER.debug(
            "browser_install_check_skip",
            "Playwright browser executable is already available",
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
    with _PLAYWRIGHT_INSTALL_LOCK:
        if executable_path is not None and executable_path.exists():
            return
        _LOGGER.info(
            "browser_install_start",
            "installing Playwright browser",
            pretty=_browser_row(f"installing {browser}"),
            browser=browser,
            command=" ".join(command),
            executable_path=executable_path,
        )
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            _LOGGER.error(
                "browser_install_failure",
                "Playwright browser installation failed",
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
                "Playwright browser install completed but executable is missing",
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
            "Playwright browser installed",
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


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _cleanup_failure_message(failures: list[BaseException]) -> str:
    if len(failures) == 1:
        failure = failures[0]
        return (
            "Playwright page cleanup failed: "
            f"{type(failure).__name__}: {failure}"
        )
    joined = "; ".join(
        f"{type(failure).__name__}: {failure}"
        for failure in failures
    )
    return f"{len(failures)} Playwright cleanup actions failed: {joined}"


__all__ = [
    "PlaywrightCookie",
    "JourneyPlaywrightPage",
    "ensure_browser_installed",
    "open_page",
]
