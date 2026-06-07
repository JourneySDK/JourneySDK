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
journey logs --help
journey agent --help
```

`journey --help` explains the execution loop, targeted `--develop-step` and `--step` commands, state flags, touchpoint
reference commands, and recovery commands. `journey logs --help` explains how to discover scopes and sources before
reading artifacts. `journey agent --help` explains print/install modes for packaged assistant guidance.

When a Journey command fails, the CLI prints an instructional block:

```console
What happened: ...
Try this: ...
Next commands:
  journey ...
```

Structured and JSON Lines output include the same recovery data in `instructions`, `next_commands`, and
`help_command` fields so agents can continue without external docs.

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
  explicit `branch(start_from=...)` or positive `retry=...` boundary. If there is no explicit boundary, the case starts
  again from the beginning.

Journey checks saved state before reuse and labels the decision as `fresh`, `replayed`, or `invalidated`. See
[Retries And Resume](03-retries-and-resume.md) for the state model and when to use `--no-state` for fresh-path evidence.

With `--no-state`, Ctrl-C stops the run immediately and Journey cannot resume it. Use `--no-state-update` when a run
should read existing state but leave the state file unchanged.

## Browser Setup

Playwright and LangChain are included in the default package install. The first Journey browser step automatically
downloads Chromium in the active environment. That first launch needs network access and can take a moment.
Journey stores run evidence under `.journey/logs/`: structured Journey events, touchpoint logs, browser traces,
browser videos, and browser console/network events. Use `--no-logs` only when a run should not write any local
debugging artifacts. Use `--no-browser-recording` when browser console/network logs should still be kept but
Playwright trace/video capture should be skipped. Journey clears existing logs at the start of a run so `journey logs`
shows the current run's cases.

After a run, use `journey logs` from the project root to browse recorded evidence interactively. Choose all cases, one
case, or browse branch and step scopes; then open a merged Playwright trace, open a merged WebM recording, print raw
text logs, or print artifact paths. The log browser lists touchpoints and touchpoint-defined sources such as Docker
Compose service names, and selecting a parent touchpoint aggregates all child logs.

For agent loops, discover filters before reading large logs:

```bash
journey logs --list-scopes
journey logs --list-log-sources --case case_1 --step start_services
journey logs --show --case case_1 --step start_services --touchpoint docker --source web --source worker --tail 80
journey logs --paths --step report_issue --touchpoint browser
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
appends packaged touchpoint references. The targeted verification loop, touchpoint discovery, and log-browsing commands
live in that shared instruction body so the user prompt can stay short. The default command is print-only and does not
write files.

When an agent is asked to fix a failing Journey file, it should run the failing command or full journey once, use the
first failed step and any `Retry failed step:` command as the focused `--develop-step` loop, inspect correlated
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
journey --touchpoint-docs docker
journey --touchpoint-docs browser
journey --touchpoint-docs email
journey --touchpoint-docs webhook
journey --touchpoint-docs http
journey --touchpoint-docs all
```
