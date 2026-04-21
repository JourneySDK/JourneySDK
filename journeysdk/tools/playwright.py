"""Official Playwright page-state tool."""

from __future__ import annotations

import json
from collections.abc import Mapping
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


class _PlaywrightPageSnapshotPayload(TypedDict):
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


def _normalize_snapshot_payload(
    payload: Mapping[str, object],
) -> _PlaywrightPageSnapshotPayload:
    if set(payload) != {"url", "cookies", "local_storage"}:
        raise ValueError(
            "JourneyPlaywrightPage state expects exactly 'url', 'cookies', and 'local_storage'."
        )

    url = payload["url"]
    if not isinstance(url, str) or not url:
        raise TypeError("JourneyPlaywrightPage state url must be a non-empty string.")

    cookies = payload["cookies"]
    if not isinstance(cookies, list):
        raise TypeError(
            "JourneyPlaywrightPage state cookies must be a list of cookie objects."
        )
    normalized_cookies: list[PlaywrightCookie] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise TypeError(
                "JourneyPlaywrightPage state cookies must contain only cookie dictionaries."
            )
        normalized_cookie = cast(
            PlaywrightCookie,
            json.loads(json.dumps(cookie, sort_keys=True)),
        )
        normalized_cookies.append(normalized_cookie)

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

    return {
        "url": url,
        "cookies": normalized_cookies,
        "local_storage": normalized_local_storage,
    }


def _snapshot_json_from_payload(payload: Mapping[str, object]) -> str:
    normalized = _normalize_snapshot_payload(payload)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshot_json_from_url(url: str) -> str:
    return _snapshot_json_from_payload(
        {
            "url": url,
            "cookies": [],
            "local_storage": {},
        }
    )


def _snapshot_json_from_page(page: PlaywrightPage) -> str:
    return _snapshot_json_from_payload(
        {
            "url": getattr(page, "url"),
            "cookies": list(page.context.cookies()),
            "local_storage": dict(page.evaluate(_STORAGE_SCRIPT)),
        }
    )


def _snapshot_payload_from_json(value: str) -> _PlaywrightPageSnapshotPayload:
    if not isinstance(value, str):
        raise TypeError("JourneyPlaywrightPage state expects a JSON string.")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("JourneyPlaywrightPage state received invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise TypeError("JourneyPlaywrightPage state expects a JSON object.")
    return _normalize_snapshot_payload(cast(Mapping[str, object], payload))


def _snapshot_url(snapshot_json: str) -> str:
    return cast(str, _snapshot_payload_from_json(snapshot_json)["url"])


def _snapshot_cookies(snapshot_json: str) -> tuple[PlaywrightCookie, ...]:
    cookies = _snapshot_payload_from_json(snapshot_json)["cookies"]
    return tuple(cast(PlaywrightCookie, dict(cookie)) for cookie in cookies)


def _snapshot_local_storage(snapshot_json: str) -> dict[str, str]:
    return dict(
        cast(
            dict[str, str],
            _snapshot_payload_from_json(snapshot_json)["local_storage"],
        )
    )


class JourneyPlaywrightPage(PlaywrightPage):
    """Playwright page wrapper that can be saved and reopened by Journey."""

    def __init__(
        self,
        impl_obj: object | None = None,
        *,
        snapshot_json: str | None = None,
    ) -> None:
        if impl_obj is None:
            if snapshot_json is None:
                raise TypeError(
                    "JourneyPlaywrightPage needs either a live Playwright page or saved state."
                )
            snapshot_json = _snapshot_json_from_payload(
                _snapshot_payload_from_json(snapshot_json)
            )
            self._impl_obj = None
            self._loop = None
            self._dispatcher_fiber = None
            self._journey_step_closed = True
        else:
            super().__init__(impl_obj)
            self._journey_step_closed = False
        self._journey_snapshot_json = snapshot_json

    @classmethod
    def _from_live_page(
        cls,
        page: PlaywrightPage,
        *,
        fallback_snapshot_json: str,
    ) -> JourneyPlaywrightPage:
        if isinstance(page, cls):
            page._journey_snapshot_json = fallback_snapshot_json
            page._journey_step_closed = False
            return page

        impl_obj = getattr(page, "_impl_obj", None)
        if impl_obj is None:
            raise TypeError("open_page(...) expected Playwright to return a sync Page.")
        return cls(impl_obj, snapshot_json=fallback_snapshot_json)

    @classmethod
    def _from_snapshot_json(cls, snapshot_json: str) -> JourneyPlaywrightPage:
        return cls(snapshot_json=snapshot_json)

    @classmethod
    def _restore_pickle(cls, snapshot_json: str) -> JourneyPlaywrightPage:
        return cls._from_snapshot_json(snapshot_json)

    @property
    def url(self) -> str:
        if self._is_live:
            return cast(str, super().url)
        return _snapshot_url(self._snapshot_for_storage())

    @property
    def _is_live(self) -> bool:
        return (
            not bool(getattr(self, "_journey_step_closed", True))
            and getattr(self, "_impl_obj", None) is not None
        )

    def __repr__(self) -> str:
        live = "live" if self._is_live else "saved"
        return f"JourneyPlaywrightPage(url={self.url!r}, state={live})"

    def _snapshot_for_storage(self) -> str:
        if self._is_live:
            self._journey_snapshot_json = _snapshot_json_from_page(self)
        if self._journey_snapshot_json is None:
            raise RuntimeError("JourneyPlaywrightPage has no saved page state.")
        return self._journey_snapshot_json

    def _mark_step_closed(self) -> None:
        self._journey_step_closed = True

    def __store__(self, context: JourneyStoreContext) -> object:
        """Store the current page state for Journey replay."""

        del context
        return self._snapshot_for_storage()

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
        return cls._from_snapshot_json(payload)

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (type(self)._restore_pickle, (self._snapshot_for_storage(),))


def open_page(
    page_or_url: JourneyPlaywrightPage | str,
    *,
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
    headless: bool = True,
) -> JourneyPlaywrightPage:
    """Open a fresh Playwright page from a URL or saved Journey page."""

    state = _normalize_open_page_input(page_or_url)
    state_snapshot_json = state._snapshot_for_storage()
    state_url = _snapshot_url(state_snapshot_json)
    state_cookies = _snapshot_cookies(state_snapshot_json)
    state_local_storage = _snapshot_local_storage(state_snapshot_json)
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
                page._snapshot_for_storage()
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
        if state_cookies:
            context.add_cookies(list(state_cookies))
        native_page = context.new_page()
        page = JourneyPlaywrightPage._from_live_page(
            cast(PlaywrightPage, native_page),
            fallback_snapshot_json=state_snapshot_json,
        )
        page.goto(state_url, wait_until="load")
        page.evaluate(_REHYDRATE_STORAGE_SCRIPT, state_local_storage)
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


def _normalize_open_page_input(
    page_or_url: JourneyPlaywrightPage | str,
) -> JourneyPlaywrightPage:
    if isinstance(page_or_url, JourneyPlaywrightPage):
        return page_or_url
    if isinstance(page_or_url, str):
        return JourneyPlaywrightPage._from_snapshot_json(
            _snapshot_json_from_url(page_or_url)
        )
    raise TypeError("open_page(...) expects a URL string or JourneyPlaywrightPage.")


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
    "open_page",
]
