"""Official Playwright page-state tool."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

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


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
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
    normalized_cookies: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise TypeError(
                "PlaywrightPageState cookies must contain only cookie dictionaries."
            )
        normalized_cookie = cast(
            dict[str, Any],
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


def _load_sync_playwright() -> Any:
    try:
        module = importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as exc:
        if exc.name not in {"playwright", "playwright.sync_api"}:
            raise
        raise ImportError(
            "journey.tools.playwright requires the optional 'playwright' package. "
            "Install it with `uv run --with playwright ...` or `pip install playwright`."
        ) from exc
    return getattr(module, "sync_playwright")


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
    def from_page(cls, page: Any) -> PlaywrightPageState:
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
        return cls._from_payload(cast(dict[str, Any], payload))

    @classmethod
    def _from_payload(cls, payload: dict[str, Any]) -> PlaywrightPageState:
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

    @property
    def url(self) -> str:
        return cast(str, self._payload()["url"])

    @property
    def cookies(self) -> tuple[dict[str, Any], ...]:
        cookies = cast(list[dict[str, Any]], self._payload()["cookies"])
        return tuple(dict(cookie) for cookie in cookies)

    @property
    def local_storage(self) -> dict[str, str]:
        return dict(cast(dict[str, str], self._payload()["local_storage"]))

    def _payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._payload_json))


def capture_page_state(page: Any) -> PlaywrightPageState:
    """Capture one live Playwright page as resumable state."""

    return PlaywrightPageState.from_page(page)


@contextmanager
def open_page(
    page_state: PlaywrightPageState | str,
    *,
    browser: Literal["chromium", "firefox", "webkit"] = "chromium",
    headless: bool = True,
) -> Iterator[Any]:
    """Open a fresh Playwright page from saved page state."""

    state = (
        page_state
        if isinstance(page_state, PlaywrightPageState)
        else PlaywrightPageState.from_json(page_state)
    )
    sync_playwright = _load_sync_playwright()
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, browser, None)
        if browser_type is None:
            raise ValueError(
                "open_page(..., browser=...) expects 'chromium', 'firefox', or 'webkit'."
            )
        launched_browser = browser_type.launch(headless=headless)
        context = launched_browser.new_context()
        try:
            if state.cookies:
                context.add_cookies(list(state.cookies))
            page = context.new_page()
            page.goto(state.url, wait_until="load")
            page.evaluate(_REHYDRATE_STORAGE_SCRIPT, state.local_storage)
            page.reload(wait_until="load")
            yield page
        finally:
            context.close()
            launched_browser.close()


__all__ = ["PlaywrightPageState", "capture_page_state", "open_page"]
