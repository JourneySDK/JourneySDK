# Rehydration Journey

This is the advanced replay stage.

The example is intentionally small so you can focus on one behavior: a later case can start from an earlier checkpoint
and reuse the saved state from that point instead of rerunning the shared setup.

## What this teaches

- what `branch(start_from=...)` means during full execution
- why checkpoint-started branches can reuse earlier step results
- how targeted execution still reports the replay anchor for the selected path

## Files to read

- `example/rehydration_journey/rehydration_journey.py`

## Run it

1. Plan the journey:

```bash
uv run journey plan --file example/rehydration_journey/rehydration_journey.py
```

What to expect:

- two planned cases
- both cases contain `prepare_context` and `shared_after_checkpoint`
- one case ends with `assert_branch_a`
- one case ends with `assert_branch_b`

2. Execute the whole journey:

```bash
uv run journey execute --file example/rehydration_journey/rehydration_journey.py
```

What to expect:

- case 1 runs `prepare_context`, `shared_after_checkpoint`, and `assert_branch_a`
- case 2 jumps straight to the branch marker and `assert_branch_b`
- `prepare_context` and `shared_after_checkpoint` do not rerun in case 2

3. Execute only the second branch:

```bash
uv run journey execute --file example/rehydration_journey/rehydration_journey.py --step assert_branch_b
```

What to expect:

- one selected case
- the case stops at `assert_branch_b`
- the final case line includes `replay_anchor=cp_1`

## Why this matters

Large journeys often have expensive shared setup. Checkpoint rehydration is how `journey` avoids repeating that setup
for every later branch when the data can be restored safely from saved step values.

## Next step

Continue with [`fail_fast_journeys/README.md`](../fail_fast_journeys/README.md) for one final operational CLI feature:
`--fail-fast`.
