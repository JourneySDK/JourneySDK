---
name: journey-developer
description: Develop, execute, debug, and maintain Journey SDK journey files. Use when creating or updating Python journeys that use journeysdk primitives, running Journey CLI full or targeted executions, iterating with --develop-step and --state, or keeping journey authoring guidance aligned with SDK, CLI, examples, and docs behavior.
---

# Journey Developer

## Core Workflow

Use this workflow when developing Journey SDK journeys:

1. Inspect nearby journeys and docs before editing. Prefer the existing file's imports, labels, helper style, and tool setup. Check `docs/` examples and `journeysdk/api.py` when behavior is unclear.
2. Define explicit top-level step functions. Decorate one or more module-level entrypoints with `@journey` or `@journey.journey`, then call `step(...)`, `checkpoint()`, and `branch(...)` inside the entrypoint.
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

Use `step(..., retry=..., retry_delay=..., retry_from=...)` for polling or replay. Use `checkpoint()` and `branch(start_from=...)` for branch replay anchors. Keep values that cross retry, checkpoint, branch, or `--state` boundaries pickle-serializable or implement the Journey rehydration protocol with top-level classes and explicit `__store__` / `__restore__` methods.

## Run Journeys

Run from the project that owns the journey:

```bash
uv run journey
uv run journey --file docs/first_journey/first_journey.py
uv run journey --file docs/simple_journey/simple_journey.py --step assert_local_file_contents
```

Use `--file` to scope discovery to one file, `--journey` to select one decorated entrypoint, and targeted `--step LABEL` to execute only the single flow that reaches a step label.

Use `--state PATH` whenever a run may need to resume after interruption or preserve successful step results:

```bash
uv run journey --file docs/resume_journey/resume_journey.py --state .journey/run.state
```

## Develop One Step

Use `--develop-step LABEL --state .journey/develop-step.state` for agent-friendly edit-run loops. Noninteractive develop-step runs execute the target path, pause after the target step, store state, print the paused result, and exit:

```bash
uv run journey --file path/to/journey.py --develop-step target_label --state .journey/develop-step.state
```

After editing code, retry the same paused step by rerunning the same command with the same `--develop-step` label and `--state` file. To continue after a successful pause, target the next step label with the same state file:

```bash
uv run journey --file path/to/journey.py --develop-step next_label --state .journey/develop-step.state
```

If the paused step failed, retry the same paused step first; continuing to a later label is invalid until the failed step succeeds. Develop-step retry/continue reloads and recompiles the journey file, so code edits are picked up without a long-lived process. Avoid `--interactive` for non-human agent runs; reserve it for a person who wants an in-process continue/retry prompt.

## Maintain Journeys

- Keep journey labels, examples, docs, and tests aligned with CLI behavior.
- Prefer explicit step functions over anonymous lambdas or deeply nested closures.
- Store external resource handles as serializable descriptors or rehydratable top-level classes, not as live sockets, browsers, sessions, or clients.
- Keep official tool usage side-effect free during planning; acquire live resources inside step execution.
- Use noninteractive `--develop-step` with persistent state for coding agents, and run the full relevant journey or test suite before wrapping up.
