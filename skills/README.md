# Journey SDK Agent Instructions

This directory contains the source-distributed Claude Code skill for working on Journey SDK. Installed Journey SDK can
also print or write assistant guidance for common coding assistants:

```bash
journey --agent-instructions codex
journey --agent-instructions claude --install-agent-instructions
journey --agent-instructions cursor --install-agent-instructions
journey --agent-instructions generic
```

Printing is the default. Install mode writes the selected assistant's default project file and refuses to replace an
existing file unless `--force-agent-instructions` is passed.

## Source Skill

Claude Code: use `journey --agent-instructions claude --install-agent-instructions`, or copy/symlink
`skills/journey-developer` into your Claude Code skills directory when working from a source checkout.
