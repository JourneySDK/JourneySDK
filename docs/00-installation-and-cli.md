# Installation And CLI

Run every command in this guide from the repository root.

## Install The Python Package

Install Journey SDK into an existing Python environment:

```bash
pip install journey-sdk
```

Add Journey SDK to a `uv` project:

```bash
uv add journey-sdk
```

Import the Journey SDK API from `journeysdk`:

```python
from journeysdk import journey, step
```

## Install The CLI

Run the CLI once without installing it:

```bash
uvx --from journey-sdk journey --help
```

Install a persistent `journey` command with `uv`:

```bash
uv tool install journey-sdk
journey --help
```

If your shell cannot find the command yet, refresh the shell PATH integration:

```bash
uv tool update-shell
```

Install the CLI inside a virtual environment with `pip`:

```bash
python -m pip install journey-sdk
journey --help
```

Use the CLI from a project-local environment:

```bash
uv add journey-sdk
uv run journey --help
```

## CLI Help

Journey keeps command-related guidance in the `--help` output. Both human developers and coding agents should use 
these commands as the self-contained command manuals before choosing flags or repairing a failed command:

```bash
journey --help
journey loop --help
journey verify --help
journey evidence --help
journey dev --help
journey agent --help
```

`journey --help` is the short command index. `journey loop --help` explains the focused replay loop for one step.
`journey verify --help` explains branch and full-journey verification. `journey evidence --help` explains how to
discover scopes and sources before reading artifacts. `journey dev --help` explains how to pause at a browser step,
inspect rendered page state, and get branch-extension guidance. `journey agent --help` explains
print/install modes for packaged assistant guidance.

When a Journey command fails, the CLI prints an instructional block:

```console
What happened: ...
Try this: ...
Next commands:
  journey ...
```

Structured and JSON Lines output include the same recovery data in `instructions`, `next_commands`, and
`help_command` fields so agents can continue without external docs.

## Develop Browser Branches

Use `journey dev` when an app is already running and you want to extend Journey coverage from an actual rendered page:

```bash
journey dev open_main_page --file journeys/app_journey.py
journey loop new_branch_step --file journeys/app_journey.py
```

The dev command executes the selected Journey through a step, pauses on the live browser page, writes rendered-page
artifacts under `.journey/dev/...`, lists actionable elements found on the page, and prints concrete instructions for
adding the next step or branch. Omit the step label to pause after the first step in the selected Journey. Human pretty
mode keeps browser resources open until the prompt is answered.

For agent workflows, use JSON Lines output. `--output jsonl` implies `--agent`, closes resources after inspection, and
emits a structured `dev_result` with `candidate_flows`, `rendered_page` artifact paths, `actionable_elements`, and
`extension_instructions`:

```bash
journey dev open_main_page --file journeys/app_journey.py --output jsonl
```

When starting a new browser Journey file, let dev initialize a minimal first step from a start URL:

```bash
journey dev --file journeys/app_journey.py --url http://127.0.0.1:3000
```

The generated skeleton opens the provided URL with `open_page(...)`. After that, edit the Journey source using the
dev result, choose coarse step boundaries, and prove the new behavior with `journey loop` or `journey verify`.

## Ctrl-C And Resumable Runs

Journey persists execution state by default when you want a long run to survive interruption:

```bash
uv run journey
```

With persistent state, Ctrl-C has two levels:

- First Ctrl-C is graceful. Journey prints `Ctrl-C received. Finishing the active step so Journey can save progress.
  Press Ctrl-C again to stop now.`, lets the active step reach post-exit, saves progress, and stops.
- Second Ctrl-C is forceful. Journey prints `Ctrl-C received again. Stopping now; this step will restart from the
  nearest replay boundary on resume.`, stops the dirty step as soon as it can, and resumes later from the nearest
  explicit `branch(replay_from=...)` or positive `retry=...` boundary. If there is no explicit boundary, the case starts
  again from the beginning.

Journey checks saved state before reuse and labels the decision as `fresh`, `replayed`, or `invalidated`. See
[Retries And Resume](03-retries-and-resume.md) for the state model and when to use `--fresh` for fresh-path evidence.

With `journey verify --fresh`, Ctrl-C stops the run immediately and Journey cannot resume it. Use `--reuse-state` when
a verification run should read existing state instead of starting fresh.

## Browser Setup

Playwright and LangChain are included in the default package install. The first Journey browser step automatically
downloads Chromium in the active environment. That first launch needs network access and can take a moment.
Journey stores run evidence under `.journey/logs/`: structured Journey events, touchpoint logs, browser traces,
browser videos, and browser console/network events. Use `--no-logs` only when a run should not write any local
debugging artifacts. Use `--no-browser-recording` when browser console/network logs should still be kept but
Playwright trace/video capture should be skipped. Journey clears existing logs at the start of a run so `journey evidence`
shows the current run's cases.

After a run, use `journey evidence` from the project root to browse recorded evidence interactively. Choose all cases, one
case, or browse branch and step scopes; then open a merged Playwright trace, open a merged WebM recording, print raw
text logs, or print artifact paths. The log browser lists touchpoints and touchpoint-defined sources such as Docker
Compose service names, and selecting a parent touchpoint aggregates all child logs.

For agent loops, discover filters before reading large logs:

```bash
journey evidence --list-scopes
journey evidence --list-log-sources --case case_1 --step start_services
journey evidence --show --case case_1 --step start_services --touchpoint docker --source web --source worker --tail 80
journey evidence --paths --step report_issue --touchpoint browser
```

## Local Development Installs

Install the current checkout with `pip`:

```bash
pip install -e .
```

Add the current checkout to a `uv` project:

```bash
uv add --editable /path/to/journey-sdk
```

Install the CLI directly from a local checkout:

```bash
uv tool install --editable /path/to/journey-sdk
journey --help
```

For the full contributor workflow, including package smoke tests and manual publishing, see
[`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## AI Agent Instructions

The easiest way to start an agentic Journey loop is one prompt:

```text
Use Journey SDK for this task: <describe the user flow>. Run journey agent codex first.
```

Replace `codex` with `claude`, `cursor`, or `generic` for another assistant. The assistant should run the installed CLI
for a complete guidance packet:

```bash
journey agent codex
journey agent claude
journey agent cursor
journey agent generic
```

The default output includes the shared assistant-specific Journey guidance from the packaged instruction template, then
appends packaged touchpoint references. The step replay loop, touchpoint discovery, and evidence commands
live in that shared instruction body so the user prompt can stay short. The default command is print-only and does not
write files.

When an agent is asked to fix a failing Journey file, it should run the failing command or full journey once, use the
first failed step and any `Retry failed step:` command as the focused `journey loop` command, inspect correlated
`.journey/logs` evidence, and rerun until executable Journey evidence passes.

Install assistant-specific guidance when a project-level agent file or skill should be available persistently:

```bash
journey agent codex --install
journey agent claude --install
journey agent cursor --install
journey agent generic --install
```

Install mode writes the selected guidance to its default project path and refuses to replace an existing file unless
`--force` is passed.

Agents can also print detailed packaged touchpoint references before using official helpers:

```bash
journey touchpoints docker
journey touchpoints browser
journey touchpoints email
journey touchpoints webhook
journey touchpoints http
journey touchpoints all
```
