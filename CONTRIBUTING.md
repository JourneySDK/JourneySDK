# Contributing

Run contributor commands from the Journey SDK repository root: the directory that contains this
`CONTRIBUTING.md` and `pyproject.toml`.

## Local Setup

Install the dev environment and run the test suite:

```bash
uv sync --extra dev
uv run pytest
```

## Editable Installs

Install this checkout in editable mode in the current project environment:

```bash
uv pip install -e .
uv run journey --help
```

Add the local checkout to another `uv` project in editable mode.
Run this command from that other project's root, not from the Journey SDK repository:

```bash
uv add --editable /path/to/journey-sdk
```

Install the CLI from a local checkout in editable mode:

```bash
uv tool install --editable /path/to/journey-sdk
journey --help
```

If your shell cannot find `journey` yet, run `uv tool update-shell` or open a new shell session.

## Local Package Smoke Test

From the Journey SDK repository root, build the package, install the built wheel, and verify the
`journey` CLI:

```bash
./scripts/smoke_test_package.sh
```

That script:

- runs `uv build`
- installs the built wheel into a clean virtual environment and verifies `import journeysdk`
- installs the built wheel as a temporary `uv` tool and verifies `journey --help`
- runs a one-off `uv tool run --from <wheel> journey --help`

## Public Typing

Public SDK and official tool APIs should avoid `Any`. Prefer named aliases or protocols for
callable roles, `TypedDict` for dictionary payloads, and `object` when callers must narrow an
unknown value themselves. The public typing contract is covered by
`tests/test_public_typing_contract.py`.

## Step-Exit Tool Lifecycle

Official tools that open live resources inside a step should register cleanup
with `journeysdk.session.register_step_exit_callback`. This hook is only valid
while a step function is executing. Do not call lifecycle-aware tools during
planning, module import, or between `step(...)` calls.

Use this pattern when a tool owns a resource that should not outlive the step
attempt:

```python
from journeysdk.session import register_step_exit_callback


def open_resource():
    resource = acquire_resource()
    closed = False

    def cleanup():
        nonlocal closed
        if closed:
            return
        closed = True
        resource.close()

    register_step_exit_callback(cleanup)
    return resource
```

Callbacks run in LIFO order when the step exits on success, failure, retry,
develop-step pause, or interruption. Keep callbacks idempotent, and close only
resources owned by that tool call. If a step returns a value that must survive
retries, `--state`, or checkpoint replay, the returned value must implement the
Journey rehydration protocol documented in the README; do not rely on pickling
live resources.

Tests for lifecycle-aware tools should cover successful cleanup, cleanup after
failure, cleanup on retry or interruption, the outside-step guard, and
rehydration of returned values.

## Manual Release Flow

1. Update the package version in `pyproject.toml`.
2. From the Journey SDK repository root, run `uv run pytest`.
3. From the Journey SDK repository root, run `./scripts/smoke_test_package.sh`.
4. Build the release artifacts with `uv build`.
5. Publish them with `uv publish`.
