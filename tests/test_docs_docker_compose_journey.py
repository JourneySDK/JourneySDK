from __future__ import annotations

import importlib

import journey
from journey.models import StepNode
from journey.tools import docker as journey_docker

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


def test_docker_compose_example_compiles_without_touching_docker(
    monkeypatch,
):
    original_run = journey_docker.subprocess.run

    def fail_run(*args, **kwargs):
        raise AssertionError("compile_journey() should not call Docker.")

    monkeypatch.setattr(journey_docker.subprocess, "run", fail_run)

    reloaded = importlib.reload(docker_compose_module)
    first_plan = journey.compile_journey(reloaded.docker_compose_journey)
    second_plan = journey.compile_journey(reloaded.docker_compose_journey)

    monkeypatch.setattr(journey_docker.subprocess, "run", original_run)
    assert sorted(_case_labels(first_plan)) == sorted(
        [
            [
                "run_docker",
                "assert_stack_running",
                "capture_stack_summary",
                "assert_running_branch",
            ],
            [
                "run_docker",
                "assert_stack_running",
                "capture_stack_summary",
                "assert_boot_logs_branch",
            ],
        ]
    )
    assert sorted(_case_labels(second_plan)) == sorted(
        [
            [
                "run_docker",
                "assert_stack_running",
                "capture_stack_summary",
                "assert_running_branch",
            ],
            [
                "run_docker",
                "assert_stack_running",
                "capture_stack_summary",
                "assert_boot_logs_branch",
            ],
        ]
    )
