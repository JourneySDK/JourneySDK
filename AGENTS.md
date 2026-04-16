# AGENTS.md

## Project

Journey SDK is a workflow-as-code QA toolkit for long, branching, async, cross-system user journeys. Authors write one
journey in sequential Python with primitives like `step`, `checkpoint`, and `retry`, and Journey SDK compiles or
executes the resulting linear cases.

See `README.md` for the deeper product description, use cases, and tutorial context.

## Key files

- `journeysdk/api.py`: SDK API that QA can use to write journeys
- `journeysdk/tools/email.py`: official email tool entrypoint
- `journeysdk/tools/webhook.py`: official webhook tool entrypoint
- `journeysdk/planner.py`: journey compilation (aka planning)
- `journeysdk/executor.py`: execution of a compiled journey
- `journeysdk/cli.py`: CLI implementation
- `docs/`: runnable tutorial journeys plus the handbook pages that explain them

## Preferred commands

- `uv run pytest`
- `uv run journey`
- `uv run journey --file docs/first_journey/first_journey.py`
- `uv run journey --file docs/simple_journey/simple_journey.py --step assert_local_file_contents`
- `uv build`

CLI commands discover functions annotated with `@journey` / `@journey.journey` in the current directory. Use `--file`
to scope to one file, `--journey` to scope to one decorated function name, and `--step` to execute only the single
flow that reaches a target step label.

## Core principles

- **Developer-centric**: The developer-facing interfaces (API and CLI) must be straightforward and intuitive.
- **Resumable tests**: Steps can be interrupted and resumed (when `--state` is provided).
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

## Change guidance

- Keep docs (including this `AGENTS.md`, `README.md`, and `docs/`), plus tests, aligned with behavior changes.
- Verify every change by running `uv run pytest` and confirming the full test suite passes before wrapping up.
- Keep the shared cloud auth and reservation pattern documented anywhere an official cloud tool is introduced.
- Keep docstrings in `journeysdk/api.py` up to date (it is the SDK API).
- Prefer adding or updating tests before changing planner, executor, or validator semantics.
- When changing step-label behavior, check both full execution and targeted `--step` execution.
- When changing branch behavior, verify case counts, label paths, ambiguity handling, and replay-anchor reporting.
- Showcase every user-facing feature with runnable and documented examples in `docs/`.
- Follow strict typing.
- Do not reference private service implementation details in docs, tests, or code.
