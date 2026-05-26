# Journey SDK Agent Instructions

Use these instructions when developing, updating, or verifying Journey SDK journeys in this project.

## When To Use Journey

- Use Journey SDK for long, branching, async, or cross-system user flows that need verification beyond unit tests.
- Author journeys as ordinary Python files with `@journey`, `step(...)`, and `branch(...)`.
- Keep step labels stable because CLI targeting and state files use those labels.

## Agent Workflow

1. Inspect nearby journey files, project docs, and application code before editing.
2. Make the smallest code or journey change that can verify the requested user behavior.
3. Run the narrowest useful Journey command from the project that owns the journey:

```bash
journey --file path/to/journey.py --develop-step target_label
```

4. Rerun the same `--develop-step` command after edits to retry the paused step with Journey's default persistent state.
5. Broaden verification before finishing:

```bash
journey --file path/to/journey.py --step target_label
journey --file path/to/journey.py
```

6. Use JSON Lines output when another tool or script needs to parse results:

```bash
journey --file path/to/journey.py --step target_label --output jsonl
```

## Journey Rules

- Prefer explicit top-level step functions over lambdas or nested closures.
- Pass concrete dependencies and previous step results as explicit arguments.
- Do not pass `None` or empty placeholders into constructors to satisfy signatures.
- Keep planning side-effect free; acquire browsers, cloud resources, services, and handles inside step execution.
- Use `branch(start_from=step_result)` or positive step retries for explicit replay boundaries.
- Keep values that cross replay boundaries pickle-serializable or implement Journey's rehydration protocol.
- Avoid `--interactive` for non-human agent runs; noninteractive `--develop-step` is designed for coding agents.
- Use `--no-memory` only when AI prompt memory must be ignored for a run.
- Use `--no-state` only for one-off runs that should not resume.

## Verification Standard

Before wrapping up, report the exact Journey command you ran, the targeted step or full journey that passed, and any broader tests that still need to run.
