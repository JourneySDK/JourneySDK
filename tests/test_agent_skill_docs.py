from __future__ import annotations

from pathlib import Path


def _public_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_journey_developer_skill_covers_agent_development_loop():
    root = _public_root()
    skill_path = root / "skills" / "journey-developer" / "SKILL.md"

    assert skill_path.exists()

    skill_text = skill_path.read_text(encoding="utf-8")
    lower_skill_text = skill_text.lower()

    assert "--develop-step" in skill_text
    assert "--state" in skill_text
    assert "--step" in skill_text
    assert "targeted" in lower_skill_text
    assert "retry the same paused step" in lower_skill_text
    assert "target the next step" in lower_skill_text
    assert "--interactive" in skill_text
    assert "non-human agent runs" in lower_skill_text


def test_journey_developer_skill_explains_when_journey_sdk_applies():
    skill_text = (
        _public_root() / "skills" / "journey-developer" / "SKILL.md"
    ).read_text(encoding="utf-8")
    lower_skill_text = skill_text.lower()

    assert "workflow-as-code QA toolkit" in skill_text
    assert "testing long, branching, async, cross-system user journeys" in skill_text
    assert "browsers" in lower_skill_text
    assert "apis" in lower_skill_text
    assert "webhooks" in lower_skill_text
    assert "delayed side effects" in lower_skill_text
    assert "generic python scripts" in lower_skill_text
    assert "unrelated workflow automation" in lower_skill_text


def test_public_agents_reminds_authors_to_keep_skill_aligned():
    agents_text = (_public_root() / "AGENTS.md").read_text(encoding="utf-8")

    assert "skills/journey-developer/SKILL.md" in agents_text
    assert "Journey CLI behavior, docs, examples, or journey authoring guidance changes" in agents_text
