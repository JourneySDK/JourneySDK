"""Core datamodels for planning and execution."""

from __future__ import annotations

from datetime import timedelta
from dataclasses import dataclass
from typing import Literal, TypeAlias

StepRetryDelay: TypeAlias = int | float | timedelta
StepArgument: TypeAlias = object
StepArguments: TypeAlias = tuple[StepArgument, ...]
StepKeywordArguments: TypeAlias = dict[str, StepArgument]
StepResult: TypeAlias = object
NodeExecutionStatus: TypeAlias = Literal["executed", "replayed", "failed"]


@dataclass(frozen=True)
class BranchCase:
    key: str | None
    start_from: str | None = None


@dataclass(frozen=True)
class PlannedValue:
    node_id: str
    kind: str
    access_path: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> "PlannedValue":
        if name.startswith("_"):
            raise AttributeError(name)
        return PlannedValue(
            node_id=self.node_id,
            kind=self.kind,
            access_path=self.access_path + (name,),
        )


def _duration_to_seconds(value: object, *, field_name: str) -> float:
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{field_name} must be an int, float, or datetime.timedelta value."
        )
    else:
        seconds = float(value)

    if seconds < 0:
        raise ValueError(f"{field_name} must be zero or greater.")
    return seconds


@dataclass(frozen=True)
class StepRetry:
    retries: int
    delay_seconds: float
    from_node_id: str | None = None


StepRetryFrom: TypeAlias = PlannedValue | None


@dataclass
class StepNode:
    node_id: str
    label: str | None
    fn_ref: str
    args: StepArguments
    kwargs: StepKeywordArguments
    retry: StepRetry | None = None
    source_fingerprint: str | None = None


RunNode = StepNode
EvaluateNode = StepNode


@dataclass
class BranchMarkerNode:
    node_id: str
    group_id: str
    active_key: str
    start_from: str | None


PlanNode: TypeAlias = StepNode | BranchMarkerNode


@dataclass
class CasePlan:
    case_id: str
    branch_env: dict[str, str]
    nodes: list[PlanNode]


@dataclass
class JourneyPlan:
    journey_id: str
    function_ref: str
    case_plans: list[CasePlan]


@dataclass
class NodeExecutionRecord:
    node_id: str
    node_type: str
    label: str | None
    status: NodeExecutionStatus
    result: StepResult = None
    error: str | None = None


@dataclass
class CaseExecutionReport:
    case_id: str
    branch_env: dict[str, str]
    records: list[NodeExecutionRecord]
    completed: bool
    stopped_at_label: str | None = None
    replay_anchor: str | None = None


@dataclass
class ExecutionReport:
    journey_id: str
    function_ref: str
    case_reports: list[CaseExecutionReport]
