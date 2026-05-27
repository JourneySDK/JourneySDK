# Journey SDK Agent Instructions

This repository keeps assistant guidance in one canonical packaged template:

- `journeysdk/agent_templates/instructions.md`

Before adding, changing, or running Journey specs in this repository, read that file or ask the installed CLI for the
Codex rendering:

```bash
journey --agent-instructions codex
```

For downstream projects, keep using the packaged install flow when a project-level assistant file should be written:

```bash
journey --agent-instructions codex --install-agent-instructions
```

The CLI also renders Claude, Cursor, and generic variants from the same canonical body.
