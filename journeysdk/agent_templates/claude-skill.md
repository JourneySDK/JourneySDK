---
name: journey-developer
description: Use Journey SDK to create, run, debug, and maintain real user journey tests. Use when application code should be verified through a Journey flow, when a Journey SDK journey uses journeysdk primitives or journeysdk.touchpoints, or when iterating quickly with journey --develop-step, --step, default state, and JSONL output.
---

# Journey Developer

## When To Use Journey

Use Journey SDK when a change should be verified against a real user journey, not just a unit test. A journey is useful
when the behavior crosses browsers, APIs, background jobs, email, webhooks, payments, Docker-managed local services,
third-party systems, or delayed side effects.

Journey is also useful for fast partial verification: run only the case that reaches one step, or pause after one
target step and retry it repeatedly while editing code.

Do not use Journey SDK for generic Python scripts, ordinary unit tests, or workflow automation that is not authored as
a Journey SDK journey.

## Agent Workflow

1. Inspect nearby journeys, project docs, and application code before editing.
2. Make the smallest code or journey change that can verify the requested user behavior.
3. Run the narrowest useful Journey command from the project that owns the journey:

```bash
journey --file path/to/journey.py --develop-step target_label
```

4. Rerun the same `--develop-step` command after edits to retry the paused step with Journey's default persistent state.
5. Broaden verification before finishing:

```bash
journey --file path/to/journey.py --step target_label
journey --file path/to/journey.py
```

6. Use JSON Lines output when another tool or script needs to parse results:

```bash
journey --file path/to/journey.py --step target_label --output jsonl
```

## Author Journeys

Use plain Python step functions, then call them from a module-level `@journey` entrypoint:

```python
from journeysdk import branch, journey, step

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

- Prefer explicit top-level step functions over lambdas or nested closures.
- Keep step labels stable because CLI targeting, docs, and state files depend on them.
- Pass concrete dependencies and previous step results as explicit arguments.
- Do not pass `None` or empty placeholders into constructors to satisfy signatures.
- Keep planning side-effect free; acquire browsers, cloud resources, services, and handles inside step execution.
- Choose step boundaries around meaningful durable procedures, not tiny "do" and "assert" fragments.

## Replay And Touchpoints

Official touchpoints live under `journeysdk.touchpoints` and are used from step functions. Use them when a journey needs
browser pages, Journey Cloud email or webhook resources, or Docker Compose local infrastructure.

Use `branch(start_from=step_result)` or positive `step(..., retry=...)` to create explicit replay boundaries. Values
that cross replay boundaries should be pickle-serializable or implement Journey's rehydration protocol.

Prompt-capable browser steps can use `page.prompt(...)`. Use literal, unique prompt-memory names for committed journeys
when repeatability matters. Use `--no-memory` only when a run should ignore prompt memory.

## Develop One Step

Use noninteractive `--develop-step LABEL` for agent-friendly edit-run loops. It executes the target case, pauses after
the target step boundary, stores state, prints the paused result, and exits.

```bash
journey --file path/to/journey.py --develop-step target_label
```

Rerun the same command to retry the paused step after editing code. If the paused step failed, retry that same step
first; continuing to a later label is invalid until the failed step succeeds. Avoid `--interactive` for non-human agent
runs.

## Verification Standard

Before wrapping up, report the exact Journey command you ran, the targeted step or full journey that passed, and any
broader tests that still need to run.
