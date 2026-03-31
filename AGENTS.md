# AGENTS.md

## Project

Journey SDK is a workflow-as-code QA toolkit for long, branching, async, cross-system user journeys. Authors write one
journey in sequential Python with primitives like `step`, `checkpoint`, and `retry`, and Journey SDK compiles or
executes the resulting linear cases.

See `README.md` for the deeper product description, use cases, and tutorial context.

## Key files

- `journey/api.py`: public API that QA can use to write journeys
- `journey/tools/webhook.py`: official webhook tool entrypoint
- `journey/planner.py`: journey compilation (aka planning)
- `journey/executor.py`: execution of a compiled journey
- `journey/cli.py`: CLI implementation
- `example/`: runnable examples that also serve as docs and tutorials

## Preferred commands

- `uv run pytest`
- `cd ../private && uv run --with ../public --extra dev pytest`
- `uv run journey plan`
- `uv run journey plan --file example/first_journey/first_journey.py`
- `uv run journey execute`
- `uv run journey execute --file example/simple_journey/simple_journey.py --step assert_local_file_contents`
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
- Planning must stay side-effect free; authentication happens only at execution time.
- The first API key to reserve a cloud resource should own it from then on.
- That first-key-wins rule should be consistent across cloud tools, whether the reserved identifier is a webhook path,
  a mail inbox, or another cloud-managed handle.
- Public callback URLs may remain unauthenticated when the generated URL itself is the capability used by the system
  under test.

## Change guidance

- Keep docs (including this `AGENTS.md`, `README.md`, and `example/`), plus tests, aligned with behavior changes.
- Verify every change by running `uv run pytest` and confirming the full test suite passes before wrapping up.
- When changes affect cloud webhook compatibility or shared service contracts in the combined workspace, also run
  `cd ../private && uv run --with ../public --extra dev pytest`.
- Keep the shared cloud auth and reservation pattern documented anywhere an official cloud tool is introduced.
- Keep docstrings in `journey/api.py` up to date (it is the public API).
- Prefer adding or updating tests before changing planner, executor, or validator semantics.
- When changing step-label behavior, check both full execution and targeted `--step` execution.
- When changing branch behavior, verify case counts, label paths, ambiguity handling, and replay-anchor reporting.
- Showcase every user-facing feature with runnable and documented examples in `example/`.
- Follow strict typing.
- Do not import from `../private` or reference private service implementation details in public docs, tests, or code.
