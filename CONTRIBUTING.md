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
- installs the built wheel as a temporary `uv` tool and verifies `journey --help` plus
  `journey --agent-instructions codex`
- runs a one-off `uv tool run --from <wheel> journey --help` plus
  `journey --agent-instructions codex`

## Public Typing

Public SDK and official touchpoint APIs should avoid `Any`. Prefer named aliases or protocols for
callable roles, `TypedDict` for dictionary payloads, and `object` when callers must narrow an
unknown value themselves. The public typing contract is covered by
`tests/test_public_typing_contract.py`.

## Touchpoint Lifecycle Protocols

Official touchpoints that open live resources should follow the Journey rehydration
protocol plus the step and case lifecycle documented in the README. Use
`__exit__` for resources that must close after one step, and `__case_exit__`
for resources that must stay live across multiple steps in one case.
Lifecycle-aware touchpoint tests should cover successful cleanup of returned
handles, nested returned handles, cleanup failure, the outside-step guard for
resource helpers, explicit cleanup for non-returned live resources, case-exit
cleanup where applicable, and rehydration of returned values.

## Prompt Memory Pattern

Official touchpoints that add an AI-driven `prompt(...)` method should use the shared helpers in
`journeysdk._prompt_memory` instead of inventing their own storage. The method should accept a `memory` option where
omitting `memory` uses generated callsite memory, a string overrides the memory name, and `memory=None` disables memory
for that prompt. Respect `--no-memory` / `execute(..., no_memory=True)` and
`--no-memory-update` / `execute(..., no_memory_update=True)`, and store only compact replay code and checks from
successful runs. Do not persist screenshots, rendered HTML, full model prompts, or other raw observations that may
contain secrets.

Memory files are named `.journey/[memory].memory.md` and live under the journey source file's `.journey` folder. Planning must be able to validate
explicit and generated memory names and reject duplicates before execution.

## Planning Hooks And Core Dependencies

Keep the component dependency direction described in `AGENTS.md`: high-level orchestration modules stay generic, and
feature/helper modules plug in through narrow hooks. If a feature needs compile-time validation when a step is planned,
put that validation in the helper module owned by the feature and register it as a plain step-planning hook.

## Logger API

Journey-owned output must go through `journeysdk.logger`; do not use direct `print(...)` calls for SDK, CLI, touchpoint, or
tutorial diagnostics. The logger owns levels, stdout routing, redaction, `pretty` / `structured` / `jsonl` formatting,
and `--log-level off` suppression.

Keep dependency direction one-way: SDK modules may depend on `journeysdk.logger`, but `logger.py` must not know about
CLI, executor, Playwright, Docker, email, webhook, or cloud event names. Each module that emits an event owns its own
human-facing `pretty=` text.

```python
from journeysdk.logger import get_logger, pretty_line, pretty_row

_LOGGER = get_logger("my-touchpoint")

_LOGGER.info(
    "resource_start",
    "starting resource",
    pretty=pretty_row("My touchpoint", "starting resource", indent=8, label_width=27, style="touchpoint"),
    resource_id=resource_id,
)
```

Use `message` and keyword fields for machine-readable data. `--output structured` and `--output jsonl` include the
event name, message, and fields, but never the `pretty=` value. Use `pretty=` only for human console rendering:

- `pretty=None` lets the logger render a generic line from `message` plus fields.
- `pretty=False` suppresses that event in `pretty` mode while preserving `structured` and `jsonl`.
- `pretty="text"` emits one human line.
- `pretty_line(...)` and `pretty_row(...)` emit styled, aligned human lines; pass a list for multi-line output.

ANSI color is applied only for TTY streams and only from explicit generic styles such as `heading`, `touchpoint`, `accent`,
`code`, `success`, `warning`, `error`, and `muted`. Captured output and CI stay plain ASCII. Sensitive fields and
password-like text are redacted in all formats, but callers should still avoid putting secrets in prose.

## Manual Release Flow

1. Update the package version in `pyproject.toml`.
2. From the Journey SDK repository root, run `uv run pytest`.
3. From the Journey SDK repository root, run `./scripts/smoke_test_package.sh`.
4. Build the release artifacts with `uv build`.
5. Publish them with `uv publish`.
