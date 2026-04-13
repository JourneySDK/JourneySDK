"""Core datamodels for planning and execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from dataclasses import dataclass, field
from typing import Any, TypeAlias

StepRetryDelay: TypeAlias = int | float | timedelta


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


def _duration_to_seconds(value: Any, *, field_name: str) -> float:
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
    from_checkpoint: str | None = None


@dataclass
class CheckpointRef:
    name: str


StepRetryFrom: TypeAlias = PlannedValue | CheckpointRef | None


@dataclass
class StepNode:
    node_id: str
    label: str | None
    fn_ref: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    retry: StepRetry | None = None


RunNode = StepNode
EvaluateNode = StepNode


@dataclass
class CheckpointNode:
    node_id: str
    name: str
    store_fn_ref: str | None = None
    restore_fn_ref: str | None = None
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def has_hooks(self) -> bool:
        return self.store_fn_ref is not None and self.restore_fn_ref is not None


@dataclass
class BranchMarkerNode:
    node_id: str
    group_id: str
    active_key: str
    start_from: str | None


PlanNode: TypeAlias = StepNode | CheckpointNode | BranchMarkerNode


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
    ok: bool
    result: Any = None
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


CheckpointHook: TypeAlias = Callable[..., object]
