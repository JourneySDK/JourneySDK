# Journey SDK

Journey SDK is a workflow-as-code QA toolkit for testing long, branching, async, cross-system user journeys.

## Overview

Journey SDK is built around a simple idea: write one long-running journey in sequential Python, then let Journey SDK
compile it into linear executable flows that can stop and resume at step boundaries. It is designed for workflows where
a single journey can touch browsers, edge devices, background jobs, third-party services, voice or AI systems, and
delayed side effects.

Every `step(...)` call is an interruption boundary. With `--state`, Journey saves the successful steps that came before
the active step. If execution is interrupted while a step is running, Journey never resumes inside that function body;
the next run restarts the affected step from the top with the same saved inputs.

Each step is just plain Python, so teams can use existing testing tools and scripts without adapting them to a special
framework. A `step` can run browser automation, mobile checks, API assertions, or service-specific validation logic.
Official tools live under `journeysdk.tools`; today that includes the `webhook` tool for hosting a local webhook
endpoint or acquiring a cloud-hosted one, the `email` tool for direct or cloud-hosted inbox access, the `docker`
tool for local Compose-backed snapshots, and the `playwright` tool for resumable page state plus bounded LLM-driven
page interaction. Retryable steps can poll for async effects or replay from an earlier step or checkpoint.

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

## AI Agent Support

Use the [`journey-developer` skill](skills/journey-developer/SKILL.md) when an AI coding agent needs to create,
execute, debug, or maintain Journey SDK journeys.

## Install

### Install The Python Package

Install Journey SDK into an existing environment:

```bash
pip install journey-sdk
```

Or add it to a `uv`-managed project:

```bash
uv add journey-sdk
```

For authoring, import only the Journey primitives you use:

```python
from journeysdk import checkpoint, journey, step
```

### Install The CLI

Run the CLI once without installing it:

```bash
uvx --from journey-sdk journey --help
```

Install a persistent CLI with `uv`:

```bash
uv tool install journey-sdk
journey --help
```

If your shell cannot find `journey` yet, refresh the shell PATH hook:

```bash
uv tool update-shell
```

Install the CLI inside a virtual environment with `pip`:

```bash
python -m pip install journey-sdk
journey --help
```

Use the CLI from a project-local environment:

```bash
uv add journey-sdk
uv run journey --help
```

Playwright and LiteLLM are included in the default install. The first browser step automatically downloads Chromium
in the active environment, so there is no separate `playwright install` step for the standard Journey SDK flow.

See [`docs/00-installation-and-cli.md`](docs/00-installation-and-cli.md) for the full CLI installation guide, local
editable installs, and local wheel smoke testing.

### Develop Locally

```bash
uv sync --extra dev
uv run pytest
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for editable-install workflows, the local package smoke test, and the manual
publish checklist.

## Authoring model

Write one journey in sequential Python with `step`, `checkpoint`, and optional step retries via
`step(..., retry=..., retry_delay=..., retry_from=...)`. Decorate module-level journey entrypoints with
`@journey`. Journey SDK compiles that authoring flow into linear executable cases so teams can cover branching
workflows without duplicating test code. Step functions are plain callables: pass every required input as explicit
arguments, and return any value that later steps or resumed runs must reuse. The step boundary is the durable unit:
successful steps can be reused, while interrupted or retried steps restart from the top with saved inputs.

Retryable steps can poll for async effects, rerun from the step itself, or replay from an earlier step/checkpoint.
They are retried when they raise an exception and `retry` is greater than 0. The explicit defaults are `retry=0`,
`retry_delay=5`, and `retry_from=None`; when retries are enabled and `retry_from` is omitted, the current step is
retried.

## Glossary

- **Journey**: one decorated Python function that describes the full workflow under test.
- **Case**: one linear executable path compiled from a journey, including one selected branch choice for each branch
  group.
- **Step**: one `step(...)` call and the plain Python function it runs.
- **Step boundary**: the boundary before and after a step where Journey can save progress, stop, retry, or resume.
- **State file**: the `--state` file that stores selected cases, completed case reports, active progress, saved step
  bindings, and checkpoint snapshots.
- **Saved step binding**: stored step inputs, metadata, and optional result that Journey can use when replaying or
  resuming.
- **Dirty step**: the step that had started but had not completed when execution was interrupted.
- **Replay**: rerunning part of a case from a step boundary while reusing saved values before that boundary.
- **Replay boundary**: the step or checkpoint index where replay starts.
- **Replay anchor**: the checkpoint name reported for a targeted branch run or used by retry/checkpoint replay.
- **Checkpoint snapshot**: saved records, step bindings, retry counters, and attempt counters captured at a checkpoint.
- **Branch**: an inline `if branch(): ... elif branch(): ...` arm that compiles into a separate case.
- **Targeted run**: a `--step LABEL` run that executes the one case reaching that label and stops after it. A reported
  `replay_anchor` identifies the branch checkpoint, but targeted runs do not skip directly to that checkpoint.
- **Develop-step pause**: a `--develop-step LABEL` stop after the selected step, used for quick edit-run loops.
- **Pause action**: `continue` or `retry` after a develop-step pause.
- **Rehydration**: storing and restoring values that cross replay boundaries.
- **Rehydratable value**: a value with `__store__` and `__restore__` hooks for custom replay storage.
- **Step-exit lifecycle**: cleanup of returned values that expose `__exit__` after Journey has saved the step result.

## Journey Rehydration Protocol

When retries, `--state`, checkpoint replay, or checkpoint-started branches need
to reuse a step value across a replay boundary, Journey rehydrates that value
from SDK-managed saved step bindings. Any step argument or return value that
may cross one of those boundaries must be pickle-serializable or implement the
Journey rehydration protocol:

```python
class ExternalState:
    def __store__(self, context):
        return {"payload": "pickle-serializable"}

    @classmethod
    def __restore__(cls, payload, context):
        return cls(...)
```

`__store__(context)` returns a pickle-serializable payload. Journey stores the
payload together with an importable reference to the value's class, so custom
rehydratable classes must be defined at module top level, not inside a function.
`__restore__(payload, context)` receives that payload and returns the restored
step value.

The context object describes where and why the value is being stored or
restored. Use `context.artifact_root` for larger file artifacts. Inspect
`context.boundary_kind`, `context.boundary_id`, and `context.checkpoint_name`
when a value needs different behavior for active state, step bindings, or
checkpoint snapshots.

Restored values should be usable as later step inputs. For values backed by live
external resources, store enough data to reopen the resource explicitly in the
next step instead of trying to pickle the live resource itself. Official tools
follow this pattern: `JourneyPlaywrightPage` stores browser state, and later
steps reopen it with `open_page(saved_page)`.

## Step-Exit Tool Lifecycle

Official tools that open live resources inside a step should return an object
with the standard context-manager `__exit__(exc_type, exc, traceback)` method.
After a step function returns, Journey stores the returned value, discovers
returned `__exit__` handles, and closes them before the next step runs. In
noninteractive `--develop-step` mode, Journey stores the returned value and
closes returned handles before the command exits. With
`--develop-step --interactive`, Journey stores the returned value and shows the
continue/retry prompt while those handles are still live, then closes them after
the user chooses `continue` or `retry`, or cancels the prompt.

Use this pattern when a tool owns a resource that should not outlive the step
attempt:

```python
class ResourceHandle:
    def __init__(self):
        self._resource = acquire_resource()
        self._closed = False

    def __exit__(self, exc_type, exc, traceback):
        if self._closed:
            return
        self._closed = True
        self._resource.close()


def open_resource():
    return ResourceHandle()


def use_resource():
    handle = open_resource()
    handle.do_work()
    return handle
```

Journey looks for lifecycle handles in the direct step result and inside
built-in `tuple`, `list`, and `dict` containers. It de-duplicates handles by
object identity and calls `__exit__` in reverse discovery order. On successful
step returns, `__exit__` receives `(None, None, None)`. Journey ignores the
return value, so `__exit__` cannot suppress cleanup failures.

The important constraint is visibility: Journey only auto-exits handles it can
see in the returned value graph. A live local resource that is not returned is
outside this protocol. Either return the handle, return a container that
contains it, or close it explicitly with local `try` / `finally` code.

Keep lifecycle methods idempotent, and close only resources owned by that tool
call. If the step returns a value that must survive retries, `--state`, or
checkpoint replay, that value should also implement the Journey rehydration
protocol above; do not rely on pickling live resources. `JourneyPlaywrightPage`
is the canonical example because it implements both protocols: `__exit__`
closes the live browser objects at step exit, while `__store__` / `__restore__`
save enough browser state for a later step to reopen the page explicitly.

Official tools are ordinary Python helpers that return step callables or serializable helper values. For example, the
webhook tool can host a local endpoint before the app under test sends to it:

```python
from journeysdk import step
from journeysdk.tools.webhook import host_webhook_endpoint

receive_invoice_paid = host_webhook_endpoint(port=8765, path="/invoice-paid")

step(receive_invoice_paid, retry=3, retry_delay=1)
```

The same module can also use a cloud-hosted webhook endpoint:

```python
from journeysdk import step
from journeysdk.tools.webhook import get_webhook_endpoint, wait_for_webhook_request

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
from journeysdk import step
from journeysdk.tools.email import get_email_inbox, send_email, wait_for_email

inbox = step(get_email_inbox())
step(send_email(subject="Welcome", text_body="Hello from Journey"))
message = step(
    wait_for_email(subject_contains="Welcome", timeout=1, poll_interval=0.1),
    inbox,
)
```

The Docker tool can start a local Compose app as a step value and pair a checkpoint with exact rollback of container
filesystems plus Docker-managed volume contents. `DockerComposeStack` already implements the rehydration protocol, so
plain `checkpoint()` is enough:

```python
from journeysdk import checkpoint, step
from journeysdk.tools.docker import run_docker

stack = step(run_docker(compose_file="docker-compose.yml"))
after_boot = checkpoint()
step(assert_compose_logs, stack)
```

Current Docker snapshots are intentionally strict: bind mounts, external volumes, read-only mounts, and multi-container
services are rejected so restore can stay exact and predictable.

```python
from journeysdk import step

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
from journeysdk.tools.playwright import (
    JourneyPlaywrightPage,
    open_page,
)

def login_and_capture_session():
    page = open_page("https://app.example/login")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard")
    return page

def assert_dashboard(session: JourneyPlaywrightPage) -> JourneyPlaywrightPage:
    page = open_page(session)
    assert page.url.endswith("/dashboard")
    return page
```

The same live page can also run a bounded LLM action loop and return a structured result:

```python
from journeysdk.tools.playwright import JourneyPlaywrightPromptResult, open_page

def capture_popup_title() -> JourneyPlaywrightPromptResult:
    page = open_page("https://app.example/login")
    try:
        return page.prompt(
            'click on a "Sign in" button and get the title of the opened popup',
            model="anthropic/claude-sonnet-4-5",
        )
    finally:
        page.__exit__(None, None, None)
```

Set provider credentials with the provider's normal environment variables such as `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`, and either pass `model=...` or set `JOURNEY_PLAYWRIGHT_PROMPT_MODEL`.

Interrupted executions can also be resumed with `journey --state run.state`. When state persistence is
enabled, Journey stores the step inputs and outputs it may need to replay later, so those values must be
pickle-serializable. If a run is interrupted while a step is active, the saved dirty step restarts from the top on the
next run with the same inputs; Journey does not resume inside the function body. The same replay rule applies to steps
that may be replayed because of retries or `branch(start_from=...)`. The state file is kept after the run finishes, so
rerunning the same command can reuse that saved progress; delete the file when you want to start fresh.

## How it works

1. Write a journey in Python using the primitives from `journeysdk/api.py`.
2. Run `journey`, which compiles the authored journey into linear cases and executes them.
3. Use `--step` when you only want the case that reaches one target step label.

The default human-readable CLI output prints the compiled plan and final summaries on stdout. Live diagnostics stream
to stderr as structured Journey log lines, for example:

```console
[journey] time=2026-04-25T10:30:12.345Z level=INFO component=executor event=step_success message="  step create_customer_profile attempt=1 ok duration=0.012s" attempt=1 case=case_1 duration=0.012s file=docs/first_journey/first_journey.py journey=first_journey node_index=0 step=create_customer_profile
```

Use `--log-level debug|info|warning|error|off` to tune those diagnostics. The default is `info`; `--log-level off`
keeps stdout output but suppresses Journey diagnostics.

CLI commands discover functions annotated with `@journey` in the current directory. Use `--file`
to scope to one file, `--journey` to scope to one decorated function name, and `--step` to execute only the single
flow that reaches a target step label. A targeted run still starts from the selected case's beginning; a
`replay_anchor` in the report identifies the branch checkpoint but does not mean Journey skipped shared setup.
Use `--develop-step` to run that same single case in development mode. By
default it executes one target step, stores state, prints the paused result, and exits so coding agents can iterate
with synchronous command calls. Run the same `--develop-step LABEL --state dev.state` command to retry that step from
its replay boundary, or target the next step with the same state file to continue. Add `--interactive` to keep the
current process open and prompt after each paused step. Develop-step retries are unlimited and do not spend the step's
configured `step(..., retry=...)` budget. Each retry or continue reloads and recompiles the journey file first, so
edits to the current step, later steps, or future journey structure are picked up. If the already-run part of the
selected case changed, Journey starts that case over so the reused prefix is not stale.

## Core principles

- **Workflow as code**: author one test journey in Python and let Journey SDK compile it into linear flows
- **Simplicity over flexibility**: keep the framework footprint small so the testing logic stays easy to follow
- **Tool-friendly**: integrate external systems and domain-specific tools without forcing them into a custom DSL
- **Journey-centric**: optimize around the full business process rather than isolated pages or API calls
- **Interruptible step boundaries**: keep long journeys restartable by saving progress between steps and replaying from
  explicit boundaries
- **Single-step execution**: make it cheap to run only the flow that reaches a target step label during development
- **Fast step iteration**: retry one paused develop step from saved state without replaying the whole journey

## Quick start

Execute all compiled cases:

```bash
uv run journey
```

The default output shows the compiled cases first. Live execution logs are append-only stderr lines that include
component, event, timestamp, case, step, retry, and duration fields.

Execute with persisted state so Ctrl-C can be resumed later:

```bash
uv run journey --state run.state
```

Execute only the case that reaches a target step label:

```bash
uv run journey --step assert_local_file_contents
```

Execute one target case in development mode and stop after the target step:

```bash
uv run journey --develop-step assert_local_file_contents --state dev.state
```

Rerun that command to retry the same step after editing code. To continue, target
the next step with the same state file. For a human prompt loop, add
`--interactive`:

```bash
uv run journey --develop-step assert_local_file_contents --state dev.state --interactive
```

The cloud webhook helpers use `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL` at execution time. Point those
variables at your hosted cloud control plane or any compatible service:

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

Smoke test the built package and CLI locally:

```bash
./scripts/smoke_test_package.sh
```

See [`docs/README.md`](docs/README.md) for the runnable handbook. It starts with getting oriented in Journey's
authoring model, then walks through branching, retries, resume, browser automation, Journey Cloud integrations, and
debugging failure modes with code, commands, and expected CLI output.
