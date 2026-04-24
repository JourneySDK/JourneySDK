from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "00-installation-and-cli.md",
    ROOT / "docs" / "04-browser-and-local-integrations.md",
    ROOT / "docs" / "README.md",
)


def test_base_package_includes_playwright_and_litellm_runtime_dependencies() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert any(dependency.startswith("playwright") for dependency in dependencies)
    assert any(dependency.startswith("litellm") for dependency in dependencies)


def test_install_docs_do_not_require_with_flags_for_journey_commands() -> None:
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")

        assert "--with playwright journey" not in text, path
        assert "--with litellm journey" not in text, path
        assert "uvx --from journey-sdk --with playwright journey --help" not in text, path
        assert "uvx --from journey-sdk --with playwright --with litellm journey --help" not in text, path
        assert "uv tool install journey-sdk --with playwright" not in text, path
        assert "playwright install chromium" not in text, path

    browser_docs = (ROOT / "docs" / "04-browser-and-local-integrations.md").read_text(
        encoding="utf-8"
    )
    assert "automatically download Chromium" in browser_docs
    assert "uv run journey --file docs/playwright_resume_journey/playwright_resume_journey.py" in browser_docs
