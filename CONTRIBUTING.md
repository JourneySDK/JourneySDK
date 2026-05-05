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

## Tool Lifecycle Protocols

Official tools that open live resources should follow the Journey rehydration
protocol and step lifecycle documented in the README. Lifecycle-aware
tool tests should cover successful cleanup of returned handles, nested returned
handles, cleanup failure, the outside-step guard for resource helpers, explicit
cleanup for non-returned live resources, and rehydration of returned values.

## Prompt Memory Pattern

Official tools that add an AI-driven `prompt(...)` method should use the shared helpers in
`journeysdk._prompt_memory` instead of inventing their own storage. The method should accept
`memory: str | None = None`, respect `--no-memory` / `execute(..., no_memory=True)` and
`--no-memory-update` / `execute(..., no_memory_update=True)`, and store only compact summaries from successful runs.
Do not persist screenshots, rendered HTML, full model prompts, or other raw observations that may contain secrets.

Memory files are named `[memory].memory.json` and live beside the journey source. Planning must be able to validate
literal memory names and reject duplicates before execution.

## Planning Hooks And Core Dependencies

Keep the component dependency direction described in `AGENTS.md`: high-level orchestration modules stay generic, and
feature/helper modules plug in through narrow hooks. If a feature needs compile-time validation when a step is planned,
put that validation in the helper module owned by the feature and register it as a plain step-planning hook.

## Logger API

Journey-owned output must go through `journeysdk.logger`; do not use direct `print(...)` calls for SDK, CLI, tool, or
tutorial diagnostics. The logger owns levels, stdout routing, redaction, `pretty` / `structured` / `jsonl` formatting,
and `--log-level off` suppression.

Keep dependency direction one-way: SDK modules may depend on `journeysdk.logger`, but `logger.py` must not know about
CLI, executor, Playwright, Docker, email, webhook, or cloud event names. Each module that emits an event owns its own
human-facing `pretty=` text.

```python
from journeysdk.logger import get_logger, pretty_line, pretty_row

_LOGGER = get_logger("my-tool")

_LOGGER.info(
    "resource_start",
    "starting resource",
    pretty=pretty_row("My tool", "starting resource", indent=8, label_width=27, style="tool"),
    resource_id=resource_id,
)
```

Use `message` and keyword fields for machine-readable data. `--output structured` and `--output jsonl` include the
event name, message, and fields, but never the `pretty=` value. Use `pretty=` only for human console rendering:

- `pretty=None` lets the logger render a generic line from `message` plus fields.
- `pretty=False` suppresses that event in `pretty` mode while preserving `structured` and `jsonl`.
- `pretty="text"` emits one human line.
- `pretty_line(...)` and `pretty_row(...)` emit styled, aligned human lines; pass a list for multi-line output.

ANSI color is applied only for TTY streams and only from explicit generic styles such as `heading`, `tool`, `accent`,
`code`, `success`, `warning`, `error`, and `muted`. Captured output and CI stay plain ASCII. Sensitive fields and
password-like text are redacted in all formats, but callers should still avoid putting secrets in prose.

## Manual Release Flow

1. Update the package version in `pyproject.toml`.
2. From the Journey SDK repository root, run `uv run pytest`.
3. From the Journey SDK repository root, run `./scripts/smoke_test_package.sh`.
4. Build the release artifacts with `uv build`.
5. Publish them with `uv publish`.
