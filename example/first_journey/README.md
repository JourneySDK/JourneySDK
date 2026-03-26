# First Journey

Start here if `journey` is new to you.

This stage shows the smallest useful journey: one decorated function, three steps, and one value passed from step to
step.

## What this teaches

- how to mark a module-level function with `@journey.journey`
- how `journey.step(...)` adds steps to the journey
- how one step's return value becomes the input to the next step
- how `journey plan --file ...` and `journey execute --file ...` work

## Files to read

- `example/first_journey/first_journey.py`

## Run it

1. Plan the journey:

```bash
uv run journey plan --file example/first_journey/first_journey.py
```

What to expect:

- one discovered journey called `first_journey`
- one planned case
- step labels `create_customer_profile`, `send_welcome_message`, and `assert_welcome_message_sent`

2. Execute the journey:

```bash
uv run journey execute --file example/first_journey/first_journey.py
```

What to expect:

- one case starts and finishes successfully
- the steps run in the same order they appear in Python
- the final summary says one journey executed and zero failed

## Why this matters

This is the core `journey` authoring model. You write plain Python functions, call them through `journey.step(...)`,
and pass explicit values from one step to the next. The CLI then plans or executes that flow for you.

## Next step

Continue with [`selection_journeys/README.md`](../selection_journeys/README.md) to learn how discovery works when one
file contains more than one journey.
