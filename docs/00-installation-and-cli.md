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

## Inspect A Plan Without Running

Use `--plan-only` when you want to discover journeys, compile branch cases, validate a requested target label, and exit
before any step code executes:

```bash
uv run journey --file journeys/checkout_journey.py --plan-only
uv run journey --file journeys/checkout_journey.py --develop-step submit_order --plan-only
```

This is the safest first command for coding agents working in a large app. It shows the step labels and branch cases
available for targeted execution without starting Docker services, opening browsers, sending emails, or mutating app
state.

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

AI coding agents can ask the installed CLI for a complete bootstrap packet:

```bash
journey --agent-bootstrap codex
journey --agent-bootstrap claude
journey --agent-bootstrap cursor
journey --agent-bootstrap generic
```

The bootstrap output includes assistant-specific Journey guidance, the canonical targeted verification loop, copy-paste
commands, and packaged touchpoint references. It is print-only and does not write files.

Use assistant-specific instructions when a project-level agent file should be installed or printed by itself:

```bash
journey --agent-instructions codex
journey --agent-instructions claude
journey --agent-instructions cursor
journey --agent-instructions generic
```

Add `--install-agent-instructions` to write the selected guidance to its default project path. Install mode refuses to
replace an existing file unless `--force-agent-instructions` is passed.

Agents can also print detailed packaged touchpoint references before using official helpers:

```bash
journey --touchpoint-docs docker
journey --touchpoint-docs browser
journey --touchpoint-docs email
journey --touchpoint-docs webhook
journey --touchpoint-docs http
journey --touchpoint-docs all
```
