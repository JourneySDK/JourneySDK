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
    assert "Use Journey SDK when a change should be verified against a real user journey" in skill
    assert "## Develop One Step" in skill
    assert "journey --file journeys/<feature>_journey.py --develop-step target_label" in skill


def test_all_agent_instruction_templates_explain_when_to_use_journey() -> None:
    templates = resources.files("journeysdk.agent_templates")
    for name in ("claude-skill.md", "codex.md", "cursor.mdc", "generic.md"):
        text = templates.joinpath(name).read_text(encoding="utf-8")
        assert (
            "Use Journey SDK when a change should be verified against a real user journey"
            in text
        ), name
        assert "fast partial verification" in text, name
        assert "journeys/<feature>_journey.py" in text, name
        assert "add new specs under `journeys/<feature>_journey.py`" in text, name
        assert "Each `step(...)` should encapsulate a meaningful, retryable part of the user journey" in text, name
        assert "clear_basket_and_add_items" in text, name
        assert "branch(start_from=step_result)" in text, name
        assert "Step function names are stable CLI labels" in text, name
        assert "journey --file journeys/<feature>_journey.py --develop-step target_label" in text, name
        assert "journey --file journeys/<feature>_journey.py --step target_label" in text, name
        assert "journey --file journeys/<feature>_journey.py" in text, name
        assert "## Use Touchpoints" in text, name
        assert "Touchpoints are systems a step talks to" in text, name
        assert "steps remain the durable retry/replay boundary" in text, name
        assert "`journeysdk.touchpoints`" in text, name
        assert "browser, email, webhook, and Docker Compose touchpoints" in text, name
        assert "app-specific touchpoints as plain Python helper functions" in text, name
        assert "Acquire live resources inside step execution" in text, name
        assert "serializable or rehydratable handles" in text, name
        assert "open_page" in text, name
        assert "JourneyBrowserPage" in text, name
        assert "page.prompt(..., memory=...)" in text, name
        assert "--no-browser-recording" in text, name
        assert "get_email_inbox" in text, name
        assert "send_email" in text, name
        assert "wait_for_email" in text, name
        assert "JOURNEY_CLOUD_API_KEY" in text, name
        assert "JOURNEY_CLOUD_BASE_URL" in text, name
        assert "get_webhook_endpoint" in text, name
        assert "wait_for_webhook_request" in text, name
        assert "run_docker" in text, name
        assert "DockerLogMatcher" in text, name


def test_public_docs_explain_journey_spec_step_and_branch_guidance() -> None:
    readme = (_public_root() / "README.md").read_text(encoding="utf-8")
    branching_docs = (
        _public_root() / "docs" / "02-branching-and-targeted-runs.md"
    ).read_text(encoding="utf-8")
    combined = readme + "\n" + branching_docs

    assert "Adding Journey Specs" in combined
    assert "journeys/<feature>_journey.py" in combined
    assert "Each `step(...)` should encapsulate a meaningful, retryable part of the user journey" in combined
    assert "clear_basket_and_add_items" in combined
    assert "Stable step function names become CLI labels" in combined
    assert "Use `branch(...)` for alternate user paths after shared setup" in combined
    assert "branch(start_from=step_result)" in combined
    assert (
        "Values crossing replay boundaries must be pickle-serializable or implement "
        "Journey's rehydration protocol"
    ) in combined


def test_public_docs_explain_touchpoint_efficiency_guidance() -> None:
    docs_readme = (_public_root() / "docs" / "README.md").read_text(encoding="utf-8")

    assert "A touchpoint is different from a step" in docs_readme
    assert "A step is the durable unit Journey can save, retry, resume, or replay" in docs_readme
    assert "touchpoint is what that step talks to" in docs_readme
    assert "keep live resource acquisition inside step functions" in docs_readme
    assert "`journeysdk.touchpoints`" in docs_readme
    assert "open_page(...)" in docs_readme
    assert "get_email_inbox()" in docs_readme
    assert "wait_for_webhook_request(...)" in docs_readme
    assert "run_docker(...)" in docs_readme
    assert "DockerLogMatcher" in docs_readme
    assert "targeted `--develop-step`" in docs_readme


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
