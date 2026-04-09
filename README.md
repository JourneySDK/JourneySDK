# Journey SDK

Journey SDK is a workflow-as-code QA toolkit for testing long, branching, async, cross-system user journeys.

## Overview

Journey SDK is built around a simple idea: write one journey in sequential Python, then let Journey SDK compile it into
linear executable flows. It is designed for workflows where a single journey can touch browsers, edge devices,
background jobs, third-party services, voice or AI systems, and delayed side effects.

Each step is just plain Python, so teams can use existing testing tools and scripts without adapting them to a special
framework. A `step` can run browser automation, mobile checks, API assertions, or service-specific validation logic.
Official tools live under `journey.tools`; today that includes the `webhook` tool for hosting a local webhook
endpoint or acquiring a cloud-hosted one, the `email` tool for direct or cloud-hosted inbox access, and the
`playwright` tool for resumable page state. Retryable steps can poll for async effects or replay from an earlier step
or checkpoint.

That makes Journey SDK useful for flows such as:

- verifying a Stripe payment or a HubSpot ticket as part of a user journey
- polling until background work becomes visible in a third-party system
- capturing a browser screenshot and asking an LLM to validate non-deterministic or AI-driven UI output

## Who it's for

- QA engineers who need to test long, branching, cross-system flows without hand-writing every branch as a separate
  test
- developers and test engineers who want to express journey logic in plain Python instead of splitting it across
  multiple frameworks
- platform and workflow teams building internal automations, customer lifecycle flows, or agentic products with async
  steps and third-party integrations
- AI coding agents that need to generate, run, and iterate on journey tests while implementing features

## Install

Install the published package as:

```bash
pip install journey-sdk
```

For authoring, prefer importing only the Journey primitives you use:

```python
from journey import checkpoint, journey, step
```

## Authoring model

Write one journey in sequential Python with `step`, `checkpoint`, and optional step retries via
`step(..., retry=..., retry_delay=..., retry_from=...)`. Decorate module-level journey entrypoints with
`@journey`. Journey SDK compiles that authoring flow into linear executable cases so teams can cover branching
workflows without duplicating test code. Step functions are plain callables: pass every required input as explicit
arguments, and return any value that later steps or resumed runs must reuse.

When retries, persisted state, or checkpoint-started branches need to replay a
step, journey rehydrates that step from pickle-backed saved inputs and outputs.
Any step argument or return value that may be replayed that way must be
pickle-serializable.

Retryable steps can poll for async effects, rerun from the step itself, or replay from an earlier step/checkpoint.
They are retried when they raise an exception and `retry` is greater than 0. The explicit defaults are `retry=0`,
`retry_delay=5`, and `retry_from=None`; when retries are enabled and `retry_from` is omitted, the current step is
retried.

Official tools are ordinary Python helpers that return step callables or serializable helper values. For example, the
webhook tool can host a local endpoint before the app under test sends to it:

```python
from journey import step
from journey.tools.webhook import host_webhook_endpoint

receive_invoice_paid = host_webhook_endpoint(port=8765, path="/invoice-paid")

step(receive_invoice_paid, retry=3, retry_delay=1)
```

The same module can also use a cloud-hosted webhook endpoint:

```python
from journey import step
from journey.tools.webhook import get_webhook_endpoint, wait_for_webhook_request

endpoint = step(get_webhook_endpoint(path="/invoice-paid"))
step(send_invoice_paid_callback, endpoint.url)
request_payload = step(
    wait_for_webhook_request(path="/invoice-paid", timeout=1, poll_interval=0.1),
    endpoint,
    retry=3,
    retry_delay=1,
)
```

The official email tool follows the same step-oriented model. It can use direct SMTP + IMAP credentials or fall back
to Journey Cloud:

```python
from journey import step
from journey.tools.email import get_email_inbox, send_email, wait_for_email

inbox = step(get_email_inbox())
step(send_email(subject="Welcome", text_body="Hello from Journey"))
message = step(
    wait_for_email(subject_contains="Welcome", timeout=1, poll_interval=0.1),
    inbox,
)
```

```python
from journey import step

created = step(create_subscription)
step(
    invoice_paid,
    created,
    retry=15,
    retry_delay=2,
    retry_from=created,
)
```

The Playwright tool packages one page into a resumable step value:

```python
from journey.tools.playwright import (
    PlaywrightPageState,
    capture_page_state,
    open_page,
)

def login_and_capture_session():
    with open_page(PlaywrightPageState.from_url("https://app.example/login")) as page:
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url("**/dashboard")
        return capture_page_state(page)

def assert_dashboard(session):
    with open_page(session) as page:
        assert page.url.endswith("/dashboard")
```

Interrupted executions can also be resumed with `journey execute --state run.state`. When state persistence is
enabled, journey stores the step inputs and outputs it may need to replay later, so those values must be
pickle-serializable. The same rule applies to steps that may be replayed because of retries or
`branch(start_from=...)`. The state file is kept after the run finishes, so rerunning the same command can reuse that
saved progress; delete the file when you want to start fresh.

## How it works

1. Write a journey in Python using the public primitives from `journey/api.py`.
2. Compile it in dry-run mode with `journey plan`, which turns the authored journey into linear cases.
3. Execute all cases, or just the case that reaches one target step label, with `journey execute`.

The default human-readable CLI output streams progress in real time: it prints each case start, branch selection,
step attempt, retry, step status, and case completion as execution happens.

CLI commands discover functions annotated with `@journey` in the current directory. Use `--file`
to scope to one file, `--journey` to scope to one decorated function name, and `--step` to execute only the single
flow that reaches a target step label. Use `--develop-step` to run that same single case interactively, pausing
after the target step and each later step so you can continue or retry while iterating on one part of the journey.

## Core principles

- **Workflow as code**: author one test journey in Python and let Journey SDK compile it into linear flows
- **Simplicity over flexibility**: keep the framework footprint small so the testing logic stays easy to follow
- **Tool-friendly**: integrate external systems and domain-specific tools without forcing them into a custom DSL
- **Journey-centric**: optimize around the full business process rather than isolated pages or API calls
- **Single-step execution**: make it cheap to run only the flow that reaches a target step label during development
- **Interactive step retries**: pause on one target step and each later step so local iteration feels fast

## Quick start

Plan a journey without executing it:

```bash
uv run journey plan
```

Execute all compiled cases:

```bash
uv run journey execute
```

The default output streams one append-only log line per execution event, including retries and per-case durations.

Execute with persisted state so Ctrl-C can be resumed later:

```bash
uv run journey execute --state run.state
```

Execute only the path that reaches a target step label:

```bash
uv run journey execute --step assert_local_file_contents
```

Execute one target path interactively and pause after that step and each later step:

```bash
uv run journey execute --develop-step assert_local_file_contents
```

The cloud webhook helpers use `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL` at execution time only, so planning
stays side-effect free. Point those variables at your hosted cloud control plane or any compatible service:

```bash
export JOURNEY_CLOUD_API_KEY=journey-demo-key
export JOURNEY_CLOUD_BASE_URL=https://journey-cloud.example.test
```

If you want the email tool to use a direct mail server instead of Journey Cloud, configure these execution-time
variables:

```bash
export JOURNEY_EMAIL_ADDRESS=qa@example.test
export JOURNEY_EMAIL_FROM_ADDRESS=journey@example.test
export JOURNEY_EMAIL_SMTP_HOST=smtp.example.test
export JOURNEY_EMAIL_SMTP_PORT=587
export JOURNEY_EMAIL_SMTP_USERNAME=journey-user
export JOURNEY_EMAIL_SMTP_PASSWORD=journey-pass
export JOURNEY_EMAIL_SMTP_STARTTLS=true
export JOURNEY_EMAIL_IMAP_HOST=imap.example.test
export JOURNEY_EMAIL_IMAP_PORT=993
export JOURNEY_EMAIL_IMAP_USERNAME=journey-user
export JOURNEY_EMAIL_IMAP_PASSWORD=journey-pass
export JOURNEY_EMAIL_IMAP_SSL=true
export JOURNEY_EMAIL_IMAP_MAILBOX=INBOX
```

When those direct email settings are absent or incomplete, the official email tool falls back to Journey Cloud and
uses the default hosted inbox assigned to the active `JOURNEY_CLOUD_API_KEY`.

Journey Cloud authenticates SDK control-plane calls with `Authorization: Bearer $JOURNEY_CLOUD_API_KEY`. The same
pattern should apply to all Journey cloud tools: the first API key that reserves a cloud resource becomes its owner.
That means a webhook path, mail inbox, or similar cloud-managed identifier belongs to the API key that claimed it
first, and other API keys should not be able to reserve or manage that same resource afterward.

## Testing

Run the full framework suite from this root:

```bash
uv run pytest
```

If you are working in the combined workspace and changed shared cloud email or webhook behavior, also run the sibling
service suite with this framework package injected:

```bash
cd ../private
uv run --with ../public --extra dev pytest
```

See [`docs/README.md`](docs/README.md) for the runnable handbook. It starts with getting oriented in Journey's
authoring model, then walks through branching, retries, resume, browser automation, Journey Cloud integrations, and
debugging failure modes with code, commands, and expected CLI output.
