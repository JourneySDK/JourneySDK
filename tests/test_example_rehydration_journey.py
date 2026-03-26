from __future__ import annotations

import journey
import example.rehydration_journey as rehydration_example
from journey.models import StepNode


def _case_labels(plan: journey.JourneyPlan) -> list[list[str]]:
    return [
        [
            node.label
            for node in case_plan.nodes
            if isinstance(node, StepNode) and node.label is not None
        ]
        for case_plan in plan.case_plans
    ]


def test_rehydration_example_compiles_and_reuses_checkpoint_state():
    rehydration_example.reset_demo_state()
    plan = journey.compile_journey(rehydration_example.rehydration_journey)

    assert _case_labels(plan) == [
        ["prepare_context", "shared_after_checkpoint", "assert_branch_a"],
        ["prepare_context", "shared_after_checkpoint", "assert_branch_b"],
    ]

    rehydration_example.reset_demo_state()
    report = journey.execute(rehydration_example.rehydration_journey)

    seed = rehydration_example.EVENTS[0].split("_")[1]
    assert rehydration_example.EVENTS == [
        f"prepare_{seed}",
        f"shared_seed-{seed}",
        f"branch_a_seed-{seed}",
        f"branch_b_seed-{seed}",
    ]
    assert [case.case_id for case in report.case_reports] == ["case_1", "case_2"]


def test_rehydration_example_supports_targeted_execution_with_replay_anchor():
    rehydration_example.reset_demo_state()

    report = journey.execute(
        rehydration_example.rehydration_journey,
        step="assert_branch_b",
    )

    assert len(report.case_reports) == 1
    assert report.case_reports[0].stopped_at_label == "assert_branch_b"
    assert report.case_reports[0].replay_anchor == "cp_1"
