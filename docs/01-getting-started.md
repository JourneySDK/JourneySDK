# 01 Getting Started

This chapter is for the first ten minutes with Journey.

The goal is simple: understand what a journey looks like in Python, how to run it, and how to narrow a file down to
one journey when you need script-friendly JSONL output.

## The Mental Model

- Import the Journey primitives you use directly with `from journeysdk import ...`.
- Mark one top-level function with `@journey`.
- Add steps with `step(...)`.
- Pass step results explicitly into later steps.
- Treat every step as a boundary where Journey can save progress, stop, retry, or resume.
- Use `journey` to compile and run the authored flow as linear executable cases.
- Use selection flags when you want one file, one journey, or one target case.

If you remember only one thing, remember this: Journey does not ask you to stop writing Python. It compiles ordinary
Python step calls into a runnable plan, and long-running work stays manageable because resume and replay happen at step
boundaries.

## The Smallest Useful Journey

Read `docs/first_journey/first_journey.py`.

```python
from journeysdk import journey, step


@journey
def first_journey() -> None:
    profile = step(create_customer_profile)
    message = step(send_welcome_message, profile)
    step(assert_welcome_message_sent, message)
```

That is the whole authored flow. The helper functions in the same file do the real work:

- `create_customer_profile()` returns a customer payload
- `send_welcome_message(profile)` uses that payload
- `assert_welcome_message_sent(message)` validates the result

### Run It

```bash
uv run journey --file docs/first_journey/first_journey.py
```

Expected pretty stdout includes:

```console
Plan
  docs/first_journey/first_journey.py:first_journey ...
      create_customer_profile  start attempt=1 ...
      create_customer_profile  ok attempt=1 duration=... ...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

The important part is not the formatting. The important part is that Journey routes plan summaries, execution results,
and every step boundary through one logger-owned stdout stream. Use `--output structured` for logfmt-style fields,
`--output jsonl` for one parseable JSON object per line, `--log-level off` to suppress all Journey-owned output, or
`--log-level debug` for more detail.

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
{"time":"...","level":"INFO","component":"executor","event":"step_success","message":"  step load_invoice_reminder attempt=1 ok duration=...","step":"load_invoice_reminder"}
{"time":"...","level":"INFO","component":"cli","event":"execute_result","message":"execution result","payload":{"journeys":[{"file":".../docs/selection_journeys/selection_journeys.py","journey_name":"invoice_reminder_journey","report":{"journey_id":"invoice_reminder_journey","function_ref":"...","case_reports":[{"case_id":"case_1","completed":true,"stopped_at_label":null,"replay_anchor":null,"records":[{"label":"load_invoice_reminder","ok":true},{"label":"assert_invoice_reminder","ok":true}]}]}}],"errors":[]}}
```

## What To Notice

- Step outputs stay explicit. The second step receives the first step's return value directly.
- `--journey` is the easiest way to work in a file that holds several flows.
- `--output jsonl` is for tooling and CI. The default pretty output is better for humans during local development.

Continue with [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md) when you want one authored flow to cover multiple paths.
