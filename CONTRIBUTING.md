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
  `journey agent codex`
- verifies installed agent artifacts for Codex (`.agents/skills/journey/SKILL.md`), Claude Code
  (`.claude/skills/journey/SKILL.md`), and Cursor (`.cursor/skills/journey/SKILL.md`)
- runs a one-off `uv tool run --from <wheel> journey --help` plus `journey agent codex`

## Documentation Alignment

Every SDK change should include a docs and instruction review for the surfaces that describe the touched behavior:
`docs/`, `README.md`, `AGENTS.md`, this `CONTRIBUTING.md`, `journeysdk/touchpoint_docs/*.md`, and
`journeysdk/agent_templates/instructions.md`. Keep the packaged agent template aligned with generated Codex repo skill,
Claude `/journey` skill, Cursor `/journey` skill, and generic assistant output. The install paths follow the public
Claude Code skills, Cursor skills, and Codex skills documentation. If no doc update is needed, note that the relevant
surfaces were reviewed.

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

## Agent Loop Boundary

Journey SDK should not call an LLM directly from touchpoints. Keep the agentic loop with the coding agent that invokes
Journey: expose browser state, logs, traces, videos, and `journey dev --output jsonl` context, then let that agent edit
ordinary Python Journey files with the human in the loop.

## Planning Hooks And Core Dependencies

Keep the component dependency direction described in `AGENTS.md`: high-level orchestration modules stay generic, and
feature/helper modules plug in through narrow runtime protocols such as rehydration and dev contributions. Do not add
direct dependencies from core orchestration modules to touchpoints or agent-specific helpers.

## Logger API

Journey-owned output must go through `journeysdk.logger`; do not use direct `print(...)` calls for SDK, CLI, touchpoint, or
tutorial diagnostics. The logger owns levels, stdout routing, redaction, `pretty` / `jsonl` formatting, and
`--log-level off` suppression.

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

Use `message` and keyword fields for machine-readable data. `--output jsonl` includes the event name, message, and
fields, but never the `pretty=` value. Use `pretty=` only for human console rendering:

- `pretty=None` lets the logger render a generic line from `message` plus fields.
- `pretty=False` suppresses that event in `pretty` mode while preserving `jsonl`.
- `pretty="text"` emits one human line.
- `pretty_line(...)` and `pretty_row(...)` emit styled, aligned human lines; pass a list for multi-line output.

ANSI color is applied only for TTY streams and only from explicit generic styles such as `heading`, `touchpoint`, `accent`,
`code`, `success`, `warning`, `error`, and `muted`. Captured output and CI stay plain ASCII. Sensitive fields and
password-like text are redacted in all formats, but callers should still avoid putting secrets in prose.

## Release Flow

1. Update the package version in `pyproject.toml`.
2. From the Journey SDK repository root, run `uv run pytest`.
3. Create a tag that matches the package version, using either `vX.Y.Z` or `X.Y.Z`.
4. Push the tag to GitHub.
5. GitHub Actions runs `.github/workflows/publish-package.yml`, verifies that the tag matches `[project].version`,
   verifies that the version is not already present on PyPI, then runs `./scripts/publish_package.sh`.

The JourneySDK repository must have a `UV_PUBLISH_TOKEN` secret configured for PyPI publishing.
