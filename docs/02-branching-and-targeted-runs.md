# 02 Branching and Targeted Runs

The first real Journey payoff appears when one authored function turns into more than one executable case.

This chapter covers three related ideas:

- `checkpoint()` lets later cases replay from a known point
- `branch()` lets one function produce several linear cases
- `--step` and `--develop-step` let you run only the path that reaches one target label

## Branch Once, Reuse Shared Setup

Read `docs/branching_journey/branching_journey.py`.

```python
from journeysdk import branch, checkpoint, journey, step


@journey
def branching_journey() -> None:
    signup_request = step(load_signup_request)
    classified = step(classify_signup_request, signup_request)

    after_classification = checkpoint()
    if branch():
        step(assert_fast_track_path, classified)
    elif branch(start_from=after_classification):
        step(assert_manual_review_path, classified)
```

Why this shape matters:

- `load_signup_request` and `classify_signup_request` are shared setup
- the checkpoint gives later execution a replay anchor
- each `branch(...)` arm becomes its own case

`checkpoint()` is marker-only. External system state belongs on the values that
cross that boundary, not on the checkpoint itself. If a step result needs
custom replay behavior, define it at module top level and make that value
implement the Journey rehydration protocol:

```python
class BrowserSession:
    def __store__(self, context):
        ...

    @classmethod
    def __restore__(cls, payload, context):
        ...


session = step(open_browser_session)
after_login = checkpoint()
```

Journey stores and restores replayable step values whenever execution truly
rewinds to that boundary, such as a checkpoint-started later branch. The
protocol is documented in the README's Journey Rehydration Protocol section.

Journey compiles the branch structure internally before execution. A normal run executes every generated case; a
targeted run uses the compiled labels to choose one path.

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
Journey docs/branching_journey/branching_journey.py:branching_journey
journey_id=branching_journey function_ref=...
- case_2 start branches={bg_1=branch_2}
  step load_signup_request attempt=1 start
  step load_signup_request attempt=1 ok duration=...
  step classify_signup_request attempt=1 start
  step classify_signup_request attempt=1 ok duration=...
  branch bg_1=branch_2
  step assert_manual_review_path attempt=1 start
  step assert_manual_review_path attempt=1 ok duration=...
- case_2 ok steps=3 duration=... stopped_at=assert_manual_review_path replay_anchor=cp_1
Summary: 1 journey executed, 1 case executed, 0 failed
```

That output is the reason `--step` is so useful during development: Journey chooses the single case that reaches the label you care about.

### Pause After the Target Step While You Iterate

```bash
uv run journey --file docs/branching_journey/branching_journey.py --develop-step assert_manual_review_path
```

```console
Plan
Journey docs/branching_journey/branching_journey.py:branching_journey
journey_id=branching_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['load_signup_request', 'classify_signup_request', 'assert_fast_track_path']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['load_signup_request', 'classify_signup_request', 'assert_manual_review_path']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
Paused after step assert_manual_review_path attempt=1 ok.
Summary: 1 journey executed, 1 case executed, 0 failed
```

Use `--develop-step` when you are actively editing one branch and want Journey to stop at the point you care about instead of rerunning the whole world every time. After every continue or retry choice, Journey reloads and recompiles the selected journey file before it runs more code. Edits to the retried step or later steps are picked up immediately; if code that Journey would have reused from the already-run prefix changed, the selected case starts again from the beginning.

## Rehydrate Later Cases from a Checkpoint

Read `docs/rehydration_journey/rehydration_journey.py`.

```python
from journeysdk import branch, checkpoint, journey, step


@journey
def rehydration_journey() -> None:
    payload = next_external_payload()
    context = step(prepare_context, payload)

    after_setup = checkpoint()
    shared = step(shared_after_checkpoint, context)

    if branch(start_from=after_setup):
        step(assert_branch_a, shared)
    elif branch(start_from=after_setup):
        step(assert_branch_b, shared)
```

This example is intentionally small. It exists to show one idea clearly: later branches can restart from saved checkpoint state instead of rerunning shared setup.

If a value created before the checkpoint implements `__store__` /
`__restore__`, Journey restores that external state before the later branch
continues from the checkpoint anchor.

### Target the Second Branch

```bash
uv run journey --file docs/rehydration_journey/rehydration_journey.py --step assert_branch_b
```

```console
Plan
Journey docs/rehydration_journey/rehydration_journey.py:rehydration_journey
journey_id=rehydration_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['prepare_context', 'shared_after_checkpoint', 'assert_branch_a']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['prepare_context', 'shared_after_checkpoint', 'assert_branch_b']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
Journey docs/rehydration_journey/rehydration_journey.py:rehydration_journey
journey_id=rehydration_journey function_ref=...
- case_2 start branches={bg_1=branch_2}
  step prepare_context attempt=1 ok duration=...
  step shared_after_checkpoint attempt=1 ok duration=...
  branch bg_1=branch_2
  step assert_branch_b attempt=1 ok duration=...
- case_2 ok steps=3 duration=... stopped_at=assert_branch_b replay_anchor=cp_1
Summary: 1 journey executed, 1 case executed, 0 failed
```

The `replay_anchor=cp_1` part is what matters. It tells you which checkpoint Journey used as the branch's starting point.

## What To Notice

- Authoring stays sequential, even when execution becomes multi-case.
- `checkpoint()` is not just a marker for humans. It is a replay anchor for targeted runs and later branch execution.
- External replay behavior lives on values themselves through `__store__` / `__restore__`, so retries, checkpoint rewinds, and `--state` all use the same rehydration path.
- `--step` picks one case. `--develop-step` picks one case and then pauses so you can iterate faster.
- Branching does not force you into a new DSL. It is still ordinary Python with `if` and `elif`.

Continue with [03 Retries and Resume](03-retries-and-resume.md) when the path is linear but the world around it is not ready yet.
