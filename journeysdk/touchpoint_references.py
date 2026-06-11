"""Packaged touchpoint reference documentation."""

from __future__ import annotations

from importlib import resources

_DOC_PACKAGE = "journeysdk.touchpoint_docs"
_SUPPORTED_TARGETS = ("docker", "browser", "email", "webhook", "http")
_ALL_TARGET = "all"


def supported_touchpoint_doc_targets() -> tuple[str, ...]:
    """Return supported touchpoint documentation targets in CLI display order."""

    return (*_SUPPORTED_TARGETS, _ALL_TARGET)


def render_touchpoint_docs(target: str) -> str:
    """Load packaged touchpoint reference documentation."""

    normalized_target = _normalize_target(target)
    if normalized_target == _ALL_TARGET:
        parts = [_touchpoint_index()]
        parts.extend(_load_doc(name) for name in _SUPPORTED_TARGETS)
        return "\n\n".join(part.rstrip() for part in parts) + "\n"
    return _load_doc(normalized_target)


def _load_doc(target: str) -> str:
    return (
        resources.files(_DOC_PACKAGE)
        .joinpath(f"{target}.md")
        .read_text(encoding="utf-8")
    )


def _touchpoint_index() -> str:
    targets = ", ".join(f"`{name}`" for name in _SUPPORTED_TARGETS)
    return (
        "# Journey SDK Touchpoint Reference\n\n"
        f"Available touchpoint references: {targets}.\n\n"
        "Use `journey touchpoints <name>` to print one reference."
    )


def _normalize_target(target: str) -> str:
    if target not in supported_touchpoint_doc_targets():
        supported = ", ".join(supported_touchpoint_doc_targets())
        raise ValueError(
            f"Unsupported touchpoint documentation target {target!r}. "
            f"Choose one of: {supported}."
        )
    return target
