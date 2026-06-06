# Journey SDK

Agent-native verification loops for long product workflows.

Journey SDK helps developers and AI coding agents prove that critical user journeys still work after code changes.
One Python spec can cover a long product flow across branching paths, browser work, async side effects, local Docker
services, hosted email, hosted webhooks, and app-specific integrations that participate in the user flow.

The wedge is simple: AI coding agents can write code quickly, but high-revenue teams need reliable, resumable,
cross-system verification loops before they trust those changes. Journey keeps those loops in ordinary Python and makes
late-flow iteration practical through durable step boundaries, branch replay, targeted runs, and packaged agent
instructions.

## Why Journey

- **Built for coding-agent loops**: agents can inspect generated cases with `--plan-only`, run one target step with
  `--develop-step`, retry it after edits, then broaden to `--step` or the full journey before finishing.
- **One Python journey for meaningful user paths**: author shared setup once and use `branch()` for alternate paths
  instead of duplicating checkout, onboarding, billing, or lifecycle tests.
- **Fast replay from durable boundaries**: make each step earn its checkpoint, then use `branch(start_from=...)`,
  retries, and state so late user-flow steps can be developed without rerunning every expensive setup action. See
  [Retries and Resume](docs/03-retries-and-resume.md) for how Journey marks state as fresh, replayed, or invalidated.
- **Cross-system verification without a new DSL**: drive the browser, start Docker Compose apps, wait for hosted email
  or webhook callbacks, and keep app-specific checks in ordinary Python helpers.

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

Give a coding agent the complete Journey loop from the installed CLI:

```bash
journey --agent-bootstrap codex
journey --agent-bootstrap claude
```

The canonical first-run guide is [Getting Started](docs/01-getting-started.md). It covers the smallest useful journey,
running one file, selecting one journey, and JSON Lines output for tools.

## Authoring Guides

- [Branching and Targeted Runs](docs/02-branching-and-targeted-runs.md): canonical guidance for adding journey specs,
  choosing coarse durable step boundaries, using `branch(start_from=...)`, and iterating with `--step` and
  `--develop-step`.
- [Retries and Resume](docs/03-retries-and-resume.md): state management, retry boundaries, interrupted runs, and resume.
- [Browser and Local Touchpoints](docs/04-browser-and-local-integrations.md): browser, local file, Docker Compose, and
  browser prompt tutorials.
- [Journey Cloud Touchpoints](docs/05-journey-cloud-integrations.md): hosted webhook and email examples.
- [Debugging and Failure Modes](docs/06-debugging-and-failure-modes.md): failure reports, logs, and `--fail-fast`.

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

Use the bootstrap packet when an AI coding agent needs the whole Journey loop in one command:

```bash
journey --agent-bootstrap codex
journey --agent-bootstrap claude
journey --agent-bootstrap cursor
journey --agent-bootstrap generic
```

It prints target-specific assistant guidance, the canonical `--plan-only` -> `--develop-step` -> `--step --no-state`
-> full journey loop, and packaged touchpoint references. Use packaged assistant instructions when a project-level
assistant file should be written:

```bash
journey --agent-instructions codex
journey --agent-instructions claude --install-agent-instructions
journey --agent-instructions cursor --install-agent-instructions
```

Printing is the default. Install mode writes the selected assistant's default project file and refuses to replace an
existing file unless `--force-agent-instructions` is passed. The shared source for these instructions is
`journeysdk/agent_templates/instructions.md`.

When SDK behavior, CLI flags, touchpoints, journey authoring guidance, or assistant workflows change, review and align
[docs](docs/README.md), [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md),
`journeysdk/touchpoint_docs/*.md`, and `journeysdk/agent_templates/instructions.md` so README guidance and rendered
assistant instructions stay in sync.

## Develop Locally

```bash
uv sync --extra dev
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor workflows, local package smoke testing, and publishing notes.
