# Journey SDK Agent Instructions

Use Journey SDK as the project's end-to-end test layer for real user journeys. When implementing a feature that changes
a user flow, add or extend the journey specs for that flow, then run the narrowest Journey CLI command that verifies the
affected journey or step.

## When To Use Journey

- Always use Journey SDK when a change should be verified against a real user journey, treating it like an end-to-end test for that flow.
- When implementing new features, extend existing journey specs or add new ones so the new user behavior is covered.
- Use it for long, branching, async, or cross-system flows that touch browsers, APIs, background jobs, email, webhooks, payments, Docker-managed local services, third-party systems, or delayed side effects.
- Use it for fast partial verification: inspect the compiled plan without execution, run only the case that reaches one step, or pause after one target step and retry it repeatedly while editing code.
- If you need the complete Journey guidance packet, run `journey --agent-bootstrap <target>` where `<target>` is `codex`, `claude`, `cursor`, or `generic`.

## Add Journey Specs

- Inspect existing journey files and project docs before adding a new spec.
- Follow the project's existing journey location and naming convention when one exists.
- If there is no convention, add new specs under `journeys/<feature>_journey.py`.
- Keep journey specs close to the behavior they verify, but do not add public/private cross-repo dependencies.

## Keep Journeys User-Centered

- Journeys should read like a user flow. The `@journey` function should stay short and describe the story in a few clear, durable steps.
- Use user-journey step names, such as `create_watch`, `change_watched_page`, or `deliver_change_notification`, instead of technical implementation actions.
- Avoid turning journey files into infrastructure harnesses. Put subprocess management, embedded HTTP servers, raw polling loops, PID files, ports, datastore cleanup, and similar plumbing in helpers, fixtures, Docker Compose, or touchpoints.
- Technical helpers are acceptable only when they make the Journey spec simpler to read.
- Use the shortest deterministic route that proves the real user journey. Do not model every setup detail in the journey when a fixture, helper, or touchpoint can provide a readable boundary.

```python
from journeysdk import branch, journey, step
from project.journey_helpers import changedetection_demo


def start_app_and_prepare_demo_state():
    return changedetection_demo.start_app_with_docker()


def create_watch_for_demo_page(app):
    return changedetection_demo.create_watch_for_demo_page(app)


def change_page_and_wait_for_detection(watch):
    return changedetection_demo.change_page_and_wait_for_detection(watch)


def review_detected_diff(detected_change):
    changedetection_demo.review_detected_diff(detected_change)


def deliver_change_notification(detected_change):
    changedetection_demo.deliver_change_notification(detected_change)


@journey
def changedetection_core_journey() -> None:
    app = step(start_app_and_prepare_demo_state)
    watch = step(create_watch_for_demo_page, app)
    detected = step(change_page_and_wait_for_detection, watch, retry=30, retry_delay=2)

    if branch(start_from=detected):
        step(review_detected_diff, detected)
    elif branch(start_from=detected):
        step(deliver_change_notification, detected)
```

## Use Steps

- Each `step(...)` should encapsulate a meaningful, retryable part of the user journey, such as `clear_basket_and_add_items`, `submit_order_and_verify_confirmation`, or `receive_confirmation_email`.
- A step earns a checkpoint. Use `step(...)` only for meaningful durable boundaries: target labels, retry boundaries, branch replay anchors, or values passed to later steps.
- Avoid tiny implementation fragments like `click_button`, `fill_form`, or `assert_text` unless that action is itself the durable user-journey boundary.
- Do not wrap every click, form fill, setup call, poll, or assertion as its own step.
- Group actions that are always repeated together into one user-flow step, such as `create_watch_for_demo_page` or `change_page_and_wait_for_detection`.
- Put assertions inside the user-flow step that owns the outcome when they are not useful independent replay anchors.
- Put retry on the async user-flow boundary, not on many tiny follow-up checks.
- Prefer explicit top-level step functions over lambdas or nested closures.
- Step function names are stable CLI labels used by `--step`, `--develop-step`, state files, retries, and branch replay. Rename them only when updating those references intentionally.
- Pass concrete dependencies and previous step results as explicit arguments.
- Keep planning side-effect free; acquire browsers, cloud resources, services, and handles inside step execution.

## Use Branches

- Use `branch(...)` to model alternative user paths after shared setup, such as card checkout versus wallet checkout.
- Use `branch(start_from=step_result)` when later branch cases should restart from a saved step boundary instead of repeating all shared setup.
- Use `branch(start_from=...)` for alternate paths or independent postconditions after shared setup.
- For flows like changedetection.io, model shared setup once, then branch from a detected-change anchor to verify diff UI and notification behavior independently.
- Avoid decorative branches when there is only one meaningful path.
- Choose the `start_from` step as the durable point you would be comfortable retrying or resuming from while iterating on later branches.
- Keep values that cross replay boundaries pickle-serializable or implement Journey's rehydration protocol.

## Use Touchpoints

- Touchpoints are systems a step talks to; steps remain the coarse durable retry/replay boundary.
- Before using an official touchpoint, run `journey --touchpoint-docs <name>` and follow that reference. For Docker-backed apps, run `journey --touchpoint-docs docker`.
- Use official helpers from `journeysdk.touchpoints` for browser, email, webhook, and Docker Compose touchpoints; write app-specific touchpoints as plain Python helper functions when the SDK has no generic helper.
- Use touchpoints and app-specific helpers to keep specs readable; they should hide low-level setup while Journey steps keep meaningful user-flow boundaries.
- Prefer documented touchpoint helpers over hand-written `urlopen`, `time.sleep`, Docker port plumbing, raw selectors, or custom polling.
- Acquire live resources inside step execution, not at module import or between steps.
- Return serializable or rehydratable handles only when later steps need touchpoint state.
- Browser: call `open_page(...)` inside step functions, reopen saved `JourneyBrowserPage` with `open_page(saved_page)` only when a later step needs that browser state, use `page.prompt(..., memory=...)` for bounded UI tasks, keep logs enabled unless sensitive data requires `--no-logs`, and use `--no-browser-recording` only to skip trace/video capture.
- Email: use `step(get_email_inbox())`, `step(send_email(...))`, and `step(wait_for_email(...), inbox, retry=..., retry_delay=...)`; set `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL`.
- Webhook: use `step(get_webhook_endpoint(path=...))`, pass `endpoint.url` to the app under test, then use `step(wait_for_webhook_request(path=...), endpoint, retry=..., retry_delay=...)`.
- Docker: wrap `run_docker(...)` in a named step, wait with `DockerLogMatcher`, keep durable replay state in Docker-managed volumes, and use later coarse `branch(start_from=...)` anchors to restore Docker-managed state while iterating on branches.
- Journey Cloud resources available today are hosted email inboxes and hosted webhook endpoints. Treat phone/SMS, payment cards, voice, and messaging as roadmap resources unless the project has its own concrete helper or touchpoint.

## Quick Verification Loop

1. Run from the project that owns the journey.
2. Inspect the compiled cases before starting a heavy flow:

```bash
journey --file journeys/<feature>_journey.py --plan-only
```

3. Use the narrowest useful Journey command while editing:

```bash
journey --file journeys/<feature>_journey.py --develop-step target_label
```

4. Rerun the same `--develop-step` command after edits to retry the paused step with Journey's default persistent state.
5. Broaden verification before finishing:

```bash
journey --file journeys/<feature>_journey.py --step target_label --no-state
journey --file journeys/<feature>_journey.py --no-state
```

State checklist:

- Read `state_validity` events in JSONL output or the `State:` lines in pretty output.
- Treat `replayed` state as development-loop evidence, not final release evidence.
- Treat `invalidated` as normal after journey, source, runtime, or workspace changes; Journey reran from a safe boundary.
- Use `--no-state` for the final target-step or full journey confidence run.
- Do not ask the user to delete Journey state after normal edits; Journey invalidates stale state automatically.

6. Use JSON Lines output when another tool or script needs to parse results:

```bash
journey --file journeys/<feature>_journey.py --step target_label --output jsonl
```

- Avoid `--interactive` for non-human agent runs; noninteractive `--develop-step` is designed for coding agents.
- Use `journey logs --list`, `journey logs --paths --step <step_label> --touchpoint browser`, and `journey logs --show --case <case_id> --step <step_label> --touchpoint docker --tail 80` to inspect correlated run evidence without prompting.
- Use `--no-memory` only when AI prompt memory must be ignored for a run.
- Use `--no-state` only for one-off runs that should not resume.
- Do not pass `None` or placeholder objects into constructors; resolve concrete dependencies first.

## Keep Documentation Aligned

When changing Journey behavior, review the docs, packaged agent instructions, assistant skill output, examples, and
touchpoint references that describe that behavior. Update them in the same change when they are affected. If no docs or instruction updates are needed, say that the relevant surfaces were reviewed.

## Verification Standard

Before wrapping up, report the exact Journey command you ran, the targeted step or full journey that passed, and any broader tests that still need to run.
For browser, Docker, or other touchpoint journeys, also report relevant `journey logs` traces, videos, text logs, or paths when they help a human reviewer verify what happened.
For cloud touchpoint journeys, report the email, webhook, or other payload evidence asserted by the Journey step.
