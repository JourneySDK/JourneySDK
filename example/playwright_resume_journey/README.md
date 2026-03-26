# Playwright Resume Journey

This stage demonstrates a resumable browser session with the official Playwright tool.

The example logs in to a tiny local auth demo, captures the browser session as `PlaywrightPageState`, and then opens a
manual interrupt window in the next step. Tutorial notes printed to stderr explain what was saved and when to press
`Ctrl-C`.

## What this teaches

- how `PlaywrightPageState` captures one browser page as a serializable step value
- how `open_page(...)` rehydrates the same URL, cookies, and `localStorage`
- how a resumed case restarts the interrupted step with the same saved browser session instead of logging in again

## Files to read

- `example/playwright_resume_journey/playwright_resume_journey.py`

## Playwright note

This stage needs Playwright and Chromium. If Chromium is not installed yet, run this once:

```bash
uv run --with playwright python -m playwright install chromium
```

## Run it

1. Reset the demo:

```bash
uv run python -c "from example.playwright_resume_journey import reset_demo_state; reset_demo_state(state_path='/tmp/journey-playwright-resume-tutorial.state')"
```

2. Start the journey with a state file:

```bash
uv run --with playwright journey execute --file example/playwright_resume_journey/playwright_resume_journey.py --state /tmp/journey-playwright-resume-tutorial.state
```

When the run reaches `continue_authenticated_dashboard`, watch for the tutorial note that starts with
`[tutorial] Press Ctrl-C during the next 2.0 seconds...` and press `Ctrl-C` before that pause ends.

What to expect from the first run:

- `login_and_capture_session` signs in and saves a `PlaywrightPageState` for the dashboard
- `continue_authenticated_dashboard` reopens that authenticated page and prints that the saved browser state is ready
- the process exits as interrupted and leaves the state file behind

3. Run the exact same command again, but let it finish this time:

```bash
uv run --with playwright journey execute --file example/playwright_resume_journey/playwright_resume_journey.py --state /tmp/journey-playwright-resume-tutorial.state
```

What to expect from the second run:

- the case starts with `resume` instead of `start`
- `login_and_capture_session` does not rerun
- `continue_authenticated_dashboard` starts again on attempt 2 with the same saved `PlaywrightPageState`
- the protected action completes successfully without logging in again

## Why this matters

Real browser flows often depend on cookies and client-side storage. This example shows how `journey` can persist a
lightweight browser session value, restart the interrupted step from its boundary, and reopen the same authenticated
page on the resumed run.

## Next step

Continue with [`rehydration_journey/README.md`](../rehydration_journey/README.md) to see the more general checkpoint
rehydration model for later branches.
