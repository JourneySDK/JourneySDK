"""Assistant instruction templates packaged with Journey SDK."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_TEMPLATE_PACKAGE = "journeysdk.agent_templates"

_TEMPLATE_FILES = {
    "codex": "codex.md",
    "claude": "claude-skill.md",
    "cursor": "cursor.mdc",
    "generic": "generic.md",
}

_DEFAULT_TARGETS = {
    "codex": Path("AGENTS.md"),
    "claude": Path(".claude") / "skills" / "journey-developer" / "SKILL.md",
    "cursor": Path(".cursor") / "rules" / "journey-developer.mdc",
    "generic": Path("JOURNEY_AGENT.md"),
}


def supported_agent_instruction_targets() -> tuple[str, ...]:
    """Return supported assistant target names in CLI display order."""

    return tuple(_TEMPLATE_FILES)


def default_agent_instruction_path(target: str) -> Path:
    """Return the default project-relative install path for an assistant target."""

    return _DEFAULT_TARGETS[_normalize_target(target)]


def render_agent_instructions(target: str) -> str:
    """Load the packaged assistant guidance for an assistant target."""

    template = _TEMPLATE_FILES[_normalize_target(target)]
    return (
        resources.files(_TEMPLATE_PACKAGE)
        .joinpath(template)
        .read_text(encoding="utf-8")
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
    if target not in _TEMPLATE_FILES:
        supported = ", ".join(supported_agent_instruction_targets())
        raise ValueError(
            f"Unsupported assistant target {target!r}. Choose one of: {supported}."
        )
    return target
