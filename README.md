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

- **Built for coding-agent loops**: agents run the failing journey or focused step, retry it with `--develop-step`
  after edits until it passes, then broaden to `--step` or the full journey before finishing.
- **One Python journey for meaningful user paths**: author shared setup once and use `branch()` for alternate paths
  instead of duplicating checkout, onboarding, billing, or lifecycle tests.
- **Fast replay from durable boundaries**: make each step an intentional replay boundary for a whole operation, then use
  `branch(start_from=...)`, retries, and state so late user-flow steps can be developed without rerunning every
  expensive setup action. See
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

The CLI help surfaces are the command manuals for humans and coding agents:

```bash
journey --help
journey logs --help
journey agent --help
```

See [Installation And CLI](docs/00-installation-and-cli.md) for the complete install guide, self-healing CLI help,
editable installs, browser setup, and local package smoke testing.

Agent-authored journeys are not considered verified by code generation alone. After adding or changing a Journey, the
agent should run executable `journey --file ...` commands, use `--develop-step` or `--step` for each requested branch
target, and finish with fresh `--no-state` evidence whenever the app infrastructure is available. If the app is not
running, the agent should follow documented local startup commands before declaring the run environment-blocked. Agents
should list secret configuration by key presence only and must not print credential values while discovering the
environment; they should not dump `.env*` or credential files wholesale. For auth, seed data, payments, or other test
setup, agents should inspect existing E2E helpers before guessing credentials or magic codes, and request approval
before running repo-supported setup that mutates external services.

## Quick Start

Give your coding assistant one line:

```text
<describe your task>. Use Journey SDK (run `journey agent codex` for instructions).
```

Replace `codex` with `claude`, `cursor`, or `generic` for another assistant. The assistant should run that command,
read the full SDK guidance, discover touchpoint docs as needed, add or extend the smallest useful journey spec, run the
failing journey or focused step, iterate with targeted Journey commands until executable evidence passes, and report the
exact commands and evidence it used.

To avoid adding that line to every prompt, install persistent guidance once from the project root:

```bash
journey agent codex --install
journey agent claude --install
journey agent cursor --install
journey agent generic --install
```

Install mode writes the selected assistant's default project file or skill and refuses to replace an existing file
unless `--force` is passed.

If you are authoring a journey by hand, use [Getting Started](docs/01-getting-started.md). It covers imports,
top-level `@journey` functions, running one file, selecting one journey, and JSON Lines output for tools.

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

## CLI Help

Use `journey --help` as the self-contained execution command manual. It includes the agentic verification loop,
targeted run commands, state guidance, and recovery commands. Use `journey logs --help` for artifact inspection and
`journey agent --help` before printing or installing assistant guidance.

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

For a new agentic loop, give the assistant the one-line quickstart prompt above. The assistant should run the bootstrap
command itself:

```bash
journey agent codex
journey agent claude
journey agent cursor
journey agent generic
```

`journey agent <target>` is print-only by default. It prints the shared target-specific assistant guidance from
`journeysdk/agent_templates/instructions.md`, then appends the packaged touchpoint references. The verification loop and
log-browsing commands live in that shared instruction body so the user prompt can stay short. The guidance directs
agents to run the failed step or journey and keep rerunning executable Journey commands until the fix is proven. It also
directs agents that add new branching journeys to execute every requested branch target and report state/log evidence
instead of stopping at generated code, import checks, or lint. When a shared setup label is ambiguous across branches,
agents should target a branch-specific step instead of disabling branches as an iteration shortcut. Use
`--install` when a
project-level assistant file or skill should be written:

```bash
journey agent codex --install
journey agent claude --install
journey agent cursor --install
journey agent generic --install
```

Printing is the default. Install mode writes the selected assistant's default project file and refuses to replace an
existing file unless `--force` is passed. The shared source for these instructions is
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
