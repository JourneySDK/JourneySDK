from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
import urllib.request

import journeysdk as journey
import pytest
from journeysdk.models import StepNode
from journeysdk.touchpoints import docker as journey_docker

docker_compose_module = importlib.import_module(
    "docs.docker_compose_journey.docker_compose_journey"
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


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _demo_stack() -> SimpleNamespace:
    return SimpleNamespace(
        statuses={
            "app": (
                journey_docker.DockerContainerStatus(
                    service="app",
                    container_id="app-container-1",
                    container_name="journey-docker-docs-app-1",
                    state="running",
                    health="healthy",
                    exit_code=0,
                    image="demo-app:latest",
                ),
            ),
            "db": (
                journey_docker.DockerContainerStatus(
                    service="db",
                    container_id="db-container-1",
                    container_name="journey-docker-docs-db-1",
                    state="running",
                    health="healthy",
                    exit_code=0,
                    image="postgres:16-alpine",
                ),
            ),
        }
    )


def test_docker_compose_example_compiles_without_touching_docker(
    monkeypatch,
):
    def fail_run(*args, **kwargs):
        raise AssertionError("compile_journey() should not call Docker.")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("compile_journey() should not call the Docker demo app.")

    monkeypatch.setattr(journey_docker.subprocess, "run", fail_run)
    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    reloaded = importlib.reload(docker_compose_module)
    first_plan = journey.compile_journey(reloaded.docker_compose_journey)
    second_plan = journey.compile_journey(reloaded.docker_compose_journey)

    assert sorted(_case_labels(first_plan)) == sorted(
        [
            [
                "start_docker_stack",
                "assert_stack_ready",
                "capture_baseline_state",
                "increment_counter",
                "assert_increment_branch",
            ],
            [
                "start_docker_stack",
                "assert_stack_ready",
                "capture_baseline_state",
                "read_counter_state",
                "assert_restored_counter_branch",
            ],
        ]
    )
    assert sorted(_case_labels(second_plan)) == sorted(
        [
            [
                "start_docker_stack",
                "assert_stack_ready",
                "capture_baseline_state",
                "increment_counter",
                "assert_increment_branch",
            ],
            [
                "start_docker_stack",
                "assert_stack_ready",
                "capture_baseline_state",
                "read_counter_state",
                "assert_restored_counter_branch",
            ],
        ]
    )


def test_capture_baseline_state_reads_counter_payload_without_docker(
    monkeypatch: pytest.MonkeyPatch,
):
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _FakeHTTPResponse:
        requests.append(request)
        assert timeout == 5
        return _FakeHTTPResponse({"count": 0, "database": "ready"})

    monkeypatch.setenv("JOURNEY_DOCKER_DOCS_PORT", "19090")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    baseline = docker_compose_module.capture_baseline_state(_demo_stack())

    assert baseline == {
        "count": 0,
        "database": "ready",
        "app_state": "running",
        "db_health": "healthy",
    }
    assert requests[0].full_url == "http://127.0.0.1:19090/counter"
    assert requests[0].get_method() == "GET"


def test_increment_counter_parses_increment_payload_without_docker(
    monkeypatch: pytest.MonkeyPatch,
):
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _FakeHTTPResponse:
        requests.append(request)
        assert timeout == 5
        return _FakeHTTPResponse({"before": 0, "after": 1, "database": "ready"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    incremented = docker_compose_module.increment_counter(_demo_stack())

    assert incremented == {"before": 0, "after": 1}
    assert requests[0].full_url == "http://127.0.0.1:18080/counter/increment"
    assert requests[0].get_method() == "POST"


def test_assert_restored_counter_branch_checks_restored_baseline_count():
    assert (
        docker_compose_module.assert_restored_counter_branch(
            {"count": 0},
            {"count": 0, "database": "ready"},
        )
        is True
    )

    with pytest.raises(AssertionError, match="Expected restored counter 0, got 1."):
        docker_compose_module.assert_restored_counter_branch(
            {"count": 0},
            {"count": 1, "database": "ready"},
        )
