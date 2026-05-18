# AGENTS.md

## Project

Journey SDK is an AI-assisted workflow-as-code QA toolkit for long, branching, async, cross-system user journeys.
Authors write one journey spec in ordinary Python, compile user paths with `branch()`, replay from saved step
boundaries with `branch(start_from=...)`, interrupt long waits with default persistent state, use cloud touchpoints from
`journeysdk.touchpoints`, and describe browser work with `page.prompt(...)`.

See `README.md` for the deeper product description, use cases, tutorial context, and glossary. Keep the
public-facing landing page, `README.md`, and `docs/` aligned when product messaging or SDK surfaces change; docs and
README copy are the source of truth for supported APIs, and landing-page copy must not invent SDK helpers. Use that
vocabulary consistently: step boundary, state file, saved step binding, dirty step, replay boundary, replay anchor,
branch-anchor snapshot, step lifecycle, develop-step pause, pause action, rehydration, and rehydratable value.

## Key files

- `journeysdk/api.py`: SDK API that QA can use to write journeys
- `journeysdk/touchpoints/email.py`: official email touchpoint entrypoint
- `journeysdk/touchpoints/webhook.py`: official webhook touchpoint entrypoint
- `journeysdk/planner.py`: journey compilation (aka planning)
- `journeysdk/executor.py`: execution of a compiled journey
- `journeysdk/cli.py`: CLI implementation
- `journeysdk/logger.py`: shared diagnostic logging API and generic output formatting
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

Journey-owned output uses `journeysdk.logger.get_logger(...)` and writes to stdout. The default `pretty` output is for
humans; `--output structured` emits `[journey] ...` logfmt records and `--output jsonl` emits JSON Lines. The CLI
controls visibility with `--log-level debug|info|warning|error|off`.

## Core principles

- **Developer-centric**: The developer-facing interfaces (API and CLI) must be straightforward and intuitive.
- **Resumable tests**: With default CLI state, first Ctrl-C is graceful and resumes after the completed step; second
  Ctrl-C stops now and later restarts the dirty step from saved inputs. With `--no-state`, Ctrl-C cannot resume.
- **Extensible design**: There are official touchpoints, and everyone is welcome to add their own. Adding new touchpoints must be
  straightforward and intuitive.
- **Clear documentation**: To make it developer-friendly, all docs must be written in plain English, with enough
  context to understand it, even by non-senior engineers.
- **Consistent cloud semantics**: Official cloud touchpoints should share the same auth and ownership rules across resource
  types.
- **Simple local lifetimes**: Do not add explicit deletion cleanup for ordinary local variables, including frames from
  `inspect.currentframe()`, unless there is a concrete resource or lifecycle reason. Prefer normal Python scope.

## Cloud touchpoint pattern

- Journey SDK cloud touchpoints authenticate control-plane calls with `JOURNEY_CLOUD_API_KEY` against a Journey Cloud base
  URL.
- Compilation should stay side-effect free; authentication happens only at execution time.
- The first API key to reserve a cloud-managed handle should own it from then on.
- That first-key-wins rule should be consistent across cloud touchpoints, whether the reserved identifier is a webhook path,
  a mail inbox, or another cloud-managed handle.
- Callback URLs may remain unauthenticated when the generated URL itself is the capability used by the system
  under test.

## Prompt memory pattern

- Official touchpoints with AI-driven `prompt(...)` methods should accept a `memory` option and use the shared helpers
  in `journeysdk._prompt_memory`. Omitted `memory` should use generated callsite memory, a string should override the
  memory name, and `memory=None` should disable memory for that prompt.
- Official touchpoints with AI-driven `prompt(...)` methods should accept `output=...` for optional structured output and
  use the shared helpers in `journeysdk._prompt_output`. Omitted `output` stores plain text on `result.output`;
  explicit `output` uses native model structured-output support and stores a dictionary on `result.output`.
- If an AI-driven `prompt(...)` cannot complete the requested task because the observed app state blocks it, the prompt
  should raise instead of returning a successful result that merely summarizes the failure.
- Memory files are named `.journey/[memory].memory.md`, live under the journey source file's `.journey` folder, and are intended to be
  reviewable replay fast paths in version control. Generated names should be stable and callsite-based.
- `--no-memory` and `execute(..., no_memory=True)` must disable all prompt-memory reads and writes.
- `--no-memory-update` and `execute(..., no_memory_update=True)` must still allow prompt-memory reads but skip writes.
- Store compact replay code and checks from successful runs only. Do not persist screenshots, rendered HTML, full model
  prompts, or raw observations.
- Keep explicit and generated memory names unique within a compiled journey so planning can report mistakes before execution.

## Core dependency rule

Keep dependency direction one-way so high-level orchestration does not depend on lower-level feature helpers.

- Foundational shared modules: `journeysdk/errors.py`, `journeysdk/models.py`, `journeysdk/types.py`,
  `journeysdk/utils.py`, `journeysdk/session.py`, `journeysdk/rehydration.py`, and `journeysdk/logger.py`.
- Core orchestration modules: `journeysdk/api.py`, `journeysdk/validator.py`, `journeysdk/planner.py`,
  `journeysdk/executor.py`, `journeysdk/state.py`, `journeysdk/discovery.py`, and `journeysdk/cli.py`.
- Feature/helper modules: `journeysdk/_prompt_memory.py`, `journeysdk/_prompt_engine.py`,
  `journeysdk/_prompt_output.py`, and everything under `journeysdk/touchpoints/`.

Core orchestration modules may import foundational modules and other core orchestration modules. They must not import
feature/helper modules or touchpoint modules. Feature/helper modules may import foundational modules and narrow hooks exposed
by core modules when they need to plug into planning or execution. `journeysdk/__init__.py` is the composition/export
root and may import modules to assemble the public package.

## Logger dependency rule

`journeysdk/logger.py` is infrastructure. It must not know about CLI, executor, Playwright, Docker, email, webhook,
cloud, or any other component's event names or field semantics.

Other modules depend on the logger by passing machine-readable event data and optional human-facing `pretty=` output:

```python
from journeysdk.logger import get_logger, pretty_row

_LOGGER = get_logger("component")

_LOGGER.info(
    "event_name",
    "machine-readable message",
    pretty=pretty_row("Component", "human readable detail", indent=8, label_width=27, style="touchpoint"),
    useful_field="value",
)
```

Use `pretty=False` for events that should remain in `structured` and `jsonl` but should not be shown in the human
timeline. Do not add `if component == ...` or `if event == ...` branches to `logger.py`; put that formatting beside the
event emitter instead. See `CONTRIBUTING.md` for the full Logger API rules.

## Change guidance

- Keep docs (including this `AGENTS.md`, `README.md`, and `docs/`), public-facing landing-page copy, plus tests,
  aligned with behavior changes.
- Use `journeysdk.logger.get_logger(...)` for SDK, touchpoint, or tutorial diagnostics instead of ad hoc `print(...)`.
- Keep logger dependencies inverted: `journeysdk/logger.py` may know generic concepts like levels, redaction, rows, and
  styles, but it must not branch on component names, Journey event names, or event field semantics. Put human `pretty=`
  formatting beside the module that emits the event.
- Do not add tests that assert exact prose inside `*.md` files; prefer behavior-level checks for runnable examples,
  CLI behavior, repository boundaries, or relevant file existence.
- Verify every change by running `uv run pytest` and confirming the full test suite passes before wrapping up.
- Whenever Journey CLI behavior, docs, examples, or journey authoring guidance changes, update
  `skills/journey-developer/SKILL.md` in the same change.
- Keep the shared cloud auth and reservation pattern documented anywhere an official cloud touchpoint is introduced.
- Keep docstrings in `journeysdk/api.py` up to date (it is the SDK API).
- Prefer adding or updating tests before changing planner, executor, or validator semantics.
- When changing step-label behavior, check both full execution and targeted `--step` execution.
- When changing branch behavior, verify case counts, label paths, ambiguity handling, and replay-anchor reporting.
- Showcase every user-facing feature with runnable and documented examples in `docs/`.
- Follow strict typing.
- Do not reference private service implementation details in docs, tests, or code.
