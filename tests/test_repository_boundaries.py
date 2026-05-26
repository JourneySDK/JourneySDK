from __future__ import annotations

import ast
from importlib import resources
from pathlib import Path
import tomllib


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

    assert package_data["journeysdk.agent_templates"] == ["*.md", "*.mdc"]


def test_packaged_claude_skill_contains_journey_developer_metadata() -> None:
    skill = (
        resources.files("journeysdk.agent_templates")
        .joinpath("claude-skill.md")
        .read_text(encoding="utf-8")
    )

    assert "name: journey-developer" in skill
    assert "## Develop One Step" in skill
    assert "journey --agent-instructions claude --install-agent-instructions" in skill


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
