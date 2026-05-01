# AGENTS.md

## Project

Journey SDK is a workflow-as-code QA toolkit for long, branching, async, cross-system user journeys. Authors write one
journey in sequential Python with primitives like `step`, `branch`, and `step(..., retry=...)`, and
Journey SDK compiles or executes the resulting linear cases.

See `README.md` for the deeper product description, use cases, tutorial context, and glossary. Use that vocabulary
consistently: step boundary, state file, saved step binding, dirty step, replay boundary, replay anchor,
branch-anchor snapshot, step lifecycle, develop-step pause, pause action, rehydration, and rehydratable value.

## Key files

- `journeysdk/api.py`: SDK API that QA can use to write journeys
- `journeysdk/tools/email.py`: official email tool entrypoint
- `journeysdk/tools/webhook.py`: official webhook tool entrypoint
- `journeysdk/planner.py`: journey compilation (aka planning)
- `journeysdk/executor.py`: execution of a compiled journey
- `journeysdk/cli.py`: CLI implementation
- `journeysdk/logger.py`: shared diagnostic logging API and common stderr format
- `docs/`: runnable tutorial journeys plus the handbook pages that explain them

## Preferred commands

- `uv run pytest`
- `uv run journey`
- `uv run journey --file docs/first_journey/first_journey.py`
- `uv run journey --file docs/simple_journey/simple_journey.py --step assert_local_file_contents`
- `uv build`

CLI commands discover functions annotated with `@journey` / `@journey.journey` in the current directory. Use `--file`
to scope to one file, `--journey` to scope to one decorated function name, and `--step` to execute only the single
case that reaches a target step label. Targeted `--step` runs report `replay_anchor` for branch step anchors, but they
do not skip directly to that anchor unless state or retry behavior causes replay.

Journey diagnostics use `journeysdk.logger.get_logger(...)` and emit `[journey] ...` lines to stderr by default. The
CLI controls this with `--log-level debug|info|warning|error|off`; keep stdout for plans, summaries, prompts, and JSON.

## Core principles

- **Developer-centric**: The developer-facing interfaces (API and CLI) must be straightforward and intuitive.
- **Resumable tests**: With CLI `--state`, first Ctrl-C lets the active step finish and resume after it; second Ctrl-C
  interrupts the dirty step, which later restarts from the top with saved inputs.
- **Extensible design**: There are official tools, and everyone is welcome to add their own. Adding new tools must be
  straightforward and intuitive.
- **Clear documentation**: To make it developer-friendly, all docs must be written in plain English, with enough
  context to understand it, even by non-senior engineers.
- **Consistent cloud semantics**: Official cloud tools should share the same auth and ownership rules across resource
  types.

## Cloud tool pattern

- Journey SDK cloud tools authenticate control-plane calls with `JOURNEY_CLOUD_API_KEY` against a Journey Cloud base
  URL.
- Compilation should stay side-effect free; authentication happens only at execution time.
- The first API key to reserve a cloud resource should own it from then on.
- That first-key-wins rule should be consistent across cloud tools, whether the reserved identifier is a webhook path,
  a mail inbox, or another cloud-managed handle.
- Callback URLs may remain unauthenticated when the generated URL itself is the capability used by the system
  under test.

## Prompt memory pattern

- Official tools with AI-driven `prompt(...)` methods should accept `memory: str | None = None` and use the shared
  helpers in `journeysdk._prompt_memory`.
- Official tools with AI-driven `prompt(...)` methods should accept `output=...` for optional structured output and
  use the shared helpers in `journeysdk._prompt_output`. Omitted `output` stores plain text on `result.output`;
  explicit `output` uses native model structured-output support and stores a dictionary on `result.output`.
- If an AI-driven `prompt(...)` cannot complete the requested task because the observed app state blocks it, the prompt
  should raise instead of returning a successful result that merely summarizes the failure.
- Memory files are named `[memory].memory.json`, live beside the journey source file, and are intended to be
  reviewable in version control.
- `--no-memory` and `execute(..., no_memory=True)` must disable all prompt-memory reads and writes.
- `--no-memory-update` and `execute(..., no_memory_update=True)` must still allow prompt-memory reads but skip writes.
- Store compact lessons from successful runs only. Do not persist screenshots, rendered HTML, full model prompts, or
  raw observations.
- Keep memory names literal and unique within a compiled journey so planning can report mistakes before execution.

## Change guidance

- Keep docs (including this `AGENTS.md`, `README.md`, and `docs/`), plus tests, aligned with behavior changes.
- Use `journeysdk.logger.get_logger(...)` for SDK, tool, or tutorial diagnostics instead of ad hoc
  `print(..., file=sys.stderr)`.
- Do not add tests that assert exact prose inside `*.md` files; prefer behavior-level checks for runnable examples,
  CLI behavior, repository boundaries, or relevant file existence.
- Verify every change by running `uv run pytest` and confirming the full test suite passes before wrapping up.
- Whenever Journey CLI behavior, docs, examples, or journey authoring guidance changes, update
  `skills/journey-developer/SKILL.md` in the same change.
- Keep the shared cloud auth and reservation pattern documented anywhere an official cloud tool is introduced.
- Keep docstrings in `journeysdk/api.py` up to date (it is the SDK API).
- Prefer adding or updating tests before changing planner, executor, or validator semantics.
- When changing step-label behavior, check both full execution and targeted `--step` execution.
- When changing branch behavior, verify case counts, label paths, ambiguity handling, and replay-anchor reporting.
- Showcase every user-facing feature with runnable and documented examples in `docs/`.
- Follow strict typing.
- Do not reference private service implementation details in docs, tests, or code.
