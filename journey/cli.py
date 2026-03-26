"""CLI for journey v1."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .discovery import DiscoveredJourney, discover_journeys
from .errors import (
    AmbiguousStepSelectionError,
    JourneyError,
    JourneySelectionError,
    NoJourneysFoundError,
    StepNotFoundError,
)
from .executor import _ExecutionObserver, _execute_plan
from .models import (
    BranchMarkerNode,
    CaseExecutionReport,
    CasePlan,
    ExecutionReport,
    JourneyPlan,
    StepNode,
)
from .planner import compile_journey


@dataclass(frozen=True)
class _CommandError:
    file: str | None
    journey_name: str | None
    phase: str
    error_type: str
    message: str
    hint: str | None


@dataclass(frozen=True)
class _PlannedJourney:
    file_path: Path
    journey_name: str
    function: Callable[..., Any]
    plan: JourneyPlan


@dataclass(frozen=True)
class _ExecutedJourney:
    file_path: Path
    journey_name: str
    plan: JourneyPlan
    report: ExecutionReport


def _labels_for_case(case: CasePlan) -> list[str]:
    return [
        node.label
        for node in case.nodes
        if hasattr(node, "label") and getattr(node, "label") is not None
    ]


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _count(items: int, singular: str) -> str:
    noun = singular if items == 1 else f"{singular}s"
    return f"{items} {noun}"


def _format_branch_env(branch_env: dict[str, str]) -> str:
    entries = ", ".join(f"{key}={value}" for key, value in branch_env.items())
    return "{" + entries + "}"


def _count_step_records(case: CaseExecutionReport) -> int:
    return sum(1 for record in case.records if record.node_type == "StepNode")


def _format_duration(seconds: float) -> str:
    return f"{seconds:.3f}s"


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _step_name(node: StepNode) -> str:
    return node.label or node.node_id


class _LiveTextReporter(_ExecutionObserver):
    def __init__(
        self,
        *,
        root: Path,
        file_path: Path,
        journey_name: str,
        needs_separator: bool,
    ) -> None:
        self._display = _display_path(root, file_path)
        self._journey_name = journey_name
        self._needs_separator = needs_separator

    def _emit(self, line: str = "") -> None:
        print(line, flush=True)

    def on_journey_start(
        self,
        *,
        plan: JourneyPlan,
        selected_cases: list[Any],
    ) -> None:
        del selected_cases
        if self._needs_separator:
            self._emit()
        self._emit(f"Journey {self._display}:{self._journey_name}")
        self._emit(
            f"journey_id={plan.journey_id} function_ref={plan.function_ref}"
        )

    def on_case_start(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
    ) -> None:
        del stop_after_index, replay_anchor
        self._emit(
            f"- {case_plan.case_id} start branches={_format_branch_env(case_plan.branch_env)}"
        )

    def on_case_resume(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
        replay_from_index: int,
    ) -> None:
        del stop_after_index, replay_anchor, replay_from_index
        self._emit(
            f"- {case_plan.case_id} resume branches={_format_branch_env(case_plan.branch_env)}"
        )

    def on_step_start(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
    ) -> None:
        del case_plan, node_index
        self._emit(f"  step {_step_name(node)} attempt={attempt} start")

    def on_branch(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
    ) -> None:
        del case_plan, node_index
        self._emit(f"  branch {node.group_id}={node.active_key}")

    def on_retry(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
        delay_seconds: float,
        remaining_retries: int,
        error: Exception,
    ) -> None:
        del case_plan, node_index
        self._emit(
            "  "
            f"step {_step_name(node)} attempt={attempt} retry "
            f"duration={_format_duration(duration_seconds)} "
            f"delay={_format_duration(delay_seconds)} "
            f"remaining={remaining_retries} "
            f"error={_format_exception(error)}"
        )

    def on_step_success(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
    ) -> None:
        del case_plan, node_index
        self._emit(
            "  "
            f"step {_step_name(node)} attempt={attempt} ok "
            f"duration={_format_duration(duration_seconds)}"
        )

    def on_step_failure(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
        error: Exception,
    ) -> None:
        del case_plan, node_index
        self._emit(
            "  "
            f"step {_step_name(node)} attempt={attempt} failed "
            f"duration={_format_duration(duration_seconds)} "
            f"error={_format_exception(error)}"
        )

    def on_step_interrupted(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
        duration_seconds: float,
        error: BaseException,
    ) -> None:
        del case_plan, node_index
        self._emit(
            "  "
            f"step {_step_name(node)} attempt={attempt} interrupted "
            f"duration={_format_duration(duration_seconds)} "
            f"error={_format_exception(error)}"
        )

    def on_case_complete(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
        duration_seconds: float,
    ) -> None:
        del case_plan
        parts = [
            f"- {report.case_id}",
            "ok",
            f"steps={_count_step_records(report)}",
            f"duration={_format_duration(duration_seconds)}",
        ]
        if report.stopped_at_label is not None:
            parts.append(f"stopped_at={report.stopped_at_label}")
        if report.replay_anchor is not None:
            parts.append(f"replay_anchor={report.replay_anchor}")
        self._emit(" ".join(parts))


def _error_from_exception(
    exc: Exception,
    *,
    phase: str,
    file_path: str | None = None,
    journey_name: str | None = None,
) -> _CommandError:
    return _CommandError(
        file=file_path,
        journey_name=journey_name,
        phase=phase,
        error_type=type(exc).__name__,
        message=str(exc),
        hint=exc.hint if isinstance(exc, JourneyError) else None,
    )


def _emit_errors(errors: list[_CommandError]) -> None:
    for error in errors:
        location = error.file or "<selection>"
        if error.journey_name is not None:
            location = f"{location}:{error.journey_name}"
        print(f"ERROR [{error.phase}] {location} ({error.error_type})")
        print(f"What happened: {error.message}")
        if error.hint is not None:
            print(f"Try this: {error.hint}")
        print()


def _discover_targets(
    args: argparse.Namespace,
) -> tuple[Path, list[DiscoveredJourney], list[_CommandError]]:
    root = Path.cwd().resolve()
    discovered, discovery_errors = discover_journeys(
        root,
        file_path=args.file,
        journey_name=args.journey,
        fail_fast=args.fail_fast,
    )
    errors = [
        _error_from_exception(
            error,
            phase="discover",
            file_path=error.file_path,
        )
        for error in discovery_errors
    ]

    if not discovered and not errors:
        raise NoJourneysFoundError(
            root=str(root),
            file_path=args.file,
            journey_name=args.journey,
        )

    return root, discovered, errors


def _plan_targets(
    targets: list[DiscoveredJourney],
    *,
    fail_fast: bool,
) -> tuple[list[_PlannedJourney], list[_CommandError]]:
    planned: list[_PlannedJourney] = []
    errors: list[_CommandError] = []

    for target in targets:
        try:
            plan = compile_journey(target.function)
        except Exception as exc:
            errors.append(
                _error_from_exception(
                    exc,
                    phase="plan",
                    file_path=str(target.file_path),
                    journey_name=target.journey_name,
                )
            )
            if fail_fast:
                break
            continue

        planned.append(
            _PlannedJourney(
                file_path=target.file_path,
                journey_name=target.journey_name,
                function=target.function,
                plan=plan,
            )
        )

    return planned, errors


def _locate_step_matches(plan: JourneyPlan, step: str) -> list[tuple[CasePlan, int]]:
    matches: list[tuple[CasePlan, int]] = []
    for case in plan.case_plans:
        for index, node in enumerate(case.nodes):
            if getattr(node, "label", None) == step:
                matches.append((case, index))
    return matches


def _execute_all_targets(
    targets: list[DiscoveredJourney],
    *,
    root: Path,
    fail_fast: bool,
    state: str | None = None,
    stream_live: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    executed: list[_ExecutedJourney] = []
    errors: list[_CommandError] = []

    if state is not None and len(targets) != 1:
        return [], [
            _error_from_exception(
                JourneySelectionError(
                    "Resuming with --state requires exactly one selected journey.",
                    hint="Pass `--file`, `--journey`, or `--step` to narrow the selection to one journey.",
                ),
                phase="execute",
            )
        ]

    for index, target in enumerate(targets):
        try:
            plan = compile_journey(target.function)
            observer = (
                _LiveTextReporter(
                    root=root,
                    file_path=target.file_path,
                    journey_name=target.journey_name,
                    needs_separator=index > 0,
                )
                if stream_live
                else None
            )
            report = _execute_plan(
                target.function,
                plan=plan,
                state=state,
                observer=observer,
            )
        except Exception as exc:
            errors.append(
                _error_from_exception(
                    exc,
                    phase="execute",
                    file_path=str(target.file_path),
                    journey_name=target.journey_name,
                )
            )
            if fail_fast:
                break
            continue

        executed.append(
            _ExecutedJourney(
                file_path=target.file_path,
                journey_name=target.journey_name,
                plan=plan,
                report=report,
            )
        )

    return executed, errors


def _execute_target_step(
    planned: list[_PlannedJourney],
    *,
    root: Path,
    step: str,
    state: str | None = None,
    stream_live: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    flow_matches: dict[str, _PlannedJourney] = {}

    for item in planned:
        matches = _locate_step_matches(item.plan, step)
        for case, _ in matches:
            flow_key = f"{item.file_path}:{item.journey_name}:{case.case_id}"
            flow_matches[flow_key] = item

    if not flow_matches:
        return [], [_error_from_exception(StepNotFoundError(step), phase="execute")]

    if len(flow_matches) != 1:
        return [], [
            _error_from_exception(
                AmbiguousStepSelectionError(step, sorted(flow_matches)),
                phase="execute",
            )
        ]

    selected = next(iter(flow_matches.values()))
    try:
        observer = (
            _LiveTextReporter(
                root=root,
                file_path=selected.file_path,
                journey_name=selected.journey_name,
                needs_separator=False,
            )
            if stream_live
            else None
        )
        report = _execute_plan(
            selected.function,
            plan=selected.plan,
            step=step,
            state=state,
            observer=observer,
        )
    except Exception as exc:
        return [], [
            _error_from_exception(
                exc,
                phase="execute",
                file_path=str(selected.file_path),
                journey_name=selected.journey_name,
            )
        ]

    return [
        _ExecutedJourney(
            file_path=selected.file_path,
            journey_name=selected.journey_name,
            plan=selected.plan,
            report=report,
        )
    ], []


def _emit_plan_output(
    root: Path,
    planned: list[_PlannedJourney],
    errors: list[_CommandError],
    *,
    as_json: bool,
) -> None:
    if as_json:
        payload = {
            "journeys": [
                {
                    "file": str(item.file_path),
                    "journey_name": item.journey_name,
                    "plan": asdict(item.plan),
                }
                for item in planned
            ],
            "errors": [asdict(error) for error in errors],
        }
        print(json.dumps(payload, default=str, indent=2))
        return

    for item in planned:
        display = _display_path(root, item.file_path)
        print(f"Journey {display}:{item.journey_name}")
        print(
            f"journey_id={item.plan.journey_id} function_ref={item.plan.function_ref}"
        )
        for case in item.plan.case_plans:
            print(
                f"- {case.case_id} branch_env={case.branch_env} "
                f"labels={_labels_for_case(case)}"
            )
        print()

    _emit_errors(errors)

    total_cases = sum(len(item.plan.case_plans) for item in planned)
    print(
        "Summary: "
        f"{_count(len(planned), 'journey')} planned, "
        f"{_count(total_cases, 'case')} planned, "
        f"{len(errors)} failed"
    )


def _emit_execute_output(
    root: Path,
    executed: list[_ExecutedJourney],
    errors: list[_CommandError],
    *,
    as_json: bool,
) -> None:
    if as_json:
        payload = {
            "journeys": [
                {
                    "file": str(item.file_path),
                    "journey_name": item.journey_name,
                    "report": asdict(item.report),
                }
                for item in executed
            ],
            "errors": [asdict(error) for error in errors],
        }
        print(json.dumps(payload, default=str, indent=2))
        return

    _emit_errors(errors)

    total_cases = sum(len(item.report.case_reports) for item in executed)
    print(
        "Summary: "
        f"{_count(len(executed), 'journey')} executed, "
        f"{_count(total_cases, 'case')} executed, "
        f"{len(errors)} failed"
    )


def _emit_interrupt_output(*, state: str | None, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "interrupted": True,
                    "state": state,
                    "message": "Journey execution was interrupted before it finished.",
                    "hint": (
                        f"Run the same command again with --state {state} to resume."
                        if state is not None
                        else "Run the same command again to start over."
                    ),
                },
                indent=2,
            )
        )
        return

    print("Interrupted.")
    print("What happened: Journey execution was interrupted before it finished.")
    if state is None:
        print("Try this: Run the same command again to start over.")
        return
    print(f"Try this: Run the same command again with --state {state} to resume.")


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        root, targets, errors = _discover_targets(args)
    except JourneySelectionError as exc:
        _emit_plan_output(
            Path.cwd().resolve(),
            [],
            [_error_from_exception(exc, phase="discover")],
            as_json=args.json,
        )
        return 1

    planned, plan_errors = _plan_targets(targets, fail_fast=args.fail_fast)
    errors.extend(plan_errors)
    _emit_plan_output(root, planned, errors, as_json=args.json)
    return 0 if not errors else 1


def _cmd_execute(args: argparse.Namespace) -> int:
    try:
        root, targets, errors = _discover_targets(args)
    except JourneySelectionError as exc:
        _emit_execute_output(
            Path.cwd().resolve(),
            [],
            [_error_from_exception(exc, phase="discover")],
            as_json=args.json,
        )
        return 1

    executed: list[_ExecutedJourney] = []

    try:
        if args.step is None:
            run_results, run_errors = _execute_all_targets(
                targets,
                root=root,
                fail_fast=args.fail_fast,
                state=args.state,
                stream_live=not args.json,
            )
            executed.extend(run_results)
            errors.extend(run_errors)
        else:
            planned, plan_errors = _plan_targets(targets, fail_fast=args.fail_fast)
            errors.extend(plan_errors)
            if not errors:
                run_results, run_errors = _execute_target_step(
                    planned,
                    root=root,
                    step=args.step,
                    state=args.state,
                    stream_live=not args.json,
                )
                executed.extend(run_results)
                errors.extend(run_errors)
    except KeyboardInterrupt:
        _emit_interrupt_output(state=args.state, as_json=args.json)
        return 130

    _emit_execute_output(root, executed, errors, as_json=args.json)
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="journey", description="journey workflow test compiler")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("plan", help="Compile decorated journeys and print case plans")
    plan_cmd.add_argument("--file", help="Plan journeys defined in one Python file")
    plan_cmd.add_argument("--journey", help="Plan one decorated journey by function name")
    plan_cmd.add_argument("--json", action="store_true", help="Emit JSON")
    plan_cmd.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first discovery or planning failure",
    )
    plan_cmd.set_defaults(func=_cmd_plan)

    execute_cmd = sub.add_parser("execute", help="Compile and execute decorated journeys")
    execute_cmd.add_argument("--file", help="Execute journeys defined in one Python file")
    execute_cmd.add_argument("--journey", help="Execute one decorated journey by function name")
    execute_cmd.add_argument("--step", help="Execute only the flow that reaches one step label")
    execute_cmd.add_argument("--state", help="Persist and resume execution state in one file")
    execute_cmd.add_argument("--json", action="store_true", help="Emit JSON")
    execute_cmd.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first discovery, planning, or execution failure",
    )
    execute_cmd.set_defaults(func=_cmd_execute)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
