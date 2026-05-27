# 02 Branching and Targeted Runs

The first real Journey payoff appears when one authored function turns into more than one executable case.

This chapter covers three related ideas:

- step anchors let later cases replay from a known point
- `branch()` lets one function produce several linear cases
- `--step` and `--develop-step` let you run only the case that reaches one target label

## Choosing Step And Branch Boundaries

Write journey specs as plain Python files. If the project has no existing convention, place new specs under
`journeys/<feature>_journey.py`.

Journeys should read like a user flow. Keep the `@journey` function short enough to scan as the user's story, with
technical setup hidden behind fixtures, Docker Compose, touchpoints, or small app-specific helpers. Avoid turning
journey files into infrastructure harnesses: subprocess management, embedded HTTP servers, raw polling loops, PID
files, ports, datastore cleanup, and similar plumbing should stay outside the journey spec. Use the shortest
deterministic route that proves the real user journey.

Each `step(...)` should encapsulate a meaningful, retryable part of the user journey. Prefer names like
`clear_basket_and_add_items`, `submit_order`, or `assert_confirmation_email` over tiny fragments such as `click_button`.
Stable step function names become CLI labels used by `--step`, `--develop-step`, state, retries, and branch replay.

Use `step(...)` only for meaningful durable boundaries: target labels, retry boundaries, branch replay anchors, or values passed to later steps.
Do not wrap every click, form fill, setup call, poll, or assertion as its own step.
Group actions that are always repeated together into one user-flow step, such as `create_watch_for_demo_page` or `change_page_and_wait_for_detection`.
Put retry on the async user-flow boundary, not on many tiny follow-up checks.

Use `branch(...)` for alternate user paths after shared setup. Use `branch(start_from=step_result)` when later branch
cases should restart from a saved step boundary instead of repeating every shared setup step. Choose `start_from` as the
durable point you would be comfortable retrying or resuming from while iterating on later branches. Values crossing
replay boundaries must be pickle-serializable or implement Journey's rehydration protocol.
Use `branch(start_from=...)` for alternate paths or independent postconditions after shared setup.
For flows like changedetection.io, model shared setup once, then branch from a detected-change anchor to verify diff UI and notification behavior independently.
Avoid decorative branches when there is only one meaningful path.

## Branch Once, Reuse Shared Setup

Read `docs/branching_journey/branching_journey.py`.

```python
from journeysdk import branch, journey, step


@journey
def branching_journey() -> None:
    signup_request = step(load_signup_request)
    classified = step(classify_signup_request, signup_request)

    if branch():
        step(assert_fast_track_path, classified)
    elif branch(start_from=classified):
        step(assert_manual_review_path, classified)
```

Why this shape matters:

- `load_signup_request` and `classify_signup_request` are shared setup
- `branch(start_from=classified)` gives later execution a replay anchor at that step's post-exit boundary
- each `branch(...)` arm becomes its own case

External system state belongs on step values that cross explicit replay boundaries. If a step result needs custom
rehydration behavior, define it at module top level and make that value implement the Journey rehydration protocol:

```python
class BrowserSession:
    def __store__(self, context):
        ...

    @classmethod
    def __restore__(cls, payload, context):
        ...


session = step(open_browser_session)
if branch(start_from=session):
    ...
```

Journey stores and restores replayable step values only when execution truly rewinds to an explicit replay boundary,
such as a step-started later branch. Touchpoint-specific rehydration behavior is documented in the packaged references
printed by `journey --touchpoint-docs <name>`.

Journey compiles the branch structure internally before execution. A normal run executes every generated case; a
targeted run uses the compiled labels to choose one case.

### Run Only the Branch That Reaches One Step

```bash
uv run journey --file docs/branching_journey/branching_journey.py --step assert_manual_review_path
```

```console
Plan
  docs/branching_journey/branching_journey.py:branching_journey ...
    case_1  labels: load_signup_request, classify_signup_request, assert_fast_track_path; branches: {bg_1=branch_1}
    case_2  labels: load_signup_request, classify_signup_request, assert_manual_review_path; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
    case_2  branches={bg_1=branch_2}
      load_signup_request  start attempt=1
      load_signup_request  ok attempt=1 duration=...
      classify_signup_request  start attempt=1
      classify_signup_request  ok attempt=1 duration=...
      branch bg_1  branch_2
      assert_manual_review_path  start attempt=1
      assert_manual_review_path  ok attempt=1 duration=...
    case_2 done steps=3 duration=... stopped_at=assert_manual_review_path replay_anchor=classify_signup_request
  Summary: 1 journey executed, 1 case executed, 0 failed
```

That output is the reason `--step` is so useful during development: Journey chooses the single case that reaches the
label you care about. The reported `replay_anchor` names the branch step anchor, but this targeted run still starts
from the selected case's beginning.

### Stop After the Target Step While You Iterate

```bash
uv run journey --file docs/branching_journey/branching_journey.py --develop-step assert_manual_review_path
```

```console
Plan
  docs/branching_journey/branching_journey.py:branching_journey ...
    case_1  labels: load_signup_request, classify_signup_request, assert_fast_track_path; branches: {bg_1=branch_1}
    case_2  labels: load_signup_request, classify_signup_request, assert_manual_review_path; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
Development mode stopped after step assert_manual_review_path attempt=1 ok.
  Summary: 0 journeys executed, 0 cases executed, 0 failed
```

Use `--develop-step` when you are actively editing one branch and want Journey to pause after the step boundary you
care about. Rerun the same command to retry the paused step after editing code, or target the next step with the same
state file to continue. Develop-step retry and continue replay from the paused step's nearest explicit replay boundary;
when no explicit boundary exists, the selected case starts again from the beginning. Add `--interactive` when you want
Journey to keep the process open and prompt after each paused step. Journey reloads and recompiles the selected journey
file before each retry or continue, so edits are picked up immediately.

## Rehydrate Later Cases from a Step Anchor

Read `docs/rehydration_journey/rehydration_journey.py`.

```python
from journeysdk import branch, journey, step


@journey
def rehydration_journey() -> None:
    payload = next_external_payload()
    context = step(prepare_context, payload)

    shared = step(shared_after_anchor, context)

    if branch(start_from=context):
        step(assert_branch_a, shared)
    elif branch(start_from=context):
        step(assert_branch_b, shared)
```

This example is intentionally small. It exists to show one idea clearly: in a full multi-case run, later branches can
restart from a saved step anchor instead of rerunning earlier shared setup.

If a value created by the anchor step implements `__store__` / `__restore__`, Journey restores that external state
only when a later branch actually starts from the anchor's post-exit boundary.

### Target the Second Branch

```bash
uv run journey --file docs/rehydration_journey/rehydration_journey.py --step assert_branch_b
```

```console
Plan
  docs/rehydration_journey/rehydration_journey.py:rehydration_journey ...
    case_1  labels: prepare_context, shared_after_anchor, assert_branch_a; branches: {bg_1=branch_1}
    case_2  labels: prepare_context, shared_after_anchor, assert_branch_b; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
    case_2  branches={bg_1=branch_2}
      prepare_context  ok attempt=1 duration=...
      shared_after_anchor  ok attempt=1 duration=...
      branch bg_1  branch_2
      assert_branch_b  ok attempt=1 duration=...
    case_2 done steps=3 duration=... stopped_at=assert_branch_b replay_anchor=prepare_context
  Summary: 1 journey executed, 1 case executed, 0 failed
```

The `replay_anchor=prepare_context` part tells you which step is the branch replay anchor. For a targeted `--step` run
it is reported metadata; Journey still executes from the selected case's required beginning unless existing state or
retry behavior causes replay.

## What To Notice

- Authoring stays sequential, even when execution becomes multi-case.
- Branch `start_from` points to an earlier step result, and later branch cases can resume from that step's post-exit
  boundary.
- External replay behavior lives on values themselves through `__store__` / `__restore__`, but those hooks run only
  when an explicit replay boundary needs that value.
- `--step` picks one case. `--develop-step` picks one case and stops after the target so you can iterate faster.
- Branching does not force you into a new DSL. It is still ordinary Python with `if` and `elif`.

Continue with [03 Retries and Resume](03-retries-and-resume.md) when the path is linear but the world around it is not ready yet.
