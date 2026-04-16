# 01 Getting Started

This chapter is for the first ten minutes with Journey.

The goal is simple: understand what a journey looks like in Python, how to run it, and how to narrow a file down to one journey when you need script-friendly JSON output.

## The Mental Model

- Import the Journey primitives you use directly with `from journeysdk import ...`.
- Mark one top-level function with `@journey`.
- Add steps with `step(...)`.
- Pass step results explicitly into later steps.
- Use `journey` to compile and run the authored flow as linear executable cases.
- Use selection flags when you want one file, one journey, or one target path.

If you remember only one thing, remember this: Journey does not ask you to stop writing Python. It compiles ordinary Python step calls into a runnable plan.

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

```console
Journey docs/first_journey/first_journey.py:first_journey
journey_id=first_journey function_ref=...
- case_1 start branches={}
  step create_customer_profile attempt=1 start
  step create_customer_profile attempt=1 ok duration=...
  step send_welcome_message attempt=1 start
  step send_welcome_message attempt=1 ok duration=...
  step assert_welcome_message_sent attempt=1 start
  step assert_welcome_message_sent attempt=1 ok duration=...
- case_1 ok steps=3 duration=...
Summary: 1 journey executed, 1 case executed, 0 failed
```

The important part is not the formatting. The important part is that Journey shows every step boundary, every attempt, and the final summary without hiding what ran.

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
- `--json` switches the CLI into machine-readable output

### Execute One Journey as JSON

```bash
uv run journey --file docs/selection_journeys/selection_journeys.py --journey invoice_reminder_journey --json
```

```json
{
  "journeys": [
    {
      "file": ".../docs/selection_journeys/selection_journeys.py",
      "journey_name": "invoice_reminder_journey",
      "report": {
        "journey_id": "invoice_reminder_journey",
        "function_ref": "...",
        "case_reports": [
          {
            "case_id": "case_1",
            "completed": true,
            "stopped_at_label": null,
            "replay_anchor": null,
            "records": [
              {
                "label": "load_invoice_reminder",
                "ok": true,
                "result": {
                  "reminder_id": "invoice-001",
                  "channel": "email"
                }
              },
              {
                "label": "assert_invoice_reminder",
                "ok": true,
                "result": true
              }
            ]
          }
        ]
      }
    }
  ],
  "errors": []
}
```

## What To Notice

- Step outputs stay explicit. The second step receives the first step's return value directly.
- `--journey` is the easiest way to work in a file that holds several flows.
- `--json` is for tooling and CI. The default text output is better for humans during local development.

Continue with [02 Branching and Targeted Runs](02-branching-and-targeted-runs.md) when you want one authored flow to cover multiple paths.
