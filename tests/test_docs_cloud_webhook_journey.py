from __future__ import annotations

import importlib
import urllib.request

import journey
import pytest
from journey.models import StepNode

from journey.tools._webhook_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from tests._cloud_stub import serve_in_background

cloud_webhook_docs = importlib.import_module("docs.cloud_webhook_journey.cloud_webhook_journey")


def _case_labels(plan: journey.JourneyPlan) -> list[list[str]]:
    return [
        [
            node.label
            for node in case_plan.nodes
            if isinstance(node, StepNode) and node.label is not None
        ]
        for case_plan in plan.case_plans
    ]


def test_cloud_webhook_docs_compiles_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the cloud service.")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    first_plan = journey.compile_journey(cloud_webhook_docs.cloud_webhook_journey)
    second_plan = journey.compile_journey(cloud_webhook_docs.cloud_webhook_journey)

    expected_labels = [
        [
            "get_webhook_invoice_paid",
            "send_invoice_paid_webhook_later",
            "receive_webhook_invoice_paid",
            "assert_invoice_paid_webhook",
        ]
    ]
    assert _case_labels(first_plan) == expected_labels
    assert _case_labels(second_plan) == expected_labels


def test_cloud_webhook_docs_executes_against_local_journey_cloud(
    monkeypatch: pytest.MonkeyPatch,
):
    with serve_in_background() as cloud:
        monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, cloud.api_key)
        monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, cloud.base_url)
        cloud_webhook_docs.reset_demo_state()

        report = journey.execute(cloud_webhook_docs.cloud_webhook_journey)

    assert [case.case_id for case in report.case_reports] == ["case_1"]
    assert cloud_webhook_docs.EVENTS == [
        "send_invoice_paid_webhook_later:"
        + report.case_reports[0].records[0].result.url,  # type: ignore[union-attr]
        "assert_invoice_paid_webhook",
    ]
