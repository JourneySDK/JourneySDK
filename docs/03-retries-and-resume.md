# 03 Retries and Resume

Not every failure means the journey is wrong. Sometimes the system under test is still catching up. Sometimes a long
run needs to stop cleanly and resume from saved state.

This chapter covers three patterns:

- how Journey saves, reuses, and invalidates state
- retries when a step needs to poll or replay from an earlier step boundary
- default state when a run is interrupted and you want to resume from the nearest explicit replay boundary later

## How Journey State Works

Journey state is a development tool for fast retry, replay, and interruption recovery. It is not a replacement for a
fresh full-path run before review, merge, or release.

The model is:

1. Journey saves progress at durable boundaries: targeted steps, retry boundaries, branch anchors, and interrupted
   step boundaries.
2. The state file stores the saved values plus a validity envelope for the journey plan, step source, runtime, and
   workspace inputs.
3. On the next run, Journey compares that envelope with the current journey.
4. If the state is still valid, Journey replays from the nearest safe boundary instead of repeating earlier setup.
5. If the state is stale, Journey reports the invalidation and reruns from the nearest safe boundary instead of trusting
   the old checkpoint.

State output uses three words:

| Status | Meaning |
| --- | --- |
| `fresh` | No saved state was reused. |
| `replayed` | Journey reused a valid saved boundary. |
| `invalidated` | Journey detected stale state and reran from a safe boundary. |

Individual report records use a separate `status` field:

| Record status | Meaning |
| --- | --- |
| `executed` | The node completed by running in this invocation. |
| `replayed` | The node completed by restoring saved state or a branch anchor. |
| `failed` | The node ran and failed. |

Pretty output shows the decision inline:

```console
State: reused boundary case_1
State: invalidated boundary case_1; Reason: plan_shape; Action: rerunning from step index 1
```

JSON Lines output gives agents the same decision as a structured event:

```json
{"event":"state_validity","status":"invalidated","boundary_id":"case_1","reason":"plan_shape","action":"rerunning from step index 1"}
```

Use the modes this way:

| Command shape | Use it for | State behavior |
| --- | --- | --- |
| `journey dev <label>` | Fast edit loop for one target step. | Reads and writes state. |
| `journey verify --step <label>` | Fresh verification of the case that reaches one step. | Fresh by default; add `--reuse-state` for stateful diagnostics. |
| `journey verify --fresh` | Fresh-path confidence before review, merge, or release. | Does not read or write reusable state. |
| `journey verify --reuse-state --no-state-update` | Inspect or replay existing state without changing it. | Reads state but leaves the file unchanged. |
| `--output jsonl` | Agent-readable execution evidence. | Emits `state_validity` events and per-record `status`. |

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

Those three journeys represent the three most common retry boundaries. Retry is disabled by default; it is active only
when `retry` is explicitly greater than `0`.

- retry only the failing step
- retry from an earlier step result
- retry from an earlier setup step and replay everything from that step

Step-anchor replay follows the same rule as persistent state: Journey stores only the replay closure needed for the
explicit boundary, then reruns from the anchor step. If one of those saved values needs custom side effects, put that
logic on a module-level value type with `__store__` / `__restore__` as described in the README's Journey Rehydration
Protocol section.

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

Journey stores step results before the replay boundary only when they must be skipped or restored later. Values at or
after the replay boundary are rerun instead of restored.

### Retry the Current Step

```bash
uv run journey verify --fresh --file docs/retry_journey/retry_journey.py --journey retry_current_step_journey
```

```console
Plan
  docs/retry_journey/retry_journey.py:retry_current_step_journey ...
    case_1  labels: prepare_same_step_demo, wait_for_same_step
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      prepare_same_step_demo  start executed attempt=1
      prepare_same_step_demo  executed attempt=1 duration=...
      wait_for_same_step  start executed attempt=1
Warning: wait_for_same_step retry after ... (RuntimeError: still waiting for the same-step retry demo)
      wait_for_same_step  start executed attempt=2
      wait_for_same_step  executed attempt=2 duration=...
    case_1 done steps=2 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

### Retry from an Earlier Step Result

```bash
uv run journey verify --fresh --file docs/retry_journey/retry_journey.py --journey retry_from_step_result_journey
```

```console
Plan
  docs/retry_journey/retry_journey.py:retry_from_step_result_journey ...
    case_1  labels: issue_report_request, wait_for_report, assert_report_ready
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      issue_report_request  start executed attempt=1
      issue_report_request  executed attempt=1 duration=...
      wait_for_report  start executed attempt=1
Warning: wait_for_report retry after ... (RuntimeError: report not ready yet)
      issue_report_request  start executed attempt=2
      issue_report_request  executed attempt=2 duration=...
      wait_for_report  start executed attempt=2
      wait_for_report  executed attempt=2 duration=...
      assert_report_ready  executed attempt=1 duration=...
    case_1 done steps=3 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

### Retry from an Earlier Setup Step

```bash
uv run journey verify --fresh --file docs/retry_journey/retry_journey.py --journey retry_from_step_anchor_journey
```

```console
Plan
  docs/retry_journey/retry_journey.py:retry_from_step_anchor_journey ...
    case_1  labels: load_status_request, refresh_status_cache, wait_for_anchor_retry, assert_anchor_retry_ready
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      load_status_request  executed attempt=1 duration=...
      refresh_status_cache  executed attempt=1 duration=...
Warning: wait_for_anchor_retry retry after ... (RuntimeError: step-anchor retry demo is still waiting)
      refresh_status_cache  start executed attempt=2
      refresh_status_cache  executed attempt=2 duration=...
      wait_for_anchor_retry  executed attempt=2 duration=...
      assert_anchor_retry_ready  executed attempt=1 duration=...
    case_1 done steps=4 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

## Resume an Interrupted Run

Read `docs/resume_journey/resume_journey.py`.

```python
from journeysdk import journey, step


def wait_for_resume_signal(
    ticket: dict[str, str],
    pause_seconds: float,
) -> dict[str, str]:
    _tutorial_note(
        "wait_for_resume_signal() is starting with ticket "
        f"{ticket['ticket_id']}. This journey has no explicit replay boundary, so "
        "a forceful interruption restarts from the case beginning."
    )
    _tutorial_note(
        f"Press Ctrl-C once during the next {pause_seconds:.1f} seconds to stop "
        "gracefully after this step reaches post-exit. Press Ctrl-C a second time "
        "to stop now; Journey will rerun from the nearest explicit replay boundary."
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
persistent state, first Ctrl-C is graceful: Journey lets the active step finish, exit returned handles, and stop at
post-exit. Press Ctrl-C a second time to stop now; Journey interrupts the dirty step, and on the next run restarts from
the nearest explicit replay boundary. With no `branch(replay_from=...)` or positive `retry=...` boundary, that means the
case starts from the beginning. With `journey verify --fresh`, Ctrl-C stops immediately and cannot resume.

### Reset the Demo State

```bash
uv run python -c "from docs.resume_journey import reset_demo_state; reset_demo_state()"
```

### First Run: Graceful Ctrl-C

```bash
uv run journey verify --reuse-state --file docs/resume_journey/resume_journey.py
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
      load_support_ticket  executed attempt=1 duration=...
      wait_for_resume_signal  start executed attempt=1
Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.
      wait_for_resume_signal  executed attempt=1 duration=...
Interrupted: Journey execution was interrupted before it finished.
Hint: Run the same command again to resume from saved progress.
```

Additional pretty stdout:

```console
wait_for_resume_signal() is starting with saved ticket ticket-001. ...
Press Ctrl-C once during the next 2.0 seconds to stop gracefully after this step reaches post-exit. ...
```

### Second Run After Graceful Ctrl-C: Restart From the Case Boundary

```bash
uv run journey verify --reuse-state --file docs/resume_journey/resume_journey.py
```

Expected pretty stdout:

```console
Plan
  docs/resume_journey/resume_journey.py:resume_journey ...
    case_1  labels: load_support_ticket, wait_for_resume_signal, assert_resumed_ticket
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1 resume
      load_support_ticket  executed attempt=2 duration=...
      wait_for_resume_signal  executed attempt=2 duration=...
      assert_resumed_ticket  executed attempt=1 duration=...
    case_1 done steps=3 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

Additional pretty stdout:

```console
The journey finished. This demo has no explicit replay boundary, so an interrupted run restarts the case from the
beginning. Add `branch(replay_from=...)` or a positive `retry=...` when a step value should be saved and reused.
```

### If You Press Ctrl-C Twice

The second Ctrl-C is forceful. It is useful when the active step is blocked and you do not want to wait for it to reach
post-exit.

Expected pretty stdout includes:

```console
      wait_for_resume_signal  start executed attempt=1
Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.
Ctrl-C received again. Stopping now; this step will restart from the nearest replay boundary on resume.
Warning: wait_for_resume_signal interrupted after ... (KeyboardInterrupt)
Interrupted: Journey execution was interrupted before it finished.
Hint: Run the same command again to resume from saved progress.
```

On the next run, this demo restarts from the case beginning because there is no explicit replay boundary:

```console
Execution
    case_1 resume
      load_support_ticket  start executed attempt=2
      load_support_ticket  executed attempt=2 duration=...
      wait_for_resume_signal  start executed attempt=2
      wait_for_resume_signal  executed attempt=2 duration=...
      assert_resumed_ticket  executed attempt=1 duration=...
    case_1 done steps=3 duration=...
```

## What To Notice

- Retries are explicit. Journey does not silently retry behind your back.
- `retry=0` is the default. `retry_from=` or `retry_delay=` without a positive `retry` does not create a replay
  boundary.
- Step anchors define replay boundaries only when used by `branch(replay_from=...)` or a step with positive `retry`.
  Retry from a step reruns the anchor step; branch `replay_from` resumes from the anchor step's completed post-exit
  state.
- Keep those boundaries coarse. A step that only clicks one button or checks one line of text usually adds storage,
  restore, and recording overhead without making retries or later branches faster.
- Any value that Journey may need to skip or restore later must be pickle-serializable or rehydratable.
- Persistent state keeps only the saved bindings needed by explicit replay boundaries. Ordinary completed steps are
  rerun from the nearest boundary instead of restored.
- First Ctrl-C in a default CLI run stops after the completed step; second Ctrl-C interrupts the dirty step. The next
  run restarts from the nearest explicit replay boundary, or from the case beginning when there is no such boundary.
  With `journey verify --fresh`, Ctrl-C cannot resume. Journey never jumps into the middle of the function.

Continue with [04 Browser and Local Integrations](04-browser-and-local-integrations.md) when your steps need to open real pages, receive webhooks, or inspect local files.
