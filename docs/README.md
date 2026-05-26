# Journey Docs

Journey is easiest to learn when the code, the command, and the output stay next to each other. Keep the README
glossary nearby: the handbook uses its vocabulary for step boundaries, replay boundaries, branch-anchor snapshots, and
rehydration.

This directory does two jobs:

- it contains the runnable tutorial journeys under `docs/<journey_name>/...`
- it contains the handbook pages in this directory that explain why each pattern exists and what a healthy run looks like

Run every command in this handbook from the repository root.

## Before You Start

- Start with [00 Installation and CLI](00-installation-and-cli.md) if you need to install the package, install the
  persistent `journey` command, or work from a local checkout.
- AI coding agents can run `journey --agent-instructions codex|claude|cursor|generic` for Journey authoring and CLI
  iteration guidance, or add `--install-agent-instructions` to write the selected project file.
- Use `uv run journey --file ...` when you want to run one journey file.
- The browser chapter auto-installs Chromium the first time a browser step runs.
- Journey Cloud examples need execution-time environment variables:

```bash
export JOURNEY_CLOUD_API_KEY=<your-api-key>
export JOURNEY_CLOUD_BASE_URL=https://<cloud-base-url>
```

- The Docker Compose step-anchor snapshot example expects local `docker` and `docker compose` access when you execute it.
- With default state, CLI Ctrl-C is graceful the first time: Journey lets the active step reach post-exit, then resumes
  later from the nearest explicit replay boundary. Press Ctrl-C again to stop now; the dirty step restarts later from
  that same boundary, or from the case beginning when no boundary exists. Without state, Ctrl-C stops immediately and
  cannot resume.

## Touchpoints in One Minute

A journey often leaves the browser. It may create a checkout in the web app, receive a payment callback, send a receipt
email, open a support ticket, update a CRM record, or wait for an Ops workflow. In Journey, each of those systems,
services, or channels is a **touchpoint**.

Touchpoints are how a test talks to the world around the service under test:

- use a browser touchpoint to act like the user
- use email, webhook, SMS, payment, CRM, or support touchpoints to verify side effects
- use local infrastructure touchpoints, such as Docker Compose, to set up or restore systems needed by the journey

A touchpoint is different from a step. A step is the durable unit Journey can save, retry, resume, or replay. A
touchpoint is what that step talks to. Official SDK touchpoints are imported from `journeysdk.touchpoints`; anything
specific to your app can be a plain Python helper in the same journey file or your own test support module.

## Reading Order

1. [00 Installation and CLI](00-installation-and-cli.md)
2. [01 Getting Started](01-getting-started.md)
3. [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md)
4. [03 Retries and Resume](03-retries-and-resume.md)
5. [04 Browser and Local Touchpoints](04-browser-and-local-integrations.md)
6. [05 Journey Cloud Touchpoints](05-journey-cloud-integrations.md)
7. [06 Debugging and Failure Modes](06-debugging-and-failure-modes.md)

## Choose by Task

- If you need install commands for `pip`, `uv`, `uvx`, or the persistent CLI, start with
  [00 Installation and CLI](00-installation-and-cli.md).
- If you want to write your first Journey function, start with [01 Getting Started](01-getting-started.md).
- If you want one authored flow to become multiple executable paths, go to [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md).
- If you need polling, replay, or resumable state, go to [03 Retries and Resume](03-retries-and-resume.md).
- If your journey needs browser work, local files, or Docker Compose snapshots, go to [04 Browser and Local Touchpoints](04-browser-and-local-integrations.md).
- If your journey talks to Journey Cloud-hosted webhooks or email, go to [05 Journey Cloud Touchpoints](05-journey-cloud-integrations.md).
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
- `docs/browser_resume_journey/browser_resume_journey.py`
- `docs/browser_prompt_journey/browser_prompt_journey.py`
- `docs/cloud_webhook_journey/cloud_webhook_journey.py`
- `docs/cloud_email_journey/cloud_email_journey.py`
- `docs/fail_fast_journeys/fail_fast_journeys.py`

## One-Minute Tour

If you only want the shape of a Journey run, start here:

```bash
uv run journey --file docs/first_journey/first_journey.py
```

Expected pretty stdout includes:

```console
Plan
  docs/first_journey/first_journey.py:first_journey ...
      create_customer_profile  ok attempt=1 duration=... ...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

That output shows the core Journey model:

- one top-level function becomes one or more executable cases
- each case is still plain Python steps in order
- the CLI emits compiled cases, summaries, and step boundaries as pretty stdout logs by default
- `--output structured` switches to logfmt-style fields and `--output jsonl` switches to JSON Lines
- stateful runs can replay from step boundaries instead of rerunning everything from scratch

Continue with [01 Getting Started](01-getting-started.md) if Journey is new to you.
