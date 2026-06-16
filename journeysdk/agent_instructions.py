"""Assistant instruction templates packaged with Journey SDK."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_TEMPLATE_PACKAGE = "journeysdk.agent_templates"
_BODY_TEMPLATE = "instructions.md"

_SUPPORTED_TARGETS = ("codex", "claude", "cursor", "generic")

_CLAUDE_ENVELOPE = """---
name: journey-developer
description: Use Journey SDK to replay one meaningful user-journey step while coding, then broaden to branch/full verification with journey dev, journey verify, touchpoints, and executable evidence. Use whenever code changes should be proven through a user flow or a Journey SDK spec/touchpoint changes.
---

"""

_CURSOR_ENVELOPE = """---
description: Use Journey SDK to replay one meaningful user-journey step while coding, then verify branch/full flows with executable evidence.
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
