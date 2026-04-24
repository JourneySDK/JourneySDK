from __future__ import annotations

import importlib
from pathlib import Path

import journeysdk as journey
import pytest
from journeysdk.models import StepNode

pytest.importorskip("playwright.sync_api")

playwright_prompt_module = importlib.import_module(
    "docs.playwright_prompt_journey.playwright_prompt_journey"
)


def _case_labels(plan: journey.JourneyPlan) -> list[list[str]]:
    return [
        [
            node.label
            for node in case_plan.nodes
            if isinstance(node, StepNode) and node.label is not None
        ]
        for case_plan in plan.case_plans
    ]


def test_playwright_prompt_example_compiles_with_structured_result_surface():
    reloaded = importlib.reload(playwright_prompt_module)
    source = Path(reloaded.__file__).read_text(encoding="utf-8")
    first_plan = journey.compile_journey(reloaded.playwright_prompt_journey)
    second_plan = journey.compile_journey(reloaded.playwright_prompt_journey)

    assert "_playwright_" + "prompt" not in source
    assert _case_labels(first_plan) == [["capture_popup_title", "assert_prompt_result"]]
    assert _case_labels(second_plan) == [["capture_popup_title", "assert_prompt_result"]]
