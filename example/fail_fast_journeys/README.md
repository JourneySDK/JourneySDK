# Fail-Fast Journeys

This stage is intentionally different from the others: one journey is supposed to fail.

That makes it a safe place to learn what `--fail-fast` changes.

## What this teaches

- what the default behavior looks like when one discovered journey fails
- how `--fail-fast` stops before later journeys are processed
- when fail-fast is useful during local debugging or CI

## Files to read

- `example/fail_fast_journeys/fail_fast_journeys.py`

## Run it

1. Execute the file without `--fail-fast`:

```bash
uv run journey execute --file example/fail_fast_journeys/fail_fast_journeys.py
```

What to expect:

- `broken_demo_journey` fails with `expected tutorial failure`
- `good_demo_journey` still runs after that failure
- the summary reports one failed journey overall

2. Execute the same file with `--fail-fast`:

```bash
uv run journey execute --file example/fail_fast_journeys/fail_fast_journeys.py --fail-fast
```

What to expect:

- `broken_demo_journey` still fails
- `good_demo_journey` does not run
- the summary stops after the first failure

## Why this matters

The default mode is helpful when you want a full picture of what passed and what failed. `--fail-fast` is better when
you want the first failure to stop the run immediately so you can debug it right away.

The same flag also exists on `journey plan` if you want discovery or planning errors to stop the command early.

## Next step

Go back to [`example/README.md`](../README.md) whenever you want to jump to a different stage.
