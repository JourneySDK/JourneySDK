# Journey Docs

This directory is the runnable handbook for Journey SDK. Run commands from the repository root unless a page says
otherwise.

## Canonical Guides

1. [Installation And CLI](00-installation-and-cli.md): package install, persistent CLI setup, CLI flags, browser setup,
   Ctrl-C behavior, and assistant/touchpoint documentation commands.
2. [Getting Started](01-getting-started.md): the first Journey spec, running one file, selecting one journey, and JSON
   Lines output.
3. [Branching And Targeted Runs](02-branching-and-targeted-runs.md): adding journey specs, choosing coarse durable step
   and branch boundaries, `branch(start_from=...)`, `--plan-only`, `--step`, and `--develop-step`.
4. [Retries And Resume](03-retries-and-resume.md): state management, retry loops, replay boundaries, and resumable runs.
5. [Browser And Local Touchpoints](04-browser-and-local-integrations.md): browser, local file, Docker Compose, and
   browser prompt tutorials.
6. [Journey Cloud Touchpoints](05-journey-cloud-integrations.md): hosted webhook and email examples.
7. [Debugging And Failure Modes](06-debugging-and-failure-modes.md): failure reports, logs, fail-fast runs, and
   troubleshooting.

## Packaged References

Detailed touchpoint API references are packaged with the SDK and available from any installed `journey` CLI:

```bash
journey --touchpoint-docs browser
journey --touchpoint-docs docker
journey --touchpoint-docs email
journey --touchpoint-docs webhook
journey --touchpoint-docs http
journey --touchpoint-docs all
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
