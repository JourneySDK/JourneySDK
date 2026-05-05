from __future__ import annotations

import ast
from pathlib import Path
import tomllib


def _public_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def test_planner_has_no_prompt_memory_dependency() -> None:
    planner_path = _public_root() / "journeysdk" / "planner.py"
    tree = ast.parse(planner_path.read_text(encoding="utf-8"), filename=str(planner_path))

    forbidden_modules = {"_prompt_memory", "journeysdk._prompt_memory"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name for alias in node.names}
            assert not imported & forbidden_modules
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            module = "." * node.level + node.module
            assert node.module not in forbidden_modules
            assert module not in {f".{name}" for name in forbidden_modules}

    forbidden_names = {
        "PromptMemoryReference",
        "collect_prompt_memory_references",
        "format_duplicate_prompt_memory_error",
        "prompt_memory_refs_by_name",
    }
    planner_text = planner_path.read_text(encoding="utf-8")
    for name in forbidden_names:
        assert name not in planner_text


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
