"""Official Playwright page-state tool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from journeysdk.rehydration import JourneyRestoreContext, JourneyStoreContext
from journeysdk.session import register_step_exit_callback
from playwright.sync_api import Page as PlaywrightPage
from playwright.sync_api import sync_playwright


class PlaywrightCookie(TypedDict, total=False):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: Literal["Strict", "Lax", "None"]


class PlaywrightPagePayload(TypedDict):
    url: str
    cookies: list[PlaywrightCookie]
    local_storage: dict[str, str]


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


def _normalize_payload(payload: Mapping[str, object]) -> PlaywrightPagePayload:
    if set(payload) != {"url", "cookies", "local_storage"}:
        raise ValueError(
            "PlaywrightPageState expects exactly 'url', 'cookies', and 'local_storage'."
        )

    url = payload["url"]
    if not isinstance(url, str) or not url:
        raise TypeError("PlaywrightPageState url must be a non-empty string.")

    cookies = payload["cookies"]
    if not isinstance(cookies, list):
        raise TypeError("PlaywrightPageState cookies must be a list of cookie objects.")
    normalized_cookies: list[PlaywrightCookie] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise TypeError(
                "PlaywrightPageState cookies must contain only cookie dictionaries."
            )
        normalized_cookie = cast(
            PlaywrightCookie,
            json.loads(json.dumps(cookie, sort_keys=True)),
        )
        normalized_cookies.append(normalized_cookie)

    local_storage = payload["local_storage"]
    if not isinstance(local_storage, dict):
        raise TypeError(
            "PlaywrightPageState local_storage must be a dictionary of strings."
        )
    normalized_local_storage: dict[str, str] = {}
    for key, value in local_storage.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                "PlaywrightPageState local_storage must contain only string keys and values."
            )
        normalized_local_storage[key] = value

    return {
        "url": url,
        "cookies": normalized_cookies,
        "local_storage": normalized_local_storage,
    }


@dataclass(frozen=True)
class PlaywrightPageState:
    """Serializable page snapshot for resumable Playwright steps."""

    _payload_json: str

    @classmethod
    def from_url(cls, url: str) -> PlaywrightPageState:
        """Create an empty page state that opens a fresh page at one URL."""

        return cls._from_payload(
            {
                "url": url,
                "cookies": [],
                "local_storage": {},
            }
        )

    @classmethod
    def from_page(cls, page: PlaywrightPage) -> PlaywrightPageState:
        """Capture the current page URL, cookies, and local storage."""

        payload = {
            "url": getattr(page, "url"),
            "cookies": list(page.context.cookies()),
            "local_storage": dict(page.evaluate(_STORAGE_SCRIPT)),
        }
        return cls._from_payload(payload)

    @classmethod
    def from_json(cls, value: str) -> PlaywrightPageState:
        """Restore a page state from its JSON representation."""

        if not isinstance(value, str):
            raise TypeError("PlaywrightPageState.from_json(...) expects a JSON string.")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "PlaywrightPageState.from_json(...) received invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise TypeError(
                "PlaywrightPageState.from_json(...) expects a JSON object."
            )
        return cls._from_payload(cast(Mapping[str, object], payload))

    @classmethod
    def _from_payload(cls, payload: Mapping[str, object]) -> PlaywrightPageState:
        normalized = _normalize_payload(payload)
        return cls(
            _payload_json=json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def to_json(self) -> str:
        """Return the canonical JSON representation of the saved page state."""

        return self._payload_json

    def __store__(self, context: JourneyStoreContext) -> object:
        """Store the page snapshot for Journey replay."""

        del context
        return self._payload_json

    @classmethod
    def __restore__(
        cls,
        payload: object,
        context: JourneyRestoreContext,
    ) -> PlaywrightPageState:
        """Restore the page snapshot from Journey replay state."""

        del context
        if not isinstance(payload, str):
            raise TypeError("PlaywrightPageState.__restore__(...) expects a JSON string.")
        return cls.from_json(payload)

    @property
    def url(self) -> str:
        return cast(str, self._payload()["url"])

    @property
    def cookies(self) -> tuple[PlaywrightCookie, ...]:
        cookies = self._payload()["cookies"]
        return tuple(cast(PlaywrightCookie, dict(cookie)) for cookie in cookies)

    @property
    def local_storage(self) -> dict[str, str]:
        return dict(cast(dict[str, str], self._payload()["local_storage"]))

    def _payload(self) -> PlaywrightPagePayload:
        return cast(PlaywrightPagePayload, json.loads(self._payload_json))


class JourneyPlaywrightPage(PlaywrightPage):
    """Playwright page wrapper that can be saved and reopened by Journey."""

    def __init__(
        self,
        impl_obj: object | None = None,
        *,
        state: PlaywrightPageState | None = None,
    ) -> None:
        if impl_obj is None:
            if state is None:
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
        self._journey_page_state = state

    @classmethod
    def _from_live_page(
        cls,
        page: PlaywrightPage,
        *,
        fallback_state: PlaywrightPageState,
    ) -> JourneyPlaywrightPage:
        if isinstance(page, cls):
            page._journey_page_state = fallback_state
            page._journey_step_closed = False
            return page

        impl_obj = getattr(page, "_impl_obj", None)
        if impl_obj is None:
            raise TypeError("open_page(...) expected Playwright to return a sync Page.")
        return cls(impl_obj, state=fallback_state)

    @classmethod
    def _from_state(cls, state: PlaywrightPageState) -> JourneyPlaywrightPage:
        return cls(state=state)

    @classmethod
    def _restore_pickle(cls, state_json: str) -> JourneyPlaywrightPage:
        return cls._from_state(PlaywrightPageState.from_json(state_json))

    @property
    def url(self) -> str:
        if self._is_live:
            return cast(str, super().url)
        return self._state_for_storage().url

    @property
    def _is_live(self) -> bool:
        return (
            not bool(getattr(self, "_journey_step_closed", True))
            and getattr(self, "_impl_obj", None) is not None
        )

    def __repr__(self) -> str:
        live = "live" if self._is_live else "saved"
        return f"JourneyPlaywrightPage(url={self.url!r}, state={live})"

    def _state_for_storage(self) -> PlaywrightPageState:
        if self._is_live:
            self._journey_page_state = PlaywrightPageState.from_page(self)
        if self._journey_page_state is None:
            raise RuntimeError("JourneyPlaywrightPage has no saved page state.")
        return self._journey_page_state

    def _mark_step_closed(self) -> None:
        self._journey_step_closed = True

    def __store__(self, context: JourneyStoreContext) -> object:
        """Store the current page state for Journey replay."""

        del context
        return self._state_for_storage().to_json()

    @classmethod
    def __restore__(
        cls,
        payload: object,
        context: JourneyRestoreContext,
    ) -> JourneyPlaywrightPage:
        """Restore a saved page handle for explicit reopening in a later step."""

        del context
        if not isinstance(payload, str):
            raise TypeError("JourneyPlaywrightPage.__restore__(...) expects a JSON string.")
        return cls._from_state(PlaywrightPageState.from_json(payload))

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (type(self)._restore_pickle, (self._state_for_storage().to_json(),))


def capture_page_state(page: PlaywrightPage) -> PlaywrightPageState:
    """Capture one live Playwright page as resumable state."""

    if isinstance(page, JourneyPlaywrightPage):
        return page._state_for_storage()
    return PlaywrightPageState.from_page(page)


def open_page(
    page_state: JourneyPlaywrightPage | PlaywrightPageState | str,
    *,
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
    headless: bool = True,
) -> JourneyPlaywrightPage:
    """Open a fresh Playwright page from saved page state."""

    state = _normalize_open_page_state(page_state)
    manager = None
    launched_browser = None
    context = None
    page: JourneyPlaywrightPage | None = None
    cleanup_started = False

    def cleanup() -> None:
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        failures: list[Exception] = []

        if page is not None:
            try:
                page._state_for_storage()
            except Exception as exc:  # pragma: no cover - surfaced through executor
                failures.append(exc)
            finally:
                page._mark_step_closed()

        if context is not None:
            try:
                context.close()
            except Exception as exc:  # pragma: no cover - environment dependent
                failures.append(exc)
        if launched_browser is not None:
            try:
                launched_browser.close()
            except Exception as exc:  # pragma: no cover - environment dependent
                failures.append(exc)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover - environment dependent
                failures.append(exc)

        if failures:
            raise RuntimeError(_cleanup_failure_message(failures))

    register_step_exit_callback(cleanup)

    try:
        manager = sync_playwright()
        playwright = manager.__enter__()
        browser_type = getattr(playwright, browser, None)
        if browser_type is None:
            raise ValueError(
                "open_page(..., browser=...) expects 'chromium', 'firefox', or 'webkit'."
            )
        launched_browser = browser_type.launch(headless=headless)
        context = launched_browser.new_context()
        if state.cookies:
            context.add_cookies(list(state.cookies))
        native_page = context.new_page()
        page = JourneyPlaywrightPage._from_live_page(
            cast(PlaywrightPage, native_page),
            fallback_state=state,
        )
        page.goto(state.url, wait_until="load")
        page.evaluate(_REHYDRATE_STORAGE_SCRIPT, state.local_storage)
        page.reload(wait_until="load")
        return page
    except Exception as exc:
        try:
            cleanup()
        except Exception as cleanup_exc:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(str(cleanup_exc))
        raise


def _normalize_open_page_state(
    page_state: JourneyPlaywrightPage | PlaywrightPageState | str,
) -> PlaywrightPageState:
    if isinstance(page_state, JourneyPlaywrightPage):
        return page_state._state_for_storage()
    if isinstance(page_state, PlaywrightPageState):
        return page_state
    if isinstance(page_state, str):
        if page_state.lstrip().startswith("{"):
            return PlaywrightPageState.from_json(page_state)
        return PlaywrightPageState.from_url(page_state)
    raise TypeError(
        "open_page(...) expects a URL string, PlaywrightPageState, or JourneyPlaywrightPage."
    )


def _cleanup_failure_message(failures: list[Exception]) -> str:
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
    "PlaywrightPagePayload",
    "PlaywrightPageState",
    "capture_page_state",
    "open_page",
]
