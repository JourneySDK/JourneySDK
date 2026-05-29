from __future__ import annotations

import builtins
import importlib
from pathlib import Path

import journeysdk as journey
from journeysdk.models import StepNode

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


def test_demo_site_html_reads_cloud_webhook_url_from_page_query():
    page_file = Path(simple_journey.__file__).with_name("demo_site.html")
    html = page_file.read_text(encoding="utf-8")

    assert page_file.exists()
    assert "Trigger endpoint A" in html
    assert "Store a local file" in html
    assert "mode: \"no-cors\"" in html
    assert "URLSearchParams" in html
    assert "webhookUrl" in html
    assert "localhost:8765" not in html


def test_local_file_helpers_read_and_validate_downloaded_file(
    tmp_path: Path,
    monkeypatch,
):
    stored_file = tmp_path / "stored-message.txt"
    stored_file.write_text("Stored by the journey demo page.\n", encoding="utf-8")

    monkeypatch.setattr(simple_journey, "_STORED_FILE", stored_file)

    file_info = simple_journey._local_file_contents()

    assert file_info == {
        "path": str(stored_file),
        "content": "Stored by the journey demo page.\n",
    }
    assert simple_journey._assert_local_file_contents(file_info) is None


def test_webhook_assertion_expects_get_query_payload():
    assert simple_journey._assert_endpoint_a_webhook(
        {
            "method": "GET",
            "path": "/endpoint-a",
            "query": {
                "event": ["endpoint_a"],
                "source": ["journey_demo"],
            },
        }
    ) is None


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
            "demo_homepage_ready",
            "trigger_endpoint_a_and_verify_webhook",
        ],
        [
            "demo_homepage_ready",
            "store_local_file_and_verify_contents",
        ],
    ]
    assert _case_labels(first_plan) == expected_labels
    assert _case_labels(second_plan) == expected_labels

    homepage_node = next(
        node
        for node in first_plan.case_plans[0].nodes
        if isinstance(node, StepNode) and node.label == "demo_homepage_ready"
    )
    webhook_node = next(
        node
        for node in second_plan.case_plans[0].nodes
        if isinstance(node, StepNode) and node.label == "trigger_endpoint_a_and_verify_webhook"
    )
    file_node = next(
        node
        for node in first_plan.case_plans[1].nodes
        if isinstance(node, StepNode) and node.label == "store_local_file_and_verify_contents"
    )
    assert homepage_node.args == ()
    assert webhook_node.args == ()
    assert file_node.args == ()
