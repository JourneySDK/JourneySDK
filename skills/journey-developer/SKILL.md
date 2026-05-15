---
name: journey-developer
description: Develop, execute, debug, and maintain Journey SDK workflow-as-code QA/testing journeys for long, branching, async, cross-system user flows. Use when creating or updating Python journeys that use journeysdk primitives or official journeysdk.touchpoints integrations, running Journey CLI full or targeted step executions, iterating with --develop-step and default state, or keeping journey authoring guidance aligned with SDK, CLI, examples, and docs behavior.
---

# Journey Developer

## Journey SDK Context

Journey SDK is a workflow-as-code QA toolkit for testing long, branching, async, cross-system user journeys. Use this
skill when a Journey SDK journey tests product or business flows that touch browsers, APIs, mobile or edge devices,
background jobs, third-party services, webhooks, email, AI or voice systems, or delayed side effects. Use the README
glossary vocabulary when explaining behavior: step boundary, state file, saved step binding, dirty step, replay
boundary, replay anchor, branch-anchor snapshot, step lifecycle, develop-step pause, pause action, rehydration, and
rehydratable value.

Do not use this skill for generic Python scripts, generic unit tests, or unrelated workflow automation that is not authored as a Journey SDK journey.

## Official Touchpoints

Official touchpoints live under `journeysdk.touchpoints`. They are ordinary Python helpers that return step callables or
serializable helper values, so use them with `step(...)` and keep planning side-effect free. Acquire live or hosted
resources while steps execute, and make returned values serializable or rehydratable when they cross replay boundaries
for retry, branch, or persistent-state behavior.

- `journeysdk.touchpoints.webhook`: acquire a Journey Cloud-hosted endpoint, then wait for received webhook requests.
- `journeysdk.touchpoints.email`: get a Journey Cloud-hosted inbox, send email, and wait for received email.
- `journeysdk.touchpoints.docker`: start local Docker Compose apps and pair step anchors with exact snapshots for supported container and volume state.
- `journeysdk.touchpoints.browser`: open browser pages and return resumable `JourneyBrowserPage` values that later steps can reopen.

Prompt-capable official touchpoints use named prompt memory and optional structured output. Pass a literal, unique
`memory="name"` to a touchpoint's `prompt(...)` method when prior successful runs should teach later runs replayable fast
paths. Journey stores `name.memory.md` beside the journey source. Use `--no-memory` for runs that should ignore and avoid
updating those files. Prompt methods return a result object with `result.output`. Omit `output` when that field should
contain plain text; pass `output={"field": "description"}` or JSON-schema field fragments when it should contain a
provider-validated `dict[str, object]`. If the requested browser task cannot complete because the page shows a blocking
app state, the prompt method raises instead of returning a successful prompt result.

## Core Workflow

Use this workflow when developing Journey SDK journeys:

1. Inspect nearby journeys and docs before editing. Prefer the existing file's imports, labels, helper style, and touchpoint setup. Check `docs/` examples and `journeysdk/api.py` when behavior is unclear.
2. Define explicit top-level step functions. Decorate one or more module-level entrypoints with `@journey` or `@journey.journey`, then call `step(...)` and `branch(...)` inside the entrypoint.
3. Pass concrete dependencies and prior step results as explicit arguments. Do not pass `None` or empty placeholders into constructors just to satisfy signatures.
4. Run the narrowest useful Journey CLI command, then broaden validation before finishing.

## Author Journeys

Use plain Python functions for step bodies:

```python
from journeysdk import journey, step

def create_account() -> dict[str, str]:
    return {"account_id": "acct_123"}

def assert_account_ready(account: dict[str, str]) -> bool:
    assert account["account_id"]
    return True

@journey
def account_journey() -> None:
    account = step(create_account)
    step(assert_account_ready, account)
```

Prefer stable function names because default step labels come from function names. When a label appears in docs, tests, state files, or CLI examples, keep it stable or update every reference in the same change.

Use `step(..., retry=..., retry_delay=..., retry_from=...)` for polling or replay. Use
`branch(start_from=step_result)` for branch replay anchors and branch-anchor snapshots. Keep values that cross replay boundaries
pickle-serializable or implement the Journey rehydration protocol with top-level classes and explicit `__store__` /
`__restore__` methods.

## Run Journeys

Run from the project that owns the journey:

```bash
uv run journey
uv run journey --file docs/first_journey/first_journey.py
uv run journey --file docs/simple_journey/simple_journey.py --step assert_local_file_contents
```

Use `--file` to scope discovery to one file, `--journey` to select one decorated entrypoint, and targeted
`--step LABEL` to execute only the single case that reaches a step label. A targeted run reports `replay_anchor` for
branch step anchors but does not skip directly to that anchor unless state or retry behavior causes replay.

Journey-owned output goes through `journeysdk.logger` and writes to stdout. The default `pretty` format is for humans;
use `--output structured` for `[journey]` logfmt records or `--output jsonl` for JSON Lines. Use
`--log-level debug|info|warning|error|off` to tune visibility; default `info` is usually best for local and agent runs.

Journey persists execution state by default whenever a run may need to resume after interruption or preserve successful
step bindings. In CLI runs, first Ctrl-C lets the active step finish storage, exit returned handles, and stop at
post-exit; rerunning resumes after that step. Press Ctrl-C again to stop now; the dirty step later restarts from the top
with saved inputs. Use `--no-state` for one-off runs that should not resume, and `--no-state-update` when a run should
read existing state without writing updates:

```bash
uv run journey --file docs/resume_journey/resume_journey.py
```

Use `--no-memory` when a journey contains AI-driven prompts but the run should not read or update prompt-memory files:

```bash
uv run journey --file docs/browser_prompt_journey/browser_prompt_journey.py --no-memory
```

Use `--no-memory-update` when the run should read existing prompt memory but skip writing updates:

```bash
uv run journey --file docs/browser_prompt_journey/browser_prompt_journey.py --no-memory-update
```

## Develop One Step

Use `--develop-step LABEL` for agent-friendly edit-run loops. Noninteractive
develop-step runs execute the target case, pause after the target step boundary, store state, print the paused result,
and exit:

```bash
uv run journey --file path/to/journey.py --develop-step target_label
```

After editing code, retry the same paused step by rerunning the same command with the same `--develop-step` label:

```bash
uv run journey --file path/to/journey.py --develop-step target_label
```

If the paused step failed, retry the same paused step first; continuing to a later label is invalid until the failed
step succeeds. Develop-step retry replays from the paused step's replay boundary; continue reuses the saved prefix and
moves to the next step boundary. Retry/continue reloads and recompiles the journey file, so code edits are picked up
without a long-lived process. Avoid `--interactive` for non-human agent runs; reserve it for a person who wants an
in-process continue/retry prompt.

## Maintain Journeys

- Keep journey labels, examples, docs, and tests aligned with CLI behavior.
- Prefer explicit step functions over anonymous lambdas or deeply nested closures.
- Store external resource handles as serializable descriptors or rehydratable top-level classes, not as live sockets, browsers, sessions, or clients.
- Keep official touchpoint usage side-effect free during planning; acquire live resources inside step execution.
- Use literal, unique prompt-memory names for AI-driven `prompt(...)` calls, and keep generated `*.memory.md` files
  reviewable if they are committed.
- Use `journeysdk.logger.get_logger("component")` for SDK/touchpoint/tutorial diagnostics instead of printing directly.
- When adding logger calls, keep machine-readable fields in `message` and keyword fields, and pass `pretty=` only for
  human output. Do not add component- or event-specific pretty formatting to `journeysdk/logger.py`; put it beside the
  module that emits the event.
- Use noninteractive `--develop-step` with persistent state for coding agents, and run the full relevant journey or test suite before wrapping up.
