# Browser Touchpoint Reference

Use the browser touchpoint when a step needs to act through the UI or preserve a browser page across replay boundaries.

## Public API

- `open_page(page_or_url, browser="chromium", headless=True) -> JourneyBrowserPage`
- `JourneyBrowserPage`: Playwright `Page` wrapper that can be stored and reopened by Journey.
- `JourneyBrowserPage.prompt(instruction, *, memory=..., output=None, model=None, max_steps=None, action_timeout_seconds=None)`: bounded AI browser task helper.
- `BrowserCookie`: serializable browser cookie shape.
- `ensure_browser_installed()`: installs the browser runtime used by `open_page`.

## Authoring Pattern

Call `open_page(...)` inside a coarse user-flow step function, never at module import time or while planning. Do not
split one browser outcome into one Journey step per click, form fill, wait, and assertion. Return a
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

Use `journey recordings` from the project root to browse completed browser-recorded cases. Pick a case, then choose
whether to open a merged Playwright trace, open a merged WebM recording, print the generated artifact paths, go back to
the case list, or quit.

```bash
journey recordings
```

```console
Recordings
1. case_1  journey=signup_journey run=8bc31a94e2f1 branches={} steps=3 traces=3 videos=3 started=2026-05-28T12:00:01Z dir=.journey/recordings
2. case_2  journey=signup_journey run=8bc31a94e2f1 branches={plan=paid} steps=4 traces=4 videos=4 started=2026-05-28T12:01:12Z dir=.journey/recordings
Select a case number, or q to quit:
```

After selecting a case:

```console
case_1: [t] open trace, [v] open video, [p] print paths, [b] back, [q] quit:
```

Choose `t` to merge the case's step traces into one `.trace.zip` and open it with Playwright Trace Viewer. Choose `v`
to merge the case's step videos into one `.webm` and open it with the OS video viewer. Choose `p` when you only need
the generated artifact paths for sharing or later inspection.

## Replay

`JourneyBrowserPage` stores URL, cookies, and local storage. Reopen it with `open_page(saved_page)` inside a later step.
Do that only when the later step needs the same browser state as a real replay boundary. Do not pass live Playwright
objects through step results.
