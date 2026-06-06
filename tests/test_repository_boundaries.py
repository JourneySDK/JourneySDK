from __future__ import annotations

import ast
from importlib import resources
from pathlib import Path
import re
import tomllib

from journeysdk.agent_instructions import render_agent_bootstrap, render_agent_instructions


def _public_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _workspace_root() -> Path:
    return _public_root().parent


def _tracked_text_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".venv" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ".md", ".toml", ".txt"}
        and path.name != "test_repository_boundaries.py"
    ]


def _local_markdown_links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    links: list[str] = []
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
        link = match.group(1).strip()
        if (
            not link
            or link.startswith("#")
            or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", link)
        ):
            continue
        links.append(link)
    return links


def _resolve_markdown_link(source: Path, link: str) -> Path:
    target = link.split("#", 1)[0].split("?", 1)[0]
    return (source.parent / target).resolve()


def test_public_tree_does_not_reference_private_modules_or_paths():
    root = _public_root()
    forbidden_tokens = [
        "journey_webhook_shared",
        "uv run python -m journey_cloud",
        "python -m journey_cloud",
        "journey_cloud/",
        "private/",
        "../private",
    ]

    for path in _tracked_text_files(root):
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"Found forbidden token {token!r} in {path}"
        assert "from journey_cloud" not in text, f"Found private import in {path}"
        assert "import journey_cloud" not in text, f"Found private import in {path}"


def test_base_package_includes_playwright_and_langchain_runtime_dependencies() -> None:
    pyproject = tomllib.loads((_public_root() / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert any(dependency.startswith("playwright") for dependency in dependencies)
    assert any(dependency.startswith("langchain") for dependency in dependencies)
    assert not any(dependency.startswith("litellm") for dependency in dependencies)


def test_package_data_includes_agent_instruction_templates() -> None:
    pyproject = tomllib.loads((_public_root() / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert package_data["journeysdk.agent_templates"] == ["instructions.md"]
    assert package_data["journeysdk.touchpoint_docs"] == ["*.md"]


def test_agent_instruction_templates_use_single_canonical_body() -> None:
    template_root = _public_root() / "journeysdk" / "agent_templates"

    assert (template_root / "instructions.md").is_file()
    for removed_template in ("codex.md", "claude-skill.md", "cursor.mdc", "generic.md"):
        assert not (template_root / removed_template).exists()


def test_agent_instruction_rendering_wraps_shared_body() -> None:
    body = (
        resources.files("journeysdk.agent_templates")
        .joinpath("instructions.md")
        .read_text(encoding="utf-8")
    )

    assert render_agent_instructions("generic") == body
    assert render_agent_instructions("codex") == body

    claude = render_agent_instructions("claude")
    claude_envelope, claude_body = claude.split("\n---\n\n", maxsplit=1)
    assert claude_envelope.startswith("---\n")
    assert "name:" in claude_envelope
    assert "description:" in claude_envelope
    assert claude_body == body

    cursor = render_agent_instructions("cursor")
    cursor_envelope, cursor_body = cursor.split("\n---\n\n", maxsplit=1)
    assert cursor_envelope.startswith("---\n")
    assert "description:" in cursor_envelope
    assert "globs:" in cursor_envelope
    assert "alwaysApply:" in cursor_envelope
    assert cursor_body == body


def test_agent_bootstrap_appends_touchpoint_docs_to_shared_body() -> None:
    instructions = render_agent_instructions("codex").rstrip()
    bootstrap = render_agent_bootstrap("codex")
    prefix = f"{instructions}\n\n---\n\n"

    assert bootstrap.startswith(prefix)
    appendix = bootstrap.removeprefix(prefix)
    assert appendix.startswith("# Journey SDK Touchpoint Reference")
    assert "# Journey SDK Agent Bootstrap" not in appendix


def test_agent_instruction_template_mentions_documentation_alignment() -> None:
    body = (
        resources.files("journeysdk.agent_templates")
        .joinpath("instructions.md")
        .read_text(encoding="utf-8")
    )

    required_phrases = (
        "Keep Documentation Aligned",
        "packaged agent instructions",
        "assistant skill output",
        "If no docs or instruction updates are needed",
    )

    for phrase in required_phrases:
        assert phrase in body


def test_local_markdown_links_in_public_doc_entrypoints_resolve() -> None:
    entrypoints = (
        _public_root() / "README.md",
        _public_root() / "AGENTS.md",
        _public_root() / "CONTRIBUTING.md",
        _public_root() / "docs" / "README.md",
        _public_root() / "docs" / "04-browser-and-local-integrations.md",
    )

    for path in entrypoints:
        for link in _local_markdown_links(path):
            target = _resolve_markdown_link(path, link)
            assert target.exists(), f"{path} links to missing local target {link!r}"


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.level:
                imported.add("." * node.level + node.module)
            else:
                imported.add(node.module)
    return imported


def test_core_orchestration_modules_do_not_import_feature_helpers() -> None:
    sdk = _public_root() / "journeysdk"
    core_modules = (
        "api.py",
        "validator.py",
        "planner.py",
        "executor.py",
        "state.py",
        "discovery.py",
        "cli.py",
    )
    forbidden_imports = {
        "._prompt_memory",
        "._prompt_engine",
        "._prompt_output",
        ".touchpoints",
        "journeysdk._prompt_memory",
        "journeysdk._prompt_engine",
        "journeysdk._prompt_output",
        "journeysdk.touchpoints",
    }

    for module in core_modules:
        imported = _imported_module_names(sdk / module)
        for item in imported:
            assert item not in forbidden_imports
            assert not item.startswith(".touchpoints.")
            assert not item.startswith("journeysdk.touchpoints.")


def test_public_tree_does_not_reference_legacy_namespace() -> None:
    root = _public_root()
    legacy_segment = bytes((116, 111, 111, 108, 115)).decode()
    forbidden_tokens = (f"journeysdk.{legacy_segment}", f"journeysdk/{legacy_segment}")

    for path in _tracked_text_files(root):
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"Found legacy SDK namespace {token!r} in {path}"


def test_root_agents_stays_workspace_level() -> None:
    text = (_workspace_root() / "AGENTS.md").read_text(encoding="utf-8")
    for token in ("planner.py", "executor.py", "_prompt_memory.py", "touchpoint modules"):
        assert token not in text


def test_prompt_memory_owns_its_planning_hook() -> None:
    root = _public_root()
    assert not (root / "journeysdk" / "_prompt_memory_planning.py").exists()

    prompt_memory_text = (root / "journeysdk" / "_prompt_memory.py").read_text(
        encoding="utf-8"
    )
    assert "_register_planning_step_hook" in prompt_memory_text


def test_planner_hook_api_stays_minimal() -> None:
    planner_text = (_public_root() / "journeysdk" / "planner.py").read_text(
        encoding="utf-8"
    )
    for name in (
        "_PlanSessionHook",
        "_CompilePlanningHook",
        "_PlanningHookFactory",
        "_register_planning_hook_factory",
        "_make_compile_planning_hooks",
    ):
        assert name not in planner_text


def test_prompt_memory_planning_state_stays_cohesive() -> None:
    prompt_memory_text = (
        _public_root() / "journeysdk" / "_prompt_memory.py"
    ).read_text(encoding="utf-8")
    assert "_PromptMemoryPlanningState" in prompt_memory_text
    for name in (
        "_prompt_memory_refs_by_name",
        "_prompt_memory_refs_seen_in_session",
        "planning_session_state",
        'getattr(session, "planning_state")',
        'getattr(session, "planning_session_state")',
        "cast(",
    ):
        assert name not in prompt_memory_text
