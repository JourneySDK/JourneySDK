# Simple Journey: Browser and Webhook Flow

This stage turns the core `journey` ideas into a realistic cross-system example.

The journey opens a local demo page with Playwright, branches into two flows, waits for a webhook in one branch, and
checks a downloaded file in the other branch.

## What this teaches

- how browser automation fits into ordinary `journey.step(...)` functions
- how the official webhook tool returns a step callable
- how one journey can cover a browser path and a webhook path together
- how targeted execution with `--step` works in a realistic flow

## Files to read

- `example/simple_journey/simple_journey.py`
- `example/simple_journey/demo_site.html`

## Playwright note

This is the only tutorial stage that needs Playwright. If Chromium is not installed yet, run this once:

```bash
uv run --with playwright python -m playwright install chromium
```

## Run it

1. Plan the journey:

```bash
uv run journey plan --file example/simple_journey/simple_journey.py
```

What to expect:

- one discovered journey called `simple_journey`
- two planned cases
- labels for the shared page setup, the webhook branch, and the file branch

2. Execute the full flow:

```bash
uv run --with playwright journey execute --file example/simple_journey/simple_journey.py
```

What to expect:

- `assert_demo_homepage` opens the local HTML page and checks the UI
- one case clicks the button that sends a webhook to `receive_webhook_endpoint_a`
- the other case downloads a file and verifies its contents
- both cases finish successfully

3. Execute only the file branch:

```bash
uv run --with playwright journey execute --file example/simple_journey/simple_journey.py --step assert_local_file_contents
```

What to expect:

- only one case is selected
- the final case line includes `stopped_at=assert_local_file_contents`
- the final case line also includes `replay_anchor=cp_1`

## Why this matters

This example shows the real strength of `journey`: the framework does not care whether a step talks to a browser, a
local file, or a webhook endpoint. If it is plain Python, it can be part of the same journey.

## Next step

Continue with [`playwright_resume_journey/README.md`](../playwright_resume_journey/README.md) to see how the official
Playwright tool can capture and resume a browser session.
