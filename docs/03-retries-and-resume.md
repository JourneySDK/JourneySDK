# 03 Retries and Resume

Not every failure means the journey is wrong. Sometimes the system under test is still catching up. Sometimes a run gets interrupted halfway through.

This chapter covers both cases:

- retries when a step needs to poll or replay from earlier state
- `--state` when a whole run is interrupted and you want to continue later

## Three Retry Shapes

Read `docs/retry_journey/retry_journey.py`.

```python
@journey.journey
def retry_current_step_journey() -> None:
    journey.step(prepare_same_step_demo)
    journey.step(wait_for_same_step, retry=1, retry_delay=0)


@journey.journey
def retry_from_step_result_journey() -> None:
    request = journey.step(issue_report_request)
    report = journey.step(
        wait_for_report,
        request,
        retry=1,
        retry_delay=0,
        retry_from=request,
    )
    journey.step(assert_report_ready, report)


@journey.journey
def retry_from_checkpoint_journey() -> None:
    request = journey.step(load_status_request)
    retry_anchor = journey.checkpoint()
    cache = journey.step(refresh_status_cache)
    result = journey.step(
        wait_for_checkpoint_retry,
        request,
        cache,
        retry=1,
        retry_delay=0,
        retry_from=retry_anchor,
    )
    journey.step(assert_checkpoint_retry_ready, result)
```

Those three journeys represent the three most common retry strategies:

- retry only the failing step
- retry from an earlier step result
- retry from a checkpoint and replay everything after it

### Retry the Current Step

```bash
uv run journey execute --file docs/retry_journey/retry_journey.py --journey retry_current_step_journey
```

```console
Journey docs/retry_journey/retry_journey.py:retry_current_step_journey
journey_id=retry_current_step_journey function_ref=...
- case_1 start branches={}
  step prepare_same_step_demo attempt=1 start
  step prepare_same_step_demo attempt=1 ok duration=...
  step wait_for_same_step attempt=1 start
  step wait_for_same_step attempt=1 retry duration=... delay=0.000s remaining=0 error=RuntimeError: still waiting for the same-step retry demo
  step wait_for_same_step attempt=2 start
  step wait_for_same_step attempt=2 ok duration=...
- case_1 ok steps=2 duration=...
Summary: 1 journey executed, 1 case executed, 0 failed
```

### Retry from an Earlier Step Result

```bash
uv run journey execute --file docs/retry_journey/retry_journey.py --journey retry_from_step_result_journey
```

```console
Journey docs/retry_journey/retry_journey.py:retry_from_step_result_journey
journey_id=retry_from_step_result_journey function_ref=...
- case_1 start branches={}
  step issue_report_request attempt=1 start
  step issue_report_request attempt=1 ok duration=...
  step wait_for_report attempt=1 start
  step wait_for_report attempt=1 retry duration=... delay=0.000s remaining=0 error=RuntimeError: report not ready yet
  step issue_report_request attempt=2 start
  step issue_report_request attempt=2 ok duration=...
  step wait_for_report attempt=2 start
  step wait_for_report attempt=2 ok duration=...
  step assert_report_ready attempt=1 ok duration=...
- case_1 ok steps=3 duration=...
Summary: 1 journey executed, 1 case executed, 0 failed
```

### Retry from a Checkpoint

```bash
uv run journey execute --file docs/retry_journey/retry_journey.py --journey retry_from_checkpoint_journey
```

```console
Journey docs/retry_journey/retry_journey.py:retry_from_checkpoint_journey
journey_id=retry_from_checkpoint_journey function_ref=...
- case_1 start branches={}
  step load_status_request attempt=1 ok duration=...
  step refresh_status_cache attempt=1 ok duration=...
  step wait_for_checkpoint_retry attempt=1 retry duration=... delay=0.000s remaining=0 error=RuntimeError: checkpoint retry demo is still waiting
  step refresh_status_cache attempt=2 start
  step refresh_status_cache attempt=2 ok duration=...
  step wait_for_checkpoint_retry attempt=2 ok duration=...
  step assert_checkpoint_retry_ready attempt=1 ok duration=...
- case_1 ok steps=4 duration=...
Summary: 1 journey executed, 1 case executed, 0 failed
```

## Resume an Interrupted Run with `--state`

Read `docs/resume_journey/resume_journey.py`.

```python
def wait_for_resume_signal(
    ticket: dict[str, str],
    pause_seconds: float,
) -> dict[str, str]:
    _tutorial_note(
        "wait_for_resume_signal() is starting with saved ticket "
        f"{ticket['ticket_id']}. journey resumes at the step boundary, so this step "
        "restarts from the top on resume with the same saved inputs."
    )
    _tutorial_note(
        f"Press Ctrl-C during the next {pause_seconds:.1f} seconds to interrupt after "
        "the earlier step has already been saved. Then rerun the same command with "
        "--state to resume from this step boundary."
    )
    time.sleep(pause_seconds)
    return ticket


@journey.journey
def resume_journey() -> None:
    pause_seconds = 2.0
    ticket = journey.step(load_support_ticket)
    resumed_ticket = journey.step(wait_for_resume_signal, ticket, pause_seconds)
    journey.step(assert_resumed_ticket, resumed_ticket)
```

The key rule is that Journey resumes at a step boundary, not in the middle of a function body.

### Reset the Demo State

```bash
uv run python -c "from docs.resume_journey import reset_demo_state; reset_demo_state(state_path='/tmp/journey-resume-tutorial.state')"
```

### First Run: Interrupt It

```bash
uv run journey execute --file docs/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

Press `Ctrl-C` when the tutorial note tells you to.

Expected stdout:

```console
Journey docs/resume_journey/resume_journey.py:resume_journey
journey_id=resume_journey function_ref=...
- case_1 start branches={}
  step load_support_ticket attempt=1 ok duration=...
  step wait_for_resume_signal attempt=1 start
  step wait_for_resume_signal attempt=1 interrupted duration=...
Interrupted.
Try this: Run the same command again with --state
```

Expected stderr:

```console
[tutorial] Loaded support ticket ticket-001 and saved it as the result of load_support_ticket().
[tutorial] wait_for_resume_signal() is starting with saved ticket ticket-001. journey resumes at the step boundary, so this step restarts from the top on resume with the same saved inputs.
[tutorial] Press Ctrl-C during the next 2.0 seconds to interrupt after the earlier step has already been saved. Then rerun the same command with --state to resume from this step boundary.
```

### Second Run: Resume It

```bash
uv run journey execute --file docs/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

Expected stdout:

```console
Journey docs/resume_journey/resume_journey.py:resume_journey
journey_id=resume_journey function_ref=...
- case_1 resume branches={}
  step wait_for_resume_signal attempt=2 start
  step wait_for_resume_signal attempt=2 ok duration=...
  step assert_resumed_ticket attempt=1 ok duration=...
- case_1 ok steps=3 duration=...
Summary: 1 journey executed, 1 case executed, 0 failed
```

Expected stderr:

```console
[tutorial] wait_for_resume_signal() is starting with saved ticket ticket-001. journey resumes at the step boundary, so this step restarts from the top on resume with the same saved inputs.
[tutorial] The journey finished. If this run resumed from saved state, wait_for_resume_signal() restarted with the same saved ticket while load_support_ticket() was reused from the earlier successful step.
```

## What To Notice

- Retries are explicit. Journey does not silently retry behind your back.
- `retry_from=` is the switch that decides how much earlier work gets replayed.
- Any value that Journey may need to replay later must be pickle-serializable.
- `--state` keeps successful step results so the rerun can skip what already succeeded.
- Resume starts the interrupted step again from the top. It does not jump into the middle of the function.

Continue with [04 Browser and Local Integrations](04-browser-and-local-integrations.md) when your steps need to open real pages, receive webhooks, or inspect local files.
