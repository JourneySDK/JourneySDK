"""Assistant instruction templates packaged with Journey SDK."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_TEMPLATE_PACKAGE = "journeysdk.agent_templates"
_BODY_TEMPLATE = "instructions.md"

_SUPPORTED_TARGETS = ("codex", "claude", "cursor", "generic")

_CLAUDE_ENVELOPE = """---
name: journey-developer
description: Use Journey SDK as the end-to-end test layer for real user journeys. Use whenever code changes should be verified through a user flow, when implementing features that should extend or add journey specs, when a Journey SDK journey uses journeysdk primitives or journeysdk.touchpoints, or when iterating quickly with journey --develop-step, --step, default state, and JSONL output.
---

"""

_CURSOR_ENVELOPE = """---
description: Use Journey SDK as the end-to-end test layer for real user journeys; extend specs for new user-facing features and verify with targeted CLI loops.
globs: "**/*.py"
alwaysApply: false
---

"""

_ENVELOPES = {
    "codex": "",
    "claude": _CLAUDE_ENVELOPE,
    "cursor": _CURSOR_ENVELOPE,
    "generic": "",
}

_DEFAULT_TARGETS = {
    "codex": Path("AGENTS.md"),
    "claude": Path(".claude") / "skills" / "journey-developer" / "SKILL.md",
    "cursor": Path(".cursor") / "rules" / "journey-developer.mdc",
    "generic": Path("JOURNEY_AGENT.md"),
}


def supported_agent_instruction_targets() -> tuple[str, ...]:
    """Return supported assistant target names in CLI display order."""

    return _SUPPORTED_TARGETS


def default_agent_instruction_path(target: str) -> Path:
    """Return the default project-relative install path for an assistant target."""

    return _DEFAULT_TARGETS[_normalize_target(target)]


def render_agent_instructions(target: str) -> str:
    """Load the packaged assistant guidance for an assistant target."""

    normalized_target = _normalize_target(target)
    body = (
        resources.files(_TEMPLATE_PACKAGE)
        .joinpath(_BODY_TEMPLATE)
        .read_text(encoding="utf-8")
    )
    return f"{_ENVELOPES[normalized_target]}{body}"


def render_agent_bootstrap(target: str) -> str:
    """Render a complete agent bootstrap packet for a Journey SDK loop."""

    normalized_target = _normalize_target(target)
    from .touchpoint_references import render_touchpoint_docs

    instructions = render_agent_instructions(normalized_target).rstrip()
    touchpoint_docs = render_touchpoint_docs("all").rstrip()
    return (
        f"{instructions}\n\n"
        "---\n\n"
        "# Journey SDK Agent Bootstrap\n\n"
        f"Target assistant: `{normalized_target}`\n\n"
        "Use this packet when an AI coding agent needs to add, debug, or verify a real user journey. "
        "Journey SDK turns end-to-end user flows into durable, resumable verification loops that agents can run "
        "narrowly while editing and broadly before finishing.\n\n"
        "## Canonical Agent Loop\n\n"
        "1. Read the installed Journey instructions and existing journey specs.\n"
        "2. Inspect the plan before executing any browser, Docker, email, webhook, or app-mutating work.\n"
        "3. Run the one late step or branch under active development with `--develop-step`.\n"
        "4. Fix code or the journey, then rerun the same `--develop-step` target until it is green.\n"
        "5. Use browser recordings, traces, stdout, JSONL, and touchpoint payloads as evidence.\n"
        "6. Broaden to the target step with `--no-state`, then the full journey with `--no-state`.\n"
        "7. Report the exact Journey command, target, result, artifacts, and any remaining broader checks.\n\n"
        "## Copy-Paste Commands\n\n"
        "```bash\n"
        f"journey --agent-instructions {normalized_target} --install-agent-instructions\n"
        "journey --file journeys/<feature>_journey.py --plan-only\n"
        "journey --file journeys/<feature>_journey.py --develop-step <target_step>\n"
        "journey --file journeys/<feature>_journey.py --step <target_step> --no-state\n"
        "journey --file journeys/<feature>_journey.py --no-state\n"
        "journey recordings\n"
        "```\n\n"
        "Use `--output jsonl` when another tool needs machine-readable events. Avoid `--interactive` for non-human "
        "agent runs unless a human explicitly wants a pause prompt.\n\n"
        "## Touchpoint Rule Of Thumb\n\n"
        "Keep Journey steps coarse and user-centered. Use touchpoints inside those steps for the real systems involved "
        "in the flow: browser, Docker/local services, hosted email, hosted webhooks, HTTP checks, and app-specific "
        "helpers. Available Journey Cloud touchpoints today are hosted email inboxes and hosted webhook endpoints; "
        "future cloud-hosted resources should follow the same durable step and ownership model.\n\n"
        "---\n\n"
        f"{touchpoint_docs}\n"
    )


def install_agent_instructions(
    target: str,
    *,
    root: Path,
    force: bool = False,
) -> Path:
    """Write packaged assistant guidance to the target's default project path."""

    destination = root / default_agent_instruction_path(target)
    if destination.exists() and not force:
        raise FileExistsError(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_agent_instructions(target), encoding="utf-8")
    return destination


def _normalize_target(target: str) -> str:
    if target not in _SUPPORTED_TARGETS:
        supported = ", ".join(supported_agent_instruction_targets())
        raise ValueError(
            f"Unsupported assistant target {target!r}. Choose one of: {supported}."
        )
    return target
