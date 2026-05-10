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

  stores step results before the replay boundary and reuses them on ...
retries and resume, so the same external-state logic works for step-anchor
rewinds and `--state` restores.

### Retry the Current Step

```bash
uv run journey --file docs/retry_journey/retry_journey.py --journey retry_current_step_journey
```

```console
Plan
  docs/retry_journey/retry_journey.py:retry_current_step_journey ...
    case_1  labels: prepare_same_step_demo, wait_for_same_step
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      prepare_same_step_demo  start attempt=1
      prepare_same_step_demo  ok attempt=1 duration=...
      wait_for_same_step  start attempt=1
Warning: wait_for_same_step retry after ... (RuntimeError: still waiting for the same-step retry demo)
      wait_for_same_step  start attempt=2
      wait_for_same_step  ok attempt=2 duration=...
    case_1 done steps=2 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

### Retry from an Earlier Step Result

```bash
uv run journey --file docs/retry_journey/retry_journey.py --journey retry_from_step_result_journey
```

```console
Plan
  docs/retry_journey/retry_journey.py:retry_from_step_result_journey ...
    case_1  labels: issue_report_request, wait_for_report, assert_report_ready
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      issue_report_request  start attempt=1
      issue_report_request  ok attempt=1 duration=...
      wait_for_report  start attempt=1
Warning: wait_for_report retry after ... (RuntimeError: report not ready yet)
      issue_report_request  start attempt=2
      issue_report_request  ok attempt=2 duration=...
      wait_for_report  start attempt=2
      wait_for_report  ok attempt=2 duration=...
      assert_report_ready  ok attempt=1 duration=...
    case_1 done steps=3 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

### Retry from an Earlier Setup Step

```bash
uv run journey --file docs/retry_journey/retry_journey.py --journey retry_from_step_anchor_journey
```

```console
Plan
  docs/retry_journey/retry_journey.py:retry_from_step_anchor_journey ...
    case_1  labels: load_status_request, refresh_status_cache, wait_for_anchor_retry, assert_anchor_retry_ready
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      load_status_request  ok attempt=1 duration=...
      refresh_status_cache  ok attempt=1 duration=...
Warning: wait_for_anchor_retry retry after ... (RuntimeError: step-anchor retry demo is still waiting)
      refresh_status_cache  start attempt=2
      refresh_status_cache  ok attempt=2 duration=...
      wait_for_anchor_retry  ok attempt=2 duration=...
      assert_anchor_retry_ready  ok attempt=1 duration=...
    case_1 done steps=4 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
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
        "to stop now; Journey will rerun this step later from saved inputs."
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
post-exit. Press Ctrl-C a second time to stop now; Journey interrupts the dirty step, and on the next run restarts
that step from the top with saved inputs. Without `--state`, Ctrl-C stops immediately and cannot resume.

### Reset the Demo State

```bash
uv run python -c "from docs.resume_journey import reset_demo_state; reset_demo_state(state_path='/tmp/journey-resume-tutorial.state')"
```

### First Run: Graceful Ctrl-C

```bash
uv run journey --file docs/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

Press `Ctrl-C` once when the tutorial note tells you to. The command stops after the active step completes; press it a
second time only if you want to force an immediate dirty-step interruption.

Expected pretty stdout:

```console
Plan
  docs/resume_journey/resume_journey.py:resume_journey ...
    case_1  labels: load_support_ticket, wait_for_resume_signal, assert_resumed_ticket
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      load_support_ticket  ok attempt=1 duration=...
      wait_for_resume_signal  start attempt=1
Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.
      wait_for_resume_signal  ok attempt=1 duration=...
Interrupted: Journey execution was interrupted before it finished.
Hint: Run the same command again with --state ... to resume from saved progress.
```

Additional pretty stdout:

```console
wait_for_resume_signal() is starting with saved ticket ticket-001. ...
Press Ctrl-C once during the next 2.0 seconds to stop gracefully after this step reaches post-exit. ...
```

### Second Run After Graceful Ctrl-C: Continue After the Completed Step

```bash
uv run journey --file docs/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

Expected pretty stdout:

```console
Plan
  docs/resume_journey/resume_journey.py:resume_journey ...
    case_1  labels: load_support_ticket, wait_for_resume_signal, assert_resumed_ticket
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1 resume
      assert_resumed_ticket  ok attempt=1 duration=...
    case_1 done steps=3 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

Additional pretty stdout:

```console
The journey finished. After a graceful Ctrl-C, saved completed steps are reused. After a forceful Ctrl-C, wait_for_resume_signal() restarts with the same saved ticket while load_support_ticket() is reused.
```

### If You Press Ctrl-C Twice

The second Ctrl-C is forceful. It is useful when the active step is blocked and you do not want to wait for it to reach
post-exit.

Expected pretty stdout includes:

```console
      wait_for_resume_signal  start attempt=1
Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.
Ctrl-C received again. Stopping now; this step will restart from saved inputs on resume.
Warning: wait_for_resume_signal interrupted after ... (KeyboardInterrupt)
Interrupted: Journey execution was interrupted before it finished.
Hint: Run the same command again with --state ... to resume from saved progress.
```

On the next run, Journey reuses earlier successful steps and starts the dirty step again with the saved arguments:

```console
Execution
    case_1 resume
      wait_for_resume_signal  start attempt=2
      wait_for_resume_signal  ok attempt=2 duration=...
      assert_resumed_ticket  ok attempt=1 duration=...
    case_1 done steps=3 duration=...
```

## What To Notice

- Retries are explicit. Journey does not silently retry behind your back.
- `retry_from=` is the switch that decides the replay boundary.
- Step anchors define replay boundaries. Retry from a step reruns the anchor step; branch `start_from` resumes from the
  anchor step's completed post-exit state.
- Any value that Journey may need to replay later must be pickle-serializable or rehydratable.
- `--state` keeps successful step bindings so the rerun can skip what already succeeded.
- First Ctrl-C in a CLI `--state` run resumes after the completed step; second Ctrl-C restarts the dirty step from the
  top later. Without `--state`, Ctrl-C cannot resume. Journey never jumps into the middle of the function.

Continue with [04 Browser and Local Integrations](04-browser-and-local-integrations.md) when your steps need to open real pages, receive webhooks, or inspect local files.
