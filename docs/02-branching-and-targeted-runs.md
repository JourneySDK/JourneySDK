# 02 Branching And Step Loops

Journey's core payoff is that one Python user journey can become a replayable agent loop:

- develop a single meaningful step with `journey dev`
- verify one branch target with `journey verify --step`
- verify the whole journey and its branches with `journey verify`

## Choose Replayable Step Boundaries

Each `step(...)` should encapsulate one operation an agent would rerun while coding. Good examples are
`clear_basket_and_add_items`, `submit_order_and_verify_confirmation`, `receive_confirmation_email`, and
`complete_checkout_and_verify_registration_effects`.

A step is a boundary Journey can target, retry, store, log, invalidate, and replay from. It should earn that cost. Do
not wrap every click, form fill, poll, wait, touchpoint call, or assertion as its own step. Keep actions together when
they recover together, and put helper calls inside the step that owns the user-facing outcome.

Step function names become CLI labels used by `journey dev`, `journey verify --step`, state files, retries, evidence
filters, and branch replay. Prefer explicit top-level functions and stable user-flow names.

## Branch Once, Reuse Shared Setup

Read `docs/branching_journey/branching_journey.py`.

```python
from journeysdk import branch, journey, step


@journey
def branching_journey() -> None:
    signup_request = step(load_signup_request)
    classified = step(classify_signup_request, signup_request)

    if branch():
        step(approve_fast_track_signup, classified)
    elif branch(replay_from=classified):
        step(queue_manual_review_signup, classified)
```

Why this shape matters:

- `load_signup_request` and `classify_signup_request` are shared setup.
- `branch(replay_from=classified)` marks `classified` as the replay anchor for later branch cases.
- each `branch(...)` arm becomes its own executable case.

Values crossing replay boundaries must be pickle-serializable or implement Journey's rehydration protocol:

```python
class BrowserSession:
    def __store__(self, context):
        ...

    @classmethod
    def __restore__(cls, payload, context):
        ...


session = step(open_browser_session)
if branch(replay_from=session):
    step(complete_branch_from_session, session)
```

Touchpoint-specific rehydration behavior is documented in the packaged references printed by
`journey touchpoints <name>`.

## Develop One Step While Coding

Use `journey dev` when you are actively editing one step and want Journey to stop after that target step:

```bash
uv run journey dev queue_manual_review_signup --file docs/branching_journey/branching_journey.py
```

```console
Plan
  docs/branching_journey/branching_journey.py:branching_journey ...
    case_1  labels: load_signup_request, classify_signup_request, approve_fast_track_signup; branches: {bg_1=branch_1}
    case_2  labels: load_signup_request, classify_signup_request, queue_manual_review_signup; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
Dev stopped after step queue_manual_review_signup attempt=1 executed.
  Summary: dev queue_manual_review_signup stopped after target, 0 failed
```

Rerun the same command after each edit. Journey keeps its state by default, reloads the selected journey file, and
retries from the nearest explicit replay boundary or from the case beginning when no boundary exists.

When a command fails, copy the CLI's `Retry failed step: ...` command as the focused dev. Inspect `What happened`,
`Try this`, and `Next commands` before editing.

## Verify One Branch Target

Use `journey verify --step` when you want the case that reaches one label:

```bash
uv run journey verify --step queue_manual_review_signup --file docs/branching_journey/branching_journey.py
```

```console
Plan
  docs/branching_journey/branching_journey.py:branching_journey ...
    case_1  labels: load_signup_request, classify_signup_request, approve_fast_track_signup; branches: {bg_1=branch_1}
    case_2  labels: load_signup_request, classify_signup_request, queue_manual_review_signup; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
    case_2  branches={bg_1=branch_2}
      load_signup_request  executed attempt=1 duration=...
      classify_signup_request  executed attempt=1 duration=...
      branch bg_1  branch_2
      queue_manual_review_signup  executed attempt=1 duration=...
    case_2 done steps=3 duration=... stopped_at=queue_manual_review_signup replay_anchor=classify_signup_request
  Summary: 1 journey executed, 1 case executed, 0 failed
```

The reported `replay_anchor` names the branch step anchor. A fresh target verification still executes from the selected
case's required beginning unless existing state or retry behavior causes replay.

## Broaden Before Finishing

For coding agents, the verification ladder is:

```bash
journey dev <target_step> --file journeys/<feature>_journey.py
journey evidence --step <target_step>
journey verify --step <target_step> --file journeys/<feature>_journey.py --fresh
journey verify --file journeys/<feature>_journey.py --fresh
```

Use `--output jsonl` when another tool needs structured results:

```bash
journey verify --step <target_step> --file journeys/<feature>_journey.py --output jsonl
```

In JSONL output, read `state_validity` events and each report record's `status` (`executed`, `replayed`, or `failed`)
before relying on a run as evidence. Treat replayed state as development-loop evidence, not final confidence. Use
`--fresh` for final target-step or full-journey verification whenever feasible.

## Rehydrate Later Cases From A Step Anchor

Read `docs/rehydration_journey/rehydration_journey.py`.

```python
from journeysdk import branch, journey, step


@journey
def rehydration_journey() -> None:
    payload = next_external_payload()
    context = step(prepare_context, payload)

    shared = step(shared_after_anchor, context)

    if branch(replay_from=context):
        step(complete_branch_a_from_anchor, shared)
    elif branch(replay_from=context):
        step(complete_branch_b_from_anchor, shared)
```

In a full multi-case run, later branches can restart from the saved `context` anchor instead of rerunning earlier shared
setup. If the anchor value implements `__store__` / `__restore__`, Journey restores that external state only when a
later branch actually starts from the anchor's post-exit boundary.

Target the second branch:

```bash
uv run journey verify --step complete_branch_b_from_anchor --file docs/rehydration_journey/rehydration_journey.py
```

## What To Notice

- Authoring stays sequential, even when execution becomes multi-case.
- `branch(replay_from=...)` points to an earlier step result, and later branch cases can resume from that step's
  post-exit boundary.
- Coarse steps make those boundaries useful. Tiny click/assertion steps add state and evidence overhead without giving
  agents a better replay point.
- External replay behavior lives on values themselves through `__store__` / `__restore__`, and those hooks run only
  when an explicit replay boundary needs that value.
- `journey dev` is for rapid edit loops. `journey verify --step` and `journey verify` are for broader evidence.

Continue with [03 Retries and Resume](03-retries-and-resume.md) when the path is linear but the world around it is not ready yet.
