from __future__ import annotations

import importlib
import urllib.request

import journeysdk as journey
import pytest
from journeysdk.models import StepNode

from journeysdk.touchpoints._email_cloud import (
    JOURNEY_CLOUD_API_KEY_ENV,
    JOURNEY_CLOUD_BASE_URL_ENV,
)
from tests._cloud_stub import serve_in_background

cloud_email_docs = importlib.import_module("docs.cloud_email_journey.cloud_email_journey")


def _case_labels(plan: journey.JourneyPlan) -> list[list[str]]:
    return [
        [
            node.label
            for node in case_plan.nodes
            if isinstance(node, StepNode) and node.label is not None
        ]
        for case_plan in plan.case_plans
    ]


def test_cloud_email_docs_compiles_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the cloud service.")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.delenv(JOURNEY_CLOUD_API_KEY_ENV, raising=False)
    monkeypatch.delenv(JOURNEY_CLOUD_BASE_URL_ENV, raising=False)

    first_plan = journey.compile_journey(cloud_email_docs.cloud_email_journey)
    second_plan = journey.compile_journey(cloud_email_docs.cloud_email_journey)

    expected_labels = [
        [
            "send_welcome_email_and_verify_delivery",
        ]
    ]
    assert _case_labels(first_plan) == expected_labels
    assert _case_labels(second_plan) == expected_labels


def test_cloud_email_docs_executes_against_local_journey_cloud(
    monkeypatch: pytest.MonkeyPatch,
):
    with serve_in_background() as cloud:
        monkeypatch.setenv(JOURNEY_CLOUD_API_KEY_ENV, cloud.api_key)
        monkeypatch.setenv(JOURNEY_CLOUD_BASE_URL_ENV, cloud.base_url)
        cloud_email_docs.reset_demo_state()

        report = journey.execute(cloud_email_docs.cloud_email_journey)

    assert [case.case_id for case in report.case_reports] == ["case_1"]
    assert cloud_email_docs.EVENTS == ["assert_welcome_email"]
