# Journey SDK Assistant Guide

Use this guide when an AI coding assistant needs to create, edit, run, or debug Journey SDK journeys.

## Core Loop

1. Inspect existing journeys and docs before editing.
2. Update application or journey code.
3. Run the smallest useful Journey verification:

```bash
journey --file path/to/journey.py --develop-step target_label
```

4. Rerun the same command after each edit to retry the paused step.
5. Broaden to a targeted run or full file before finishing:

```bash
journey --file path/to/journey.py --step target_label
journey --file path/to/journey.py
```

6. Use `--output jsonl` for machine-readable results.

## Authoring Guidance

- Use ordinary Python functions for steps.
- Decorate entrypoints with `@journey`.
- Use `step(...)` for durable procedures and `branch(...)` for user-path choices.
- Use `branch(start_from=...)` and positive retries when replay boundaries matter.
- Keep values crossing replay boundaries serializable or rehydratable.
- Avoid side effects while compiling/planning the journey.
- Avoid `--interactive` in automated assistant loops.
