# 02 Branching and Targeted Runs

The first real Journey payoff appears when one authored function turns into more than one executable case.

This chapter covers three related ideas:

- step anchors let later cases replay from a known point
- `branch()` lets one function produce several linear cases
- `--step` and `--develop-step` let you run only the case that reaches one target label

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

External system state belongs on the step values that cross replay boundaries.
If a step result needs custom rehydration behavior, define it at module top
level and make that value implement the Journey rehydration protocol:

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

Journey stores and restores replayable step values whenever execution truly
rewinds to a step boundary, such as a step-started later branch. The protocol is
documented in the README's Journey Rehydration Protocol section.

Journey compiles the branch structure internally before execution. A normal run executes every generated case; a
targeted run uses the compiled labels to choose one case.

### Run Only the Branch That Reaches One Step

```bash
uv run journey --file docs/branching_journey/branching_journey.py --step assert_manual_review_path
```

```console
Plan
Journey docs/branching_journey/branching_journey.py:branching_journey
journey_id=branching_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['load_signup_request', 'classify_signup_request', 'assert_fast_track_path']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['load_signup_request', 'classify_signup_request', 'assert_manual_review_path']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 start branches={bg_1=branch_2}"
[journey] time=... level=INFO component=executor event=execution_log message="  step load_signup_request attempt=1 start"
[journey] time=... level=INFO component=executor event=execution_log message="  step load_signup_request attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step classify_signup_request attempt=1 start"
[journey] time=... level=INFO component=executor event=execution_log message="  step classify_signup_request attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  branch bg_1=branch_2"
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_manual_review_path attempt=1 start"
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_manual_review_path attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 ok steps=3 duration=... stopped_at=assert_manual_review_path replay_anchor=classify_signup_request"
Summary: 1 journey executed, 1 case executed, 0 failed
```

That output is the reason `--step` is so useful during development: Journey chooses the single case that reaches the
label you care about. The reported `replay_anchor` names the branch step anchor, but this targeted run still starts
from the selected case's beginning.

### Stop After the Target Step While You Iterate

```bash
uv run journey --file docs/branching_journey/branching_journey.py --develop-step assert_manual_review_path --state dev.state
```

```console
Plan
Journey docs/branching_journey/branching_journey.py:branching_journey
journey_id=branching_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['load_signup_request', 'classify_signup_request', 'assert_fast_track_path']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['load_signup_request', 'classify_signup_request', 'assert_manual_review_path']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
Development mode stopped after step assert_manual_review_path attempt=1 ok.
Summary: 0 journeys executed, 0 cases executed, 0 failed
```

Use `--develop-step` when you are actively editing one branch and want Journey to pause after the step boundary you
care about. Rerun the same command to retry the paused step after editing code, or target the next step with the same
state file to continue. Develop-step retries replay from the paused step's replay boundary, are unlimited, and do not
spend the step's configured `step(..., retry=...)` budget. Add `--interactive` when you want Journey to keep the process
open and prompt after each paused step. Journey reloads and recompiles the selected journey file before each retry or
continue, so edits to the retried step or later steps are picked up immediately; if code that Journey would have reused
from the already-run prefix changed, the selected case starts again from the beginning.

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

If a value created by the anchor step implements `__store__` / `__restore__`,
Journey restores that external state before the later branch continues from the
anchor's post-exit boundary.

### Target the Second Branch

```bash
uv run journey --file docs/rehydration_journey/rehydration_journey.py --step assert_branch_b
```

```console
Plan
Journey docs/rehydration_journey/rehydration_journey.py:rehydration_journey
journey_id=rehydration_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['prepare_context', 'shared_after_anchor', 'assert_branch_a']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['prepare_context', 'shared_after_anchor', 'assert_branch_b']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 start branches={bg_1=branch_2}"
[journey] time=... level=INFO component=executor event=execution_log message="  step prepare_context attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step shared_after_anchor attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  branch bg_1=branch_2"
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_branch_b attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 ok steps=3 duration=... stopped_at=assert_branch_b replay_anchor=prepare_context"
Summary: 1 journey executed, 1 case executed, 0 failed
```

The `replay_anchor=prepare_context` part tells you which step is the branch replay anchor. For a targeted `--step` run
it is reported metadata; Journey still executes from the selected case's required beginning unless existing state or
retry behavior causes replay.

## What To Notice

- Authoring stays sequential, even when execution becomes multi-case.
- Branch `start_from` points to an earlier step result, and later branch cases resume from that step's post-exit
  boundary.
- External replay behavior lives on values themselves through `__store__` / `__restore__`, so retries, branch
  rewinds, and `--state` all use the same rehydration path.
- `--step` picks one case. `--develop-step` picks one case and stops after the target so you can iterate faster.
- Branching does not force you into a new DSL. It is still ordinary Python with `if` and `elif`.

Continue with [03 Retries and Resume](03-retries-and-resume.md) when the path is linear but the world around it is not ready yet.
