# Journey SDK Agent Instructions

Use Journey SDK as the project's end-to-end test layer for real user journeys. When implementing a feature that changes
a user flow, add or extend the journey specs for that flow, then run the narrowest Journey CLI command that verifies the
affected journey or step.

## When To Use Journey

- Always use Journey SDK when a change should be verified against a real user journey, treating it like an end-to-end test for that flow.
- When implementing new features, extend existing journey specs or add new ones so the new user behavior is covered.
- Use it for long, branching, async, or cross-system flows that touch browsers, APIs, background jobs, email, webhooks, payments, Docker-managed local services, third-party systems, or delayed side effects.
- Use it for fast partial verification: run only the case that reaches one step, or pause after one target step and retry it repeatedly while editing code.
- If you need the complete Journey guidance packet and have not already received it, run `journey agent <target>` where `<target>` is `codex`, `claude`, `cursor`, or `generic`.

## Working From A One-Line Prompt

- If this guidance came from installed assistant instructions and the complete bootstrap packet is not already in context, run and read `journey agent <target>` yourself.
- Proceed autonomously: inspect the project, find or create the relevant journey spec, fetch touchpoint references with the Journey CLI as needed, run the targeted verification loop, and report the exact Journey commands and evidence before finishing.
- When asked to write, add, or extend a Journey spec, do not stop after code generation, import checks, lint, or type checks. A new or changed Journey is unfinished until you have run at least one executable `journey --file ...` command against it, unless the app infrastructure is genuinely unavailable.
- For a new branching journey, run the shared setup and every requested branch target with Journey selection commands such as `--develop-step <branch_step>` or `--step <branch_step>`; then finish with a broader fresh run such as `--step <target> --no-state` or full `--no-state` when infrastructure permits.
- When asked to fix a Journey file, do not stop after static review or a plausible code edit. Run the failing Journey command or the full journey once, use the first failing step as the source of truth, then iterate with the CLI's `Retry failed step:` command or the narrowest equivalent `--develop-step` command until it passes.
- Do not ask the user whether to run the journey when verification is the requested task. If the app is not running and the repository provides a normal local startup command, start the required services or request tool approval for that command; classify the run as `environment-blocked` only after the repo-provided startup path is missing or fails.
- Do not print secret values from `.env`, credentials files, or CLI output. Do not `cat`, `sed`, `nl`, `head`, `tail`, or `Read` `.env*`, `journeys/.env`, credentials files, or other secret-bearing files wholesale. When checking configuration, list variable names or presence only, load needed values directly into the process environment without echoing them, and redact values before writing logs, memory, summaries, or final answers.

## Fetch More Journey Guidance

- Use the installed Journey CLI to fetch Journey reference material.
- Run `journey --help` before choosing or repairing Journey execution commands when command usage is unclear.
- Run `journey logs --help` before inspecting artifacts when log filters, trace paths, or text logs are unclear.
- Run `journey agent --help` before installing or replacing persistent assistant guidance.
- Run `journey --touchpoint-docs all` to inspect every packaged touchpoint reference before choosing helpers for a new flow.
- Run `journey --touchpoint-docs browser`, `journey --touchpoint-docs docker`, `journey --touchpoint-docs email`, `journey --touchpoint-docs webhook`, or `journey --touchpoint-docs http` for focused helper guidance.
- Do not ask the user for Journey reference material that can be printed by the installed CLI.

## Add Journey Specs

- Inspect existing journey files, tests, fixtures, and local helper APIs before adding a new spec.
- For sign-in, seed data, payments, email, or other test setup, inspect existing E2E helpers and setup scripts before guessing credentials, magic codes, or UI flows. If the repo-supported setup mutates an external service, request approval or report the setup as the explicit environment blocker instead of brute-forcing credentials.
- Follow the project's existing journey location and naming convention when one exists.
- If there is no convention, add new specs under `journeys/<feature>_journey.py`.
- Keep journey specs close to the behavior they verify, but do not add public/private cross-repo dependencies.
- If the spec needs a fixture, create or locate the fixture before running the journey; do not leave required image, PDF, or data files as placeholders.

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

- Each `step(...)` is an intentional replay boundary for one whole operation in the user journey, such as `clear_basket_and_add_items`, `submit_order_and_verify_confirmation`, or `receive_confirmation_email`.
- Choose step scope by recovery value, not by function-name patterns. Before splitting, ask: would this be a meaningful place to restart from the function start, is the state stable and durable enough to restore, and is restoring it cheaper or more correct than recreating it?
- Every step boundary has a cost: another label, state binding, log scope, invalidation/replay decision, and possible rehydration. External or rehydratable state makes the cost more visible, but the rule applies to all steps.
- Do not wrap every click, form fill, setup call, poll, or assertion as its own step. If actions must recover together, keep them in one step or in helpers called by that step.
- Do not split merely to freeze intermediate state for assertion or prompt tuning. Keep the suffix with the operation when it only completes or verifies the outcome produced by that operation.
- Put retry on the operation whose rerun semantics match real recovery, not on a trailing wait or assertion suffix carved away from the action that produced the state.
- Split only when the intermediate result is independently useful as a target, retry point, branch replay anchor, or durable value passed to later operations.
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
- If a shared setup step label appears in multiple branch cases and is ambiguous as a CLI target, do not comment out or disable other branches just to make it selectable. Target a branch-specific step that depends on the shared setup, or run the full journey until a branch target; any temporary narrowing must be restored before final evidence and called out explicitly.
- Keep values that cross replay boundaries pickle-serializable or implement Journey's rehydration protocol.

## Use Touchpoints

- Touchpoints are systems a step talks to; steps remain the coarse durable retry/replay boundary.
- Before using an official touchpoint, run `journey --touchpoint-docs <name>` and follow that reference. For Docker-backed apps, run `journey --touchpoint-docs docker`.
- Use official helpers from `journeysdk.touchpoints` for browser, email, webhook, and Docker Compose touchpoints; write app-specific touchpoints as plain Python helper functions when the SDK has no generic helper.
- Use touchpoints and app-specific helpers to keep specs readable; they should hide low-level setup while Journey steps keep meaningful user-flow boundaries.
- Prefer official touchpoint helpers over hand-written `urlopen`, `time.sleep`, Docker port plumbing, raw selectors, or custom polling.
- Acquire live resources inside step execution, not at module import or between steps.
- Return serializable or rehydratable handles only when later steps need touchpoint state.
- Browser: call `open_page(...)` inside step functions, reopen saved `JourneyBrowserPage` with `open_page(saved_page)` only when a later step needs that browser state, use `page.prompt(..., memory=...)` for bounded UI tasks, keep logs enabled unless sensitive data requires `--no-logs`, and use `--no-browser-recording` only to skip trace/video capture. Prompt memory files live next to the journey's `.journey` directory. If `page.prompt(...)` reaches max steps, loops on the wrong page, or wanders after a rejected action, treat that as a deterministic step implementation failure: inspect the app route, selectors, current URL/title, prompt memory replay errors, and browser artifacts, then make the step or prompt more precise before rerunning. Use `--no-memory` to bypass stale AI prompt memory during diagnosis.
- Email: use `step(get_email_inbox())`, `step(send_email(...))`, and `step(wait_for_email(...), inbox, retry=..., retry_delay=...)`; set `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL`.
- Webhook: use `step(get_webhook_endpoint(path=...))`, pass `endpoint.url` to the app under test, then use `step(wait_for_webhook_request(path=...), endpoint, retry=..., retry_delay=...)`.
- Docker: wrap `run_docker(...)` in a named step, wait with `DockerLogMatcher`, keep durable replay state in Docker-managed volumes, and use later coarse `branch(start_from=...)` anchors to restore Docker-managed state while iterating on branches.
- Journey Cloud resources available today are hosted email inboxes and hosted webhook endpoints. Treat phone/SMS, payment cards, voice, and messaging as roadmap resources unless the project has its own concrete helper or touchpoint.

## Quick Verification Loop

1. Run from the project that owns the journey.
2. When authoring a new journey, make the first draft executable quickly. After the file imports, run the full journey or the narrowest target that reaches the first requested branch; for branching flows, run each requested branch target before claiming coverage. Static checks only prove syntax, not the user journey.
3. If the Journey cannot connect to the app, inspect repository-provided local startup commands and bring up the smallest required frontend/backend/dependency stack. After startup, rerun the same Journey command that exposed the connection failure.
4. When fixing an existing failure, run the failing command from the user or the full journey once, read the first failing step, and copy the CLI's `Retry failed step:` command as the focused loop when it appears. Read every `What happened`, `Try this`, and `Next commands` block; if command usage is unclear, run the relevant `--help` command printed by the CLI. Before editing, inspect the failing step label, attempt output, current URL/title for browser failures, the last rejected browser action, and correlated `.journey/logs` artifacts.

5. Use the narrowest useful Journey command while editing:

```bash
journey --file journeys/<feature>_journey.py --develop-step target_label
```

6. Rerun the same `--develop-step` command after every edit to retry the paused step with Journey's default persistent state. Keep iterating until the target step passes.
7. Broaden verification before finishing:

```bash
journey --file journeys/<feature>_journey.py --step target_label --no-state
journey --file journeys/<feature>_journey.py --no-state
```

State checklist:

- Read `state_validity` events in JSONL output or the `State:` lines in pretty output, and read each report record's
  `status` field (`executed`, `replayed`, or `failed`) before relying on a step as fresh evidence.
- Treat `replayed` state as development-loop evidence, not final release evidence.
- Treat `invalidated` as normal after journey, source, runtime, or workspace changes; Journey reran from a safe boundary.
- Use `--no-state` for the final target-step or full journey confidence run whenever feasible.
- Do not ask the user to delete Journey state after normal edits; Journey invalidates stale state automatically.

8. Use JSON Lines output when another tool or script needs to parse results:

```bash
journey --file journeys/<feature>_journey.py --step target_label --output jsonl
```

- Avoid `--interactive` for non-human agent runs; noninteractive `--develop-step` is designed for coding agents.
- Use `journey logs --list-scopes` and `journey logs --list-log-sources --case <case_id> --step <step_label>` before reading large logs. Then use `journey logs --paths --step <step_label> --touchpoint browser` or `journey logs --show --case <case_id> --step <step_label> --touchpoint docker --source <service> --tail 80` to inspect correlated evidence without prompting.
- Use `--no-memory` only when AI prompt memory must be ignored for a run.
- Use `--no-state` only for one-off runs that should not resume.
- Do not pass `None` or placeholder objects into constructors; resolve concrete dependencies first.

## Verification Standard

Before wrapping up, report the exact Journey command you ran, the targeted step or full journey that passed, and any broader tests that still need to run.
Do not describe a Journey as tested, verified, or complete if the strongest evidence is only generated code, `py_compile`, import success, lint, or test discovery.
For new branching journeys, list every branch target that was executed, whether each relevant step status was `executed` or `replayed`, and which final `--no-state` command produced fresh confidence. If no executable Journey run completed because infrastructure was unavailable, say `environment-blocked` and name the missing dependency instead of inferring product behavior.
For browser, Docker, or other touchpoint journeys, also report relevant `journey logs` traces, videos, text logs, or paths when they help a human reviewer verify what happened.
For cloud touchpoint journeys, report the email, webhook, or other payload evidence asserted by the Journey step.
