"""CLI for journey v1."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
from .executor import (
    _ExecutionObserver,
    _PausedExecution,
    _execute_plan,
    _use_step_interrupt_controller,
)
from .logger import configure_logging, get_logger, pretty_line, pretty_row
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

_CLI_LOGGER = get_logger("cli")

_GRACEFUL_INTERRUPT_PHASES = {
    "initialization",
    "execution",
    "storage",
    "pre-exit",
    "exit",
}


class _JourneyArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message: str, file: object | None = None) -> None:
        del file
        stripped = message.rstrip()
        if not stripped:
            return
        if ": error:" in stripped:
            _CLI_LOGGER.error("parser_error", stripped)
            return
        _CLI_LOGGER.info("parser_output", stripped)


class _CliStepInterruptController:
    def __init__(self) -> None:
        self._phase: str | None = None
        self._pending_interrupt = False

    def on_step_lifecycle_phase(self, phase: str | None) -> None:
        self._phase = phase

    def is_step_interrupt_pending(self) -> bool:
        return self._pending_interrupt

    def raise_if_interrupted_after_step(self) -> None:
        if not self._pending_interrupt:
            return
        self._pending_interrupt = False
        raise KeyboardInterrupt()

    def handle_sigint(self, signum: int, frame: object) -> None:
        del signum, frame
        if self._phase in _GRACEFUL_INTERRUPT_PHASES and not self._pending_interrupt:
            self._pending_interrupt = True
            _CLI_LOGGER.warning(
                "graceful_interrupt_requested",
                "Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.",
                pretty=pretty_line(
                    "Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.",
                    style="warning",
                ),
                phase=self._phase,
            )
            return
        if self._pending_interrupt:
            _CLI_LOGGER.warning(
                "forced_interrupt_requested",
                "Ctrl-C received again. Stopping now; this step will restart from saved inputs on resume.",
                pretty=pretty_line(
                    "Ctrl-C received again. Stopping now; this step will restart from saved inputs on resume.",
                    style="warning",
                ),
                phase=self._phase,
            )
        raise KeyboardInterrupt()


@contextmanager
def _graceful_cli_interrupts(enabled: bool) -> Iterator[None]:
    if not enabled or threading.current_thread() is not threading.main_thread():
        yield
        return

    controller = _CliStepInterruptController()
    previous_handler = signal.getsignal(signal.SIGINT)

    def handler(signum: int, frame: object) -> None:
        controller.handle_sigint(signum, frame)

    signal.signal(signal.SIGINT, handler)
    try:
        with _use_step_interrupt_controller(controller):
            yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)


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


def _pretty_target(*, display_file: str | None, journey: str | None) -> str:
    if display_file is not None and journey is not None:
        return f"{display_file}:{journey}"
    if journey is not None:
        return journey
    if display_file is not None:
        return display_file
    return "journey"


def _pretty_case_line(case: str, *, labels: list[str] | None = None, branches: str | None = None) -> str:
    details = []
    if labels:
        details.append(f"labels: {', '.join(labels)}")
    if branches:
        details.append(f"branches: {branches}")
    suffix = f"  {'; '.join(details)}" if details else ""
    return f"    {case}{suffix}"


def _pretty_step_detail(action: str, *, attempt: int, duration: str | None = None) -> str:
    parts = [action, f"attempt={attempt}"]
    if duration is not None:
        parts.append(f"duration={duration}")
    return " ".join(parts)


def _pretty_step_problem(
    step: str,
    action: str,
    *,
    duration: str | None,
    error: str | None,
    fallback: str,
) -> str:
    if duration is None:
        return f"{step} {fallback}"
    detail = f"{step} {action} after {duration}"
    if error is not None:
        detail += f" ({error})"
    return detail


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
        self._journey_started = False
        self._logger = get_logger("executor")
        del needs_separator

    def on_journey_start(
        self,
        *,
        plan: JourneyPlan,
        selected_cases: list[object],
    ) -> None:
        if self._journey_started:
            return
        self._journey_started = True
        self._logger.info(
            "journey_start",
            "starting journey execution",
            pretty=pretty_line(
                f"  {_pretty_target(display_file=self._display, journey=self._journey_name)}",
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            journey_id=plan.journey_id,
            function_ref=plan.function_ref,
            cases=len(selected_cases),
        )

    def on_case_start(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
    ) -> None:
        self._logger.info(
            "case_start",
            f"- {case_plan.case_id} start branches={_format_branch_env(case_plan.branch_env)}",
            pretty=pretty_line(
                f"    {case_plan.case_id}"
                + (
                    f"  branches={_format_branch_env(case_plan.branch_env)}"
                    if case_plan.branch_env
                    else ""
                ),
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            branches=_format_branch_env(case_plan.branch_env),
            replay_anchor=replay_anchor,
            stop_after_index=stop_after_index,
        )

    def on_case_resume(
        self,
        *,
        case_plan: CasePlan,
        stop_after_index: int | None,
        replay_anchor: str | None,
        replay_from_index: int,
    ) -> None:
        self._logger.info(
            "case_resume",
            f"- {case_plan.case_id} resume branches={_format_branch_env(case_plan.branch_env)}",
            pretty=pretty_line(
                f"    {case_plan.case_id} resume"
                + (
                    f"  branches={_format_branch_env(case_plan.branch_env)}"
                    if case_plan.branch_env
                    else ""
                ),
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            branches=_format_branch_env(case_plan.branch_env),
            replay_anchor=replay_anchor,
            replay_from_index=replay_from_index,
            stop_after_index=stop_after_index,
        )

    def on_step_start(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        attempt: int,
    ) -> None:
        self._logger.info(
            "step_start",
            f"  step {_step_name(node)} attempt={attempt} start",
            pretty=pretty_row(
                _step_name(node),
                _pretty_step_detail("start", attempt=attempt),
                indent=6,
                label_width=29,
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
        )

    def on_branch(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
    ) -> None:
        self._logger.info(
            "branch_select",
            f"  branch {node.group_id}={node.active_key}",
            pretty=pretty_row(
                f"branch {node.group_id}",
                node.active_key,
                indent=6,
                label_width=29,
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            branch_group=node.group_id,
            branch=node.active_key,
            node_index=node_index,
        )

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
        self._logger.warning(
            "step_retry",
            (
                f"  step {_step_name(node)} attempt={attempt} retry "
                f"duration={_format_duration(duration_seconds)} "
                f"delay={_format_duration(delay_seconds)} "
                f"remaining={remaining_retries} "
                f"error={_format_exception(error)}"
            ),
            pretty=_pretty_step_problem(
                _step_name(node),
                "retry",
                duration=_format_duration(duration_seconds),
                error=_format_exception(error),
                fallback="retrying",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            delay=_format_duration(delay_seconds),
            remaining=remaining_retries,
            error=_format_exception(error),
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
        self._logger.info(
            "step_success",
            (
                f"  step {_step_name(node)} attempt={attempt} ok "
                f"duration={_format_duration(duration_seconds)}"
            ),
            pretty=pretty_row(
                _step_name(node),
                _pretty_step_detail(
                    "ok",
                    attempt=attempt,
                    duration=_format_duration(duration_seconds),
                ),
                indent=6,
                label_width=29,
                style="success",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
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
        self._logger.error(
            "step_failure",
            (
                f"  step {_step_name(node)} attempt={attempt} failed "
                f"duration={_format_duration(duration_seconds)} "
                f"error={_format_exception(error)}"
            ),
            pretty=_pretty_step_problem(
                _step_name(node),
                "failed",
                duration=_format_duration(duration_seconds),
                error=_format_exception(error),
                fallback="failed",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            error=_format_exception(error),
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
        self._logger.warning(
            "step_interrupted",
            (
                f"  step {_step_name(node)} attempt={attempt} interrupted "
                f"duration={_format_duration(duration_seconds)} "
                f"error={_format_exception(error)}"
            ),
            pretty=_pretty_step_problem(
                _step_name(node),
                "interrupted",
                duration=_format_duration(duration_seconds),
                error=_format_exception(error),
                fallback="interrupted",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            attempt=attempt,
            duration=_format_duration(duration_seconds),
            error=_format_exception(error),
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
        self._logger.info(
            "case_complete",
            " ".join(parts),
            pretty=pretty_line(
                f"    {report.case_id} done "
                f"steps={_count_step_records(report)} "
                f"duration={_format_duration(duration_seconds)}"
                + (
                    f" stopped_at={report.stopped_at_label}"
                    if report.stopped_at_label is not None
                    else ""
                )
                + (
                    f" replay_anchor={report.replay_anchor}"
                    if report.replay_anchor is not None
                    else ""
                ),
                style="success",
            ),
            file=self._display,
            journey=self._journey_name,
            case=report.case_id,
            steps=_count_step_records(report),
            duration=_format_duration(duration_seconds),
            stopped_at=report.stopped_at_label,
            replay_anchor=report.replay_anchor,
        )

    def on_journey_complete(self, *, report: ExecutionReport) -> None:
        self._logger.info(
            "journey_complete",
            "journey execution completed",
            pretty=False,
            file=self._display,
            journey=self._journey_name,
            journey_id=report.journey_id,
            cases=len(report.case_reports),
        )

    def on_develop_state_restart(self, *, case_id: str) -> None:
        self._logger.warning(
            "develop_state_restart",
            "Already-run journey code changed before the paused step; "
            f"restarting {case_id} from the beginning.",
            pretty=(
                "Already-run journey code changed before the paused step; "
                f"restarting {case_id} from the beginning."
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_id,
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
        _CLI_LOGGER.error(
            "command_error",
            f"ERROR [{error.phase}] {location} ({error.error_type})",
            pretty=(
                f"{error.error_type} during {error.phase} at {location}"
            ),
            phase=error.phase,
            location=location,
            error_type=error.error_type,
            error_message=error.message,
            hint=error.hint,
        )
        _CLI_LOGGER.error(
            "command_error_message",
            f"What happened: {error.message}",
            pretty=pretty_line(f"What happened: {error.message}", style="error"),
            phase=error.phase,
            location=location,
            error_type=error.error_type,
        )
        if error.hint is not None:
            _CLI_LOGGER.error(
                "command_error_hint",
                f"Try this: {error.hint}",
                pretty=pretty_line(f"Try this: {error.hint}", style="error"),
                phase=error.phase,
                location=location,
                error_type=error.error_type,
            )


def _discover_targets(
    args: argparse.Namespace,
) -> tuple[Path, list[DiscoveredJourney], list[_CommandError]]:
    root = Path.cwd().resolve()
    _CLI_LOGGER.info(
        "discovery_start",
        "discovering journeys",
        pretty=f"Discovering journeys: {root}",
        root=str(root),
        file=args.file,
        journey=args.journey,
    )
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
        _CLI_LOGGER.error(
            "discovery_failure",
            "no journeys were found",
            pretty="no journeys were found",
            root=str(root),
            file=args.file,
            journey=args.journey,
        )
        raise NoJourneysFoundError(
            root=str(root),
            file_path=args.file,
            journey_name=args.journey,
        )

    _CLI_LOGGER.info(
        "discovery_complete",
        "journey discovery completed",
        pretty=(
            f"Found {_count(len(discovered), 'journey')}"
            + (f", {len(errors)} failed" if errors else "")
        ),
        discovered=len(discovered),
        errors=len(errors),
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
        _CLI_LOGGER.info(
            "compile_start",
            "compiling journey",
            pretty=False,
            file=str(target.file_path),
            journey=target.journey_name,
        )
        try:
            plan = compile_journey(target.function)
        except Exception as exc:
            _CLI_LOGGER.error(
                "compile_failure",
                "journey compilation failed",
                pretty=False,
                file=str(target.file_path),
                journey=target.journey_name,
                error=_format_exception(exc),
            )
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
        _CLI_LOGGER.info(
            "compile_success",
            "journey compiled",
            pretty=False,
            file=str(target.file_path),
            journey=target.journey_name,
            cases=len(plan.case_plans),
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
    status = _step_stop_status(paused, verb="Paused")
    if paused.paused_step.ok:
        return f"{status} Press c to continue or r to retry: "
    return f"{status} Press c to exit with failure or r to retry: "


def _step_stop_status(paused: _PausedExecution, *, verb: str) -> str:
    action = verb.lower()
    step_name = paused.paused_step.label or paused.paused_step.node_id
    if paused.paused_step.ok:
        return (
            f"Development mode {action} after step "
            f"{step_name} attempt={paused.paused_step.attempt} ok."
        )
    if paused.paused_step.error:
        return (
            f"Development mode {action} after step "
            f"{step_name} attempt={paused.paused_step.attempt} "
            f"failed ({paused.paused_step.error})."
        )
    return (
        f"Development mode {action} after step "
        f"{step_name} attempt={paused.paused_step.attempt} failed."
    )


def _read_pause_choice(prompt: str) -> str:
    _CLI_LOGGER.info("pause_prompt", prompt, pretty=prompt)
    if not sys.stdin.isatty():
        return input("").strip().lower()

    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
    except (ImportError, OSError, AttributeError):
        return input("").strip().lower()

    try:
        try:
            tty.setcbreak(fd)
            choice = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except KeyboardInterrupt:
        raise

    if choice == "\x03":
        raise KeyboardInterrupt()
    if choice == "":
        raise EOFError()
    return choice.strip().lower()


def _prompt_for_pause_action(paused: _PausedExecution) -> str:
    while True:
        choice = _read_pause_choice(_paused_prompt(paused))
        if choice in {"c", "r"}:
            return choice
        _CLI_LOGGER.info(
            "pause_invalid_choice",
            "Press 'c' to continue or 'r' to retry.",
            pretty="Press 'c' to continue or 'r' to retry.",
        )


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
    no_memory: bool = False,
    no_memory_update: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    executed: list[_ExecutedJourney] = []
    errors: list[_CommandError] = []

    for index, item in enumerate(compiled):
        _CLI_LOGGER.info(
            "execution_start",
            "executing journey",
            pretty=False,
            file=str(item.file_path),
            journey=item.journey_name,
        )
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
                no_memory=no_memory,
                no_memory_update=no_memory_update,
                prompt_memory_root=item.file_path.parent,
            )
        except Exception as exc:
            _CLI_LOGGER.error(
                "execution_failure",
                "journey execution failed",
                pretty=(
                    f"{_pretty_target(display_file=_display_path(root, item.file_path), journey=item.journey_name)} "
                    f"failed: {_format_exception(exc)}"
                ),
                file=str(item.file_path),
                journey=item.journey_name,
                error=_format_exception(exc),
            )
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
        _CLI_LOGGER.info(
            "execution_success",
            "journey execution succeeded",
            pretty=False,
            file=str(item.file_path),
            journey=item.journey_name,
            cases=len(report.case_reports),
        )

    return executed, errors


def _execute_target_step(
    compiled: list[_CompiledJourney],
    *,
    root: Path,
    step: str,
    state: str | None = None,
    stream_live: bool = False,
    no_memory: bool = False,
    no_memory_update: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    selected, errors = _select_targeted_journey(compiled, step=step)
    if selected is None:
        return [], errors

    try:
        _CLI_LOGGER.info(
            "execution_start",
            "executing targeted journey",
            pretty=False,
            file=str(selected.file_path),
            journey=selected.journey_name,
            step=step,
        )
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
            no_memory=no_memory,
            no_memory_update=no_memory_update,
            prompt_memory_root=selected.file_path.parent,
        )
    except Exception as exc:
        _CLI_LOGGER.error(
            "execution_failure",
            "targeted journey execution failed",
            pretty=(
                f"{_pretty_target(display_file=_display_path(root, selected.file_path), journey=selected.journey_name)} "
                f"failed: {_format_exception(exc)}"
            ),
            file=str(selected.file_path),
            journey=selected.journey_name,
            step=step,
            error=_format_exception(exc),
        )
        return [], [
            _error_from_exception(
                exc,
                phase="execute",
                file_path=str(selected.file_path),
                journey_name=selected.journey_name,
            )
        ]

    _CLI_LOGGER.info(
        "execution_success",
        "targeted journey execution succeeded",
        pretty=False,
        file=str(selected.file_path),
        journey=selected.journey_name,
        step=step,
        cases=len(report.case_reports),
    )
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
    no_memory: bool = False,
    no_memory_update: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    selected, errors = _select_targeted_journey(compiled, step=develop_step)
    if selected is None:
        return [], errors

    managed_state = Path(state) if state is not None else _temporary_pause_state_path()
    cleanup_state = state is None

    try:
        _CLI_LOGGER.info(
            "execution_start",
            "executing develop step",
            pretty=False,
            file=str(selected.file_path),
            journey=selected.journey_name,
            develop_step=develop_step,
            interactive=interactive,
        )
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
                no_memory=no_memory,
                no_memory_update=no_memory_update,
                prompt_memory_root=selected.file_path.parent,
            )
            if isinstance(outcome, _PausedExecution):
                outcome.close_pending_exits()
                if stream_live:
                    _CLI_LOGGER.info(
                        "develop_step_stopped",
                        _step_stop_status(outcome, verb="Stopped"),
                        pretty=_step_stop_status(outcome, verb="Stopped"),
                        file=str(selected.file_path),
                        journey=selected.journey_name,
                        step=outcome.paused_step.label or outcome.paused_step.node_id,
                        attempt=outcome.paused_step.attempt,
                        ok=outcome.paused_step.ok,
                    )
                if not outcome.paused_step.ok:
                    return [], [_paused_failure_error(selected, outcome)]
                return [], []

            _CLI_LOGGER.info(
                "execution_success",
                "develop-step execution succeeded",
                pretty=False,
                file=str(selected.file_path),
                journey=selected.journey_name,
                develop_step=develop_step,
                cases=len(outcome.case_reports),
            )
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
                no_memory=no_memory,
                no_memory_update=no_memory_update,
                prompt_memory_root=selected.file_path.parent,
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
                    _CLI_LOGGER.info(
                        "develop_step_reload",
                        f"Reloaded and recompiled {display}:{selected.journey_name} "
                        f"after {pause_action}.",
                        pretty=(
                            f"Reloaded and recompiled {display}:{selected.journey_name} "
                            f"after {pause_action}."
                        ),
                        file=str(selected.file_path),
                        journey=selected.journey_name,
                        action=pause_action,
                    )
                continue

            report = outcome
            _CLI_LOGGER.info(
                "execution_success",
                "develop-step execution succeeded",
                pretty=False,
                file=str(selected.file_path),
                journey=selected.journey_name,
                develop_step=develop_step,
                cases=len(report.case_reports),
            )
            return [
                _ExecutedJourney(
                    file_path=selected.file_path,
                    journey_name=selected.journey_name,
                    plan=selected.plan,
                    report=report,
                )
            ], []
    except Exception as exc:
        _CLI_LOGGER.error(
            "execution_failure",
            "develop-step execution failed",
            pretty=(
                f"{_pretty_target(display_file=_display_path(root, selected.file_path), journey=selected.journey_name)} "
                f"failed: {_format_exception(exc)}"
            ),
            file=str(selected.file_path),
            journey=selected.journey_name,
            develop_step=develop_step,
            error=_format_exception(exc),
        )
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
    _CLI_LOGGER.info("plan_start", "Plan", pretty=pretty_line("Plan", style="heading"))

    for item in compiled:
        display = _display_path(root, item.file_path)
        _CLI_LOGGER.info(
            "plan_journey",
            f"Journey {display}:{item.journey_name}",
            pretty=pretty_line(
                f"  {_pretty_target(display_file=display, journey=item.journey_name)}",
                style="context",
            ),
            file=str(item.file_path),
            display_file=display,
            journey=item.journey_name,
            journey_id=item.plan.journey_id,
            function_ref=item.plan.function_ref,
            cases=len(item.plan.case_plans),
        )
        _CLI_LOGGER.info(
            "plan_metadata",
            f"journey_id={item.plan.journey_id} function_ref={item.plan.function_ref}",
            pretty=False,
            file=str(item.file_path),
            display_file=display,
            journey=item.journey_name,
            journey_id=item.plan.journey_id,
            function_ref=item.plan.function_ref,
        )
        for case in item.plan.case_plans:
            labels = _labels_for_case(case)
            _CLI_LOGGER.info(
                "plan_case",
                f"- {case.case_id} branch_env={case.branch_env} "
                f"labels={labels}",
                pretty=pretty_line(
                    _pretty_case_line(
                        case.case_id,
                        labels=labels,
                        branches=(
                            _format_branch_env(case.branch_env)
                            if case.branch_env
                            else None
                        ),
                    ),
                    style="context",
                ),
                file=str(item.file_path),
                display_file=display,
                journey=item.journey_name,
                case=case.case_id,
                branch_env=case.branch_env,
                labels=labels,
            )

    _emit_errors(errors)

    total_cases = sum(len(item.plan.case_plans) for item in compiled)
    _CLI_LOGGER.info(
        "plan_summary",
        "Summary: "
        f"{_count(len(compiled), 'journey')} planned, "
        f"{_count(total_cases, 'case')} planned, "
        f"{len(errors)} failed",
        pretty=pretty_line(
            "Summary: "
            f"{_count(len(compiled), 'journey')} planned, "
            f"{_count(total_cases, 'case')} planned, "
            f"{len(errors)} failed",
            indent=2,
            style="heading",
        ),
        journeys=len(compiled),
        cases=total_cases,
        failures=len(errors),
    )


def _emit_execute_output(
    root: Path,
    executed: list[_ExecutedJourney],
    errors: list[_CommandError],
    *,
    result_errors: list[_CommandError] | None = None,
    failure_count: int | None = None,
) -> None:
    _emit_errors(errors)

    total_cases = sum(len(item.report.case_reports) for item in executed)
    failed = len(errors) if failure_count is None else failure_count
    payload_errors = errors if result_errors is None else result_errors
    payload = {
        "journeys": [
            {
                "file": str(item.file_path),
                "journey_name": item.journey_name,
                "report": asdict(item.report),
            }
            for item in executed
        ],
        "errors": [asdict(error) for error in payload_errors],
    }
    _CLI_LOGGER.info(
        "execute_summary",
        "Summary: "
        f"{_count(len(executed), 'journey')} executed, "
        f"{_count(total_cases, 'case')} executed, "
        f"{failed} failed",
        pretty=pretty_line(
            "Summary: "
            f"{_count(len(executed), 'journey')} executed, "
            f"{_count(total_cases, 'case')} executed, "
            f"{failed} failed",
            indent=2,
            style="heading",
        ),
        journeys=len(executed),
        cases=total_cases,
        failures=failed,
    )
    _CLI_LOGGER.info(
        "execute_result",
        "execution result",
        pretty=False,
        root=str(root),
        journeys=len(executed),
        errors=len(payload_errors),
        payload=payload,
    )


def _emit_execution_section() -> None:
    _CLI_LOGGER.info(
        "execution_section",
        "Execution",
        pretty=pretty_line("Execution", style="heading"),
    )


def _emit_interrupt_output(*, state: str | None) -> None:
    _CLI_LOGGER.warning(
        "execution_interrupted",
        "journey execution was interrupted",
        pretty=pretty_line(
            "Interrupted: Journey execution was interrupted before it finished.",
            style="warning",
        ),
        state=state,
    )
    hint = (
        f"Run the same command again with --state {state} to resume from saved progress."
        if state is not None
        else (
            "This run was interrupted without --state, so it cannot resume. "
            "Run the same command again to start over, or rerun with --state <path> "
            "next time to make Ctrl-C resumable."
        )
    )
    _CLI_LOGGER.warning(
        "interrupt_summary",
        "Interrupted.",
        pretty=pretty_line(f"Hint: {hint}", style="warning"),
        state=state,
        interrupted=True,
        hint=hint,
    )
    _CLI_LOGGER.warning(
        "interrupt_message",
        "What happened: Journey execution was interrupted before it finished.",
        pretty=False,
        state=state,
        interrupted=True,
        hint=hint,
    )
    if state is None:
        return
    _CLI_LOGGER.warning(
        "interrupt_hint",
        f"Try this: {hint}",
        pretty=False,
        state=state,
    )


def _cmd_execute(args: argparse.Namespace) -> int:
    try:
        root, targets, errors = _discover_targets(args)
    except JourneySelectionError as exc:
        error = _error_from_exception(exc, phase="plan")
        root = Path.cwd().resolve()
        _emit_plan_output(root, [], [error])
        _emit_execution_section()
        _emit_execute_output(
            root,
            [],
            [],
            result_errors=[error],
            failure_count=1,
        )
        return 1

    executed: list[_ExecutedJourney] = []
    run_errors: list[_CommandError] = []

    if state_errors := _state_selection_errors(args, targets):
        errors.extend(state_errors)
        _emit_plan_output(root, [], errors)
        _emit_execution_section()
        _emit_execute_output(
            root,
            [],
            [],
            result_errors=errors,
            failure_count=len(errors),
        )
        return 1

    compiled, compile_errors = _compile_targets(
        targets,
        fail_fast=args.fail_fast,
    )
    errors.extend(compile_errors)

    _emit_plan_output(root, compiled, errors)
    _emit_execution_section()

    try:
        with _graceful_cli_interrupts(args.state is not None):
            should_execute = bool(compiled) and not (args.fail_fast and errors)
            if not should_execute:
                run_errors = []
            elif args.step is None and args.develop_step is None:
                run_results, run_errors = _execute_all_targets(
                    compiled,
                    root=root,
                    fail_fast=args.fail_fast,
                    state=args.state,
                    stream_live=True,
                    no_memory=args.no_memory,
                    no_memory_update=args.no_memory_update,
                )
                executed.extend(run_results)
            else:
                if args.develop_step is not None:
                    run_results, run_errors = _execute_target_pause(
                        compiled,
                        root=root,
                        develop_step=args.develop_step,
                        state=args.state,
                        stream_live=True,
                        interactive=args.interactive,
                        no_memory=args.no_memory,
                        no_memory_update=args.no_memory_update,
                    )
                else:
                    run_results, run_errors = _execute_target_step(
                        compiled,
                        root=root,
                        step=args.step,
                        state=args.state,
                        stream_live=True,
                        no_memory=args.no_memory,
                        no_memory_update=args.no_memory_update,
                    )
                executed.extend(run_results)
    except KeyboardInterrupt:
        _emit_interrupt_output(state=args.state)
        return 130

    all_errors = [*errors, *run_errors]
    _emit_execute_output(
        root,
        executed,
        run_errors,
        result_errors=all_errors,
        failure_count=len(all_errors),
    )
    return 0 if not all_errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
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
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable prompt-memory reads and writes for this run",
    )
    parser.add_argument(
        "--no-memory-update",
        action="store_true",
        help="Disable prompt-memory writes while still allowing reads for this run",
    )
    parser.add_argument(
        "--output",
        choices=("pretty", "structured", "jsonl"),
        default="pretty",
        help="Set Journey output format (default: pretty)",
    )
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error", "off"),
        default="info",
        help="Set Journey diagnostic logging level (default: info)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first discovery, compilation, or execution failure",
    )

    return parser


def _extract_option_value(argv: list[str], option: str) -> str | None:
    prefix = f"{option}="
    for index, value in enumerate(argv):
        if value.startswith(prefix):
            return value[len(prefix) :]
        if value == option and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _preconfigure_logging(argv: list[str]) -> None:
    level = _extract_option_value(argv, "--log-level") or "info"
    output = _extract_option_value(argv, "--output")
    output_format = output if output in {"pretty", "structured", "jsonl"} else "pretty"
    try:
        configure_logging(level, output_format=output_format)  # type: ignore[arg-type]
    except ValueError:
        configure_logging("info", output_format=output_format)


def _active_environment_python() -> Path | None:
    if "UV_RUN_RECURSION_DEPTH" not in os.environ:
        return None
    if os.environ.get("JOURNEY_ACTIVE_ENV_REEXEC") == "1":
        return None

    virtual_env = os.environ.get("VIRTUAL_ENV")
    if not virtual_env:
        return None

    active_prefix = Path(virtual_env).resolve()
    current_prefix = Path(sys.prefix).resolve()
    if current_prefix == active_prefix:
        return None

    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable_name = "python.exe" if os.name == "nt" else "python"
    active_python = active_prefix / scripts_dir / executable_name
    if not active_python.exists():
        return None
    return active_python


def _reexec_with_active_environment(argv: list[str]) -> None:
    active_python = _active_environment_python()
    if active_python is None:
        return

    env = {**os.environ, "JOURNEY_ACTIVE_ENV_REEXEC": "1"}
    os.execve(
        str(active_python),
        [str(active_python), "-m", "journeysdk.cli", *argv],
        env,
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if argv is None:
        _reexec_with_active_environment(raw_argv)
    _preconfigure_logging(raw_argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, output_format=args.output)
    if args.interactive and getattr(args, "develop_step", None) is None:
        parser.error("--interactive requires --develop-step")
    return _cmd_execute(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
