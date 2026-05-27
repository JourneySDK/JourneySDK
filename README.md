# Journey SDK

With AI, testing is the new coding.

Journey SDK helps developers and coding assistants verify real user journeys, not just units or partial integrations.
One Python spec can cover branching paths, browser work, async side effects, local Docker services, email, webhooks, and
other systems that participate in a user flow.

## Why Journey

- **One Python journey for meaningful user paths**: author the shared setup once and use `branch()` for alternate paths.
- **Fast replay from durable boundaries**: use `branch(start_from=...)`, retries, and state so late steps can be
  developed without rerunning every expensive setup action.
- **Touchpoints for real systems**: drive the browser, start Docker Compose apps, wait for email or webhooks, and keep
  app-specific checks in ordinary Python helpers.
- **Agent-friendly CLI loops**: coding assistants can run one target step with `--develop-step`, retry it after edits,
  then broaden to `--step` or the full journey before finishing.

## Install

Install Journey SDK into an existing environment:

```bash
pip install journey-sdk
```

Or add it to a `uv` project:

```bash
uv add journey-sdk
```

Run the CLI without installing it globally:

```bash
uvx --from journey-sdk journey --help
```

See [Installation And CLI](docs/00-installation-and-cli.md) for the complete install guide, CLI flags, editable
installs, browser setup, and local package smoke testing.

## Quick Start

Import the primitives you use and write a top-level `@journey` function:

```python
from journeysdk import journey, step
```

The canonical first-run guide is [Getting Started](docs/01-getting-started.md). It covers the smallest useful journey,
running one file, selecting one journey, and JSON Lines output for tools.

## Authoring Guides

- [Branching and Targeted Runs](docs/02-branching-and-targeted-runs.md): canonical guidance for adding journey specs,
  choosing durable step boundaries, using `branch(start_from=...)`, and iterating with `--step` and `--develop-step`.
- [Retries and Resume](docs/03-retries-and-resume.md): retry boundaries, interrupted runs, default state, and resume.
- [Browser and Local Touchpoints](docs/04-browser-and-local-integrations.md): browser, local file, Docker Compose, and
  browser prompt tutorials.
- [Journey Cloud Touchpoints](docs/05-journey-cloud-integrations.md): hosted webhook and email examples.
- [Debugging and Failure Modes](docs/06-debugging-and-failure-modes.md): failure reports, recordings, and `--fail-fast`.

The docs index is [Journey Docs](docs/README.md).

## Touchpoint References

Detailed touchpoint API references are packaged with the SDK so they are available in downstream projects and `uvx`
runs:

```bash
journey --touchpoint-docs browser
journey --touchpoint-docs docker
journey --touchpoint-docs email
journey --touchpoint-docs webhook
journey --touchpoint-docs http
journey --touchpoint-docs all
```

Those references are sourced from `journeysdk/touchpoint_docs/*.md` in this repository.

## AI Agent Support

Use packaged assistant instructions when an AI coding agent needs to create, execute, debug, or maintain Journey SDK
journeys:

```bash
journey --agent-instructions codex
journey --agent-instructions claude --install-agent-instructions
journey --agent-instructions cursor --install-agent-instructions
```

Printing is the default. Install mode writes the selected assistant's default project file and refuses to replace an
existing file unless `--force-agent-instructions` is passed. The shared source for these instructions is
`journeysdk/agent_templates/instructions.md`.

## Develop Locally

```bash
uv sync --extra dev
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor workflows, local package smoke testing, and publishing notes.
