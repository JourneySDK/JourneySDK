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

Prompt-generated Python snippets run with the active Playwright `page`, known `pages`, `timeout_ms`, `switch_page(...)`,
and a conservative set of safe builtins such as `print`, `len`, `str`, `sorted`, `isinstance`, and common exception
classes. `print(...)` output is captured for the next prompt turn instead of written directly to stdout. Imports, file
I/O, dynamic execution, and broad introspection helpers such as `__import__`, `open`, `eval`, `exec`, `compile`,
`globals`, `locals`, `dir`, `getattr`, and `setattr` are intentionally unavailable. Snippets must use sync Playwright
APIs, pass `timeout=timeout_ms` to timeout-aware actions and waits, and avoid long hard sleeps. Invalid prompt memory is
skipped before replay when possible; if replay code runs and then fails, fallback prompting continues from the current
live page state.

Prompt memory files are named `<memory>.memory.md` and stored next to the journey's `.journey` directory. For example,
`journeys/sign_in.py` uses `journeys/.journey/state.json` and stores `journeys/sign-in.memory.md`.

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
recover the last page opened by the step that produced `anchor_result`. This is what `journey discover <step_label>`
uses when extending an existing Journey from an ordinary setup step.
