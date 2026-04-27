# 06 Debugging and Failure Modes

Healthy documentation should show failure, not only success.

This chapter explains what Journey's failure output means and when `--fail-fast` changes the right thing.

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
Journey docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey
journey_id=broken_demo_journey function_ref=...
- case_1 branch_env={} labels=['raise_expected_failure']

Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey
journey_id=good_demo_journey function_ref=...
- case_1 branch_env={} labels=['finish_successfully']
Summary: 2 journeys planned, 2 cases planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=execution_log message="  step raise_expected_failure attempt=1 start"
[journey] time=... level=INFO component=executor event=execution_log message="  step raise_expected_failure attempt=1 failed duration=... error=RuntimeError: expected tutorial failure"

Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey
journey_id=good_demo_journey function_ref=...
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=execution_log message="  step finish_successfully attempt=1 start"
[journey] time=... level=INFO component=executor event=execution_log message="  step finish_successfully attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 ok steps=1 duration=..."
ERROR [execute] .../docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey (CallableExecutionError)
What happened: Step 'raise_expected_failure' failed while it was running: RuntimeError: expected tutorial failure
Try this: Inspect the step implementation or rerun after fixing the underlying failure.

Summary: 1 journey executed, 1 case executed, 1 failed
```

Default mode is good when you want the broadest picture from one command. Even though one journey failed, Journey still ran the later successful one.

## Fail Fast: Stop Immediately

```bash
uv run journey --file docs/fail_fast_journeys/fail_fast_journeys.py --fail-fast
```

```console
Plan
Journey docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey
journey_id=broken_demo_journey function_ref=...
- case_1 branch_env={} labels=['raise_expected_failure']

Journey docs/fail_fast_journeys/fail_fast_journeys.py:good_demo_journey
journey_id=good_demo_journey function_ref=...
- case_1 branch_env={} labels=['finish_successfully']
Summary: 2 journeys planned, 2 cases planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=execution_log message="  step raise_expected_failure attempt=1 start"
[journey] time=... level=INFO component=executor event=execution_log message="  step raise_expected_failure attempt=1 failed duration=... error=RuntimeError: expected tutorial failure"
ERROR [execute] .../docs/fail_fast_journeys/fail_fast_journeys.py:broken_demo_journey (CallableExecutionError)
What happened: Step 'raise_expected_failure' failed while it was running: RuntimeError: expected tutorial failure
Try this: Inspect the step implementation or rerun after fixing the underlying failure.

Summary: 0 journeys executed, 0 cases executed, 1 failed
```

This is the faster feedback loop when you only care about the first failure.

## How To Read Journey Failure Output

Each failed run gives you three layers of information:

- The structured stderr log line shows the exact step label, attempt number, component, event, timestamp, and error class.
- The `ERROR [execute]` block summarizes the failure in plain English.
- The final summary tells you whether the CLI continued into later journeys or stopped early.

A good local debugging sequence usually looks like this:

1. Run the full file once.
2. If one step or one branch is the real problem, switch to `--step` or `--develop-step` from [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md).
3. If the failure is timing-related, use the retry patterns from [03 Retries and Resume](03-retries-and-resume.md).
4. If the first failure is enough and you want shorter feedback loops, add `--fail-fast`.

## What To Notice

- Journey failures are step-oriented. You do not have to guess which part of the flow broke.
- Default mode and `--fail-fast` answer different questions. One maximizes coverage per run; the other maximizes speed to the first actionable error.
- Showing a failure example in the docs matters because this is what local development and CI actually look like.

After this chapter, go back to [Journey Docs](README.md) and pick the next pattern you want to use in your own journeys.
