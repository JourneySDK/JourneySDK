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
OK cli plan_start | Plan
OK cli plan_journey | Journey docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey ...
OK cli plan_metadata | journey_id=broken_demo_journey function_ref=... ...
OK cli plan_case | - case_1 branch_env={} labels=['raise_expected_failure'] ...

OK cli plan_journey | Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey ...
OK cli plan_metadata | journey_id=good_demo_journey function_ref=... ...
OK cli plan_case | - case_1 branch_env={} labels=['finish_successfully'] ...
OK cli plan_summary | Summary: 2 journeys planned, 2 cases planned, 0 failed ...

OK cli execution_section | Execution
OK executor case_start | - case_1 start branches={}
OK executor step_start | step raise_expected_failure attempt=1 start
OK executor step_failure | step raise_expected_failure attempt=1 failed duration=... error=RuntimeError: expected tutorial failure

OK cli plan_journey | Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey ...
OK cli plan_metadata | journey_id=good_demo_journey function_ref=... ...
OK executor case_start | - case_1 start branches={}
OK executor step_start | step finish_successfully attempt=1 start
OK executor step_success | step finish_successfully attempt=1 ok duration=...
OK executor case_complete | - case_1 ok steps=1 duration=...
ERROR [execute] .../docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey (CallableExecutionError)
ERR cli command_error_message | What happened: Step 'raise_expected_failure' failed while it was running: RuntimeError: expected tutorial failure ...
ERR cli command_error_hint | Try this: Inspect the step implementation or rerun after fixing the underlying failure. ...

OK cli execute_summary | Summary: 1 journey executed, 1 case executed, 1 failed ...
```

Default mode is good when you want the broadest picture from one command. Even though one journey failed, Journey still ran the later successful one.

## Fail Fast: Stop Immediately

```bash
uv run journey --file docs/fail_fast_journeys/fail_fast_journeys.py --fail-fast
```

```console
OK cli plan_start | Plan
OK cli plan_journey | Journey docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey ...
OK cli plan_metadata | journey_id=broken_demo_journey function_ref=... ...
OK cli plan_case | - case_1 branch_env={} labels=['raise_expected_failure'] ...

OK cli plan_journey | Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey ...
OK cli plan_metadata | journey_id=good_demo_journey function_ref=... ...
OK cli plan_case | - case_1 branch_env={} labels=['finish_successfully'] ...
OK cli plan_summary | Summary: 2 journeys planned, 2 cases planned, 0 failed ...

OK cli execution_section | Execution
OK executor case_start | - case_1 start branches={}
OK executor step_start | step raise_expected_failure attempt=1 start
OK executor step_failure | step raise_expected_failure attempt=1 failed duration=... error=RuntimeError: expected tutorial failure
ERROR [execute] .../docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey (CallableExecutionError)
ERR cli command_error_message | What happened: Step 'raise_expected_failure' failed while it was running: RuntimeError: expected tutorial failure ...
ERR cli command_error_hint | Try this: Inspect the step implementation or rerun after fixing the underlying failure. ...

OK cli execute_summary | Summary: 0 journeys executed, 0 cases executed, 1 failed ...
```

This is the faster feedback loop when you only care about the first failure.

## How To Read Journey Failure Output

Each failed run gives you three layers of information:

- The pretty stdout log line shows the exact step label, attempt number, component, event, timestamp, and error class.
- The `ERROR [execute]` block summarizes the failure in plain English.
- The final summary tells you whether the CLI continued into later journeys or stopped early.

A good local debugging sequence usually looks like this:

1. Run the full file once.
2. If one step or one branch is the real problem, switch to `--step` or `--develop-step` from [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md).
3. If the failure is timing-related, use the retry patterns from [03 Retries and Resume](03-retries-and-resume.md).
4. If the first failure is enough and you want shorter feedback loops, add `--fail-fast`.

## What To Notice

- Journey failures are step-oriented. You do not have to guess which part of the flow broke.
- With `--state`, first Ctrl-C lets the active CLI step finish and resumes after it. A second Ctrl-C interrupts the
  dirty step immediately; Journey restarts that step from its replay boundary later instead of resuming inside the
  function body.
- Default mode and `--fail-fast` answer different questions. One maximizes coverage per run; the other maximizes speed to the first actionable error.
- Showing a failure example in the docs matters because this is what local development and CI actually look like.

After this chapter, go back to [Journey Docs](README.md) and pick the next pattern you want to use in your own journeys.
