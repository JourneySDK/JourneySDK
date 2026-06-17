# Browser Touchpoint Reference

Use the browser touchpoint when a step needs to act through the UI or preserve a browser page across replay boundaries.

## Public API

- `open_page(page_or_url, browser="chromium", headless=True) -> JourneyBrowserPage`
- `JourneyBrowserPage`: Playwright `Page` wrapper that can be stored and reopened by Journey.
- `BrowserCookie`: serializable browser cookie shape.
- `ensure_browser_installed()`: installs the browser runtime used by `open_page`.

## Authoring Pattern

Call `open_page(...)` inside a coarse user-flow step function, never at module import time or while planning. Do not
split one browser outcome into one Journey step per click, form fill, wait, and assertion. Return a
`JourneyBrowserPage` only when later steps should reopen the same browser state:

```python
def sign_in():
    page = open_page("https://app.example/login")
    page.get_by_label("Email").fill("journey@example.com")
    page.get_by_label("Password").fill("test-password")
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("heading", name="Dashboard").wait_for()
    return page


def create_watch(session):
    page = open_page(session)
    page.get_by_role("button", name="Create watch").click()
    page.get_by_label("URL").fill("https://example.test/demo")
    page.get_by_role("button", name="Save").click()
    watch_id = page.get_by_test_id("watch-id").inner_text()
    return {"watch_id": watch_id}
```

Use stable Playwright selectors and keep the full browser action plus its assertions inside the same coarse Journey
step when they recover together. Coding agents should use `journey dev <step> --output jsonl`, rendered-page artifacts,
trace/video evidence, and `journey evidence` to inspect unclear page state, then edit this Python code directly.

## Browser Logs

By default, `open_page(...)` writes browser evidence under `.journey/logs/`: a Playwright trace, a WebM video, browser
console messages, page errors, failed requests, and response status metadata. Use `journey --no-browser-recording` when
trace/video capture should be skipped while keeping other logs. Use `journey --no-logs` only when no local run evidence
should be written. Journey clears existing logs at the start of a run so the log list reflects the current run.

Use `journey evidence` from the project root to browse completed evidence. Pick all cases, one case, a branch scope, or a
step scope, then choose whether to open a merged Playwright trace, open a merged WebM recording, show text logs, print
generated artifact paths, go back to the scope list, or quit. Step scopes narrow trace/video artifacts to that step.

```bash
journey evidence
```

```console
Logs
a. all cases  journey=signup_journey run=8bc31a94e2f1 cases=2 steps=7 traces=7 videos=7 logs=9 started=2026-05-28T12:00:01Z dir=.journey/logs
1. case_1  journey=signup_journey run=8bc31a94e2f1 branches={} steps=3 traces=3 videos=3 logs=4 started=2026-05-28T12:00:01Z dir=.journey/logs
2. case_2  journey=signup_journey run=8bc31a94e2f1 branches={plan=paid} steps=4 traces=4 videos=4 logs=5 started=2026-05-28T12:01:12Z dir=.journey/logs
b. browse branches
s. browse steps
Select a case number, a for all cases, b for branches, s for steps, or q to quit:
```

After selecting a case, all-cases entry, branch, or step:

```console
case_1: [t] open trace, [v] open video, [l] show logs, [p] print paths, [b] back, [q] quit:
```

Choose `t` to merge the selected case or execution traces into one `.trace.zip` and open it with Playwright Trace
Viewer. Choose `v` to merge videos into one `.webm` and open it with the OS video viewer. Choose `l` to print text log
artifacts. The log browser lists all touchpoints plus child sources when a touchpoint provides them. Choose `p` when you
only need the generated artifact paths for sharing or later inspection.

For coding-agent loops, discover scopes and log sources before reading large logs:

```bash
journey evidence --list-scopes
journey evidence --list-log-sources --case case_1 --step sign_in
journey evidence --show --case case_1 --step sign_in --touchpoint browser --tail 80
journey evidence --paths --step report_issue --touchpoint browser
journey evidence --paths --run 8bc31a94e2f1
```

## Replay

`JourneyBrowserPage` stores URL, cookies, and local storage. Reopen it with `open_page(saved_page)` inside a later step.
Do that only when the later step needs the same browser state as a real replay boundary. Do not pass live Playwright
objects through step results.

When a step opens a page with `open_page(...)` but returns a different domain object, Journey also records that browser
page as a replayable side output for the step. Use `browser_page_from_step_result(anchor_result)` inside a later step to
recover the last page opened by the step that produced `anchor_result`. `journey dev <step_label>` uses the same
browser side output to inspect the rendered page after an ordinary setup step.
