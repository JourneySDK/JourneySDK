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

## Ctrl-C And Resumable Runs

Journey persists execution state by default when you want a long run to survive interruption:

```bash
uv run journey
```

With persistent state, Ctrl-C has two levels:

- First Ctrl-C is graceful. Journey prints `Ctrl-C received. Finishing the active step so Journey can save progress.
  Press Ctrl-C again to stop now.`, lets the active step reach post-exit, saves progress, and stops. The same command
  resumes after that completed step.
- Second Ctrl-C is forceful. Journey prints `Ctrl-C received again. Stopping now; this step will restart from saved
  inputs on resume.`, stops the dirty step as soon as it can, and resumes later by restarting that step from saved
  inputs.

With `--no-state`, Ctrl-C stops the run immediately and Journey cannot resume it. Use `--no-state-update` when a run
should read existing state but leave the state file unchanged.

## Browser Setup

Playwright and LangChain are included in the default package install. The first Journey browser step automatically
downloads Chromium in the active environment. That first launch needs network access and can take a moment.

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

## AI Agent Skill

AI coding agents can use the source-distributed
[`journey-developer` skill](../skills/journey-developer/SKILL.md) for Journey authoring, targeted `--step` runs,
`--develop-step` loops, and persistent state guidance. See [`../skills/README.md`](../skills/README.md) for the
minimal install notes.
