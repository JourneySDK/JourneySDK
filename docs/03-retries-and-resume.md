# 03 Retries and Resume

Not every failure means the journey is wrong. Sometimes the system under test is still catching up. Sometimes a long
run needs to stop cleanly and resume from saved state.

This chapter covers both cases:

- retries when a step needs to poll or replay from an earlier step boundary
- `--state` when a run is interrupted and you want to resume from a step boundary later

## Three Retry Shapes

Read `docs/retry_journey/retry_journey.py`.

```python
from journeysdk import journey, step


@journey
def retry_current_step_journey() -> None:
    step(prepare_same_step_demo)
    step(wait_for_same_step, retry=1, retry_delay=0)


@journey
def retry_from_step_result_journey() -> None:
    request = step(issue_report_request)
    report = step(
        wait_for_report,
        request,
        retry=1,
        retry_delay=0,
        retry_from=request,
    )
    step(assert_report_ready, report)


@journey
def retry_from_step_anchor_journey() -> None:
    request = step(load_status_request)
    retry_anchor = step(refresh_status_cache)
    result = step(
        wait_for_anchor_retry,
        request,
        retry_anchor,
        retry=1,
        retry_delay=0,
        retry_from=retry_anchor,
    )
    step(assert_anchor_retry_ready, result)
```

Those three journeys represent the three most common retry boundaries:

- retry only the failing step
- retry from an earlier step result
- retry from an earlier setup step and replay everything from that step

Step-anchor replay follows the same rule as `--state`: Journey reuses saved step
bindings before the replay boundary, then reruns from the anchor step. If one of
those values needs custom side effects, put that logic on a module-level value
type with `__store__` / `__restore__` as described in the README's Journey
Rehydration Protocol section.

That matters when the replayable value wraps external state instead of being a
plain pickleable object:

```python
class ExternalState:
    def __store__(self, context):
        ...

    @classmethod
    def __restore__(cls, payload, context):
        ...


def next_context() -> ExternalState:
    ...


@journey
def retry_with_external_state() -> None:
    context = step(next_context)
    anchor = step(refresh_after_anchor, context)
    step(wait_until_ready, context, retry=1, retry_delay=0, retry_from=anchor)
```

[journey] time=... level=INFO component=cli event=plan_journey message="Journey stores step results before the replay boundary and reuses them on" ...
retries and resume, so the same external-state logic works for step-anchor
rewinds and `--state` restores.

### Retry the Current Step

```bash
uv run journey --file docs/retry_journey/retry_journey.py --journey retry_current_step_journey
```

```console
[journey] time=... level=INFO component=cli event=plan_start message=Plan
[journey] time=... level=INFO component=cli event=plan_journey message="Journey docs/retry_journey/retry_journey.py:retry_current_step_journey" ...
[journey] time=... level=INFO component=cli event=plan_metadata message="journey_id=retry_current_step_journey function_ref=..." ...
[journey] time=... level=INFO component=cli event=plan_case message="- case_1 branch_env={} labels=['prepare_same_step_demo', 'wait_for_same_step']" ...
[journey] time=... level=INFO component=cli event=plan_summary message="Summary: 1 journey planned, 1 case planned, 0 failed" ...

[journey] time=... level=INFO component=cli event=execution_section message=Execution
[journey] time=... level=INFO component=executor event=case_start message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=step_start message="  step prepare_same_step_demo attempt=1 start"
[journey] time=... level=INFO component=executor event=step_success message="  step prepare_same_step_demo attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=step_start message="  step wait_for_same_step attempt=1 start"
[journey] time=... level=INFO component=executor event=step_retry message="  step wait_for_same_step attempt=1 retry duration=... delay=0.000s remaining=0 error=RuntimeError: still waiting for the same-step retry demo"
[journey] time=... level=INFO component=executor event=step_start message="  step wait_for_same_step attempt=2 start"
[journey] time=... level=INFO component=executor event=step_success message="  step wait_for_same_step attempt=2 ok duration=..."
[journey] time=... level=INFO component=executor event=case_complete message="- case_1 ok steps=2 duration=..."
[journey] time=... level=INFO component=cli event=execute_summary message="Summary: 1 journey executed, 1 case executed, 0 failed" ...
```

### Retry from an Earlier Step Result

```bash
uv run journey --file docs/retry_journey/retry_journey.py --journey retry_from_step_result_journey
```

```console
[journey] time=... level=INFO component=cli event=plan_start message=Plan
[journey] time=... level=INFO component=cli event=plan_journey message="Journey docs/retry_journey/retry_journey.py:retry_from_step_result_journey" ...
[journey] time=... level=INFO component=cli event=plan_metadata message="journey_id=retry_from_step_result_journey function_ref=..." ...
[journey] time=... level=INFO component=cli event=plan_case message="- case_1 branch_env={} labels=['issue_report_request', 'wait_for_report', 'assert_report_ready']" ...
[journey] time=... level=INFO component=cli event=plan_summary message="Summary: 1 journey planned, 1 case planned, 0 failed" ...

[journey] time=... level=INFO component=cli event=execution_section message=Execution
[journey] time=... level=INFO component=executor event=case_start message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=step_start message="  step issue_report_request attempt=1 start"
[journey] time=... level=INFO component=executor event=step_success message="  step issue_report_request attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=step_start message="  step wait_for_report attempt=1 start"
[journey] time=... level=INFO component=executor event=step_retry message="  step wait_for_report attempt=1 retry duration=... delay=0.000s remaining=0 error=RuntimeError: report not ready yet"
[journey] time=... level=INFO component=executor event=step_start message="  step issue_report_request attempt=2 start"
[journey] time=... level=INFO component=executor event=step_success message="  step issue_report_request attempt=2 ok duration=..."
[journey] time=... level=INFO component=executor event=step_start message="  step wait_for_report attempt=2 start"
[journey] time=... level=INFO component=executor event=step_success message="  step wait_for_report attempt=2 ok duration=..."
[journey] time=... level=INFO component=executor event=step_success message="  step assert_report_ready attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=case_complete message="- case_1 ok steps=3 duration=..."
[journey] time=... level=INFO component=cli event=execute_summary message="Summary: 1 journey executed, 1 case executed, 0 failed" ...
```

### Retry from an Earlier Setup Step

```bash
uv run journey --file docs/retry_journey/retry_journey.py --journey retry_from_step_anchor_journey
```

```console
[journey] time=... level=INFO component=cli event=plan_start message=Plan
[journey] time=... level=INFO component=cli event=plan_journey message="Journey docs/retry_journey/retry_journey.py:retry_from_step_anchor_journey" ...
[journey] time=... level=INFO component=cli event=plan_metadata message="journey_id=retry_from_step_anchor_journey function_ref=..." ...
[journey] time=... level=INFO component=cli event=plan_case message="- case_1 branch_env={} labels=['load_status_request', 'refresh_status_cache', 'wait_for_anchor_retry', 'assert_anchor_retry_ready']" ...
[journey] time=... level=INFO component=cli event=plan_summary message="Summary: 1 journey planned, 1 case planned, 0 failed" ...

[journey] time=... level=INFO component=cli event=execution_section message=Execution
[journey] time=... level=INFO component=executor event=case_start message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=step_success message="  step load_status_request attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=step_success message="  step refresh_status_cache attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=step_retry message="  step wait_for_anchor_retry attempt=1 retry duration=... delay=0.000s remaining=0 error=RuntimeError: step-anchor retry demo is still waiting"
[journey] time=... level=INFO component=executor event=step_start message="  step refresh_status_cache attempt=2 start"
[journey] time=... level=INFO component=executor event=step_success message="  step refresh_status_cache attempt=2 ok duration=..."
[journey] time=... level=INFO component=executor event=step_success message="  step wait_for_anchor_retry attempt=2 ok duration=..."
[journey] time=... level=INFO component=executor event=step_success message="  step assert_anchor_retry_ready attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=case_complete message="- case_1 ok steps=4 duration=..."
[journey] time=... level=INFO component=cli event=execute_summary message="Summary: 1 journey executed, 1 case executed, 0 failed" ...
```

## Resume an Interrupted Run with `--state`

Read `docs/resume_journey/resume_journey.py`.

```python
from journeysdk import journey, step


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
        f"Press Ctrl-C once during the next {pause_seconds:.1f} seconds to stop "
        "gracefully after this step reaches post-exit. Press Ctrl-C a second time "
        "to interrupt inside this step and rerun it later from saved inputs."
    )
    time.sleep(pause_seconds)
    return ticket


@journey
def resume_journey() -> None:
    pause_seconds = 2.0
    ticket = step(load_support_ticket)
    resumed_ticket = step(wait_for_resume_signal, ticket, pause_seconds)
    step(assert_resumed_ticket, resumed_ticket)
```

The key rule is that Journey resumes at a step boundary, not in the middle of a function body. In CLI runs with
`--state`, first Ctrl-C is graceful: Journey lets the active step finish storage, exit returned handles, and stop at
post-exit. Press Ctrl-C a second time to interrupt inside the dirty step; on the next run Journey restarts that step
from the top with saved inputs.

### Reset the Demo State

```bash
uv run python -c "from docs.resume_journey import reset_demo_state; reset_demo_state(state_path='/tmp/journey-resume-tutorial.state')"
```

### First Run: Interrupt It

```bash
uv run journey --file docs/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

Press `Ctrl-C` once when the tutorial note tells you to. The command stops after the active step completes; press it a
second time only if you want to force an immediate dirty-step interruption.

Expected structured stdout:

```console
[journey] time=... level=INFO component=cli event=plan_start message=Plan
[journey] time=... level=INFO component=cli event=plan_journey message="Journey docs/resume_journey/resume_journey.py:resume_journey" ...
[journey] time=... level=INFO component=cli event=plan_metadata message="journey_id=resume_journey function_ref=..." ...
[journey] time=... level=INFO component=cli event=plan_case message="- case_1 branch_env={} labels=['load_support_ticket', 'wait_for_resume_signal', 'assert_resumed_ticket']" ...
[journey] time=... level=INFO component=cli event=plan_summary message="Summary: 1 journey planned, 1 case planned, 0 failed" ...

[journey] time=... level=INFO component=cli event=execution_section message=Execution
[journey] time=... level=INFO component=executor event=case_start message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=step_success message="  step load_support_ticket attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=step_start message="  step wait_for_resume_signal attempt=1 start"
[journey] time=... level=WARNING component=cli event=graceful_interrupt_requested message="interrupt requested; waiting for the active step to reach post-exit" ...
[journey] time=... level=INFO component=executor event=step_success message="  step wait_for_resume_signal attempt=1 ok duration=..."
[journey] time=... level=WARNING component=cli event=interrupt_summary message=Interrupted. ...
[journey] time=... level=WARNING component=cli event=interrupt_message message="What happened: Journey execution was interrupted before it finished." ...
[journey] time=... level=WARNING component=cli event=interrupt_hint message="Try this: Run the same command again with --state ..." ...
```

Additional structured stdout:

```console
[journey] time=... level=INFO component=tutorial event=tutorial_note message="Loaded support ticket ticket-001 and saved it as the result of load_support_ticket(). ..."
[journey] time=... level=INFO component=tutorial event=tutorial_note message="wait_for_resume_signal() is starting with saved ticket ticket-001. ..."
[journey] time=... level=INFO component=tutorial event=tutorial_note message="Press Ctrl-C once during the next 2.0 seconds to stop gracefully after this step reaches post-exit. ..."
```

### Second Run: Resume It

```bash
uv run journey --file docs/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

Expected structured stdout:

```console
[journey] time=... level=INFO component=cli event=plan_start message=Plan
[journey] time=... level=INFO component=cli event=plan_journey message="Journey docs/resume_journey/resume_journey.py:resume_journey" ...
[journey] time=... level=INFO component=cli event=plan_metadata message="journey_id=resume_journey function_ref=..." ...
[journey] time=... level=INFO component=cli event=plan_case message="- case_1 branch_env={} labels=['load_support_ticket', 'wait_for_resume_signal', 'assert_resumed_ticket']" ...
[journey] time=... level=INFO component=cli event=plan_summary message="Summary: 1 journey planned, 1 case planned, 0 failed" ...

[journey] time=... level=INFO component=cli event=execution_section message=Execution
[journey] time=... level=INFO component=executor event=case_resume message="- case_1 resume branches={}" ...
[journey] time=... level=INFO component=executor event=step_success message="  step assert_resumed_ticket attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=case_complete message="- case_1 ok steps=3 duration=..."
[journey] time=... level=INFO component=cli event=execute_summary message="Summary: 1 journey executed, 1 case executed, 0 failed" ...
```

Additional structured stdout:

```console
[journey] time=... level=INFO component=tutorial event=tutorial_note message="wait_for_resume_signal() is starting with saved ticket ticket-001. ..."
[journey] time=... level=INFO component=tutorial event=tutorial_note message="The journey finished. If this run resumed from saved state, wait_for_resume_signal() restarted with the same saved ticket while load_support_ticket() was reused from the earlier successful step."
```

## What To Notice

- Retries are explicit. Journey does not silently retry behind your back.
- `retry_from=` is the switch that decides the replay boundary.
- Step anchors define replay boundaries. Retry from a step reruns the anchor step; branch `start_from` resumes from the
  anchor step's completed post-exit state.
- Any value that Journey may need to replay later must be pickle-serializable or rehydratable.
- `--state` keeps successful step bindings so the rerun can skip what already succeeded.
- First Ctrl-C in a CLI `--state` run resumes after the completed step; second Ctrl-C restarts the dirty step from the
  top later. Journey never jumps into the middle of the function.

Continue with [04 Browser and Local Integrations](04-browser-and-local-integrations.md) when your steps need to open real pages, receive webhooks, or inspect local files.
