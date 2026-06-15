# Journey SDK Agent Instructions

Use Journey SDK to verify real user journeys while you code. A Journey step is a replay boundary: the smallest
meaningful slice an agent should rerun repeatedly while implementing a feature, fixing a bug, or checking an
integration.

## When To Use Journey

- Use Journey SDK whenever a code change should be proven through a user flow, not only unit tests or static checks.
- Use it for long, branching, async, or cross-system flows that touch browsers, APIs, background jobs, email, webhooks,
  payments, Docker-managed local services, third-party systems, or delayed side effects.
- Prefer Journey when you need to rerun one late-flow step repeatedly without restarting the whole journey from the
  beginning.
- If this installed guidance is not enough, run `journey agent <target>` where `<target>` is `codex`, `claude`,
  `cursor`, or `generic`.

## Core Loop

1. Find or create the Journey spec for the changed user flow.
2. Run the failing command from the user, or run the narrowest verification target that reaches the changed behavior.
3. Use the first failing Journey step as the source of truth.
4. Rerun that step with `journey loop <step_label> --file journeys/<feature>_journey.py` after every edit.
5. Inspect correlated artifacts with `journey evidence` when the step output is not enough.
6. Broaden before finishing: run `journey verify --step <step_label> --file ... --fresh` for a fresh target-step check,
   then run `journey verify --file ... --fresh` when infrastructure permits.

Do not stop after code generation, import checks, lint, type checks, or test discovery. A new or changed Journey is not
verified until at least one executable Journey CLI command has run, unless the app infrastructure is genuinely
unavailable.

When asked to write, add, or extend a Journey spec, make the first draft executable quickly. Static checks only prove
syntax, not the user journey. If the spec needs a fixture, create or locate the fixture before running the journey.

## Commands

Run help when usage is unclear:

```bash
journey --help
journey loop --help
journey verify --help
journey evidence --help
journey dev --help
journey touchpoints browser
journey touchpoints docker
journey touchpoints email
journey touchpoints webhook
journey touchpoints http
journey touchpoints all
journey agent --help
```

Use these commands while working:

```bash
journey loop target_step --file journeys/<feature>_journey.py
journey evidence --step target_step
journey verify --step target_step --file journeys/<feature>_journey.py --fresh
journey verify --file journeys/<feature>_journey.py --fresh
journey verify --step target_step --file journeys/<feature>_journey.py --output jsonl
journey dev open_main_page --file journeys/<feature>_journey.py --output jsonl
journey dev --file journeys/<feature>_journey.py --url http://127.0.0.1:3000 --output jsonl
```

If the CLI prints `Retry failed step: ...`, copy that command as the focused loop. Read every `What happened`,
`Try this`, and `Next commands` block before editing.

## Step Boundaries

- Each `step(...)` is an intentional replay boundary for one whole operation in the user journey, such as `submit_order_and_verify_confirmation`,
  `receive_confirmation_email`, or `complete_checkout_and_verify_registration_effects`.
- Choose step scope by recovery value, not by function-name patterns. Before splitting, ask: would this be a meaningful
  place to restart from the function start, is the state stable and durable enough to restore, and is restoring it
  cheaper or more correct than recreating it?
- Every step boundary has a cost: another label, state binding, log scope, invalidation/replay decision, and possible
  rehydration.
- If clicks, form fills, polls, waits, and assertions must recover together, keep them in one step or in helpers called
  by that step.
- Do not split every browser action, setup call, poll, or assertion into its own step.
- Do not split merely to freeze intermediate state for assertion or prompt tuning.
- Put retry on the operation whose rerun semantics match real recovery, not on a trailing wait or assertion suffix
  carved away from the action that produced the state.
- Split only when the intermediate result is independently useful as a loop target, retry point, branch replay anchor,
  or durable value passed to later operations.
- Apply this step boundary checklist before every new `step(...)`: add a step only when an agent would target it, retry
  from it, branch from it, or when storing/restoring its result is cheaper than rerunning the work.
- touchpoint helpers may be called inside a coarse step when their intermediate values are not useful replay boundaries.
- do not split wait/assert helpers into separate steps unless independently targetable.
- Common anti-pattern: separate steps for `get_webhook_endpoint(...)`, app startup, checkout, database assertion, email
  assertion, webhook wait, and webhook assertion. Prefer one expensive setup step, then one branch-specific late-flow
  verification step that performs the browser action and all side-effect assertions that recover together.
- Prefer explicit top-level step functions over lambdas or nested closures.
- Step function names are stable CLI labels used by `journey loop`, `journey verify --step`, state files, retries, and
  branch replay. Rename them only when updating those references intentionally.
- Keep planning side-effect free. Acquire browsers, cloud resources, services, and handles inside step execution.
- If asked to bootstrap or extend browser coverage from a running app, use `journey dev`, then edit the Journey source
  yourself and verify the new branch. For a new or empty Journey file, run
  `journey dev --file journeys/<feature>_journey.py --url http://127.0.0.1:3000 --output jsonl`; this initializes a
  minimal first browser step and emits a `dev_result`. For an existing browser step, run
  `journey dev <step_label> --file journeys/<feature>_journey.py --output jsonl`. Prefer `candidate_flows`, use
  `rendered_page` artifact paths when the page needs inspection, fall back to `actionable_elements` for exact controls,
  then add the smallest useful next branch with coarse step boundaries and prove it with `journey loop <new_step> --file ...`
  or `journey verify --step <new_step> --file ...`.

## Branches

- Use `branch(...)` to model alternative user paths after shared setup.
- Use `branch(replay_from=step_result)` when later branch cases should replay from a saved shared setup step instead of
  rebuilding the entire journey.
- Choose the replay anchor as a durable point you would be comfortable restoring while iterating on later branches.
- For a new branching journey, run every requested branch target with `journey verify --step <branch_step> --file ...`
  and finish with a broader fresh run when infrastructure permits.
- If a shared setup step label appears in multiple branch cases and is ambiguous as a CLI target, target a
  branch-specific step that depends on the shared setup. Do not comment out or disable other branches just to make it
  selectable.
- Keep values that cross replay boundaries pickle-serializable or implement Journey's rehydration protocol.

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

    if branch(replay_from=detected):
        step(review_detected_diff, detected)
    elif branch(replay_from=detected):
        step(deliver_change_notification, detected)
```

## Touchpoints

- Touchpoints are systems a step talks to; steps remain the durable retry/replay boundary.
- Before using an official touchpoint, run `journey touchpoints <name>` or `journey touchpoints all`.
- Prefer official helpers from `journeysdk.touchpoints` for browser, Docker Compose, hosted email, hosted webhooks, and
  HTTP checks before writing raw polling or plumbing.
- Acquire live resources inside step execution, not at module import or between steps.
- Return serializable or rehydratable handles only when later steps need touchpoint state.
- Browser: call `open_page(...)` inside step functions, keep logs enabled unless sensitive data requires `--no-logs`,
  and inspect `journey evidence` traces or videos when browser behavior is unclear.
- Email: call `get_email_inbox()`, `send_email(...)`, and `wait_for_email(...)` inside the step that triggers and
  verifies the email.
- Webhook: call `get_webhook_endpoint(path=...)`, pass `endpoint.url` to the app under test, then call
  `wait_for_webhook_request(endpoint, ...)` inside the step that triggers and verifies the webhook.
- Docker: wrap `run_docker(...)` in a named setup step, keep durable replay state in Docker-managed volumes, and use
  later `branch(replay_from=...)` anchors to restore Docker-managed state while iterating on branches.
- Journey Cloud touchpoints available today are hosted email inboxes and hosted webhook endpoints. Treat phone/SMS,
  payment cards, voice, and messaging as roadmap resources unless the project has its own concrete helper.

## Evidence

- Use `journey evidence --list-scopes` to discover run, case, branch, and step filters.
- Use `journey evidence --list-log-sources --case <case_id> --step <step_label>` before reading large logs.
- Use `journey evidence --paths --step <step_label> --touchpoint browser` for trace/video paths.
- Use `journey evidence --show --case <case_id> --step <step_label> --touchpoint docker --source <service> --tail 80`
  for text logs.
- In JSONL output, read `state_validity` events and each report record's `status` field (`executed`, `replayed`, or
  `failed`) before relying on evidence.
- Treat `replayed` state as development-loop evidence, not final release evidence. Use `--fresh` for final confidence
  whenever feasible.

## Environment And Secrets

- Do not ask the user whether to run the journey when verification is the requested task. Run it, or start the
  repo-supported app stack first when needed.
- If the app is not running and the repository provides a normal local startup command, start the required services or
  request tool approval for that command.
- Say `environment-blocked` only after the repo-provided startup path is missing or fails.
- Do not print secret values from `.env`, credentials files, or CLI output. List variable names or key presence only.
- Do not `cat`, `sed`, `nl`, `head`, `tail`, or `Read` `.env*`, `journeys/.env`, credentials files, or other
  secret-bearing files wholesale.
- Load needed values directly into the process environment without echoing them.
- For auth, seed data, payments, email, or other setup, inspect existing E2E helpers and setup scripts before guessing
  credentials, magic codes, or UI flows.
- If repo-supported setup mutates an external service, request approval or report the setup as the explicit environment
  blocker.

## Reporting Standard

Before finishing, report the exact Journey command you ran, the targeted step or full journey that passed, and any
broader tests that still need to run. For new branching journeys, list every branch target executed, whether each
relevant step status was `executed` or `replayed`, and which final `--fresh` command produced fresh confidence. For
browser, Docker, email, webhook, or other touchpoint journeys, report the relevant evidence paths or payload assertions
that prove what happened. Do not describe a Journey as tested, verified, or complete if the strongest evidence is only
generated code, import success, lint, or test discovery. If no executable Journey run completed because infrastructure
was unavailable, say `environment-blocked` and name the missing dependency.
