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
3. If adding a new journey spec and no project convention exists, create it under `journeys/<feature>_journey.py`.
4. Run the narrowest useful Journey command from the project that owns the journey:

```bash
journey --file journeys/<feature>_journey.py --develop-step target_label
```

5. Rerun the same `--develop-step` command after edits to retry the paused step with Journey's default persistent state.
6. Broaden verification before finishing:

```bash
journey --file journeys/<feature>_journey.py --step target_label
journey --file journeys/<feature>_journey.py
```

7. Use JSON Lines output when another tool or script needs to parse results:

```bash
journey --file journeys/<feature>_journey.py --step target_label --output jsonl
```

## Add Journey Specs

Inspect existing journey files and project docs before adding a new spec. Follow the project's current journey location
and naming convention when one exists. If there is no convention, add new specs under `journeys/<feature>_journey.py`.

Keep journey specs in the project that owns the behavior under test. Do not introduce cross-repo dependencies to make a
journey convenient.

## Develop Journey Specs

Use plain Python step functions, then call them from a module-level `@journey` entrypoint. A useful journey spec has
shared setup, durable step boundaries, and branches for meaningful alternate user paths:

```python
from journeysdk import branch, journey, step


def open_checkout() -> dict[str, str]:
    return {"cart_id": "cart_123"}


def clear_basket_and_add_items(context: dict[str, str]) -> dict[str, str]:
    return {**context, "item_count": "2"}


def complete_card_checkout(context: dict[str, str]) -> None:
    assert context["item_count"] == "2"


def complete_wallet_checkout(context: dict[str, str]) -> None:
    assert context["item_count"] == "2"


@journey
def checkout_journey() -> None:
    checkout = step(open_checkout)
    basket = step(clear_basket_and_add_items, checkout)

    if branch(start_from=basket):
        step(complete_card_checkout, basket)
    elif branch(start_from=basket):
        step(complete_wallet_checkout, basket)
```

## Use Steps

- Each `step(...)` should encapsulate a meaningful, retryable part of the user journey, such as `clear_basket_and_add_items`, `submit_order`, or `assert_confirmation_email`.
- Avoid tiny implementation fragments like `click_button` or `assert_text` unless that action is itself the durable user-journey boundary.
- Prefer explicit top-level step functions over lambdas or nested closures.
- Step function names are stable CLI labels used by `--step`, `--develop-step`, state files, retries, and branch replay.
- Pass concrete dependencies and previous step results as explicit arguments.
- Do not pass `None` or empty placeholders into constructors to satisfy signatures.
- Keep planning side-effect free; acquire browsers, cloud resources, services, and handles inside step execution.
- Choose step boundaries around durable procedures you would be comfortable retrying or resuming from.

## Use Branches

- Use `branch(...)` to model alternative user paths after shared setup, such as card checkout versus wallet checkout.
- Use `branch(start_from=step_result)` when later branch cases should restart from a saved step boundary instead of repeating all shared setup.
- Choose the `start_from` step as the durable point you want Journey to return to while iterating on later branches.
- Keep values that cross replay boundaries pickle-serializable or implement Journey's rehydration protocol.

## Use Touchpoints

Touchpoints are systems a step talks to; steps remain the durable retry/replay boundary. Official helpers live under
`journeysdk.touchpoints` for browser, email, webhook, and Docker Compose touchpoints. When the SDK has no generic
helper for the system under test, write app-specific touchpoints as plain Python helper functions in the journey or
test support code.

- Acquire live resources inside step execution, not at module import or between steps.
- Return serializable or rehydratable handles when later steps need touchpoint state.
- Browser: call `open_page(...)` inside step functions, reopen saved `JourneyBrowserPage` with `open_page(saved_page)`, use `page.prompt(..., memory=...)` for bounded UI tasks, and keep recordings enabled unless sensitive data requires `--no-browser-recording`.
- Email: use `step(get_email_inbox())`, `step(send_email(...))`, and `step(wait_for_email(...), inbox, retry=..., retry_delay=...)`; set `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL`.
- Webhook: use `step(get_webhook_endpoint(path=...))`, pass `endpoint.url` to the app under test, then use `step(wait_for_webhook_request(path=...), endpoint, retry=..., retry_delay=...)`.
- Docker: wrap `run_docker(...)` in a named step, wait with `DockerLogMatcher`, keep durable replay state in Docker-managed volumes, and use later `branch(start_from=...)` anchors to restore Docker-managed state while iterating on branches.

Use `branch(start_from=step_result)` or positive `step(..., retry=...)` to create explicit replay boundaries. Values
that cross replay boundaries should be pickle-serializable or implement Journey's rehydration protocol.

Prompt-capable browser steps can use `page.prompt(...)`. Use literal, unique prompt-memory names for committed journeys
when repeatability matters. Use `--no-memory` only when a run should ignore prompt memory.

## Develop One Step

Use noninteractive `--develop-step LABEL` for agent-friendly edit-run loops. It executes the target case, pauses after
the target step boundary, stores state, prints the paused result, and exits.

```bash
journey --file journeys/<feature>_journey.py --develop-step target_label
```

Rerun the same command to retry the paused step after editing code. If the paused step failed, retry that same step
first; continuing to a later label is invalid until the failed step succeeds. Avoid `--interactive` for non-human agent
runs.

## Verification Standard

Before wrapping up, report the exact Journey command you ran, the targeted step or full journey that passed, and any
broader tests that still need to run.
