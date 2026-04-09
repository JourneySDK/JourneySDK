# Branching Journey

This is the first stage where one authored journey turns into more than one executable case.

The example creates a checkpoint after shared setup, defines two branch options, and then executes one branch-specific
step in each case.

## What this teaches

- how plain `checkpoint()` creates a named replay anchor
- how inline `journey.branch(...)` conditions create one branch case each
- how `start_from=...` controls replay anchors for branch-specific execution
- how `journey execute --step ...` runs only the path that reaches one target label
- how `journey execute --pause-on-step ...` lets you iterate on one branch step interactively

## Files to read

- `example/branching_journey/branching_journey.py`

## Run it

1. Plan the journey:

```bash
uv run journey plan --file example/branching_journey/branching_journey.py
```

What to expect:

- one discovered journey called `branching_journey`
- two planned cases
- one case ends with `assert_fast_track_path`
- one case ends with `assert_manual_review_path`

2. Execute every case:

```bash
uv run journey execute --file example/branching_journey/branching_journey.py
```

What to expect:

- the live output prints `branch bg_1=...` when the branch decision is reached
- both cases finish successfully
- the shared setup steps appear before the branch-specific step in the first case

3. Execute only the manual-review path:

```bash
uv run journey execute --file example/branching_journey/branching_journey.py --step assert_manual_review_path
```

What to expect:

- only one case is selected
- the case stops at `assert_manual_review_path`
- the final case line includes `replay_anchor=cp_1`

4. Pause on the manual-review path and continue from the prompt:

```bash
uv run journey execute --file example/branching_journey/branching_journey.py --pause-on-step assert_manual_review_path
```

What to expect:

- the manual-review case runs up to `assert_manual_review_path`
- the CLI prints a prompt after that step finishes
- entering `c` completes the case without rerunning the earlier steps

## Why this matters

Branching is where Journey SDK starts saving real time. You author the shared setup once, keep the branch logic in one
place, and let the planner turn that into separate linear cases.

## Next step

Continue with [`retry_journey/README.md`](../retry_journey/README.md) to learn how Journey SDK handles async or delayed
effects.
