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

## Browser Logs

By default, `open_page(...)` writes browser evidence under `.journey/logs/`: a Playwright trace, a WebM video, browser
console messages, page errors, failed requests, and response status metadata. Use `journey --no-browser-recording` when
trace/video capture should be skipped while keeping other logs. Use `journey --no-logs` only when no local run evidence
should be written. Journey clears existing logs at the start of a run so the log list reflects the current run.

Use `journey logs` from the project root to browse completed cases. Pick a case, then choose whether to open a merged
Playwright trace, open a merged WebM recording, show text logs, print generated artifact paths, filter the selected
scope, go back to the case list, or quit. Choose the all-cases entry to open or print one unified trace/video for the
whole execution.

```bash
journey logs
```

```console
Logs
a. all cases  journey=signup_journey run=8bc31a94e2f1 cases=2 steps=7 traces=7 videos=7 logs=9 started=2026-05-28T12:00:01Z dir=.journey/logs
1. case_1  journey=signup_journey run=8bc31a94e2f1 branches={} steps=3 traces=3 videos=3 logs=4 started=2026-05-28T12:00:01Z dir=.journey/logs
2. case_2  journey=signup_journey run=8bc31a94e2f1 branches={plan=paid} steps=4 traces=4 videos=4 logs=5 started=2026-05-28T12:01:12Z dir=.journey/logs
Select a case number, a for all cases, or q to quit:
```

After selecting a case or the all-cases entry:

```console
case_1 filters=none: [t] open trace, [v] open video, [l] show logs, [p] print paths, [f] filter, [b] back, [q] quit:
Filter by step, branch, touchpoint, source; type clear or back:
```

Choose `t` to merge the selected case or execution traces into one `.trace.zip` and open it with Playwright Trace
Viewer. Choose `v` to merge videos into one `.webm` and open it with the OS video viewer. Choose `l` to print text log
artifacts. Choose `p` when you only need the generated artifact paths for sharing or later inspection. Choose `f`, then
select a step and `touchpoint=browser`, when you only want browser evidence for one step.

For coding-agent loops, prefer noninteractive filters:

```bash
journey logs --show --case case_1 --step sign_in --touchpoint browser --tail 80
journey logs --paths --step report_issue --touchpoint browser
journey logs --paths --run 8bc31a94e2f1
```

## Replay

`JourneyBrowserPage` stores URL, cookies, and local storage. Reopen it with `open_page(saved_page)` inside a later step.
Do that only when the later step needs the same browser state as a real replay boundary. Do not pass live Playwright
objects through step results.
