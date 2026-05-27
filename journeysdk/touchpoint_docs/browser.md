# Browser Touchpoint Reference

Use the browser touchpoint when a step needs to act through the UI or preserve a browser page across replay boundaries.

## Public API

- `open_page(page_or_url, browser="chromium", headless=True) -> JourneyBrowserPage`
- `JourneyBrowserPage`: Playwright `Page` wrapper that can be stored and reopened by Journey.
- `JourneyBrowserPage.prompt(instruction, *, memory=..., output=None, model=None, max_steps=None, action_timeout_seconds=None)`: bounded AI browser task helper.
- `BrowserCookie`: serializable browser cookie shape.
- `ensure_browser_installed()`: installs the browser runtime used by `open_page`.

## Authoring Pattern

Call `open_page(...)` inside a step function, never at module import time or while planning. Return a
`JourneyBrowserPage` only when later steps should reopen the same browser state:

```python
def sign_in():
    page = open_page("https://app.example/login")
    page.prompt("Sign in as the journey test user and stop on the dashboard.", memory="sign-in")
    return page


def create_watch(session):
    page = open_page(session)
    return page.prompt(
        "Create a watch for the demo URL and return the watch id.",
        memory="create-watch",
        output={"watch_id": "The id or UUID of the created watch."},
    )
```

Use selectors when they are stable and concise. Use `page.prompt(..., memory=...)` for bounded UI tasks when it keeps
the helper smaller and easier to maintain. Keep prompts specific and include the success condition.

## Recordings

By default, `open_page(...)` records a Playwright trace and video under `.journey/recordings/` for debugging. Disable
recordings with `journey --no-browser-recording` only when sensitive data should not be written.

## Replay

`JourneyBrowserPage` stores URL, cookies, and local storage. Reopen it with `open_page(saved_page)` inside a later step.
Do not pass live Playwright objects through step results.
