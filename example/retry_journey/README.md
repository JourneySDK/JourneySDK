# Retry Journey

This stage shows the three retry patterns in Journey SDK:

- retry the current step
- retry from an earlier step result
- retry from a checkpoint

All three examples are deterministic, so you can run them repeatedly and get the same behavior.

## What this teaches

- how `retry=` enables retries after a step raises an exception
- how `retry_delay=` controls the wait between attempts
- how `retry_from=` changes what gets replayed before the next attempt
- why replayed values must be pickle-serializable when retries can reuse them later

## Files to read

- `example/retry_journey/retry_journey.py`

## Run it

1. Plan the file:

```bash
uv run journey plan --file example/retry_journey/retry_journey.py
```

What to expect:

- three discovered journeys
- one planned case per journey

2. Retry only the current step:

```bash
uv run journey execute --file example/retry_journey/retry_journey.py --journey retry_current_step_journey
```

What to expect:

- `wait_for_same_step` starts, retries once, then succeeds
- `prepare_same_step_demo` runs only once

3. Retry from an earlier step result:

```bash
uv run journey execute --file example/retry_journey/retry_journey.py --journey retry_from_step_result_journey
```

What to expect:

- `issue_report_request` runs twice
- `wait_for_report` uses the new request on the second attempt
- the journey finishes with `assert_report_ready`

4. Retry from a checkpoint:

```bash
uv run journey execute --file example/retry_journey/retry_journey.py --journey retry_from_checkpoint_journey
```

What to expect:

- `load_status_request` runs once
- `refresh_status_cache` runs twice
- `wait_for_checkpoint_retry` runs twice and succeeds on the second attempt

## Why this matters

Async systems often need polling or replay. Journey SDK keeps that logic explicit. You decide whether a retry should
rerun only the failing step, restart from an earlier step, or replay everything after a checkpoint.

The example keeps every replayed value to plain dictionaries and strings. That is intentional. If Journey SDK may need
to save and restore a value for retries, resume, or branch replay, that value must be pickle-serializable.

## Next step

Continue with [`resume_journey/README.md`](../resume_journey/README.md) to see how interrupted runs resume from saved
state.
