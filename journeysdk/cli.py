"""CLI for journey v1."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .discovery import DiscoveredJourney, discover_journeys
from .errors import (
    AmbiguousStepSelectionError,
    ExecutionStateMismatchError,
    JourneyError,
    JourneySelectionError,
    NoJourneysFoundError,
    StepNotFoundError,
)
from .executor import _ExecutionObserver, _PausedExecution, _execute_plan
from .models import (
    BranchMarkerNode,
    CaseExecutionReport,
    CasePlan,
    ExecutionReport,
    JourneyPlan,
    StepNode,
)
from .planner import compile_journey
from .state import load_execution_state
from .types import JourneyEntrypoint


@dataclass(frozen=True)
class _CommandError:
    file: str | None
    journey_name: str | None
    phase: str
    error_type: str
    message: str
    hint: str | None


@dataclass(frozen=True)
class _CompiledJourney:
    file_path: Path
    journey_name: str
    function: JourneyEntrypoint
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
        self._journey_started = False

    def _emit(self, line: str = "") -> None:
        print(line, flush=True)

    def on_journey_start(
        self,
        *,
        plan: JourneyPlan,
        selected_cases: list[object],
    ) -> None:
        del selected_cases
        if self._journey_started:
            return
        self._journey_started = True
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

    def on_develop_state_restart(self, *, case_id: str) -> None:
        self._emit(
            "Already-run journey code changed before the paused step; "
            f"restarting {case_id} from the beginning."
        )


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
            phase="plan",
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


def _compile_targets(
    targets: list[DiscoveredJourney],
    *,
    fail_fast: bool,
) -> tuple[list[_CompiledJourney], list[_CommandError]]:
    compiled: list[_CompiledJourney] = []
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

        compiled.append(
            _CompiledJourney(
                file_path=target.file_path,
                journey_name=target.journey_name,
                function=target.function,
                plan=plan,
            )
        )

    return compiled, errors


def _state_selection_errors(
    args: argparse.Namespace,
    targets: list[DiscoveredJourney],
) -> list[_CommandError]:
    if args.state is None or len(targets) == 1:
        return []

    return [
        _error_from_exception(
            JourneySelectionError(
                "Resuming with --state requires exactly one selected journey.",
                hint=(
                    "Pass `--file`, `--journey`, `--step`, or `--develop-step` "
                    "to narrow the selection to one journey."
                ),
            ),
            phase="plan",
        )
    ]


def _locate_step_matches(plan: JourneyPlan, step: str) -> list[tuple[CasePlan, int]]:
    matches: list[tuple[CasePlan, int]] = []
    for case in plan.case_plans:
        for index, node in enumerate(case.nodes):
            if getattr(node, "label", None) == step:
                matches.append((case, index))
    return matches


def _select_targeted_journey(
    compiled: list[_CompiledJourney],
    *,
    step: str,
) -> tuple[_CompiledJourney | None, list[_CommandError]]:
    flow_matches: dict[str, _CompiledJourney] = {}

    for item in compiled:
        matches = _locate_step_matches(item.plan, step)
        for case, _ in matches:
            flow_key = f"{item.file_path}:{item.journey_name}:{case.case_id}"
            flow_matches[flow_key] = item

    if not flow_matches:
        return None, [_error_from_exception(StepNotFoundError(step), phase="execute")]

    if len(flow_matches) != 1:
        return None, [
            _error_from_exception(
                AmbiguousStepSelectionError(step, sorted(flow_matches)),
                phase="execute",
            )
        ]

    return next(iter(flow_matches.values())), []


def _paused_prompt(paused: _PausedExecution) -> str:
    status = _paused_status(paused)
    if paused.paused_step.ok:
        return f"{status} Press c to continue or r to retry: "
    return f"{status} Press c to exit with failure or r to retry: "


def _paused_status(paused: _PausedExecution) -> str:
    step_name = paused.paused_step.label or paused.paused_step.node_id
    if paused.paused_step.ok:
        return f"Paused after step {step_name} attempt={paused.paused_step.attempt} ok."
    if paused.paused_step.error:
        return (
            f"Paused after step {step_name} attempt={paused.paused_step.attempt} "
            f"failed ({paused.paused_step.error})."
        )
    return f"Paused after step {step_name} attempt={paused.paused_step.attempt} failed."


def _read_pause_choice(prompt: str) -> str:
    if not sys.stdin.isatty():
        return input(prompt).strip().lower()

    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
    except (ImportError, OSError, AttributeError):
        return input(prompt).strip().lower()

    print(prompt, end="", flush=True)
    try:
        try:
            tty.setcbreak(fd)
            choice = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except KeyboardInterrupt:
        print(flush=True)
        raise

    if choice == "\x03":
        print(flush=True)
        raise KeyboardInterrupt()
    if choice == "":
        print(flush=True)
        raise EOFError()
    print(flush=True)
    return choice.strip().lower()


def _prompt_for_pause_action(paused: _PausedExecution) -> str:
    while True:
        choice = _read_pause_choice(_paused_prompt(paused))
        if choice in {"c", "r"}:
            return choice
        print("Press 'c' to continue or 'r' to retry.", flush=True)


def _paused_failure_error(
    selected: _CompiledJourney,
    paused: _PausedExecution,
) -> _CommandError:
    message = paused.paused_step.failure_message
    if message is None:
        step_name = paused.paused_step.label or paused.paused_step.node_id
        message = f"Step '{step_name}' failed while it was running."
    return _CommandError(
        file=str(selected.file_path),
        journey_name=selected.journey_name,
        phase="execute",
        error_type="CallableExecutionError",
        message=message,
        hint=paused.paused_step.failure_hint,
    )


def _temporary_pause_state_path() -> Path:
    with tempfile.NamedTemporaryFile(
        delete=False,
        prefix=".journey-pause.",
        suffix=".state",
    ) as handle:
        path = Path(handle.name)
    path.unlink(missing_ok=True)
    return path


def _selected_develop_match(
    plan: JourneyPlan,
    develop_step: str,
) -> tuple[CasePlan, int] | None:
    matches = _locate_step_matches(plan, develop_step)
    if not matches:
        return None
    matching_case_ids = {case.case_id for case, _ in matches}
    if len(matching_case_ids) != 1:
        return None
    return min(matches, key=lambda item: item[1])


def _infer_develop_pause_action(
    state_path: Path,
    *,
    selected: _CompiledJourney,
    develop_step: str,
) -> str | None:
    state = load_execution_state(state_path)
    if state is None or state.active_case is None:
        return None
    paused_step = state.active_case.paused_step
    if paused_step is None:
        return None

    match = _selected_develop_match(selected.plan, develop_step)
    if match is None:
        return None
    case_plan, target_index = match
    if state.active_case.case_id != case_plan.case_id:
        return None

    paused_name = paused_step.label or paused_step.node_id
    if paused_name == develop_step:
        return "retry"
    if target_index > paused_step.node_index:
        if not paused_step.ok:
            raise ExecutionStateMismatchError(
                (
                    f"The journey state file '{state_path}' is paused after failed step "
                    f"{paused_name!r}, so it cannot continue to develop step "
                    f"{develop_step!r}."
                ),
                hint=(
                    "Rerun the same --develop-step target to retry the failed step, "
                    "or delete the state file to start fresh."
                ),
            )
        return "continue"
    raise ExecutionStateMismatchError(
        (
            f"The journey state file '{state_path}' is paused after step "
            f"{paused_name!r}, which is not before requested develop step "
            f"{develop_step!r}."
        ),
        hint=(
            "Rerun the paused --develop-step target to retry it, target the next "
            "later step to continue, or delete the state file to start fresh."
        ),
    )


def _execute_all_targets(
    compiled: list[_CompiledJourney],
    *,
    root: Path,
    fail_fast: bool,
    state: str | None = None,
    stream_live: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    executed: list[_ExecutedJourney] = []
    errors: list[_CommandError] = []

    for index, item in enumerate(compiled):
        try:
            observer = (
                _LiveTextReporter(
                    root=root,
                    file_path=item.file_path,
                    journey_name=item.journey_name,
                    needs_separator=index > 0,
                )
                if stream_live
                else None
            )
            report = _execute_plan(
                item.function,
                plan=item.plan,
                state=state,
                observer=observer,
            )
        except Exception as exc:
            errors.append(
                _error_from_exception(
                    exc,
                    phase="execute",
                    file_path=str(item.file_path),
                    journey_name=item.journey_name,
                )
            )
            if fail_fast:
                break
            continue

        executed.append(
            _ExecutedJourney(
                file_path=item.file_path,
                journey_name=item.journey_name,
                plan=item.plan,
                report=report,
            )
        )

    return executed, errors


def _execute_target_step(
    compiled: list[_CompiledJourney],
    *,
    root: Path,
    step: str,
    state: str | None = None,
    stream_live: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    selected, errors = _select_targeted_journey(compiled, step=step)
    if selected is None:
        return [], errors

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


def _reload_develop_target(
    selected: _CompiledJourney,
    *,
    root: Path,
    develop_step: str,
) -> tuple[_CompiledJourney | None, list[_CommandError]]:
    discovered, discovery_errors = discover_journeys(
        root,
        file_path=str(selected.file_path),
        journey_name=selected.journey_name,
        fail_fast=True,
    )
    errors = [
        _error_from_exception(
            error,
            phase="plan",
            file_path=error.file_path,
        )
        for error in discovery_errors
    ]
    if errors:
        return None, errors
    if not discovered:
        return None, [
            _error_from_exception(
                JourneySelectionError(
                    f"Journey '{selected.journey_name}' was not found after reloading.",
                    hint="Check that the journey function still exists and is decorated with @journey.",
                ),
                phase="plan",
                file_path=str(selected.file_path),
                journey_name=selected.journey_name,
            )
        ]

    recompiled, compile_errors = _compile_targets(discovered, fail_fast=True)
    if compile_errors:
        return None, compile_errors

    return _select_targeted_journey(recompiled, step=develop_step)


def _execute_target_pause(
    compiled: list[_CompiledJourney],
    *,
    root: Path,
    develop_step: str,
    state: str | None = None,
    stream_live: bool = False,
    interactive: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    selected, errors = _select_targeted_journey(compiled, step=develop_step)
    if selected is None:
        return [], errors

    managed_state = Path(state) if state is not None else _temporary_pause_state_path()
    cleanup_state = state is None

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
        pause_action: str | None = None

        if not interactive:
            if state is not None:
                pause_action = _infer_develop_pause_action(
                    Path(state),
                    selected=selected,
                    develop_step=develop_step,
                )
            outcome = _execute_plan(
                selected.function,
                plan=selected.plan,
                develop_step=develop_step,
                pause_action=pause_action,
                state=str(managed_state),
                observer=observer,
            )
            if isinstance(outcome, _PausedExecution):
                outcome.close_pending_exits()
                if stream_live:
                    print(_paused_status(outcome), flush=True)
                if not outcome.paused_step.ok:
                    return [], [_paused_failure_error(selected, outcome)]
                return [], []

            return [
                _ExecutedJourney(
                    file_path=selected.file_path,
                    journey_name=selected.journey_name,
                    plan=selected.plan,
                    report=outcome,
                )
            ], []

        while True:
            outcome = _execute_plan(
                selected.function,
                plan=selected.plan,
                develop_step=develop_step,
                pause_action=pause_action,
                state=str(managed_state),
                observer=observer,
            )
            if isinstance(outcome, _PausedExecution):
                try:
                    choice = _prompt_for_pause_action(outcome)
                except KeyboardInterrupt as exc:
                    try:
                        outcome.close_pending_exits()
                    except Exception as cleanup_exc:
                        exc.add_note(str(cleanup_exc))
                    raise
                except EOFError as exc:
                    try:
                        outcome.close_pending_exits()
                    except Exception as cleanup_exc:
                        exc.add_note(str(cleanup_exc))
                    raise
                outcome.close_pending_exits()
                if choice == "c":
                    if not outcome.paused_step.ok:
                        return [], [_paused_failure_error(selected, outcome)]
                    pause_action = "continue"
                else:
                    pause_action = "retry"
                reloaded, reload_errors = _reload_develop_target(
                    selected,
                    root=root,
                    develop_step=develop_step,
                )
                if reloaded is None:
                    return [], reload_errors
                selected = reloaded
                if stream_live:
                    display = _display_path(root, selected.file_path)
                    print(
                        f"Reloaded and recompiled {display}:{selected.journey_name} "
                        f"after {pause_action}.",
                        flush=True,
                    )
                continue

            report = outcome
            return [
                _ExecutedJourney(
                    file_path=selected.file_path,
                    journey_name=selected.journey_name,
                    plan=selected.plan,
                    report=report,
                )
            ], []
    except Exception as exc:
        return [], [
            _error_from_exception(
                exc,
                phase="execute",
                file_path=str(selected.file_path),
                journey_name=selected.journey_name,
            )
        ]
    finally:
        if cleanup_state:
            managed_state.unlink(missing_ok=True)


def _emit_plan_output(
    root: Path,
    compiled: list[_CompiledJourney],
    errors: list[_CommandError],
) -> None:
    print("Plan")

    for index, item in enumerate(compiled):
        if index > 0:
            print()
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

    if compiled and errors:
        print()

    _emit_errors(errors)

    total_cases = sum(len(item.plan.case_plans) for item in compiled)
    print(
        "Summary: "
        f"{_count(len(compiled), 'journey')} planned, "
        f"{_count(total_cases, 'case')} planned, "
        f"{len(errors)} failed"
    )


def _emit_execute_output(
    root: Path,
    executed: list[_ExecutedJourney],
    errors: list[_CommandError],
    *,
    as_json: bool,
    failure_count: int | None = None,
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
    failed = len(errors) if failure_count is None else failure_count
    print(
        "Summary: "
        f"{_count(len(executed), 'journey')} executed, "
        f"{_count(total_cases, 'case')} executed, "
        f"{failed} failed"
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


def _cmd_execute(args: argparse.Namespace) -> int:
    try:
        root, targets, errors = _discover_targets(args)
    except JourneySelectionError as exc:
        error = _error_from_exception(exc, phase="plan")
        if not args.json:
            _emit_plan_output(Path.cwd().resolve(), [], [error])
            print()
            print("Execution")
            _emit_execute_output(
                Path.cwd().resolve(),
                [],
                [],
                as_json=False,
                failure_count=1,
            )
            return 1
        _emit_execute_output(
            Path.cwd().resolve(),
            [],
            [error],
            as_json=args.json,
        )
        return 1

    executed: list[_ExecutedJourney] = []
    run_errors: list[_CommandError] = []

    if state_errors := _state_selection_errors(args, targets):
        errors.extend(state_errors)
        if not args.json:
            _emit_plan_output(root, [], errors)
            print()
            print("Execution")
            _emit_execute_output(
                root,
                [],
                [],
                as_json=False,
                failure_count=len(errors),
            )
            return 1
        _emit_execute_output(root, [], errors, as_json=True)
        return 1

    compiled, compile_errors = _compile_targets(
        targets,
        fail_fast=args.fail_fast,
    )
    errors.extend(compile_errors)

    if not args.json:
        _emit_plan_output(root, compiled, errors)
        print()
        print("Execution")

    try:
        should_execute = bool(compiled) and not (args.fail_fast and errors)
        if not should_execute:
            run_errors = []
        elif args.step is None and args.develop_step is None:
            run_results, run_errors = _execute_all_targets(
                compiled,
                root=root,
                fail_fast=args.fail_fast,
                state=args.state,
                stream_live=not args.json,
            )
            executed.extend(run_results)
        else:
            if args.develop_step is not None:
                run_results, run_errors = _execute_target_pause(
                    compiled,
                    root=root,
                    develop_step=args.develop_step,
                    state=args.state,
                    stream_live=not args.json,
                    interactive=args.interactive,
                )
            else:
                run_results, run_errors = _execute_target_step(
                    compiled,
                    root=root,
                    step=args.step,
                    state=args.state,
                    stream_live=not args.json,
                )
            executed.extend(run_results)
    except KeyboardInterrupt:
        _emit_interrupt_output(state=args.state, as_json=args.json)
        return 130

    all_errors = [*errors, *run_errors]
    if args.json:
        _emit_execute_output(root, executed, all_errors, as_json=True)
    else:
        _emit_execute_output(
            root,
            executed,
            run_errors,
            as_json=False,
            failure_count=len(all_errors),
        )
    return 0 if not all_errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="journey",
        description="execute decorated journey workflows",
    )
    parser.add_argument("--file", help="Execute journeys defined in one Python file")
    parser.add_argument(
        "--journey",
        help="Execute one decorated journey by function name",
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--step",
        help="Execute only the flow that reaches one step label",
    )
    target_group.add_argument(
        "--develop-step",
        help="Run one target step label in development mode and pause after it",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt to continue or retry after each --develop-step pause",
    )
    parser.add_argument("--state", help="Persist and resume execution state in one file")
    parser.add_argument("--json", action="store_true", help="Emit execution JSON")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first discovery, compilation, or execution failure",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interactive and getattr(args, "develop_step", None) is None:
        parser.error("--interactive requires --develop-step")
    if getattr(args, "develop_step", None) is not None and args.json:
        parser.error("--develop-step cannot be used with --json")
    return _cmd_execute(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
