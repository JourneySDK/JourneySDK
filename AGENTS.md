# Journey SDK Agent Instructions

This repository keeps assistant guidance in one canonical packaged template:

- `journeysdk/agent_templates/instructions.md`

Before adding, changing, or running Journey specs in this repository, read that file or ask the installed CLI for the
Codex guidance packet:

```bash
journey agent codex
```

For downstream projects, keep using the packaged install flow when a project-level assistant file or skill should be
written:

```bash
journey agent codex --install
```

The CLI also renders Claude, Cursor, and generic variants from the same canonical body.

For every SDK change, review and align the public docs and instruction surfaces that describe the touched behavior:
`docs/`, `README.md`, `CONTRIBUTING.md`, this `AGENTS.md`, `journeysdk/touchpoint_docs/`, and
`journeysdk/agent_templates/instructions.md`. Keep that packaged template as the single source for rendered Codex,
Claude skill, Cursor, and generic assistant instructions. If no doc update is needed, report that the relevant surfaces
were reviewed.

When authoring or reviewing journeys, treat each `step(...)` as a durable replay, retry, target, or state-passing
boundary. Do not split one real user outcome into separate click/form-fill/poll/assertion steps; put those details in a
helper and expose one coarse user-flow step.
