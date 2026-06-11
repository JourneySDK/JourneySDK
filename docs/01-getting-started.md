# 01 Getting Started

This chapter is for the first ten minutes with Journey.

The goal is simple: understand what a journey looks like in Python, how to run it, and how to narrow a file down to
one journey when you need script-friendly JSONL output.

## The Mental Model

- Import the Journey primitives you use directly with `from journeysdk import ...`.
- Mark one top-level function with `@journey`.
- Add steps with `step(...)`.
- Pass step results explicitly into later steps.
- Treat every step as an execution boundary. Only explicit replay boundaries store rehydratable state:
  `branch(start_from=...)` and steps with a positive `retry=...`.
- A step is an intentional replay boundary with cost. Each extra step can add state binding, invalidation checks, log
  scopes, and store/restore work. Do not make a step for every click, form fill, poll, helper call, touchpoint wait, or
  assertion.
- Size steps around replayable operations, not fragments. A useful step is a meaningful place to restart from the
  function start, with stable state worth restoring or passing onward.
- If actions must recover together, keep them in the same step or in helpers called by that step.
- Use touchpoints when a step needs to interact with another system, such as a browser, inbox, webhook endpoint, CRM,
  payment provider, or back-office process.
- Use `journey` to compile and run the authored flow as linear executable cases.
- Use selection flags when you want one file, one journey, or one target case.

If you remember only one thing, remember this: Journey does not ask you to stop writing Python. It compiles ordinary
Python step calls into a runnable plan, and long-running work stays manageable because explicit replay boundaries can
restart meaningful procedures instead of tiny do/assert fragments.

A touchpoint is not a special kind of step. It is the external surface a step talks to. For example, a coarse checkout
step might use the browser touchpoint to submit an order, then call email or webhook touchpoint helpers to confirm the
service produced the expected side effects. Split those checks into later steps only when an agent would independently
target, retry, or branch from them.

## Agent Bootstrap

When Codex, Claude Code, Cursor, or another coding agent is about to add or debug journeys, give it one line:

```text
Use Journey SDK for this task: <describe the user flow>. Run journey agent codex first.
```

Replace `codex` with `claude`, `cursor`, or `generic` for another assistant. The assistant should run the guidance
command itself:

```bash
journey agent codex
```

The command is print-only by default. It gives the agent the installed Journey instructions, the targeted verification
loop, and the packaged touchpoint references it should use before inventing browser, Docker, email, webhook, HTTP, or
polling helpers. To make the guidance persistent for future prompts, run `journey agent codex --install` once from the
project root. When debugging a journey, the agent should run the failing journey or the focused `--develop-step` retry
until executable evidence passes. If the agent is unsure which flags to use or how to recover, it should run
`journey --help`, `journey logs --help`, or `journey agent --help` and follow the CLI's `Next commands` block.
When adding a new journey, import checks, lint, and type checks are only setup hygiene. The agent should still run
`journey --file ...`, execute each requested branch target with `--develop-step` or `--step`, and finish with fresh
`--no-state` evidence whenever the app infrastructure is available.
If the app is not running, the agent should follow the repository's documented local startup path before declaring the
run environment-blocked. When inspecting `.env` or credential files, it should report key presence only, never print
secret values, and avoid dumping secret-bearing files wholesale. For sign-in and seed data, it should inspect existing
E2E helpers and request approval before running setup that mutates external services. For branching journeys, an
ambiguous shared setup label should be handled by targeting branch-specific steps, not by disabling branches.

## The Smallest Useful Journey

Read `docs/first_journey/first_journey.py`.

```python
from journeysdk import journey, step


@journey
def first_journey() -> None:
    profile = step(create_customer_profile)
    step(send_welcome_message_and_verify_delivery, profile)
```

That is the whole authored flow. The helper functions in the same file do the real work:

- `create_customer_profile()` returns a customer payload
- `send_welcome_message_and_verify_delivery(profile)` uses that payload and validates delivery

### Run It

```bash
uv run journey --file docs/first_journey/first_journey.py
```

Expected pretty stdout includes:

```console
Plan
  docs/first_journey/first_journey.py:first_journey ...
      create_customer_profile  start executed attempt=1 ...
      create_customer_profile  executed attempt=1 duration=... ...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

The important part is not the formatting. The important part is that Journey routes plan summaries, execution results,
and every step boundary through one logger-owned stdout stream. Use `--output jsonl` for one parseable JSON object per
line, `--log-level off` to suppress all Journey-owned output, or `--log-level debug` for more detail. The default
`pretty` output is intended for human local development.

## One File Can Define More Than One Journey

Read `docs/selection_journeys/selection_journeys.py`.

```python
from journeysdk import journey, step


@journey
def welcome_email_journey() -> None:
    job = step(load_welcome_email_job)
    step(assert_welcome_email_job, job)


@journey
def invoice_reminder_journey() -> None:
    reminder = step(load_invoice_reminder)
    step(assert_invoice_reminder, reminder)
```

This is the first time Journey's CLI selection flags matter:

- `--journey` narrows discovery to one decorated function
- `--output jsonl` switches the CLI into machine-readable JSON Lines output

### Execute One Journey as JSONL

```bash
uv run journey --file docs/selection_journeys/selection_journeys.py --journey invoice_reminder_journey --output jsonl
```

```jsonl
{"time":"...","level":"INFO","component":"cli","event":"plan_start","message":"Plan"}
{"time":"...","level":"INFO","component":"executor","event":"step_success","message":"  step load_invoice_reminder attempt=1 executed duration=...","status":"executed","step":"load_invoice_reminder"}
{"time":"...","level":"INFO","component":"cli","event":"execute_result","message":"execution result","payload":{"journeys":[{"file":".../docs/selection_journeys/selection_journeys.py","journey_name":"invoice_reminder_journey","report":{"journey_id":"invoice_reminder_journey","function_ref":"...","case_reports":[{"case_id":"case_1","completed":true,"stopped_at_label":null,"replay_anchor":null,"records":[{"label":"load_invoice_reminder","status":"executed"},{"label":"assert_invoice_reminder","status":"executed"}]}]}}],"errors":[]}}
```

## What To Notice

- Step outputs stay explicit. The second step receives the first step's return value directly.
- Assertions usually live inside the user-flow step that owns the outcome instead of becoming tiny standalone steps.
- `--journey` is the easiest way to work in a file that holds several flows.
- `--output jsonl` is for tooling and CI. The default pretty output is better for humans during local development.

Continue with [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md) when you want one authored flow to cover multiple paths.
