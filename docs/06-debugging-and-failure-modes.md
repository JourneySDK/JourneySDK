# 06 Debugging and Failure Modes

Healthy documentation should show failure, not only success.

This chapter explains what Journey's failure output means, how to think about the failing step boundary, and when
`--fail-fast` changes the right thing.

## A Deliberately Broken File

Read `docs/fail_fast_journeys/fail_fast_journeys.py`.

```python
from journeysdk import journey, step


def raise_expected_failure() -> bool:
    raise RuntimeError("expected tutorial failure")


def finish_successfully() -> bool:
    return True


@journey
def broken_demo_journey() -> None:
    step(raise_expected_failure)


@journey
def good_demo_journey() -> None:
    step(finish_successfully)
```

This file exists to answer a practical question: when one discovered journey fails, should the CLI keep going or stop immediately?

## Default Behavior: Keep Going

```bash
uv run journey --file docs/fail_fast_journeys/fail_fast_journeys.py
```

```console
Plan
  docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey ...
    case_1  labels: raise_expected_failure

  docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey ...
    case_1  labels: finish_successfully
  Summary: 2 journeys planned, 2 cases planned, 0 failed

Execution
    case_1
      raise_expected_failure  start attempt=1
Error: raise_expected_failure failed after ... (RuntimeError: expected tutorial failure)

  docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey ...
    case_1
      finish_successfully  start attempt=1
      finish_successfully  ok attempt=1 duration=...
    case_1 done steps=1 duration=...
Error: CallableExecutionError during execute at .../docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey
What happened: Step 'raise_expected_failure' failed while it was running: RuntimeError: expected tutorial failure ...
Try this: Inspect the step implementation or rerun after fixing the underlying failure. ...

  Summary: 1 journey executed, 1 case executed, 1 failed
```

Default mode is good when you want the broadest picture from one command. Even though one journey failed, Journey still ran the later successful one.

## Fail Fast: Stop Immediately

```bash
uv run journey --file docs/fail_fast_journeys/fail_fast_journeys.py --fail-fast
```

```console
Plan
  docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey ...
    case_1  labels: raise_expected_failure

  docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey ...
    case_1  labels: finish_successfully
  Summary: 2 journeys planned, 2 cases planned, 0 failed

Execution
    case_1
      raise_expected_failure  start attempt=1
Error: raise_expected_failure failed after ... (RuntimeError: expected tutorial failure)
Error: CallableExecutionError during execute at .../docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey
What happened: Step 'raise_expected_failure' failed while it was running: RuntimeError: expected tutorial failure ...
Try this: Inspect the step implementation or rerun after fixing the underlying failure. ...

  Summary: 0 journeys executed, 0 cases executed, 1 failed
```

This is the faster feedback loop when you only care about the first failure.

## How To Read Journey Failure Output

Each failed run gives you three layers of information:

- The pretty stdout line shows the step label, attempt number, and error class without exposing structured event codenames.
- The error block summarizes the failure in plain English.
- The final summary tells you whether the CLI continued into later journeys or stopped early.

A good local debugging sequence usually looks like this:

1. Run the full file once.
2. If one step or one branch is the real problem, switch to `--step` or `--develop-step` from [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md).
3. If the failure is timing-related, use the retry patterns from [03 Retries and Resume](03-retries-and-resume.md).
4. If the first failure is enough and you want shorter feedback loops, add `--fail-fast`.

If output includes `State: invalidated ...`, Journey found a stale saved checkpoint and reran from a safe boundary. That
is expected after changes to the journey plan, step source, runtime, or workspace inputs. Use `--no-state` when you want
fresh-path evidence instead of development replay.

## What To Notice

- Journey failures are step-oriented. You do not have to guess which part of the flow broke.
- With default state, first Ctrl-C logs that Journey is finishing the active step, then resumes after that completed step.
  A second Ctrl-C logs that Journey is stopping now, interrupts the dirty step, and restarts that step from saved
  inputs later instead of resuming inside the function body.
- With `--no-state`, Ctrl-C is not resumable. The interrupted summary says to rerun with state updates enabled next time.
- Default mode and `--fail-fast` answer different questions. One maximizes coverage per run; the other maximizes speed to the first actionable error.
- Showing a failure example in the docs matters because this is what local development and CI actually look like.

After this chapter, go back to [Journey Docs](README.md) and pick the next pattern you want to use in your own journeys.
