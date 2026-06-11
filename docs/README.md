# Journey Docs

This directory is the runnable handbook for Journey SDK. Run commands from the repository root unless a page says
otherwise.

## Canonical Guides

1. [Installation And CLI](00-installation-and-cli.md): package install, persistent CLI setup, CLI flags, browser setup,
   Ctrl-C behavior, and assistant/touchpoint documentation commands.
2. [Getting Started](01-getting-started.md): the first Journey spec, running one file, selecting one journey, and JSON
   Lines output.
3. [Branching And Step Loops](02-branching-and-targeted-runs.md): adding journey specs, choosing coarse durable step
   and branch boundaries, `branch(replay_from=...)`, `journey loop`, and `journey verify --step`.
4. [Retries And Resume](03-retries-and-resume.md): state management, retry loops, replay boundaries, and resumable runs.
5. [Browser And Local Touchpoints](04-browser-and-local-integrations.md): browser, local file, Docker Compose, and
   browser prompt tutorials.
6. [Journey Cloud Touchpoints](05-journey-cloud-integrations.md): hosted webhook and email examples.
7. [Debugging And Failure Modes](06-debugging-and-failure-modes.md): failure reports, evidence, fail-fast runs, and
   troubleshooting.

## Packaged References

The CLI `--help` surfaces are the self-contained command manuals. Coding agents should run the relevant help command
before choosing or repairing CLI usage:

```bash
journey --help
journey loop --help
journey verify --help
journey evidence --help
journey agent --help
```

Detailed touchpoint API references are packaged with the SDK and available from any installed `journey` CLI:

```bash
journey touchpoints browser
journey touchpoints docker
journey touchpoints email
journey touchpoints webhook
journey touchpoints http
journey touchpoints all
```

AI coding assistant guidance is packaged separately:

```text
Use Journey SDK for this task: <describe the user flow>. Run journey agent codex first.
```

```bash
journey agent codex
journey agent claude
journey agent cursor
journey agent generic
journey agent codex --install
journey agent claude --install
journey agent cursor --install
journey agent generic --install
```

Give the one-line prompt to the assistant and let it run `journey agent <target>` itself. Use the default print mode
when an agent needs the complete verification loop plus touchpoint references in one response.
Use `journey agent <target> --install` when the project should receive a persistent assistant instruction file or skill.
Agents fixing a failure should run the failed journey or focused `journey loop <step>` retry until executable evidence
passes. Agents adding new branching journeys should execute every requested branch target and finish with fresh
`journey verify --fresh` evidence when infrastructure permits; generated code, import checks, lint, or test discovery alone are not
Journey verification. If a local app is not running, agents should follow documented startup commands before declaring
the run environment-blocked, and should inspect configuration without printing secret values or dumping secret-bearing
files. For auth, seed data, payments, and similar setup, agents should inspect existing E2E helpers before guessing
credentials or magic codes, and request approval before mutating external services. Ambiguous shared setup labels in a
branching journey should be handled with branch-specific targets instead of disabling branches.

The canonical source files are `journeysdk/touchpoint_docs/*.md` and
`journeysdk/agent_templates/instructions.md`.

When changing SDK behavior, CLI flags, touchpoints, journey authoring guidance, or assistant workflows, keep this docs
tree aligned with [the public README](../README.md), [AGENTS.md](../AGENTS.md),
[CONTRIBUTING.md](../CONTRIBUTING.md), packaged touchpoint docs, and the canonical agent template so generated
assistant instructions and skills match the written docs.

## Runnable Source Map

- `docs/first_journey/first_journey.py`
- `docs/selection_journeys/selection_journeys.py`
- `docs/branching_journey/branching_journey.py`
- `docs/rehydration_journey/rehydration_journey.py`
- `docs/retry_journey/retry_journey.py`
- `docs/resume_journey/resume_journey.py`
- `docs/simple_journey/simple_journey.py`
- `docs/docker_compose_journey/docker_compose_journey.py`
- `docs/browser_resume_journey/browser_resume_journey.py`
- `docs/browser_prompt_journey/browser_prompt_journey.py`
- `docs/cloud_webhook_journey/cloud_webhook_journey.py`
- `docs/cloud_email_journey/cloud_email_journey.py`
- `docs/fail_fast_journeys/fail_fast_journeys.py`
