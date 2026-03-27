# journey Tutorial

This directory is a step-by-step tutorial for learning Journey SDK.

Every stage is runnable from the repository root. The stages start with plain Python, then add CLI filtering, branch
selection, retries, resume support, cloud-hosted webhooks, browser automation, local webhook handling, replay
behavior, and fail-fast execution.

## Before you start

- Run every command from the repo root.
- Use `uv run journey ...` for the pure-Python stages.
- The `simple_journey` and `playwright_resume_journey` stages need Playwright.

Optional Playwright setup for the browser stage:

```bash
uv run --with playwright python -m playwright install chromium
```

## Recommended order

1. [`first_journey/README.md`](first_journey/README.md): Start with one linear journey, plain `step(...)` calls, and
   `--file`.
2. [`selection_journeys/README.md`](selection_journeys/README.md): Learn discovery, `--journey`, and `--json` with
   multiple journeys in one file.
3. [`branching_journey/README.md`](branching_journey/README.md): Learn `checkpoint()`, `branch()`,
   `checkpoint(branches=[...])`, and targeted execution with `--step`.
4. [`retry_journey/README.md`](retry_journey/README.md): See same-step retries, `retry_from` with an earlier step,
   and `retry_from` with a checkpoint.
5. [`resume_journey/README.md`](resume_journey/README.md): Resume an interrupted run with `--state`.
6. [`cloud_webhook_journey/README.md`](cloud_webhook_journey/README.md): Point the official cloud webhook helpers at a
   hosted journey cloud service from a pure-Python journey.
7. [`simple_journey/README.md`](simple_journey/README.md): Run a realistic browser and local webhook flow with
   Playwright and the official local webhook tool.
8. [`playwright_resume_journey/README.md`](playwright_resume_journey/README.md): Capture a browser session as
   `PlaywrightPageState`, interrupt the run, and resume from the same authenticated page.
9. [`rehydration_journey/README.md`](rehydration_journey/README.md): Understand checkpoint replay and why later cases
   can reuse earlier work.
10. [`fail_fast_journeys/README.md`](fail_fast_journeys/README.md): Learn what `--fail-fast` changes when one journey
   fails.

## How to use this tutorial

- Read the short explanation at the top of each stage.
- Run the commands in order.
- Compare what you see with the "What to expect" notes.
- Move to the next stage once the current one makes sense.

If you only want the core authoring model, stages 1 through 5 cover the full public API and the main CLI flags without
any browser setup.
