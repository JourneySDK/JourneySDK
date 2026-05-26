# Journey SDK

With AI, testing is the new coding.

## Overview

Journey SDK gives developers and coding assistants a way to quickly verify that user journeys still work, not just
units or partial integrations. One Python spec covers all branches and touchpoints like browser, email, payments, etc.
State rehydration replays only part of a long user journey, making it fast to execute many branches and verify the
single step you are working on over and over again. Use `.prompt` to write browser test steps in natural language.

The core value is:

- **One Python spec for all branches**: use `branch()` inside ordinary Python so one journey compiles into the executable
  cases users can take, without copying shared setup into separate tests.
- **State rehydration for fast replay**: use `branch(start_from=...)` so later branch cases start from a known durable
  step boundary instead of replaying expensive browser, account, cart, or service setup.
- **Touchpoints for every system in the journey**: drive or verify each system, service, and channel involved in the
  user flow. A journey might act through the browser touchpoint, then check email, webhooks, payment providers, CRM
  records, support/Ops queues, SMS, or back-office systems through other touchpoints.
- **Interrupt long waits, resume later**: default persistent state lets a test stop while waiting on async work or a
  third-party service and continue later from an explicit replay boundary.
- **Natural-language browser steps with `.prompt`**: describe browser behavior in natural language with
  `page.prompt(...)`, use prompt memory as a versioned fast path for repeat runs, and keep tests editable by the same AI
  coding assistants that write application code.

That makes Journey SDK useful for flows such as:

- testing checkout paths such as card versus wallet payment from the same cart setup
- waiting for email, SMS, voice, webhook, payment, or third-party side effects without keeping a laptop busy
- asking an LLM-driven browser step to complete UI work while prompt memory compiles successful model work into a
  reusable fast path
- iterating on one failed late step without rerunning the whole journey from the beginning

## Who it's for

- developers and test engineers who want one Python journey for all meaningful user paths
- QA teams replacing duplicated browser/API/channel tests with compiled journey cases
- platform teams testing lifecycle flows that cross email, SMS, voice agents, payments, webhooks, and third-party APIs
- AI coding agents that need to generate, run, and iterate on tests while implementing features

## AI Agent Support

Use the packaged assistant instructions when an AI coding agent needs to create, execute, debug, or maintain Journey
SDK journeys:

```bash
journey --agent-instructions codex
journey --agent-instructions claude --install-agent-instructions
journey --agent-instructions cursor --install-agent-instructions
```

Printing is the default; install mode writes the selected assistant's default project file and refuses to replace an
existing file unless `--force-agent-instructions` is passed.

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
from journeysdk import branch, journey, step
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

Playwright and LangChain are included in the default install. The first browser step automatically downloads Chromium
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

Write one journey in sequential Python with `step`, `branch`, `branch(start_from=...)`, default state, and optional step
retries via `step(..., retry=..., retry_delay=..., retry_from=...)`. Decorate module-level journey entrypoints with
`@journey`. Journey SDK compiles that authoring flow into linear executable cases so teams can cover branching user
paths without duplicating test code.

Step functions are plain callables: pass every required input as explicit arguments, and return any value that later
explicit replay boundaries must reuse. Procedure-sized steps are the durable unit: `branch(start_from=...)` and steps
with positive `retry=...` create replay anchors, while ordinary interrupted steps restart from the nearest replay
boundary or from the case beginning.

## Touchpoints

A **Touchpoint** is any system, service, or communication channel that participates in the journey being tested. Think
of the tested service as the center of the story, and touchpoints as the places where the journey crosses system
boundaries:

- the browser or client where a user performs actions
- email, SMS, WhatsApp, voice, or push channels where the user receives messages
- payment gateways, webhook receivers, CRMs, support/Ops queues, and back-office systems where side effects appear
- local infrastructure, such as a Docker Compose app, that must be started, inspected, or restored during a run

Touchpoints are not a separate DSL. They are Python helpers used from ordinary Journey steps. Some touchpoints drive
the journey forward, such as opening a browser page or sending an email. Others observe or verify effects, such as
waiting for a webhook request or checking an inbox. A single step can use one touchpoint, and a single journey can move
through many touchpoints as the user flow crosses systems.

Official touchpoints live under `journeysdk.touchpoints`. Use them when the SDK provides a general-purpose helper,
such as browser pages, hosted email inboxes, hosted webhook endpoints, or Docker snapshots. App-specific
touchpoints, such as a Stripe assertion, HubSpot ticket lookup, internal admin API, or custom back-office check, should
stay as normal Python code beside your journey until the SDK documents an official helper for that surface.

For example, one checkout journey can create a cart once, exercise card and wallet payment paths from that cart, use
`page.prompt(...)` to drive the browser, wait for email and SMS, then verify the returned order id:

```python
from journeysdk import branch, journey, step
from journeysdk.touchpoints.email import get_email_inbox
from journeysdk.touchpoints.browser import open_page


def checkout(cart, inbox, method) -> dict[str, object]:
    page = open_page(app_checkout_url(cart))
    return page.prompt(
        f"Check out with {method}. Send receipts to {inbox.address}.",
        memory="checkout",
        output={"order_id": "The id of the created order."},
    )


@journey
def checkout_journey() -> None:
    inbox = step(get_email_inbox())
    cart = step(create_cart, inbox.address)

    if branch(start_from=cart):
        order = step(checkout, cart, inbox, "card")
    elif branch(start_from=cart):
        order = step(checkout, cart, inbox, "wallet")

    messages = step(wait_for_email_and_sms, order["order_id"], inbox)
    step(mark_order_ready, order["order_id"], messages)
```

`get_email_inbox()` and `open_page()` are documented SDK touchpoints. Functions such as `create_cart`,
`wait_for_email_and_sms`, and `mark_order_ready` are app-specific integration code. Voice agents, SMS, WhatsApp,
payments, and third-party APIs should stay app-specific unless the docs describe an official helper.

Retryable steps can poll for async effects, rerun from the step itself, or replay from an earlier step. They are
retried when they raise an exception and `retry` is greater than 0. The explicit defaults are `retry=0`,
`retry_delay=5`, and `retry_from=None`; when retries are enabled and `retry_from` is omitted, the current step is
retried.

## Glossary

- **Journey**: one decorated Python function that describes the full user journey under test.
- **Case**: one linear executable path compiled from a journey, including one selected inline `if branch()` /
  `elif branch()` choice where the journey can split.
- **Step**: one `step(...)` call and the plain Python function it runs.
- **Step boundary**: the boundary before and after a step where Journey can stop, report progress, retry, or resume.
- **Touchpoint**: a system, service, or channel that participates in the tested journey, such as a browser, email,
  webhook, payment gateway, CRM, support/Ops system, SMS provider, or back-office process.
- **State file**: the default `.journey/state.json` file that stores selected cases, sanitized completed case reports,
  active progress, replay step bindings, and branch-anchor snapshots.
- **Saved step binding**: stored step inputs, metadata, and optional result that Journey can use at an explicit replay
  boundary.
- **Dirty step**: the step that had started but had not completed when execution was interrupted.
- **Graceful interrupt**: the first Ctrl-C in a CLI run with persistent state. Journey lets the active step reach
  post-exit so progress can be saved and the next run can continue from the nearest replay boundary.
- **Forced interrupt**: the second Ctrl-C in a CLI run with persistent state. Journey stops the active dirty step as
  soon as it can; the next run restarts from the nearest replay boundary instead of jumping into the middle of the
  function.
- **Replay**: rerunning part of a case from a step boundary while reusing saved values before that boundary.
- **Replay boundary**: the step index where replay starts.
- **Replay anchor**: the step label reported for a targeted branch run or used by retry and branch replay.
- **Branch-anchor snapshot**: saved records, step bindings, retry counters, and attempt counters captured after an
  anchor step reaches post-exit.
- **Branch**: an inline `if branch(): ... elif branch(): ...` arm that compiles into a separate case.
- **Targeted run**: a `--step LABEL` run that executes the one case reaching that label and stops after it. A reported
  `replay_anchor` identifies the branch step anchor, but targeted runs do not skip directly to that anchor.
- **Step lifecycle**: initialization, execution, storage, pre-exit, exit, and post-exit for one step attempt.
- **Develop-step pause**: a `--develop-step LABEL` stop at pre-exit after the selected step has been stored and before
  returned handles are exited, used for quick edit-run loops.
- **Pause action**: `continue` or `retry` after a develop-step pause.
- **Rehydration**: storing and restoring values that cross replay boundaries.
- **Rehydratable value**: a value with `__store__` and `__restore__` hooks for custom replay storage.

## Journey Rehydration Protocol

When positive retries or step-started branches need to reuse a step value across a replay boundary, Journey rehydrates
that value from SDK-managed saved step bindings. Any step argument or return value that may cross one of those
boundaries must be pickle-serializable or implement the Journey rehydration protocol:

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
`context.boundary_kind` and `context.boundary_id` when a value needs different
behavior for active state, step bindings, or branch-anchor snapshots.

Restored values should be usable as later step inputs. For values backed by live
external resources, store enough data to reopen the resource explicitly in the
next step instead of trying to pickle the live resource itself. Official touchpoints
follow this pattern: `JourneyBrowserPage` stores browser state, and later
steps reopen it with `open_page(saved_page)`.

## Step Lifecycle

Official touchpoints that open live resources inside a step should return an object
with the standard context-manager `__exit__(exc_type, exc, traceback)` method.
Each step attempt has six phases:

1. **Initialization**: Journey restores saved values, calls `__restore__`
   hooks when needed, and resolves the arguments passed to the step function.
2. **Execution**: Journey calls the step function. The function may succeed,
   fail, retry, or be interrupted.
3. **Storage**: Journey calls `__store__` hooks when needed and stores the
   step inputs plus the returned value in the state file.
4. **Pre-exit**: `--develop-step` pauses here after a matched step, with
   returned handles still live.
5. **Exit**: Journey discovers returned `__exit__` handles and closes them
   before the next step runs.
6. **Post-exit**: in a CLI run with persistent state, the first Ctrl-C stops here after the completed step has been saved and
   exited.

In noninteractive `--develop-step` mode, Journey stores the returned value,
pauses at pre-exit, then closes returned handles before the command exits. With
`--develop-step --interactive`, Journey shows the continue/retry prompt while
those handles are still live, then closes them after the user chooses
`continue` or `retry`, or cancels the prompt.

Use this pattern when a touchpoint owns a resource that should not outlive the step
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

Keep lifecycle methods idempotent, and close only resources owned by that touchpoint
call. If the step returns a value that must survive retries, persistent state, or
branch replay, that value should also implement the Journey rehydration
protocol above; do not rely on pickling live resources. `JourneyBrowserPage`
is the canonical example because it implements both protocols: `__exit__`
closes the live browser objects at step exit, while `__store__` / `__restore__`
save enough browser state for a later step to reopen the page explicitly.

Some resources need to stay live across multiple steps in one case. Those
handles should implement `__case_exit__(exc_type, exc, traceback)` instead of
`__exit__`. Journey discovers returned `__case_exit__` handles using the same
direct-result, tuple, list, and dict traversal rules, then closes them when the
case exits successfully or unsuccessfully. On successful case exit,
`__case_exit__` receives `(None, None, None)`; on failure or interruption it
receives the active exception triple. `DockerComposeStack` uses this case
lifecycle so a Compose app can be started in one step, used by later browser or
API steps, and still be stopped at case exit.

Official touchpoints are ordinary Python helpers that return step callables or serializable helper values. For example, the
webhook touchpoint can acquire a Journey Cloud-hosted endpoint before the app under test sends to it:

```python
from journeysdk import step
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request

endpoint = step(get_webhook_endpoint(path="/invoice-paid"))
step(send_invoice_paid_callback, endpoint.url)
request_payload = step(
    wait_for_webhook_request(path="/invoice-paid", timeout=1, poll_interval=0.1),
    endpoint,
    retry=3,
    retry_delay=1,
)
```

The official email touchpoint follows the same step-oriented model and uses the default hosted inbox assigned to the active
Journey Cloud API key:

```python
from journeysdk import step
from journeysdk.touchpoints.email import get_email_inbox, send_email, wait_for_email

inbox = step(get_email_inbox())
step(send_email(subject="Welcome", text_body="Hello from Journey"))
message = step(
    wait_for_email(subject_contains="Welcome", timeout=1, poll_interval=0.1),
    inbox,
)
```

The Docker touchpoint can start a local Compose app as a step value and pair a step anchor with rollback of
Docker-managed volume contents. `DockerComposeStack` implements the rehydration
protocol and the case-exit lifecycle, so Journey stops the Compose project at
case exit without removing volumes:

```python
from journeysdk import branch, step
from journeysdk.touchpoints.docker import DockerLogMatcher, run_docker

def start_docker_stack():
    return run_docker(
        compose_file="docker-compose.yml",
        wait_for_logs=[
            DockerLogMatcher(
                service_name=r"^(app|web)$",
                message=r"server\s+ready",
                timeout=60,
            )
        ],
    )

stack = step(start_docker_stack)
baseline = step(capture_baseline_state, stack)
if branch(start_from=baseline):
    step(mutate_compose_app, stack)
elif branch(start_from=baseline):
    step(assert_compose_logs, stack)
```

Current Docker snapshots restore Docker-managed volume contents only. Snapshot payloads are stored under `.journey`
beside the state artifacts so they can be copied or removed with the rest of Journey's local state. Services should
keep durable, replay-relevant state in managed volumes so restarted containers can behave correctly after restore. Bind
mounts are allowed but treated as external host state and are not copied or restored; external volumes, read-only
volume mounts, unsupported mount types, and multi-container services are rejected so managed Docker state can stay
predictable.

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

The browser touchpoint packages one page into a resumable step value:

```python
from journeysdk.touchpoints.browser import (
    JourneyBrowserPage,
    open_page,
)

def login_and_capture_session():
    page = open_page("https://app.example/login")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard")
    return page

def assert_dashboard(session: JourneyBrowserPage) -> JourneyBrowserPage:
    page = open_page(session)
    assert page.url.endswith("/dashboard")
    return page
```

`JourneyBrowserPage` extends Playwright's sync `Page`, so ordinary Playwright page methods and locators work on the
returned object. See the [Playwright Page API](https://playwright.dev/python/docs/api/class-page) for reference.

Browser runs are recorded by default for debugging. Each `open_page()` browser context writes a Playwright trace zip,
video, and manifest under `.journey/recordings/` with flat sequence-prefixed filenames such as
`0001-case_1-login-attempt-1-context-1-run-<run-id>.trace.zip`. Open a trace with `playwright show-trace ...` to inspect
the timeline and DOM snapshots. Use `--no-browser-recording` or `execute(..., no_browser_recording=True)` when traces
and videos should not be written because page content may be sensitive.

The same live page can also run a bounded LLM action loop. By default, `page.prompt(...)` returns a plain string.
Pass `output=...` when you want LangChain structured output as a dictionary:

```python
from journeysdk.touchpoints.browser import open_page

def capture_popup_title() -> dict[str, object]:
    page = open_page("https://app.example/login")
    return page.prompt(
        'click on a "Sign in" button and get the title of the opened popup',
        memory="sign-in-popup",
        output={
            "popup_title": "The title of the opened popup.",
        },
    )
```

Set provider credentials with the provider's normal environment variables such as `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`. Browser prompts default to `anthropic:claude-haiku-4-5`; override that by passing a LangChain
model identifier like `model="anthropic:claude-haiku-4-5"` or by setting `JOURNEY_BROWSER_PROMPT_MODEL`.
By default, each `page.prompt(...)` inside a journey step stores a replayable fast path from successful runs in a
generated callsite memory file in the journey root: the current directory where you run `journey`, or `Path.cwd()` when
using `journeysdk.execute(...)` directly. Pass `memory="sign-in-popup"` to choose a stable name such as
`sign-in-popup.memory.md`, or pass `memory=None` to disable memory for one prompt. Commit and review memory files with
the journey specs so teammates and agents can reuse and improve them. Prompt memory acts like a new form of code
compilation for AI actions: the first successful run pays for the heavier LLM processing and writes a compact replayable
path, while later runs reuse that compiled path and fall back to the model only when the page no longer matches. Pass
`--no-memory` when you want a run to ignore and avoid updating prompt memory, or `--no-memory-update` when you want to
read existing memory without writing new updates.
The optional `output={...}` argument maps field names to descriptions or JSON-schema fragments and stores a
`dict[str, object]` return value instead of plain text.
Expectation wording in the instruction, such as `Expect ...`, is treated as required success criteria. If the browser
task cannot be completed or the current page does not satisfy those criteria, `page.prompt(...)` raises `RuntimeError`
instead of returning successful prompt output.

Interrupted executions resume by rerunning the same Journey command. When state persistence is enabled, Journey stores
only the step inputs and outputs needed by explicit replay boundaries, so those values must be pickle-serializable or
rehydratable. In the CLI, the first Ctrl-C during an active step is graceful: Journey logs that it is finishing the
active step, lets that step finish and exit, then stops at post-exit. Press Ctrl-C a second time to force an immediate
stop; Journey treats the current step as dirty. The next run restarts from the nearest explicit
`branch(start_from=...)` or positive `retry=...` boundary, or from the case beginning when none exists. With
`--no-state`, Ctrl-C stops immediately and the run cannot resume. Use `--no-state-update` to read existing progress
without writing new updates. Default state is cleared after a clean completion; interrupted and paused runs keep
`.journey/state.json` so the same command can resume.

## How it works

1. Write one journey spec in Python using `journey`, `step`, `branch`, and documented helpers from
   `journeysdk.touchpoints`.
2. Run `journey`, which compiles branch choices into linear executable cases and executes them.
3. Use `branch(start_from=...)`, positive retries, and state files to replay from explicit durable boundaries instead
   of rerunning every expensive setup step.
4. Rerun the same command to resume a long test interrupted while waiting on async work or a third-party service.
5. Use `--step` or `--develop-step` when you only want the case that reaches one target step label.
6. Use `page.prompt(..., memory=...)` when a browser step is easier to describe than hand-maintain with selectors.

### Adding Journey Specs

When a project has no existing convention, put new specs under `journeys/<feature>_journey.py`. A journey spec is plain
Python: define top-level step functions, call them from a module-level `@journey` function, and use stable step function
names because they become CLI labels for `--step`, `--develop-step`, state, retries, and branch replay.

Each `step(...)` should encapsulate a meaningful, retryable part of the user journey, such as
`clear_basket_and_add_items`, not a tiny implementation fragment. Use `branch(...)` for alternate user paths after
shared setup, and use `branch(start_from=step_result)` when later branch cases should restart from a saved step boundary.
Values crossing replay boundaries must be pickle-serializable or implement Journey's rehydration protocol.

Journey-owned CLI output is emitted on stdout through the Journey logger. The default `pretty` format is meant for
humans at a terminal, for example:

```console
      create_customer_profile  ok attempt=1 duration=0.012s
```

Use `--output structured` for the logfmt-style `[journey] time=... component=... event=...` format, or
`--output jsonl` when tooling should consume newline-delimited JSON log records. Use `--log-level
debug|info|warning|error|off` to tune output. The default is `info`; `--log-level off` suppresses all Journey-owned
output.

CLI commands discover functions annotated with `@journey` in the current directory. Use `--file`
to scope to one file, `--journey` to scope to one decorated function name, and `--step` to execute only the single
flow that reaches a target step label. A targeted run still starts from the selected case's beginning; a
`replay_anchor` in the report identifies the branch step anchor but does not mean Journey skipped shared setup.
Use `--develop-step` to run that same single case in development mode. By
default it executes one target step, stores state, prints the paused result, and exits so coding agents can iterate
with synchronous command calls. Run the same `--develop-step LABEL` command to retry that step from
its replay boundary. Add `--interactive` to keep the
current process open and prompt after each paused step. Develop-step retries are unlimited and do not spend the step's
configured `step(..., retry=...)` budget. Each retry or continue reloads and recompiles the journey file first, so
edits to the current step, later steps, or future journey structure are picked up. If the already-run part of the
selected case changed, Journey starts that case over so the reused prefix is not stale.

## Core principles

- **One Python spec for all branches**: author the full user journey once and let `branch()` compile the executable
  cases.
- **State rehydration for fast replay**: use `branch(start_from=...)` and positive retries to reuse saved setup from
  durable replay boundaries.
- **Interrupt long waits, resume later**: keep long journeys restartable by saving progress between steps by default.
- **Touchpoints for external tests**: integrate hosted inboxes, webhooks, browser pages, Docker snapshots, and
  app-specific channel or service code without forcing them into a custom DSL.
- **Natural-language browser steps with `.prompt`**: describe browser work in natural language with
  `page.prompt(...)` and let prompt memory reuse versioned compiled fast paths.
- **Built for AI coding assistants**: keep tests in ordinary Python files so coding agents can generate, edit, run, and
  debug them beside application code.

## Quick start

Execute all compiled cases:

```bash
uv run journey
```

The default output shows the compiled cases first, then a concise execution timeline. Add `--output structured` when
you need logfmt fields, or `--output jsonl` for one parseable JSON object per line.

Execute with default persisted state so Ctrl-C can be resumed later:

```bash
uv run journey
```

Press Ctrl-C once to stop after the active step reaches post-exit, or press it a second time to stop now and restart
from the nearest explicit replay boundary on resume. Use `--no-state` for a one-off run that should not read or write
state.

Execute only the case that reaches a target step label:

```bash
uv run journey --step assert_local_file_contents
```

Execute one target case in development mode and stop after the target step:

```bash
uv run journey --develop-step assert_local_file_contents
```

Rerun that command to retry the same step after editing code. For a human prompt loop, add
`--interactive`:

```bash
uv run journey --develop-step assert_local_file_contents --interactive
```

The cloud webhook and email helpers use `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL` at execution time. Point
those variables at your hosted cloud control plane or any compatible service:

```bash
export JOURNEY_CLOUD_API_KEY=journey-demo-key
export JOURNEY_CLOUD_BASE_URL=https://journey-cloud.example.test
```

The official webhook and email SDK touchpoints require Journey Cloud; the SDK no longer hosts local webhooks or talks
directly to SMTP/IMAP servers.

Journey Cloud authenticates SDK control-plane calls with `Authorization: Bearer $JOURNEY_CLOUD_API_KEY`. The same
pattern should apply to all Journey cloud touchpoints: the first API key that reserves a cloud-managed handle becomes its
owner. That means a webhook path, mail inbox, or similar cloud-managed identifier belongs to the API key that claimed
it first, and other API keys should not be able to reserve or manage that same handle afterward.

## Testing

Run the full framework suite from this root:

```bash
uv run pytest
```

Smoke test the built package and CLI locally:

```bash
./scripts/smoke_test_package.sh
```

See [`docs/README.md`](docs/README.md) for the runnable handbook. It starts with one Python spec for all branches, then
walks through state rehydration, retries, interrupting long waits and resuming later, browser automation with
`page.prompt(...)`, Journey Cloud touchpoints, and debugging failure modes with code, commands, and expected CLI output.
