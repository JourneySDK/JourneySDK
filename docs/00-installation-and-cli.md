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
journey discover --help
journey agent --help
```

`journey --help` is the short command index. `journey loop --help` explains the focused replay loop for one step.
`journey verify --help` explains branch and full-journey verification. `journey evidence --help` explains how to
discover scopes and sources before reading artifacts. `journey discover --help` explains how to crawl a running app URL
or continue from an existing browser step and write generated Journey source. `journey agent --help` explains
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

## Generate Draft Journey Source

Use `journey discover` when an app is already running and you want a first Journey spec with broad browser coverage:

```bash
journey discover http://127.0.0.1:3000 --output-file journeys/discovered_journey.py
journey verify --file journeys/discovered_journey.py
```

The discoverer composes deterministic form submits and navigation transitions first, expands bounded finite controls
with `--max-variants-per-control`, then uses Claude Haiku by default through `JOURNEY_BROWSER_PROMPT_MODEL` model
resolution when the next transition is uncertain. It crawls same-origin scenarios within `--depth`, `--max-actions`,
`--max-model-calls`, and `--action-timeout`, and writes deterministic Playwright step helpers. When a transition exposes
a stable visible identifier, `--side-effect-probes auto` can attach generic same-origin JSON, local email, local webhook,
and SDK Cloud webhook evidence assertions if reachable endpoints are discovered. Model calls happen during discovery, not during later
`journey verify` runs. Review the generated file before committing it, especially for credentials, fixture data, and
side-effectful flows.

Discovery writes live logs to stdout while it runs. For agent workflows that need structured diagnostics, use JSON Lines
output and read the final `discover_result.output_file` field:

```bash
journey discover http://127.0.0.1:3000 --output-file journeys/discovered_journey.py --output jsonl
```

When you are already working from a Journey step that lands on the page under development, anchor discovery at that
step instead of starting over from a URL:

```bash
journey discover open_main_page --file journeys/app_journey.py --output-file journeys/open_main_page_snippet.py
```

Step mode uses `--file` as the existing Journey source file to search, accepts `--journey` to disambiguate the existing
entrypoint, executes only through the requested step with temporary state and no browser recording, and starts from the
step's returned `JourneyBrowserPage` or the last page it opened with `open_page(...)`. It then writes an extension
snippet to `--output-file`. Paste the helper functions into the Journey file and call the generated function near the
anchor, for example:

```python
main_page = step(open_main_page)
discover_after_open_main_page(main_page)
```

`journey discover` writes generated code only to `--output-file`, creates missing parent directories, and refuses to
replace existing files unless `--force` is passed. In URL mode, `--file` is rejected. In step mode, `--file` selects the
source Journey file and `--output-file` selects the generated snippet destination.

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
