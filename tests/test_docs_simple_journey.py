from __future__ import annotations

import builtins
import importlib
from pathlib import Path

import journey
from journey.models import StepNode

simple_journey = importlib.import_module("docs.simple_journey.simple_journey")


def _case_labels(plan: journey.JourneyPlan) -> list[list[str]]:
    return [
        [
            node.label
            for node in case_plan.nodes
            if isinstance(node, StepNode) and node.label is not None
        ]
        for case_plan in plan.case_plans
    ]


def test_demo_site_html_contains_hardcoded_values():
    page_file = Path(simple_journey.__file__).with_name("demo_site.html")
    html = page_file.read_text(encoding="utf-8")

    assert page_file.exists()
    assert "Trigger endpoint A" in html
    assert "Store a local file" in html
    assert "mode: \"no-cors\"" in html
    assert "http://localhost:8765/endpoint-a" in html


def test_local_file_helpers_read_and_validate_downloaded_file(
    tmp_path: Path,
    monkeypatch,
):
    stored_file = tmp_path / "stored-message.txt"
    stored_file.write_text("Stored by the journey demo page.\n", encoding="utf-8")

    monkeypatch.setattr(simple_journey, "_STORED_FILE", stored_file)

    file_info = simple_journey.local_file_is_written()

    assert file_info == {
        "path": str(stored_file),
        "content": "Stored by the journey demo page.\n",
    }
    assert simple_journey.assert_local_file_contents(file_info) is True


def test_webhook_assertion_expects_get_query_payload():
    assert simple_journey.assert_endpoint_a_webhook(
        {
            "method": "GET",
            "path": "/endpoint-a",
            "query": {
                "event": ["endpoint_a"],
                "source": ["journey_demo"],
            },
        }
    ) is True


def test_simple_journey_compiles_without_importing_playwright_and_keeps_structure(
    monkeypatch,
):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "playwright" or name.startswith("playwright."):
            raise AssertionError("compile_journey() should not import Playwright.")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    first_plan = journey.compile_journey(simple_journey.simple_journey)
    second_plan = journey.compile_journey(simple_journey.simple_journey)

    expected_labels = [
        [
            "assert_demo_homepage",
            "click_trigger_endpoint_a",
            "receive_webhook_endpoint_a",
            "assert_endpoint_a_webhook",
        ],
        [
            "assert_demo_homepage",
            "click_store_local_file",
            "local_file_is_written",
            "assert_local_file_contents",
        ],
    ]
    assert _case_labels(first_plan) == expected_labels
    assert _case_labels(second_plan) == expected_labels

    homepage_node = next(
        node
        for node in first_plan.case_plans[0].nodes
        if isinstance(node, StepNode) and node.label == "assert_demo_homepage"
    )
    click_webhook_node = next(
        node
        for node in second_plan.case_plans[0].nodes
        if isinstance(node, StepNode) and node.label == "click_trigger_endpoint_a"
    )
    click_file_node = next(
        node
        for node in first_plan.case_plans[1].nodes
        if isinstance(node, StepNode) and node.label == "click_store_local_file"
    )
    assert homepage_node.args == ()
    assert click_webhook_node.args == ()
    assert click_file_node.args == ()

    receive_node = next(
        node
        for node in first_plan.case_plans[0].nodes
        if isinstance(node, StepNode) and node.label == "receive_webhook_endpoint_a"
    )
    assert receive_node.args == ()
    assert receive_node.retry is None
