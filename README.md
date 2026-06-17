# Journey SDK

Replay one meaningful user-journey step while your coding agent works, then broaden to branch and full-flow verification.

Journey SDK is a Python verification layer for agentic coding loops. A Journey spec divides a real user flow into
intentional replayable steps: checkout and email assertion, onboarding and webhook assertion, back-office state check,
or another slice that should recover together. While an agent edits code, it can rerun only the step it is developing
with `journey dev <step>`, inspect correlated evidence, and then finish with `journey verify --step` or
`journey verify`.

The same spec also covers branching flows efficiently. Shared setup runs once as a replay anchor, later branches use
`branch(replay_from=...)`, and Journey can verify all requested branch targets without forcing each path to rebuild the
whole journey from the beginning. Browser automation, local Docker services, hosted email, hosted webhooks, and
app-specific checks stay in ordinary Python.

## Why Journey

- **Develop one step while coding**: run `journey dev receive_confirmation_email --file journeys/checkout_journey.py`
  after each edit. Journey keeps state around the replay boundary so the agent can verify the same late-flow slice
  repeatedly instead of restarting the whole journey.
- **Verify branches through shared replay anchors**: author common setup once, then use `branch(replay_from=...)` for
  alternate paths. `journey verify --step <branch_step>` selects the case that reaches one branch target;
  `journey verify` runs the whole journey and its branches.
- **Bring integrations into the agent loop**: Journey touchpoints let a step drive the browser, start Docker Compose,
  wait for hosted email, capture webhook callbacks, and assert app-specific side effects in ordinary Python.
- **Return executable evidence**: `journey evidence` lists traces, videos, logs, and touchpoint artifacts by run, case,
  branch, and step so an agent can report exactly what passed or failed.

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
journey dev --help
journey verify --help
journey evidence --help
journey touchpoints all
journey agent --help
```

See [Installation And CLI](docs/00-installation-and-cli.md) for the complete install guide, self-healing CLI help,
editable installs, browser setup, and local package smoke testing.

Agent-authored journeys are not verified by code generation alone. After adding or changing a Journey, the agent should
run an executable `journey dev ...` or `journey verify ...` command, iterate on the failing step until it passes, then
broaden to a branch target or the full journey with fresh evidence whenever infrastructure permits. If the app is not
running, the agent should follow documented local startup commands before declaring the run environment-blocked.
Before authoring application coverage, agents should first establish whether the app is external or local, use the
Docker touchpoint for local app/services, and identify meaningful step or branch boundaries before writing the spec. If
repo inspection cannot answer those questions, the agent should ask the developer.
Agents should list secret configuration by key presence only and must not print credential values while discovering the
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
failing journey or focused step, rerun `journey dev <step>` until executable evidence passes, and report the
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

Default install destinations:

| Target | Path | Invocation |
| --- | --- | --- |
| `codex` | `.agents/skills/journey/SKILL.md` | `$journey` or `/skills` |
| `claude` | `.claude/skills/journey/SKILL.md` | `/journey <task>` |
| `cursor` | `.cursor/skills/journey/SKILL.md` | `/journey <task>` |
| `generic` | `JOURNEY_AGENT.md` | read or reference the file directly |

If you are authoring a journey by hand, use [Getting Started](docs/01-getting-started.md). It covers imports,
top-level `@journey` functions, running one file, selecting one journey, and JSON Lines output for tools.
If you want to extend browser coverage from a rendered app state, use
`journey dev <step_label> --file journeys/<feature>_journey.py`.
It executes the Journey through that step, inspects the live page, captures rendered-page artifacts, lists actionable
artifacts, lists candidate flows and actionable controls, and prints instructions for adding the next branch. Omit
`<step_label>` to pause after the first step. For a new empty spec, run
`journey dev --file journeys/<feature>_journey.py --url http://127.0.0.1:3000` to initialize a minimal browser Journey
and inspect its first page. Agents should use `--output jsonl`, prefer `dev_result.candidate_flows`, inspect
the rendered-page artifact paths when uncertain, edit the Journey source themselves, and then prove the added branch
with `journey dev` or `journey verify`.

## Authoring Guides

- [Branching and Step Loops](docs/02-branching-and-targeted-runs.md): canonical guidance for adding journey specs,
  choosing coarse durable step boundaries, using `branch(replay_from=...)`, and iterating with `journey dev` and
  `journey verify --step`.
- [Retries and Resume](docs/03-retries-and-resume.md): state management, retry boundaries, interrupted runs, and resume.
- [Browser and Local Touchpoints](docs/04-browser-and-local-integrations.md): browser, local file, Docker Compose, and
  browser prompt tutorials.
- [Journey Cloud Touchpoints](docs/05-journey-cloud-integrations.md): hosted webhook and email examples.
- [Debugging and Failure Modes](docs/06-debugging-and-failure-modes.md): failure reports, logs, and `--fail-fast`.

The docs index is [Journey Docs](docs/README.md).

## CLI Help

Use `journey --help` as the short command index. Use `journey dev --help` for focused development and browser branch
guidance, `journey verify --help` for branch/full verification, `journey evidence --help` for artifact inspection, and
`journey agent --help` before printing or installing assistant guidance.

## Touchpoint References

Detailed touchpoint API references are packaged with the SDK so they are available in downstream projects and `uvx`
runs:

```bash
journey touchpoints browser
journey touchpoints docker
journey touchpoints email
journey touchpoints webhook
journey touchpoints http
journey touchpoints all
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
evidence commands live in that shared instruction body so the user prompt can stay short. The guidance directs
agents to run the failed step or journey and keep rerunning executable Journey commands until the fix is proven. It also
directs agents that add new branching journeys to execute every requested branch target and report state/evidence output
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
existing file unless `--force` is passed. The Claude and Cursor installs are `journey` skills invoked as
`/journey <task>`. The Codex install is a repo-shared skill invoked with `$journey` or through `/skills`, because Codex
custom slash prompts are user-local and deprecated. These destinations follow the current public guidance for
[Claude Code skills](https://docs.anthropic.com/en/docs/claude-code/skills),
[Cursor skills](https://cursor.com/docs/skills), and
[Codex skills](https://developers.openai.com/codex/codex-manual.md). The shared source for these instructions is
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
