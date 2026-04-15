# Journey Docs

Journey is easiest to learn when the code, the command, and the output stay next to each other.

This directory does two jobs:

- it contains the runnable tutorial journeys under `docs/<journey_name>/...`
- it contains the handbook pages in this directory that explain why each pattern exists and what a healthy run looks like

Run every command in this handbook from the repository root.

## Before You Start

- Start with [00 Installation and CLI](00-installation-and-cli.md) if you need to install the package, install the
  persistent `journey` command, or work from a local checkout.
- Use `uv run journey plan --file ...` when you want to see compiled cases without executing side effects.
- Use `uv run journey execute --file ...` when you want to run those cases.
- Install Playwright only for the browser chapter:

```bash
uv run --with playwright python -m playwright install chromium
```

- Journey Cloud examples need execution-time environment variables:

```bash
export JOURNEY_CLOUD_API_KEY=<your-api-key>
export JOURNEY_CLOUD_BASE_URL=https://<cloud-base-url>
```

- The Docker Compose snapshot example expects local `docker` and `docker compose` access when you execute it.

## Reading Order

1. [00 Installation and CLI](00-installation-and-cli.md)
2. [01 Getting Started](01-getting-started.md)
3. [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md)
4. [03 Retries and Resume](03-retries-and-resume.md)
5. [04 Browser and Local Integrations](04-browser-and-local-integrations.md)
6. [05 Journey Cloud Integrations](05-journey-cloud-integrations.md)
7. [06 Debugging and Failure Modes](06-debugging-and-failure-modes.md)

## Choose by Task

- If you need install commands for `pip`, `uv`, `uvx`, or the persistent CLI, start with
  [00 Installation and CLI](00-installation-and-cli.md).
- If you want to write your first Journey function, start with [01 Getting Started](01-getting-started.md).
- If you want one authored flow to become multiple executable paths, go to [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md).
- If you need polling, replay, or resumable state, go to [03 Retries and Resume](03-retries-and-resume.md).
- If your journey needs Playwright, local files, local webhooks, or Docker Compose snapshots, go to [04 Browser and Local Integrations](04-browser-and-local-integrations.md).
- If your journey talks to Journey Cloud-hosted webhooks or email, go to [05 Journey Cloud Integrations](05-journey-cloud-integrations.md).
- If you are debugging a failure or deciding whether to use `--fail-fast`, go to [06 Debugging and Failure Modes](06-debugging-and-failure-modes.md).

## Runnable Source Map

- `docs/first_journey/first_journey.py`
- `docs/selection_journeys/selection_journeys.py`
- `docs/branching_journey/branching_journey.py`
- `docs/rehydration_journey/rehydration_journey.py`
- `docs/retry_journey/retry_journey.py`
- `docs/resume_journey/resume_journey.py`
- `docs/simple_journey/simple_journey.py`
- `docs/docker_compose_journey/docker_compose_journey.py`
- `docs/playwright_resume_journey/playwright_resume_journey.py`
- `docs/cloud_webhook_journey/cloud_webhook_journey.py`
- `docs/cloud_email_journey/cloud_email_journey.py`
- `docs/fail_fast_journeys/fail_fast_journeys.py`

## One-Minute Tour

If you only want the shape of a Journey run, start here:

```bash
uv run journey plan --file docs/first_journey/first_journey.py
```

```console
Journey docs/first_journey/first_journey.py:first_journey
journey_id=first_journey function_ref=...
- case_1 branch_env={} labels=['create_customer_profile', 'send_welcome_message', 'assert_welcome_message_sent']

Summary: 1 journey planned, 1 case planned, 0 failed
```

That output shows the core Journey model:

- one top-level function becomes one or more executable cases
- each case is still plain Python steps in order
- the CLI tells you exactly which labels will run before you execute anything

Continue with [01 Getting Started](01-getting-started.md) if Journey is new to you.
