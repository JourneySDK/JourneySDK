"""CLI for journey v1."""

from __future__ import annotations

import argparse
import os
import re
import signal
import shlex
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .agent_instructions import (
    install_agent_instructions,
    render_agent_bootstrap,
    supported_agent_instruction_targets,
)
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
    _prompt_memory_root_for_state_path,
    _resolve_browser_recording_root,
    _use_step_interrupt_controller,
)
from .logger import configure_logging, get_logger, pretty_line, pretty_row
from .models import (
    BranchMarkerNode,
    CaseExecutionReport,
    CasePlan,
    ExecutionReport,
    JourneyPlan,
    NodeExecutionRecord,
    StepNode,
)
from .planner import compile_journey
from .recordings import (
    CaseRecording,
    ExecutionRecording,
    RecordingError,
    discover_recording_cases,
    ensure_case_trace,
    ensure_case_video,
    ensure_execution_trace,
    ensure_execution_video,
    group_execution_recordings,
    open_trace_viewer,
    open_video_recording,
)
from .state import (
    default_execution_state_path,
    delete_artifact_root,
    load_execution_state,
    prepare_execution_state_storage,
)
from .touchpoint_references import (
    render_touchpoint_docs,
    supported_touchpoint_doc_targets,
)
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
        stripped = message.rstrip()
        if not stripped:
            return
        if ": error:" in stripped:
            _CLI_LOGGER.error("parser_error", stripped)
            return
        _CLI_LOGGER.info("parser_output", stripped)

    def error(self, message: str) -> None:
        usage = self.format_usage().rstrip()
        if usage:
            _CLI_LOGGER.info("parser_usage", usage, pretty=usage)
        problem = f"{self.prog}: error: {message}"
        help_command = _help_command_for_prog(self.prog)
        instructions = _parser_error_instructions(self.prog, message)
        next_commands = _parser_error_next_commands(self.prog, message)
        _CLI_LOGGER.error(
            "parser_error",
            problem,
            pretty=pretty_line(f"What happened: {problem}", style="error"),
            error_message=problem,
            instructions=instructions,
            next_commands=next_commands,
            help_command=help_command,
        )
        _CLI_LOGGER.error(
            "parser_error_hint",
            f"Try this: {instructions}",
            pretty=pretty_line(f"Try this: {instructions}", style="error"),
            instructions=instructions,
            help_command=help_command,
        )
        _emit_next_commands(
            next_commands,
            level="error",
            event="parser_error_next_commands",
            help_command=help_command,
        )
        self.exit(2)


class _CliStepInterruptController:
    def __init__(self, *, graceful: bool = True) -> None:
        self._graceful = graceful
        self._phase: str | None = None
        self._pending_interrupt = False
        self._forced_interrupt_requested = False
        self._forced_interrupt_logged = False
        self._forced_interrupt_cleanup_started = False
        self._forced_interrupt_callback_id = 0
        self._forced_interrupt_callbacks: dict[int, tuple[str, Callable[[], None]]] = {}

    def on_step_lifecycle_phase(self, phase: str | None) -> None:
        self._phase = phase

    def is_step_interrupt_pending(self) -> bool:
        return self._pending_interrupt

    def is_step_forced_interrupt_requested(self) -> bool:
        return self._forced_interrupt_requested

    def register_forced_interrupt_callback(
        self,
        name: str,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        callback_id = self._forced_interrupt_callback_id
        self._forced_interrupt_callback_id += 1
        self._forced_interrupt_callbacks[callback_id] = (name, callback)

        def unregister() -> None:
            self._forced_interrupt_callbacks.pop(callback_id, None)

        return unregister

    def raise_if_interrupted_after_step(self) -> None:
        if not self._pending_interrupt:
            return
        self._pending_interrupt = False
        raise KeyboardInterrupt()

    def handle_sigint(self, signum: int, frame: object) -> None:
        if (
            self._graceful
            and self._phase in _GRACEFUL_INTERRUPT_PHASES
            and not self._pending_interrupt
        ):
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
        self._forced_interrupt_requested = True
        if self._pending_interrupt and not self._forced_interrupt_logged:
            self._forced_interrupt_logged = True
            message = (
                "Ctrl-C received again. Stopping now; this step will restart "
                "from the nearest replay boundary on resume."
            )
            _CLI_LOGGER.warning(
                "forced_interrupt_requested",
                message,
                pretty=pretty_line(message, style="warning"),
                phase=self._phase,
            )
        self._start_forced_interrupt_cleanup()
        raise KeyboardInterrupt()

    def _start_forced_interrupt_cleanup(self) -> None:
        if self._forced_interrupt_cleanup_started:
            return
        self._forced_interrupt_cleanup_started = True
        callbacks = tuple(self._forced_interrupt_callbacks.values())
        if not callbacks:
            return

        def cleanup_worker() -> None:
            for name, callback in callbacks:
                try:
                    callback()
                except BaseException as exc:  # pragma: no cover - defensive logging
                    _CLI_LOGGER.debug(
                        "forced_interrupt_cleanup_failure",
                        "forced interrupt cleanup callback failed",
                        callback=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )

        threading.Thread(
            target=cleanup_worker,
            name="journey-forced-interrupt-cleanup",
            daemon=True,
        ).start()


@contextmanager
def _graceful_cli_interrupts(enabled: bool) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    controller = _CliStepInterruptController(graceful=enabled)
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
    instructions: str | None = None
    next_commands: tuple[str, ...] = ()
    help_command: str | None = None
    step_label: str | None = None


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


def _help_command_for_prog(prog: str) -> str:
    if prog == "journey evidence":
        return f"{prog} --help"
    if prog == "journey agent":
        return "journey agent --help"
    if prog == "journey discover":
        return "journey discover --help"
    if prog == "journey loop":
        return "journey loop --help"
    if prog == "journey verify":
        return "journey verify --help"
    if prog == "journey touchpoints":
        return "journey touchpoints --help"
    return "journey --help"


def _parser_error_instructions(prog: str, message: str) -> str:
    if prog == "journey evidence":
        if "--branch" in message:
            return (
                f"Use --branch KEY=VALUE, or run `{prog} --list-scopes` "
                "to discover branch filters from saved evidence."
            )
        return (
            f"Run `{prog} --help`, then use `{prog} --list-scopes` "
            f"and `{prog} --list-log-sources` to discover valid filters."
        )
    if prog == "journey agent":
        return (
            "Run `journey agent --help`, choose one target "
            "(codex, claude, cursor, or generic), and add --install only when "
            "writing persistent instructions."
        )
    if prog == "journey discover":
        return (
            "Run `journey discover --help`, pass one or more start URLs, and "
            "use --file to choose where the generated Journey spec should be written."
        )
    return (
        "Run `journey --help`, choose `journey loop` for focused replay or "
        "`journey verify` for fresh confidence, and use "
        "`journey agent <target>` or `journey touchpoints <name>` "
        "when you need packaged Journey guidance."
    )


def _parser_error_next_commands(prog: str, message: str) -> tuple[str, ...]:
    if prog == "journey evidence":
        commands = [f"{prog} --help", f"{prog} --list-scopes"]
        if "--branch" not in message:
            commands.append(f"{prog} --list-log-sources")
        return tuple(commands)
    if prog == "journey agent":
        return ("journey agent --help", "journey agent codex")
    if prog == "journey discover":
        return ("journey discover --help", "journey verify --help")
    return ("journey --help", "journey agent codex", "journey evidence --help")


def _default_help_command_for_phase(phase: str) -> str:
    if phase == "logs":
        return "journey evidence --help"
    if phase == "agent":
        return "journey agent --help"
    if phase == "discover":
        return "journey discover --help"
    return "journey --help"


def _default_hint_for_error(error: _CommandError) -> str:
    if error.hint is not None:
        return error.hint
    if error.phase == "plan":
        return (
            "Fix the journey file import, decorator, branch, or step structure, "
            "then rerun the Journey command so it executes the selected flow."
        )
    if error.phase == "execute":
        return (
            "Use the first failed step as the source of truth, inspect logs when "
            "artifacts exist, then rerun the focused `journey loop` command "
            "until it passes."
        )
    if error.phase == "logs":
        return (
            "Run `journey evidence --help`, discover available scopes with "
            "`journey evidence --list-scopes`, then retry with valid filters."
        )
    if error.phase == "agent":
        return (
            "Run `journey agent --help`, choose a supported target, and use "
            "`--force` only with `--install` when replacing existing guidance."
        )
    if error.phase == "discover":
        return (
            "Run `journey discover --help`, confirm the app URL is reachable, "
            "then retry with a writable --file path and model credentials."
        )
    return "Run the related Journey --help command, then retry with the corrected command."


def _default_instructions_for_error(error: _CommandError) -> str:
    if error.instructions is not None:
        return error.instructions
    if error.phase == "plan":
        return (
            "Planning failed before Journey could execute steps. Read the "
            "`What happened` line, fix the selected journey file or target, and "
            "rerun the Journey command to verify the journey with execution."
        )
    if error.phase == "execute":
        return (
            "Execution failed at a Journey step. Use `Retry failed step:` when "
            "present; otherwise select the failing label with `journey loop`, "
            "inspect `journey evidence`, and broaden to `journey verify --step` or a full fresh run after "
            "the focused loop passes."
        )
    if error.phase == "logs":
        return (
            "Evidence inspection failed. Use `journey evidence --list-scopes` to discover "
            "case, branch, and step filters, then use `--list-log-sources`, "
            "`--show`, or `--paths` with valid filters."
        )
    if error.phase == "agent":
        return (
            "Agent guidance command failed. Use `journey agent --help` to choose "
            "a target and install mode, then retry with a valid command."
        )
    if error.phase == "discover":
        return (
            "Journey discovery failed before a generated spec could be written. "
            "Read the `What happened` line, fix URL, browser, model, or output "
            "path setup, then rerun `journey discover`."
        )
    return "Use the related Journey help command, correct the command, and retry."


def _target_selection_command(root: Path, error: _CommandError, *extra: str) -> str:
    parts: list[str] = ["journey", "verify"]
    if error.file is not None:
        try:
            display_file = _display_path(root, Path(error.file))
        except TypeError:
            display_file = error.file
        parts.extend(("--file", display_file))
    if error.journey_name is not None:
        parts.extend(("--journey", error.journey_name))
    parts.extend(extra)
    return " ".join(shlex.quote(part) for part in parts)


def _default_next_commands_for_error(root: Path, error: _CommandError) -> tuple[str, ...]:
    commands: list[str] = list(error.next_commands)
    if error.phase == "plan":
        if error.file is not None or error.journey_name is not None:
            commands.append(_target_selection_command(root, error))
        else:
            commands.append("journey --help")
    elif error.phase == "execute":
        if retry_command := _retry_command_for_error(root, error):
            commands.append(retry_command)
        if error.step_label is not None:
            commands.append(
                " ".join(
                    shlex.quote(part)
                    for part in (
                        "journey",
                        "evidence",
                        "--list-log-sources",
                        "--step",
                        error.step_label,
                    )
                )
            )
            commands.append(
                " ".join(
                    shlex.quote(part)
                    for part in ("journey", "evidence", "--paths", "--step", error.step_label)
                )
            )
        else:
            commands.append("journey evidence --list-scopes")
    elif error.phase == "logs":
        commands.extend(("journey evidence --list-scopes", "journey evidence --list-log-sources"))
    elif error.phase == "agent":
        commands.append("journey agent --help")
    elif error.phase == "discover":
        commands.append("journey discover --help")

    help_command = error.help_command or _default_help_command_for_phase(error.phase)
    commands.append(help_command)
    return tuple(dict.fromkeys(command for command in commands if command))


def _resolved_error_guidance(
    root: Path,
    error: _CommandError,
) -> tuple[str, tuple[str, ...], str, str]:
    instructions = _default_instructions_for_error(error)
    next_commands = _default_next_commands_for_error(root, error)
    help_command = error.help_command or _default_help_command_for_phase(error.phase)
    hint = _default_hint_for_error(error)
    return instructions, next_commands, help_command, hint


def _command_error_payload(root: Path, error: _CommandError) -> dict[str, object]:
    payload = asdict(error)
    instructions, next_commands, help_command, hint = _resolved_error_guidance(root, error)
    payload["hint"] = hint
    payload["instructions"] = instructions
    payload["next_commands"] = next_commands
    payload["help_command"] = help_command
    return payload


def _emit_next_commands(
    commands: tuple[str, ...],
    *,
    level: str,
    event: str,
    help_command: str,
) -> None:
    if not commands:
        return
    lines: list[str | object] = [pretty_line("Next commands:", style="error" if level == "error" else "warning")]
    lines.extend(pretty_line(f"  {command}", style="error" if level == "error" else "warning") for command in commands)
    log = _CLI_LOGGER.error if level == "error" else _CLI_LOGGER.warning
    log(
        event,
        "Next commands: " + " ; ".join(commands),
        pretty=lines,
        next_commands=commands,
        help_command=help_command,
    )


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
            f"  step {_step_name(node)} attempt={attempt} start status=executed",
            pretty=pretty_row(
                _step_name(node),
                _pretty_step_detail("start executed", attempt=attempt),
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
            status="executed",
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
            f"  branch {node.group_id}={node.active_key} status=executed",
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
            status="executed",
        )

    def on_step_replay(
        self,
        *,
        case_plan: CasePlan,
        node: StepNode,
        node_index: int,
        record: NodeExecutionRecord,
    ) -> None:
        self._logger.info(
            "step_replay",
            f"  step {_step_name(node)} replayed",
            pretty=pretty_row(
                _step_name(node),
                "replayed",
                indent=6,
                label_width=29,
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            case=case_plan.case_id,
            step=_step_name(node),
            node_index=node_index,
            status=record.status,
        )

    def on_branch_replay(
        self,
        *,
        case_plan: CasePlan,
        node: BranchMarkerNode,
        node_index: int,
        record: NodeExecutionRecord,
    ) -> None:
        self._logger.info(
            "branch_replay",
            f"  branch {node.group_id}={node.active_key} replayed",
            pretty=pretty_row(
                f"branch {node.group_id}",
                f"replayed {node.active_key}",
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
            status=record.status,
        )

    def on_case_replay(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
    ) -> None:
        self._logger.info(
            "case_replay",
            f"- {report.case_id} replay branches={_format_branch_env(case_plan.branch_env)}",
            pretty=pretty_line(
                f"    {report.case_id} replay"
                + (
                    f"  branches={_format_branch_env(case_plan.branch_env)}"
                    if case_plan.branch_env
                    else ""
                ),
                style="context",
            ),
            file=self._display,
            journey=self._journey_name,
            case=report.case_id,
            branches=_format_branch_env(case_plan.branch_env),
            steps=_count_step_records(report),
            status="replayed",
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
                f"  step {_step_name(node)} attempt={attempt} executed "
                f"duration={_format_duration(duration_seconds)}"
            ),
            pretty=pretty_row(
                _step_name(node),
                _pretty_step_detail(
                    "executed",
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
            status="executed",
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
            status="failed",
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
            status="failed",
        )

    def on_case_complete(
        self,
        *,
        case_plan: CasePlan,
        report: CaseExecutionReport,
        duration_seconds: float,
    ) -> None:
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

    def on_state_validity(
        self,
        *,
        boundary_id: str,
        status: str,
        reason: str | None,
        expected: str | None,
        actual: str | None,
        action: str,
    ) -> None:
        if status == "fresh":
            message = "State: fresh run"
            pretty = pretty_line(message, indent=2, style="context")
        elif status == "replayed":
            message = f"State: reused boundary {boundary_id}"
            pretty = pretty_line(message, indent=2, style="context")
        else:
            reason_text = reason or "state changed"
            message = (
                f"State: invalidated boundary {boundary_id}; "
                f"Reason: {reason_text}; Action: {action}"
            )
            pretty = pretty_line(message, indent=2, style="warning")
        self._logger.info(
            "state_validity",
            message,
            pretty=pretty,
            file=self._display,
            journey=self._journey_name,
            boundary_id=boundary_id,
            status=status,
            reason=reason,
            expected=expected,
            actual=actual,
            action=action,
        )


def _error_from_exception(
    exc: Exception,
    *,
    phase: str,
    file_path: str | None = None,
    journey_name: str | None = None,
) -> _CommandError:
    step_label = getattr(exc, "step_label", None)
    return _CommandError(
        file=file_path,
        journey_name=journey_name,
        phase=phase,
        error_type=type(exc).__name__,
        message=str(exc),
        hint=exc.hint if isinstance(exc, JourneyError) else None,
        step_label=step_label if isinstance(step_label, str) else None,
    )


def _emit_errors(root: Path, errors: list[_CommandError]) -> None:
    for error in errors:
        location = error.file or "<selection>"
        if error.journey_name is not None:
            location = f"{location}:{error.journey_name}"
        instructions, next_commands, help_command, hint = _resolved_error_guidance(root, error)
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
            hint=hint,
            instructions=instructions,
            next_commands=next_commands,
            help_command=help_command,
        )
        _CLI_LOGGER.error(
            "command_error_message",
            f"What happened: {error.message}",
            pretty=pretty_line(f"What happened: {error.message}", style="error"),
            phase=error.phase,
            location=location,
            error_type=error.error_type,
        )
        _CLI_LOGGER.error(
            "command_error_hint",
            f"Try this: {hint}",
            pretty=pretty_line(f"Try this: {hint}", style="error"),
            phase=error.phase,
            location=location,
            error_type=error.error_type,
            instructions=instructions,
            help_command=help_command,
        )
        _emit_next_commands(
            next_commands,
            level="error",
            event="command_error_next_commands",
            help_command=help_command,
        )
        retry_command = _retry_command_for_error(root, error)
        if retry_command is not None:
            _CLI_LOGGER.error(
                "command_error_retry",
                f"Retry failed step: {retry_command}",
                pretty=pretty_line(f"Retry failed step: {retry_command}", style="error"),
                phase=error.phase,
                location=location,
                error_type=error.error_type,
                retry_command=retry_command,
            )
        artifacts = _artifact_hint_for_error(root, error)
        if artifacts is not None:
            _CLI_LOGGER.error(
                "command_error_artifacts",
                f"Artifacts: {artifacts}",
                pretty=pretty_line(f"Artifacts: {artifacts}", style="error"),
                phase=error.phase,
                location=location,
                error_type=error.error_type,
                artifacts=artifacts,
            )


def _retry_command_for_error(root: Path, error: _CommandError) -> str | None:
    if error.phase != "execute":
        return None
    if error.file is None or error.journey_name is None or error.step_label is None:
        return None
    try:
        display_file = _display_path(root, Path(error.file))
    except TypeError:
        display_file = error.file
    return " ".join(
        shlex.quote(part)
        for part in (
            "journey",
            "loop",
            error.step_label,
            "--file",
            display_file,
            "--journey",
            error.journey_name,
        )
    )


def _artifact_hint_for_error(root: Path, error: _CommandError) -> str | None:
    if error.phase != "execute":
        return None
    if error.file is None:
        return None
    return _artifact_hint_for_file(root, error.file)


def _artifact_hint_for_file(root: Path, file_path: str | Path) -> str | None:
    logs_root = Path(file_path).resolve().parent / ".journey" / "logs"
    if not logs_root.exists():
        return None
    return f"{_display_path(root, logs_root)} (run `journey evidence` to inspect)"


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
            f"Loop {action} after step "
            f"{step_name} attempt={paused.paused_step.attempt} executed."
        )
    if paused.paused_step.error:
        return (
            f"Loop {action} after step "
            f"{step_name} attempt={paused.paused_step.attempt} "
            f"failed ({paused.paused_step.error})."
        )
    return (
        f"Loop {action} after step "
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
        step_label=paused.paused_step.label or paused.paused_step.node_id,
    )


def _temporary_pause_state_path() -> Path:
    with tempfile.NamedTemporaryFile(
        delete=False,
        prefix=".journey-pause.",
        suffix=".json",
    ) as handle:
        path = Path(handle.name)
    path.unlink(missing_ok=True)
    return path


def _default_state_path_for_target(
    selected: _CompiledJourney,
) -> Path:
    return default_execution_state_path(selected.file_path)


def _prompt_memory_root_for_target(
    selected: _CompiledJourney,
) -> Path:
    return _prompt_memory_root_for_state_path(_default_state_path_for_target(selected))


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
                    "Rerun the same journey loop target to retry the failed step, "
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
            "Rerun the paused journey loop target to retry it, target the next "
            "later step to continue, or delete the state file to start fresh."
        ),
    )


def _execute_all_targets(
    compiled: list[_CompiledJourney],
    *,
    root: Path,
    fail_fast: bool,
    no_state: bool = False,
    no_state_update: bool = False,
    stream_live: bool = False,
    no_memory: bool = False,
    no_memory_update: bool = False,
    no_browser_recording: bool = False,
    no_logs: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    executed: list[_ExecutedJourney] = []
    errors: list[_CommandError] = []
    cleaned_recording_roots: set[Path] = set()

    for index, item in enumerate(compiled):
        recording_root = _resolve_browser_recording_root(item.function)
        clean_browser_recordings = recording_root not in cleaned_recording_roots
        cleaned_recording_roots.add(recording_root)
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
                state=None,
                observer=observer,
                no_state=no_state,
                no_state_update=no_state_update,
                no_memory=no_memory,
                no_memory_update=no_memory_update,
                no_browser_recording=no_browser_recording,
                clean_browser_recordings=clean_browser_recordings,
                no_logs=no_logs,
                clean_logs=clean_browser_recordings,
                prompt_memory_root=_prompt_memory_root_for_target(item),
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
    no_state: bool = False,
    no_state_update: bool = False,
    stream_live: bool = False,
    no_memory: bool = False,
    no_memory_update: bool = False,
    no_browser_recording: bool = False,
    no_logs: bool = False,
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
            state=None,
            observer=observer,
            no_state=no_state,
            no_state_update=no_state_update,
            no_memory=no_memory,
            no_memory_update=no_memory_update,
            no_browser_recording=no_browser_recording,
            no_logs=no_logs,
            prompt_memory_root=_prompt_memory_root_for_target(selected),
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
    no_state: bool = False,
    no_state_update: bool = False,
    stream_live: bool = False,
    interactive: bool = False,
    no_memory: bool = False,
    no_memory_update: bool = False,
    no_browser_recording: bool = False,
    no_logs: bool = False,
) -> tuple[list[_ExecutedJourney], list[_CommandError]]:
    selected, errors = _select_targeted_journey(compiled, step=develop_step)
    if selected is None:
        return [], errors

    default_state_path = _default_state_path_for_target(selected)
    state_arg: str | Path | None = None
    cleanup_state = False
    cleanup_root: Path | None = None

    if no_state:
        state_arg = _temporary_pause_state_path()
        cleanup_state = True
    elif no_state_update:
        storage = prepare_execution_state_storage(
            default_state_path,
            update_enabled=False,
        )
        state_arg = storage.run_path or _temporary_pause_state_path()
        cleanup_root = storage.cleanup_root

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
        clean_browser_recordings = True

        if not interactive:
            if not no_state:
                pause_action = _infer_develop_pause_action(
                    Path(state_arg) if state_arg is not None else default_state_path,
                    selected=selected,
                    develop_step=develop_step,
                )
            outcome = _execute_plan(
                selected.function,
                plan=selected.plan,
                develop_step=develop_step,
                pause_action=pause_action,
                state=state_arg,
                observer=observer,
                no_state=False,
                no_state_update=False,
                no_memory=no_memory,
                no_memory_update=no_memory_update,
                no_browser_recording=no_browser_recording,
                clean_browser_recordings=clean_browser_recordings,
                no_logs=no_logs,
                clean_logs=clean_browser_recordings,
                prompt_memory_root=_prompt_memory_root_for_target(selected),
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
                "loop execution succeeded",
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
                state=state_arg,
                observer=observer,
                no_state=False,
                no_state_update=False,
                no_memory=no_memory,
                no_memory_update=no_memory_update,
                no_browser_recording=no_browser_recording,
                clean_browser_recordings=clean_browser_recordings,
                no_logs=no_logs,
                clean_logs=clean_browser_recordings,
                prompt_memory_root=_prompt_memory_root_for_target(selected),
            )
            clean_browser_recordings = False
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
                "loop execution succeeded",
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
            "loop execution failed",
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
            assert state_arg is not None
            state_path = Path(state_arg)
            state_path.unlink(missing_ok=True)
            delete_artifact_root(state_path.parent / f"{state_path.name}.artifacts")
        if cleanup_root is not None:
            delete_artifact_root(cleanup_root)


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

    _emit_errors(root, errors)

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
    develop_step_stopped: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    _emit_errors(root, errors)

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
        "errors": [_command_error_payload(root, error) for error in payload_errors],
    }
    summary = (
        f"Summary: loop {develop_step_stopped} stopped after target, {failed} failed"
        if develop_step_stopped is not None
        else (
            "Summary: "
            f"{_count(len(executed), 'journey')} executed, "
            f"{_count(total_cases, 'case')} executed, "
            f"{failed} failed"
        )
    )
    if duration_seconds is not None:
        summary = f"{summary}, duration={_format_duration(duration_seconds)}"
    _CLI_LOGGER.info(
        "execute_summary",
        summary,
        pretty=pretty_line(summary, indent=2, style="heading"),
        journeys=len(executed),
        cases=total_cases,
        failures=failed,
        develop_step=develop_step_stopped,
        duration_seconds=duration_seconds,
    )
    artifact_hints = sorted(
        {
            hint
            for item in executed
            if (hint := _artifact_hint_for_file(root, item.file_path)) is not None
        }
    )
    for hint in artifact_hints:
        _CLI_LOGGER.info(
            "execute_artifacts",
            f"Artifacts: {hint}",
            pretty=pretty_line(f"Artifacts: {hint}", indent=2, style="muted"),
            artifacts=hint,
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


def _emit_interrupt_output(*, resumable: bool) -> None:
    _CLI_LOGGER.warning(
        "execution_interrupted",
        "journey execution was interrupted",
        pretty=pretty_line(
            "Interrupted: Journey execution was interrupted before it finished.",
            style="warning",
        ),
        resumable=resumable,
    )
    hint = (
        "Run the same command again to resume from saved progress."
        if resumable
        else (
            "This run could not save new progress, so it cannot resume from this interruption. "
            "Run the same command again to start over, or allow state updates next time to make Ctrl-C resumable."
        )
    )
    _CLI_LOGGER.warning(
        "interrupt_summary",
        "Interrupted.",
        pretty=pretty_line(f"Hint: {hint}", style="warning"),
        resumable=resumable,
        interrupted=True,
        hint=hint,
    )
    _CLI_LOGGER.warning(
        "interrupt_message",
        "What happened: Journey execution was interrupted before it finished.",
        pretty=False,
        resumable=resumable,
        interrupted=True,
        hint=hint,
    )
    if not resumable:
        return
    _CLI_LOGGER.warning(
        "interrupt_hint",
        f"Try this: {hint}",
        pretty=False,
        resumable=resumable,
    )


def _cmd_execute(args: argparse.Namespace) -> int:
    started_at = time.perf_counter()
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
            duration_seconds=time.perf_counter() - started_at,
        )
        return 1

    executed: list[_ExecutedJourney] = []
    run_errors: list[_CommandError] = []

    compiled, compile_errors = _compile_targets(
        targets,
        fail_fast=args.fail_fast,
    )
    errors.extend(compile_errors)

    _emit_plan_output(root, compiled, errors)

    _emit_execution_section()

    try:
        state_updates_enabled = not args.no_state and not args.no_state_update
        with _graceful_cli_interrupts(state_updates_enabled):
            should_execute = bool(compiled) and not (args.fail_fast and errors)
            if not should_execute:
                run_errors = []
            elif args.step is None and args.develop_step is None:
                run_results, run_errors = _execute_all_targets(
                    compiled,
                    root=root,
                    fail_fast=args.fail_fast,
                    no_state=args.no_state,
                    no_state_update=args.no_state_update,
                    stream_live=True,
                    no_memory=args.no_memory,
                    no_memory_update=args.no_memory_update,
                    no_browser_recording=args.no_browser_recording,
                    no_logs=args.no_logs,
                )
                executed.extend(run_results)
            else:
                if args.develop_step is not None:
                    run_results, run_errors = _execute_target_pause(
                        compiled,
                        root=root,
                        develop_step=args.develop_step,
                        no_state=args.no_state,
                        no_state_update=args.no_state_update,
                        stream_live=True,
                        interactive=args.interactive,
                        no_memory=args.no_memory,
                        no_memory_update=args.no_memory_update,
                        no_browser_recording=args.no_browser_recording,
                        no_logs=args.no_logs,
                    )
                else:
                    run_results, run_errors = _execute_target_step(
                        compiled,
                        root=root,
                        step=args.step,
                        no_state=args.no_state,
                        no_state_update=args.no_state_update,
                        stream_live=True,
                        no_memory=args.no_memory,
                        no_memory_update=args.no_memory_update,
                        no_browser_recording=args.no_browser_recording,
                        no_logs=args.no_logs,
                    )
                executed.extend(run_results)
    except KeyboardInterrupt:
        _emit_interrupt_output(resumable=state_updates_enabled)
        return 130

    all_errors = [*errors, *run_errors]
    _emit_execute_output(
        root,
        executed,
        run_errors,
        result_errors=all_errors,
        failure_count=len(all_errors),
        develop_step_stopped=(
            args.develop_step
            if args.develop_step is not None and not executed and not all_errors
            else None
        ),
        duration_seconds=time.perf_counter() - started_at,
    )
    return 0 if not all_errors else 1


def _cmd_agent(args: argparse.Namespace) -> int:
    target = args.target
    if not args.install:
        sys.stdout.write(render_agent_bootstrap(target))
        return 0

    try:
        destination = install_agent_instructions(
            target,
            root=Path.cwd(),
            force=args.force,
        )
    except FileExistsError as exc:
        existing_path = Path(exc.filename or exc.args[0])
        _emit_errors(
            Path.cwd().resolve(),
            [
                _CommandError(
                    file=str(existing_path),
                    journey_name=None,
                    phase="agent",
                    error_type="FileExistsError",
                    message=f"Agent instruction file already exists: {existing_path}.",
                    hint="Pass `--force` with `--install` to replace the existing file.",
                    instructions=(
                        "Decide whether the existing assistant guidance should be "
                        "kept or replaced. To replace it, rerun the same "
                        "`journey agent` install command with `--force`."
                    ),
                    next_commands=(
                        f"journey agent {target} --install --force",
                        "journey agent --help",
                    ),
                    help_command="journey agent --help",
                )
            ],
        )
        return 1

    _CLI_LOGGER.info(
        "agent_instruction_installed",
        f"Installed agent instructions at {destination}",
        pretty=pretty_line(
            f"Installed agent instructions: {destination}",
            style="success",
        ),
        target=target,
        path=str(destination),
    )
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    from .discover import DiscoverOptions, discover

    root = Path.cwd().resolve()
    try:
        result = discover(
            DiscoverOptions(
                urls=tuple(args.url),
                output_file=Path(args.file),
                journey_name=args.journey_name,
                depth=args.depth,
                max_actions=args.max_actions,
                max_model_calls=args.max_model_calls,
                max_variants_per_control=args.max_variants_per_control,
                side_effect_probes=args.side_effect_probes,
                browser=args.browser,
                headless=not args.headed,
                model=args.model,
                allow_external=args.allow_external,
                force=args.force,
            )
        )
    except Exception as exc:
        _emit_errors(
            root,
            [
                _CommandError(
                    file=args.file,
                    journey_name=args.journey_name,
                    phase="discover",
                    error_type=type(exc).__name__,
                    message=str(exc) or type(exc).__name__,
                    hint=getattr(exc, "hint", None),
                    next_commands=("journey discover --help",),
                    help_command="journey discover --help",
                )
            ],
        )
        return 1

    verify_command = " ".join(
        shlex.quote(part)
        for part in (
            "journey",
            "verify",
            "--file",
            _display_path(root, result.output_file.resolve()),
        )
    )
    _CLI_LOGGER.info(
        "discover_result",
        f"Generated Journey spec at {result.output_file}",
        pretty=[
            pretty_line(f"Generated Journey spec: {result.output_file}", style="success"),
            pretty_line(
                (
                    f"  journey={result.journey_name} actions={result.actions} "
                    f"branches={result.branches} omitted={result.omitted_actions} "
                    f"model_calls={result.model_calls} stop={result.stop_reason}"
                ),
                style="success",
            ),
            pretty_line("Next commands:", style="success"),
            pretty_line(f"  {verify_command}", style="success"),
        ],
        output_file=str(result.output_file),
        journey_name=result.journey_name,
        actions=result.actions,
        branches=result.branches,
        omitted_actions=result.omitted_actions,
        model_calls=result.model_calls,
        stop_reason=result.stop_reason,
        next_commands=(verify_command,),
    )
    return 0


def _cmd_touchpoint_docs(args: argparse.Namespace) -> int:
    sys.stdout.write(render_touchpoint_docs(args.touchpoint_docs))
    return 0


def _cmd_loop(args: argparse.Namespace) -> int:
    args.develop_step = args.step_label
    args.step = None
    return _cmd_execute(args)


def _cmd_verify(args: argparse.Namespace) -> int:
    args.no_state = not args.reuse_state
    return _cmd_execute(args)


def _read_recordings_choice(prompt: str) -> str:
    _CLI_LOGGER.info("logs_prompt", prompt, pretty=prompt)
    return input("").strip().lower()


@dataclass(frozen=True)
class _LogsFilter:
    run_id: str | None = None
    case_id: str | None = None
    step: str | None = None
    touchpoints: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    branch_filters: tuple[tuple[str, str], ...] = ()


def _logs_filter_from_args(args: argparse.Namespace) -> _LogsFilter:
    return _LogsFilter(
        run_id=args.run,
        case_id=args.case,
        step=args.step,
        touchpoints=tuple(args.touchpoint or ()),
        sources=tuple(args.source or ()),
        branch_filters=tuple(sorted(_parse_branch_filters(args.branch).items())),
    )


def _logs_filter_summary(filters: _LogsFilter) -> str:
    parts: list[str] = []
    if filters.run_id is not None:
        parts.append(f"run={filters.run_id}")
    if filters.case_id is not None:
        parts.append(f"case={filters.case_id}")
    if filters.branch_filters:
        branch = ",".join(f"{key}={value}" for key, value in filters.branch_filters)
        parts.append(f"branch={branch}")
    if filters.step is not None:
        parts.append(f"step={filters.step}")
    if filters.touchpoints:
        parts.append(f"touchpoint={','.join(filters.touchpoints)}")
    if filters.sources:
        parts.append(f"source={','.join(filters.sources)}")
    return " ".join(parts) if parts else "none"


def _branch_env_matches(
    branch_env: dict[str, str],
    filters: _LogsFilter,
) -> bool:
    for key, value in filters.branch_filters:
        if branch_env.get(key) != value:
            return False
    return True


def _step_values(*values: object) -> tuple[str, ...]:
    return tuple(str(value) for value in values if isinstance(value, str) and value)


def _manifest_step_values(manifest: object) -> tuple[str, ...]:
    return _step_values(
        getattr(manifest, "step_id", None),
        getattr(manifest, "step_label", None),
        getattr(manifest, "step_name", None),
    )


def _artifact_step_values(artifact: object) -> tuple[str, ...]:
    return _step_values(
        getattr(artifact, "step_id", None),
        getattr(artifact, "step_label", None),
        getattr(artifact, "step_name", None),
    )


def _preferred_step_value(item: object) -> str | None:
    for value in (
        getattr(item, "step_label", None),
        getattr(item, "step_name", None),
        getattr(item, "step_id", None),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _step_matches(values: tuple[str, ...], filters: _LogsFilter) -> bool:
    return filters.step is None or filters.step in values


def _browser_manifest_matches_filters(
    manifest: object,
    filters: _LogsFilter,
) -> bool:
    if filters.run_id is not None and getattr(manifest, "run_id", None) != filters.run_id:
        return False
    if filters.case_id is not None and getattr(manifest, "case_id", None) != filters.case_id:
        return False
    branch_env = getattr(manifest, "branch_env", {})
    if not isinstance(branch_env, dict) or not _branch_env_matches(branch_env, filters):
        return False
    if not _step_matches(_manifest_step_values(manifest), filters):
        return False
    if filters.touchpoints and "browser" not in filters.touchpoints:
        return False
    if filters.sources and "page" not in filters.sources:
        return False
    return True


def _log_artifact_matches_filters(
    artifact: object,
    filters: _LogsFilter,
) -> bool:
    if filters.run_id is not None and getattr(artifact, "run_id", None) != filters.run_id:
        return False
    if filters.case_id is not None and getattr(artifact, "case_id", None) != filters.case_id:
        return False
    branch_env = getattr(artifact, "branch_env", {})
    if not isinstance(branch_env, dict) or not _branch_env_matches(branch_env, filters):
        return False
    if not _step_matches(_artifact_step_values(artifact), filters):
        return False
    if filters.touchpoints and getattr(artifact, "touchpoint", None) not in filters.touchpoints:
        return False
    if filters.sources and getattr(artifact, "source", None) not in filters.sources:
        return False
    return True


def _empty_case_recording(case: CaseRecording) -> CaseRecording:
    return CaseRecording(
        recordings_dir=case.recordings_dir,
        run_id=case.run_id,
        journey_id=case.journey_id,
        function_ref=case.function_ref,
        case_id=case.case_id,
        branch_env=case.branch_env,
        manifests=(),
        log_artifacts=(),
    )


def _filter_case_recording(
    case: CaseRecording,
    filters: _LogsFilter,
    *,
    keep_empty: bool = False,
) -> CaseRecording | None:
    if filters.run_id is not None and case.run_id != filters.run_id:
        return _empty_case_recording(case) if keep_empty else None
    if filters.case_id is not None and case.case_id != filters.case_id:
        return _empty_case_recording(case) if keep_empty else None
    if not _branch_env_matches(case.branch_env, filters):
        return _empty_case_recording(case) if keep_empty else None
    if filters.step is None and not filters.touchpoints and not filters.sources:
        return case

    manifests = tuple(
        manifest
        for manifest in case.manifests
        if _browser_manifest_matches_filters(manifest, filters)
    )
    log_artifacts = tuple(
        artifact
        for artifact in case.log_artifacts
        if _log_artifact_matches_filters(artifact, filters)
    )
    if not keep_empty and not manifests and not log_artifacts:
        return None
    if manifests == case.manifests and log_artifacts == case.log_artifacts:
        return case
    return CaseRecording(
        recordings_dir=case.recordings_dir,
        run_id=case.run_id,
        journey_id=case.journey_id,
        function_ref=case.function_ref,
        case_id=case.case_id,
        branch_env=case.branch_env,
        manifests=manifests,
        log_artifacts=log_artifacts,
    )


def _empty_execution_recording(execution: ExecutionRecording) -> ExecutionRecording:
    return ExecutionRecording(
        recordings_dir=execution.recordings_dir,
        run_id=execution.run_id,
        journey_id=execution.journey_id,
        function_ref=execution.function_ref,
        cases=(),
        log_artifacts=(),
    )


def _filter_execution_recording(
    execution: ExecutionRecording,
    filters: _LogsFilter,
    *,
    keep_empty: bool = False,
) -> ExecutionRecording | None:
    if filters.run_id is not None and execution.run_id != filters.run_id:
        return _empty_execution_recording(execution) if keep_empty else None

    cases = tuple(
        filtered
        for case in execution.cases
        if (filtered := _filter_case_recording(case, filters)) is not None
    )
    log_artifacts = (
        ()
        if filters.case_id is not None
        else tuple(
            artifact
            for artifact in execution.log_artifacts
            if _log_artifact_matches_filters(artifact, filters)
        )
    )
    if not keep_empty and not cases and not log_artifacts:
        return None
    if cases == execution.cases and log_artifacts == execution.log_artifacts:
        return execution
    return ExecutionRecording(
        recordings_dir=execution.recordings_dir,
        run_id=execution.run_id,
        journey_id=execution.journey_id,
        function_ref=execution.function_ref,
        cases=cases,
        log_artifacts=log_artifacts,
    )


def _filter_recording(
    recording: CaseRecording | ExecutionRecording,
    filters: _LogsFilter,
    *,
    keep_empty: bool = False,
) -> CaseRecording | ExecutionRecording | None:
    if isinstance(recording, ExecutionRecording):
        return _filter_execution_recording(recording, filters, keep_empty=keep_empty)
    return _filter_case_recording(recording, filters, keep_empty=keep_empty)


def _case_step_count(case: CaseRecording) -> int:
    values = {manifest.step_id for manifest in case.manifests}
    values.update(
        str(getattr(artifact, "step_id"))
        for artifact in case.log_artifacts
        if getattr(artifact, "step_id", None) is not None
    )
    return len(values)


def _execution_step_count(execution: ExecutionRecording) -> int:
    values: set[str] = set()
    for case in execution.cases:
        values.update(manifest.step_id for manifest in case.manifests)
        values.update(
            str(getattr(artifact, "step_id"))
            for artifact in case.log_artifacts
            if getattr(artifact, "step_id", None) is not None
        )
    values.update(
        str(getattr(artifact, "step_id"))
        for artifact in execution.log_artifacts
        if getattr(artifact, "step_id", None) is not None
    )
    return len(values)


def _recording_case_line(index: int, case: CaseRecording, *, root: Path) -> str:
    started = case.started_at or "<unknown time>"
    try:
        recordings_dir = str(case.recordings_dir.relative_to(root))
    except ValueError:
        recordings_dir = str(case.recordings_dir)
    return (
        f"{index}. {case.case_id}  journey={case.journey_id} "
        f"run={case.run_id} branches={case.branch_summary()} "
        f"steps={_case_step_count(case)} traces={case.trace_count} videos={case.video_count} logs={case.log_count} "
        f"started={started} dir={recordings_dir}"
    )


def _recording_execution_option(
    index: int,
    executions: tuple[ExecutionRecording, ...],
) -> str:
    return "a" if len(executions) == 1 else f"a{index}"


def _recording_execution_line(
    option: str,
    execution: ExecutionRecording,
    *,
    root: Path,
) -> str:
    started = execution.started_at or "<unknown time>"
    try:
        recordings_dir = str(execution.recordings_dir.relative_to(root))
    except ValueError:
        recordings_dir = str(execution.recordings_dir)
    return (
        f"{option}. all cases  journey={execution.journey_id} "
        f"run={execution.run_id} cases={execution.case_count} "
        f"steps={_execution_step_count(execution)} traces={execution.trace_count} "
        f"videos={execution.video_count} logs={execution.log_count} started={started} dir={recordings_dir}"
    )


def _emit_recording_cases(
    cases: tuple[CaseRecording, ...],
    *,
    executions: tuple[ExecutionRecording, ...],
    root: Path,
) -> None:
    lines: list[str | object] = [pretty_line("Logs", style="heading")]
    lines.extend(
        _recording_execution_line(
            _recording_execution_option(index, executions),
            execution,
            root=root,
        )
        for index, execution in enumerate(executions, start=1)
    )
    lines.extend(
        _recording_case_line(index, case, root=root)
        for index, case in enumerate(cases, start=1)
    )
    lines.append("b. browse branches")
    lines.append("s. browse steps")
    _CLI_LOGGER.info(
        "log_cases",
        "Journey log cases discovered",
        pretty=lines,
        cases=len(cases),
        executions=len(executions),
    )


def _recording_selection_prompt(executions: tuple[ExecutionRecording, ...]) -> str:
    if len(executions) == 1:
        return "Select a case number, a for all cases, b for branches, s for steps, or q to quit:"
    if len(executions) > 1:
        return "Select a case number, an all-cases label, b for branches, s for steps, or q to quit:"
    return "Select a case number, b for branches, s for steps, or q to quit:"


def _execution_scope(
    execution: ExecutionRecording,
) -> _LogsScope:
    return _LogsScope(
        label=f"all cases for {execution.journey_id} run {execution.run_id}",
        kind="all",
        filters=_LogsFilter(run_id=execution.run_id),
        recording=execution,
    )


def _case_scope(case: CaseRecording) -> _LogsScope:
    return _LogsScope(
        label=case.case_id,
        kind="case",
        filters=_LogsFilter(run_id=case.run_id, case_id=case.case_id),
        recording=case,
    )


def _select_logs_scope(
    cases: tuple[CaseRecording, ...],
    *,
    executions: tuple[ExecutionRecording, ...],
    root: Path,
) -> _LogsScope | None:
    while True:
        _emit_recording_cases(cases, executions=executions, root=root)
        choice = _read_recordings_choice(_recording_selection_prompt(executions))
        if choice == "q":
            return None
        if choice == "b":
            selected = _select_browsed_scope(
                "Branches",
                _branch_scopes(executions),
                empty_message="No branch filters found.",
            )
            if selected == "quit":
                return None
            if isinstance(selected, _LogsScope):
                return selected
            continue
        if choice == "s":
            selected = _select_browsed_scope(
                "Steps",
                _step_scopes(executions),
                empty_message="No step filters found.",
            )
            if selected == "quit":
                return None
            if isinstance(selected, _LogsScope):
                return selected
            continue
        for index, execution in enumerate(executions, start=1):
            options = {_recording_execution_option(index, executions)}
            if len(executions) == 1:
                options.add("all")
            if choice in options:
                return _execution_scope(execution)
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(cases):
                return _case_scope(cases[index - 1])
        _CLI_LOGGER.warning(
            "recording_invalid_case_selection",
            "invalid recording case selection",
            pretty=pretty_line(
                "Choose one of the listed case numbers, all-cases labels, b, s, or q.",
                style="warning",
            ),
            selection=choice,
        )


def _open_case_trace(case: CaseRecording) -> None:
    artifact = ensure_case_trace(case)
    action = "created" if artifact.created else "reused"
    _CLI_LOGGER.info(
        "recording_trace_open",
        "opening merged trace",
        pretty=pretty_line(f"Opening merged trace ({action}): {artifact.path}", style="context"),
        path=str(artifact.path),
        created=artifact.created,
    )
    open_trace_viewer(artifact.path)


def _open_case_video(case: CaseRecording) -> None:
    artifact = ensure_case_video(case)
    action = "created" if artifact.created else "reused"
    _CLI_LOGGER.info(
        "recording_video_open",
        "opening merged video",
        pretty=pretty_line(f"Opening merged video ({action}): {artifact.path}", style="context"),
        path=str(artifact.path),
        created=artifact.created,
    )
    open_video_recording(artifact.path)


def _open_execution_trace(execution: ExecutionRecording) -> None:
    artifact = ensure_execution_trace(execution)
    action = "created" if artifact.created else "reused"
    _CLI_LOGGER.info(
        "recording_execution_trace_open",
        "opening merged execution trace",
        pretty=pretty_line(
            f"Opening merged execution trace ({action}): {artifact.path}",
            style="context",
        ),
        path=str(artifact.path),
        created=artifact.created,
    )
    open_trace_viewer(artifact.path)


def _open_execution_video(execution: ExecutionRecording) -> None:
    artifact = ensure_execution_video(execution)
    action = "created" if artifact.created else "reused"
    _CLI_LOGGER.info(
        "recording_execution_video_open",
        "opening merged execution video",
        pretty=pretty_line(
            f"Opening merged execution video ({action}): {artifact.path}",
            style="context",
        ),
        path=str(artifact.path),
        created=artifact.created,
    )
    open_video_recording(artifact.path)


def _emit_case_artifact_paths(case: CaseRecording) -> None:
    lines: list[str | object] = [
        pretty_line(f"{case.case_id} artifacts", style="heading"),
    ]
    found = False
    for label, ensure, inputs in (
        ("trace", ensure_case_trace, case.trace_inputs()),
        ("video", ensure_case_video, case.video_inputs()),
    ):
        if not inputs:
            continue
        try:
            artifact = ensure(case)
        except RecordingError as exc:
            lines.append(f"{label}: unavailable ({exc})")
            found = True
            continue
        action = "created" if artifact.created else "reused"
        lines.append(f"{label}: {artifact.path} ({action})")
        found = True
    log_artifacts = case.log_inputs()
    for artifact in log_artifacts:
        label = _log_artifact_display_label(artifact, log_artifacts)
        lines.append(f"log:{label}: {artifact.path}")
        found = True
    if not found:
        lines.append("No matching artifacts found.")
    _CLI_LOGGER.info(
        "recording_artifact_paths",
        "recording artifact paths",
        pretty=lines,
        case=case.case_id,
    )


def _emit_execution_artifact_paths(execution: ExecutionRecording) -> None:
    lines: list[str | object] = [
        pretty_line(
            f"all cases artifacts for {execution.journey_id} run {execution.run_id}",
            style="heading",
        ),
    ]
    found = False
    for label, ensure, inputs in (
        ("trace", ensure_execution_trace, execution.trace_inputs()),
        ("video", ensure_execution_video, execution.video_inputs()),
    ):
        if not inputs:
            continue
        try:
            artifact = ensure(execution)
        except RecordingError as exc:
            lines.append(f"{label}: unavailable ({exc})")
            found = True
            continue
        action = "created" if artifact.created else "reused"
        lines.append(f"{label}: {artifact.path} ({action})")
        found = True
    log_artifacts = execution.log_inputs()
    for artifact in log_artifacts:
        label = _log_artifact_display_label(artifact, log_artifacts)
        lines.append(f"log:{label}: {artifact.path}")
        found = True
    if not found:
        lines.append("No matching artifacts found.")
    _CLI_LOGGER.info(
        "recording_execution_artifact_paths",
        "recording execution artifact paths",
        pretty=lines,
        run_id=execution.run_id,
        journey=execution.journey_id,
    )


def _recording_action_label(recording: CaseRecording | ExecutionRecording) -> str:
    if isinstance(recording, ExecutionRecording):
        return f"all cases for {recording.journey_id} run {recording.run_id}"
    return recording.case_id


def _open_recording_trace(recording: CaseRecording | ExecutionRecording) -> None:
    if isinstance(recording, ExecutionRecording):
        _open_execution_trace(recording)
        return
    _open_case_trace(recording)


def _open_recording_video(recording: CaseRecording | ExecutionRecording) -> None:
    if isinstance(recording, ExecutionRecording):
        _open_execution_video(recording)
        return
    _open_case_video(recording)


def _emit_recording_artifact_paths(
    recording: CaseRecording | ExecutionRecording,
) -> None:
    if isinstance(recording, ExecutionRecording):
        _emit_execution_artifact_paths(recording)
        return
    _emit_case_artifact_paths(recording)


def _log_artifacts_for_recording(
    recording: CaseRecording | ExecutionRecording,
) -> tuple[object, ...]:
    return recording.log_inputs()


def _read_log_artifact_text(
    artifact: object,
    *,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    path = getattr(artifact, "path", None)
    if path is None:
        return ""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    if grep:
        pattern = re.compile(grep)
        lines = [line for line in lines if pattern.search(line)]
    if tail is not None:
        lines = lines[-tail:]
    return "\n".join(lines)


@dataclass(frozen=True)
class _LogsScope:
    label: str
    kind: str
    filters: _LogsFilter
    recording: CaseRecording | ExecutionRecording


@dataclass(frozen=True)
class _LogSourceOption:
    label: str
    touchpoint: str | None
    source: str | None
    artifacts: tuple[object, ...]


def _add_steps_from_case(case: CaseRecording, values: set[str]) -> None:
    for manifest in case.manifests:
        value = _preferred_step_value(manifest)
        if value is not None:
            values.add(value)
    for artifact in case.log_artifacts:
        value = _preferred_step_value(artifact)
        if value is not None:
            values.add(value)


def _add_branches_from_case(case: CaseRecording, values: set[str]) -> None:
    values.update(f"{key}={value}" for key, value in case.branch_env.items())
    for artifact in case.log_artifacts:
        branch_env = getattr(artifact, "branch_env", {})
        if isinstance(branch_env, dict):
            values.update(f"{key}={value}" for key, value in branch_env.items())


def _recording_step_values(
    recording: CaseRecording | ExecutionRecording,
) -> tuple[str, ...]:
    steps: set[str] = set()
    cases = recording.cases if isinstance(recording, ExecutionRecording) else (recording,)
    for case in cases:
        _add_steps_from_case(case, steps)
    if isinstance(recording, ExecutionRecording):
        for artifact in recording.log_artifacts:
            step = _preferred_step_value(artifact)
            if step is not None:
                steps.add(step)
    return tuple(sorted(steps))


def _recording_branch_values(
    recording: CaseRecording | ExecutionRecording,
) -> tuple[str, ...]:
    branches: set[str] = set()
    cases = recording.cases if isinstance(recording, ExecutionRecording) else (recording,)
    for case in cases:
        _add_branches_from_case(case, branches)
    if isinstance(recording, ExecutionRecording):
        for artifact in recording.log_artifacts:
            branch_env = getattr(artifact, "branch_env", {})
            if isinstance(branch_env, dict):
                branches.update(f"{key}={value}" for key, value in branch_env.items())
    return tuple(sorted(branches))


def _recording_case_count(recording: CaseRecording | ExecutionRecording) -> int:
    return recording.case_count if isinstance(recording, ExecutionRecording) else 1


def _recording_scope_counts(recording: CaseRecording | ExecutionRecording) -> str:
    return (
        f"cases={_recording_case_count(recording)} "
        f"branches={len(_recording_branch_values(recording))} "
        f"steps={_execution_step_count(recording) if isinstance(recording, ExecutionRecording) else _case_step_count(recording)} "
        f"traces={recording.trace_count} videos={recording.video_count} logs={recording.log_count}"
    )


def _scope_line(index: int, scope: _LogsScope) -> str:
    return f"{index}. {scope.label}  {_recording_scope_counts(scope.recording)}"


def _branch_scopes(executions: tuple[ExecutionRecording, ...]) -> tuple[_LogsScope, ...]:
    scopes: list[_LogsScope] = []
    for execution in executions:
        for branch in _recording_branch_values(execution):
            key, value = branch.split("=", 1)
            filters = _LogsFilter(
                run_id=execution.run_id,
                branch_filters=((key, value),),
            )
            recording = _filter_execution_recording(execution, filters)
            if recording is None:
                continue
            scopes.append(
                _LogsScope(
                    label=(
                        f"branch {branch}  journey={execution.journey_id} "
                        f"run={execution.run_id}"
                    ),
                    kind="branch",
                    filters=filters,
                    recording=recording,
                )
            )
    return tuple(sorted(scopes, key=lambda scope: scope.label))


def _step_scopes(executions: tuple[ExecutionRecording, ...]) -> tuple[_LogsScope, ...]:
    scopes: list[_LogsScope] = []
    for execution in executions:
        for step in _recording_step_values(execution):
            filters = _LogsFilter(run_id=execution.run_id, step=step)
            recording = _filter_execution_recording(execution, filters)
            if recording is None:
                continue
            scopes.append(
                _LogsScope(
                    label=(
                        f"step {step}  journey={execution.journey_id} "
                        f"run={execution.run_id}"
                    ),
                    kind="step",
                    filters=filters,
                    recording=recording,
                )
            )
    return tuple(sorted(scopes, key=lambda scope: scope.label))


def _emit_scope_browser(
    title: str,
    scopes: tuple[_LogsScope, ...],
    *,
    empty_message: str,
) -> bool:
    if not scopes:
        _CLI_LOGGER.info(
            "logs_scope_browser_empty",
            "no Journey log scopes found",
            pretty=pretty_line(empty_message, style="context"),
            scope=title.lower(),
        )
        return False
    lines: list[str | object] = [pretty_line(title, style="heading")]
    lines.extend(_scope_line(index, scope) for index, scope in enumerate(scopes, start=1))
    _CLI_LOGGER.info(
        "logs_scope_browser",
        "Journey log scopes discovered",
        pretty=lines,
        scope=title.lower(),
        count=len(scopes),
    )
    return True


def _select_browsed_scope(
    title: str,
    scopes: tuple[_LogsScope, ...],
    *,
    empty_message: str,
) -> _LogsScope | str | None:
    if not _emit_scope_browser(title, scopes, empty_message=empty_message):
        return None
    while True:
        choice = _read_recordings_choice(
            f"Select a {title.lower()} number, b to go back, or q to quit:"
        )
        if choice == "q":
            return "quit"
        if choice in {"b", "back"}:
            return None
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(scopes):
                return scopes[index - 1]
        _CLI_LOGGER.warning(
            "logs_scope_invalid_selection",
            "invalid Journey log scope selection",
            pretty=pretty_line(
                f"Choose one of the listed {title.lower()} numbers, b, or q.",
                style="warning",
            ),
            scope=title.lower(),
            selection=choice,
        )


def _dedupe_log_artifacts(artifacts: tuple[object, ...]) -> tuple[object, ...]:
    unique: dict[tuple[object, object, object], object] = {}
    for artifact in artifacts:
        key = (
            getattr(artifact, "manifest_path", None),
            getattr(artifact, "path", None),
            getattr(artifact, "sequence", None),
        )
        unique[key] = artifact
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                str(getattr(item, "touchpoint", "")),
                str(getattr(item, "source", "")),
                int(getattr(item, "sequence", 0) or 0),
            ),
        )
    )


def _log_source_options(artifacts: tuple[object, ...]) -> tuple[_LogSourceOption, ...]:
    by_touchpoint: dict[str, list[object]] = {}
    for artifact in artifacts:
        touchpoint = getattr(artifact, "touchpoint", None)
        if not isinstance(touchpoint, str) or not touchpoint:
            touchpoint = "log"
        by_touchpoint.setdefault(touchpoint, []).append(artifact)

    options: list[_LogSourceOption] = []
    for touchpoint in sorted(by_touchpoint):
        touchpoint_artifacts = _dedupe_log_artifacts(tuple(by_touchpoint[touchpoint]))
        options.append(
            _LogSourceOption(
                label=touchpoint,
                touchpoint=touchpoint,
                source=None,
                artifacts=touchpoint_artifacts,
            )
        )
        by_source: dict[str, list[object]] = {}
        for artifact in touchpoint_artifacts:
            source = getattr(artifact, "source", None)
            if isinstance(source, str) and source:
                by_source.setdefault(source, []).append(artifact)
        for source in sorted(by_source):
            options.append(
                _LogSourceOption(
                    label=f"{touchpoint}:{source}",
                    touchpoint=touchpoint,
                    source=source,
                    artifacts=_dedupe_log_artifacts(tuple(by_source[source])),
                )
            )
    return tuple(options)


def _log_artifact_display_label(
    artifact: object,
    artifacts: tuple[object, ...],
) -> str:
    touchpoint = str(getattr(artifact, "touchpoint", "log") or "log")
    source = str(getattr(artifact, "source", "") or "")
    sources = {
        str(getattr(item, "source", "") or "")
        for item in artifacts
        if getattr(item, "touchpoint", None) == touchpoint
        and getattr(item, "source", None)
    }
    if source and len(sources) > 1:
        return f"{touchpoint}:{source}"
    return touchpoint


def _emit_log_artifacts(
    label: str,
    artifacts: tuple[object, ...],
    *,
    tail: int | None = None,
    grep: str | None = None,
) -> None:
    selected = _dedupe_log_artifacts(artifacts)
    lines: list[str | object] = [
        pretty_line(f"{label} logs", style="heading"),
    ]
    if not selected:
        lines.append("No text log artifacts found.")
    for artifact in selected:
        path = getattr(artifact, "path", None)
        artifact_label = _log_artifact_display_label(artifact, selected)
        lines.append(pretty_line(f"{artifact_label} {path}", style="context"))
        text = _read_log_artifact_text(artifact, tail=tail, grep=grep)
        if text:
            lines.append(text)
    _CLI_LOGGER.info(
        "log_artifact_contents",
        "log artifact contents",
        pretty=lines,
    )


def _emit_recording_logs(
    recording: CaseRecording | ExecutionRecording,
    *,
    tail: int | None = None,
    grep: str | None = None,
) -> None:
    _emit_log_artifacts(
        _recording_action_label(recording),
        recording.log_inputs(),
        tail=tail,
        grep=grep,
    )


def _emit_log_source_browser(
    label: str,
    artifacts: tuple[object, ...],
    options: tuple[_LogSourceOption, ...],
) -> None:
    lines: list[str | object] = [pretty_line(f"{label} log sources", style="heading")]
    lines.append(f"a. all logs  logs={len(artifacts)}")
    lines.extend(
        f"{index}. {option.label}  logs={len(option.artifacts)}"
        for index, option in enumerate(options, start=1)
    )
    _CLI_LOGGER.info(
        "logs_source_browser",
        "Journey log sources discovered",
        pretty=lines,
        sources=len(options),
    )


def _parse_log_source_selection(choice: str, max_index: int) -> tuple[int, ...] | None:
    parts = [part for part in re.split(r"[\s,]+", choice.strip()) if part]
    if not parts:
        return None
    indexes: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        index = int(part)
        if not 1 <= index <= max_index:
            return None
        indexes.append(index)
    return tuple(dict.fromkeys(indexes))


def _log_source_loop(scope: _LogsScope) -> str:
    artifacts = scope.recording.log_inputs()
    if not artifacts:
        _emit_log_artifacts(scope.label, ())
        return "back"
    options = _log_source_options(artifacts)
    while True:
        _emit_log_source_browser(scope.label, artifacts, options)
        choice = _read_recordings_choice(
            "Select log source numbers, comma-separated numbers, a for all, b to go back, or q to quit:"
        )
        if choice == "q":
            return "quit"
        if choice in {"b", "back"}:
            return "back"
        if choice in {"a", "all"}:
            _emit_log_artifacts(scope.label, artifacts)
            return "back"
        indexes = _parse_log_source_selection(choice, len(options))
        if indexes is not None:
            selected_options = tuple(options[index - 1] for index in indexes)
            selected_artifacts = _dedupe_log_artifacts(
                tuple(
                    artifact
                    for option in selected_options
                    for artifact in option.artifacts
                )
            )
            selected_label = ", ".join(option.label for option in selected_options)
            _emit_log_artifacts(selected_label, selected_artifacts)
            return "back"
        _CLI_LOGGER.warning(
            "logs_source_invalid_selection",
            "invalid Journey log source selection",
            pretty=pretty_line(
                "Choose a, one or more listed numbers, b, or q.",
                style="warning",
            ),
            selection=choice,
        )


def _logs_scope_action_loop(scope: _LogsScope) -> str:
    while True:
        choice = _read_recordings_choice(
            f"{scope.label}: [t] open trace, [v] open video, [l] show logs, [p] print paths, [b] back, [q] quit:"
        )
        if choice == "t":
            try:
                _open_recording_trace(scope.recording)
            except RecordingError as exc:
                _CLI_LOGGER.warning(
                    "recording_trace_open_failure",
                    "could not open merged trace",
                    pretty=pretty_line(str(exc), style="warning"),
                    error=str(exc),
                )
        elif choice == "v":
            try:
                _open_recording_video(scope.recording)
            except RecordingError as exc:
                _CLI_LOGGER.warning(
                    "recording_video_open_failure",
                    "could not open merged video",
                    pretty=pretty_line(str(exc), style="warning"),
                    error=str(exc),
                )
        elif choice == "l":
            outcome = _log_source_loop(scope)
            if outcome == "quit":
                return "quit"
        elif choice == "p":
            _emit_recording_artifact_paths(scope.recording)
        elif choice == "b":
            return "back"
        elif choice == "q":
            return "quit"
        else:
            _CLI_LOGGER.warning(
                "recording_invalid_action",
                "invalid recording action",
                pretty=pretty_line("Choose t, v, l, p, b, or q.", style="warning"),
                selection=choice,
            )


def _parse_branch_filters(values: list[str] | None) -> dict[str, str]:
    filters: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise RecordingError("--branch expects KEY=VALUE.")
        key, item = value.split("=", 1)
        if not key:
            raise RecordingError("--branch expects a non-empty KEY.")
        filters[key] = item
    return filters


def _filter_log_cases(
    cases: tuple[CaseRecording, ...],
    filters: _LogsFilter,
) -> tuple[CaseRecording, ...]:
    return tuple(
        filtered
        for case in cases
        if (filtered := _filter_case_recording(case, filters)) is not None
    )


def _filter_log_executions(
    executions: tuple[ExecutionRecording, ...],
    filters: _LogsFilter,
) -> tuple[ExecutionRecording, ...]:
    return tuple(
        filtered
        for execution in executions
        if (filtered := _filter_execution_recording(execution, filters)) is not None
    )


def _filter_cli_parts(filters: _LogsFilter) -> tuple[str, ...]:
    parts: list[str] = []
    if filters.run_id is not None:
        parts.extend(("--run", filters.run_id))
    if filters.case_id is not None:
        parts.extend(("--case", filters.case_id))
    for key, value in filters.branch_filters:
        parts.extend(("--branch", f"{key}={value}"))
    if filters.step is not None:
        parts.extend(("--step", filters.step))
    for touchpoint in filters.touchpoints:
        parts.extend(("--touchpoint", touchpoint))
    for source in filters.sources:
        parts.extend(("--source", source))
    return tuple(parts)


def _format_filter_cli(filters: _LogsFilter) -> str:
    parts = _filter_cli_parts(filters)
    if not parts:
        return "<none>"
    return " ".join(shlex.quote(part) for part in parts)


def _logs_filter_has_values(filters: _LogsFilter) -> bool:
    return bool(_filter_cli_parts(filters))


def _logs_command_error(
    message: str,
    *,
    hint: str,
    next_commands: tuple[str, ...] = (),
) -> _CommandError:
    return _CommandError(
        file=None,
        journey_name=None,
        phase="logs",
        error_type="RecordingError",
        message=message,
        hint=hint,
        instructions=(
            "Use `journey evidence --help` for the evidence-inspection command manual, "
            "then discover valid filters with `journey evidence --list-scopes` and "
            "`journey evidence --list-log-sources` before reading large artifacts."
        ),
        next_commands=next_commands,
        help_command="journey evidence --help",
    )


def _emit_log_scopes(
    cases: tuple[CaseRecording, ...],
    *,
    executions: tuple[ExecutionRecording, ...],
) -> None:
    lines: list[str | object] = [pretty_line("Log scopes", style="heading")]
    for execution in executions:
        scope = _execution_scope(execution)
        lines.append(
            f"all  filter={_format_filter_cli(scope.filters)} "
            f"{_recording_scope_counts(scope.recording)}"
        )
    for case in cases:
        scope = _case_scope(case)
        lines.append(
            f"case {case.case_id}  filter={_format_filter_cli(scope.filters)} "
            f"{_recording_scope_counts(scope.recording)}"
        )
    for scope in _branch_scopes(executions):
        lines.append(
            f"{scope.kind} {scope.label}  filter={_format_filter_cli(scope.filters)} "
            f"{_recording_scope_counts(scope.recording)}"
        )
    for scope in _step_scopes(executions):
        lines.append(
            f"{scope.kind} {scope.label}  filter={_format_filter_cli(scope.filters)} "
            f"{_recording_scope_counts(scope.recording)}"
        )
    _CLI_LOGGER.info(
        "logs_scopes",
        "Journey log scopes",
        pretty=lines,
    )


def _log_artifacts_for_recordings(
    recordings: tuple[CaseRecording | ExecutionRecording, ...],
) -> tuple[object, ...]:
    return _dedupe_log_artifacts(
        tuple(
            artifact
            for recording in recordings
            for artifact in recording.log_inputs()
        )
    )


def _emit_log_source_listing(
    recordings: tuple[CaseRecording | ExecutionRecording, ...],
    *,
    base_filters: _LogsFilter,
) -> None:
    artifacts = _log_artifacts_for_recordings(recordings)
    options = _log_source_options(artifacts)
    lines: list[str | object] = [pretty_line("Log sources", style="heading")]
    lines.append(
        f"all  filter={_format_filter_cli(base_filters)} logs={len(artifacts)}"
    )
    for option in options:
        filters = base_filters
        if option.touchpoint is not None:
            filters = _LogsFilter(
                run_id=filters.run_id,
                case_id=filters.case_id,
                step=filters.step,
                touchpoints=(option.touchpoint,),
                sources=filters.sources,
                branch_filters=filters.branch_filters,
            )
        if option.source is not None:
            filters = _LogsFilter(
                run_id=filters.run_id,
                case_id=filters.case_id,
                step=filters.step,
                touchpoints=filters.touchpoints,
                sources=(option.source,),
                branch_filters=filters.branch_filters,
            )
        lines.append(
            f"{option.label}  filter={_format_filter_cli(filters)} "
            f"logs={len(option.artifacts)}"
        )
    _CLI_LOGGER.info(
        "logs_sources",
        "Journey log sources",
        pretty=lines,
    )


def _noninteractive_recording_selection(
    filtered_cases: tuple[CaseRecording, ...],
    filtered_executions: tuple[ExecutionRecording, ...],
    filters: _LogsFilter,
) -> tuple[CaseRecording | ExecutionRecording, ...]:
    if (
        filters.case_id is not None
        or filters.step is not None
        or filters.branch_filters
    ):
        return filtered_cases
    return filtered_executions if filtered_executions else filtered_cases


def _cmd_logs_noninteractive(
    args: argparse.Namespace,
    *,
    cases: tuple[CaseRecording, ...],
    executions: tuple[ExecutionRecording, ...],
    root: Path,
) -> int:
    filters = _logs_filter_from_args(args)
    filtered_cases = _filter_log_cases(cases, filters)
    filtered_executions = _filter_log_executions(executions, filters)
    if _logs_filter_has_values(filters) and not filtered_cases and not filtered_executions:
        _emit_errors(
            root,
            [
                _logs_command_error(
                    f"No Journey logs matched filters: {_format_filter_cli(filters)}.",
                    hint=(
                        "Run `journey evidence --list-scopes` without those filters to "
                        "discover available run, case, branch, and step values."
                    ),
                    next_commands=(
                        "journey evidence --list-scopes",
                        "journey evidence --list-log-sources",
                    ),
                )
            ],
        )
        return 1
    if args.list_scopes:
        _emit_log_scopes(filtered_cases, executions=filtered_executions)
        return 0
    selected = _noninteractive_recording_selection(
        filtered_cases,
        filtered_executions,
        filters,
    )
    if args.list_log_sources:
        _emit_log_source_listing(selected, base_filters=filters)
        return 0
    if args.list:
        _emit_recording_cases(
            filtered_cases,
            executions=filtered_executions,
            root=root,
        )
        return 0
    if args.paths:
        for recording in selected:
            _emit_recording_artifact_paths(recording)
        return 0
    if args.show:
        for recording in selected:
            _emit_recording_logs(recording, tail=args.tail, grep=args.grep)
        return 0
    return 1


def _cmd_recordings(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    try:
        _parse_branch_filters(args.branch)
    except RecordingError as exc:
        _emit_errors(
            root,
            [
                _logs_command_error(
                    str(exc),
                    hint=(
                        "Use `--branch KEY=VALUE`, or run "
                        "`journey evidence --list-scopes` to copy a branch filter "
                        "printed by Journey."
                    ),
                    next_commands=("journey evidence --help", "journey evidence --list-scopes"),
                )
            ],
        )
        return 1

    result = discover_recording_cases(root)
    for warning in result.warnings:
        _CLI_LOGGER.warning(
            "log_manifest_skipped",
            "skipped Journey log manifest",
            pretty=pretty_line(warning, style="warning"),
            warning=warning,
        )
    executions = result.executions or group_execution_recordings(result.cases)
    if not result.cases and not executions:
        _emit_errors(
            root,
            [
                _logs_command_error(
                    f"No Journey logs found under {root}.",
                    hint=(
                        "Run a Journey command with logs enabled, then run "
                        "`journey evidence` from that project root or pass `--dir` "
                        "to the directory containing `.journey/logs`. Do not use "
                        "`--no-logs` when you need artifacts."
                    ),
                    next_commands=("journey --help", "journey evidence --help"),
                )
            ],
        )
        return 1

    if args.list or args.show or args.paths or args.list_scopes or args.list_log_sources:
        return _cmd_logs_noninteractive(
            args,
            cases=result.cases,
            executions=executions,
            root=root,
        )

    while True:
        scope = _select_logs_scope(
            result.cases,
            executions=executions,
            root=root,
        )
        if scope is None:
            return 0
        outcome = _logs_scope_action_loop(scope)
        if outcome == "quit":
            return 0


def _add_execution_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", help="Journey file to execute")
    parser.add_argument(
        "--journey",
        help="Decorated journey function name to execute",
    )


def _add_runtime_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        choices=("pretty", "jsonl"),
        default="pretty",
        help="Set Journey output format (default: pretty)",
    )
    parser.add_argument(
        "--log-level",
        "--level",
        dest="log_level",
        choices=("debug", "info", "warning", "error", "off"),
        default="info",
        help="Set Journey diagnostic logging level (default: info)",
    )


def _add_runtime_control_arguments(parser: argparse.ArgumentParser) -> None:
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
        "--no-browser-recording",
        action="store_true",
        help="Disable browser trace and video artifacts for this run",
    )
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="Disable Journey evidence artifacts for this run",
    )


def build_logs_parser(*, prog: str = "journey evidence") -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog=prog,
        description="browse Journey evidence from completed runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Coding-agent evidence loop:\n"
            f"  1. Run `{prog} --help` when evidence usage is unclear.\n"
            "  2. Discover filters before reading large artifacts:\n"
            f"     {prog} --list-scopes\n"
            f"     {prog} --list-log-sources --case <case_id> --step <step_label>\n"
            "  3. Inspect focused evidence:\n"
            f"     {prog} --show --case <case_id> --step <step_label> --touchpoint docker --source <service> --tail 80\n"
            f"     {prog} --paths --step <step_label> --touchpoint browser\n"
            "\n"
            "Recovery:\n"
            f"  - If no evidence is found, rerun the Journey without --no-logs, then run `{prog}` from that project root or pass --dir.\n"
            f"  - If a filter returns no matches, rerun `{prog} --list-scopes` and copy the printed run/case/branch/step values.\n"
            "  - Use --branch KEY=VALUE exactly as printed by --list-scopes.\n"
            "\n"
            "Related CLI commands: `journey loop <step> --file <file>`, `journey verify --file <file>`, `journey agent <target>`."
        ),
    )
    parser.add_argument(
        "--dir",
        default=".",
        help="Directory to scan for .journey/logs artifacts (default: current directory)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List matching runs and cases without prompting",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print matching text log contents without prompting",
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="Print matching trace, video, and log artifact paths without prompting",
    )
    parser.add_argument(
        "--list-scopes",
        action="store_true",
        help="List browseable all/case/branch/step scopes without prompting",
    )
    parser.add_argument(
        "--list-log-sources",
        action="store_true",
        help="List log touchpoints and source filters without prompting",
    )
    parser.add_argument("--run", help="Filter by run id")
    parser.add_argument("--case", help="Filter by case id")
    parser.add_argument("--step", help="Filter artifacts by step id, label, or name")
    parser.add_argument(
        "--touchpoint",
        action="append",
        help="Filter artifacts by touchpoint; may be repeated",
    )
    parser.add_argument(
        "--source",
        action="append",
        help="Filter log artifacts by touchpoint-defined source; may be repeated",
    )
    parser.add_argument(
        "--branch",
        action="append",
        help="Filter by branch environment entry KEY=VALUE; may be repeated",
    )
    parser.add_argument(
        "--tail",
        type=int,
        help="Print only the last N matching log lines with --show",
    )
    parser.add_argument(
        "--grep",
        help="Print only matching log lines with --show",
    )
    _add_runtime_output_arguments(parser)
    return parser


def build_loop_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog="journey loop",
        description="rerun one replayable journey step while editing code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Agent loop:\n"
            "  1. Run the failing journey once, or use the `Retry failed step:` command Journey prints.\n"
            "  2. Rerun the same `journey loop <step>` command after every edit.\n"
            "  3. Finish with `journey verify --step <step> --file <file>` or `journey verify --file <file>`.\n"
            "\n"
            "Examples:\n"
            "  journey loop receive_confirmation_email --file journeys/checkout_journey.py\n"
            "  journey loop complete_checkout_and_verify_registration_effects --file journeys/agentic_loop_journey.py --output jsonl\n"
            "\n"
            "Related CLI commands: `journey verify --help`, `journey evidence --help`, `journey agent <target>`."
        ),
    )
    parser.add_argument("step_label", help="Replayable step label to rerun")
    _add_execution_scope_arguments(parser)
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt to continue or retry after each loop pause",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="Use temporary state for this loop run",
    )
    parser.add_argument(
        "--no-state-update",
        action="store_true",
        help="Read existing state but do not update the default loop checkpoint",
    )
    _add_runtime_control_arguments(parser)
    _add_runtime_output_arguments(parser)
    parser.set_defaults(step=None, develop_step=None, fail_fast=False)
    return parser


def build_verify_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog="journey verify",
        description="freshly verify a full journey or one selected step case",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Verification loop:\n"
            "  journey verify --file journeys/<feature>_journey.py\n"
            "      Run the full journey from a fresh path.\n"
            "  journey verify --step <step_label> --file journeys/<feature>_journey.py\n"
            "      Verify the selected case from a fresh path after the focused loop passes.\n"
            "  journey verify --reuse-state --step <step_label> --file journeys/<feature>_journey.py\n"
            "      Opt into existing state when investigating replay behavior.\n"
            "\n"
            "Related CLI commands: `journey loop <step> --file <file>`, `journey evidence --help`, `journey agent <target>`."
        ),
    )
    _add_execution_scope_arguments(parser)
    parser.add_argument(
        "--step",
        help="Verify only the case that reaches one step label",
    )
    state_group = parser.add_mutually_exclusive_group()
    state_group.add_argument(
        "--fresh",
        dest="reuse_state",
        action="store_false",
        default=False,
        help="Run from temporary state without reading or writing reusable checkpoints (default)",
    )
    state_group.add_argument(
        "--reuse-state",
        dest="reuse_state",
        action="store_true",
        help="Allow persistent state reuse; fresh verification is the default",
    )
    parser.add_argument(
        "--no-state-update",
        action="store_true",
        help="Read reusable state when --reuse-state is set, but do not update checkpoints",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first discovery, compilation, or execution failure",
    )
    _add_runtime_control_arguments(parser)
    _add_runtime_output_arguments(parser)
    parser.set_defaults(develop_step=None, interactive=False)
    return parser


def build_touchpoints_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog="journey touchpoints",
        description="print packaged touchpoint reference docs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Touchpoints are systems a replayable step talks to: browser, Docker, HTTP, email, and webhooks.\n"
            "Use these references before inventing polling, subprocess, inbox, or webhook plumbing.\n"
            "\n"
            "Examples:\n"
            "  journey touchpoints browser\n"
            "  journey touchpoints all"
        ),
    )
    parser.add_argument(
        "touchpoint_docs",
        choices=supported_touchpoint_doc_targets(),
        metavar="name",
        help="Reference to print: docker, browser, email, webhook, http, or all",
    )
    _add_runtime_output_arguments(parser)
    return parser


def build_discover_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog="journey discover",
        description="crawl app URLs and generate a branched Journey spec",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Browser discovery:\n"
            "  journey discover http://127.0.0.1:3000 --file journeys/discovered_journey.py\n"
            "      Use Claude Haiku to discover user paths and write deterministic Playwright steps.\n"
            "  journey discover http://127.0.0.1:18081 --depth 4 --max-actions 30 --max-model-calls 8 --force\n"
            "      Discover more of a local app and replace the generated file.\n"
            "\n"
            "The generated Journey is a draft test suite. Review it, then run:\n"
            "  journey verify --file journeys/discovered_journey.py\n"
            "\n"
            "Model selection follows --model, then JOURNEY_BROWSER_PROMPT_MODEL, then "
            "anthropic:claude-haiku-4-5."
        ),
    )
    parser.add_argument("url", nargs="+", help="Start URL to discover; may be repeated")
    parser.add_argument(
        "--file",
        default="journeys/discovered_journey.py",
        help="Generated Journey file path (default: journeys/discovered_journey.py)",
    )
    parser.add_argument(
        "--journey-name",
        default="discovered_journey",
        help="Generated @journey function name (default: discovered_journey)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=4,
        help="Maximum action depth per start URL (default: 4)",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=30,
        help="Maximum discovered actions per start URL (default: 30)",
    )
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=8,
        help="Maximum model calls across the crawl; 0 disables model fallback (default: 8)",
    )
    parser.add_argument(
        "--max-variants-per-control",
        type=int,
        default=3,
        help="Maximum finite select/radio/checkbox variants per control (default: 3)",
    )
    parser.add_argument(
        "--side-effect-probes",
        choices=("auto", "off"),
        default="auto",
        help="Probe discovered API/email/webhook evidence endpoints after stable identifiers appear (default: auto)",
    )
    parser.add_argument(
        "--browser",
        choices=("chromium", "firefox", "webkit"),
        default="chromium",
        help="Playwright browser to use (default: chromium)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser during discovery",
    )
    parser.add_argument(
        "--model",
        help="LangChain model identifier for discovery",
    )
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow discovery to follow off-origin navigations",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing generated Journey file",
    )
    _add_runtime_output_arguments(parser)
    return parser

def build_agent_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog="journey agent",
        description="print or install Journey SDK agent verification guidance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Agent verification packet:\n"
            "  1. Run `journey agent <target>` to print the complete agent guidance packet.\n"
            "  2. Inside that loop, use `journey loop` while editing and `journey verify` before finishing.\n"
            "  3. Use `journey evidence --help` for traces, videos, structured logs, and touchpoint payloads.\n"
            "  4. Use `journey agent <target> --install` only when persistent project instructions should be written.\n"
            "\n"
            "Targets: codex, claude, cursor, generic.\n"
            "Recovery:\n"
            "  - If --force is rejected, add it only together with --install.\n"
            "  - If install refuses to overwrite an existing file, rerun with --install --force only after deciding replacement is intended.\n"
            "\n"
            "Related CLI commands: `journey loop --help`, `journey verify --help`, `journey evidence --help`, `journey touchpoints all`."
        ),
    )
    parser.add_argument(
        "target",
        choices=supported_agent_instruction_targets(),
        help="Agent target to render: codex, claude, cursor, or generic",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Write persistent guidance to the target's default project path",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing assistant instruction file during install",
    )
    parser.add_argument(
        "--output",
        choices=("pretty", "jsonl"),
        default="pretty",
        help="Set Journey output format (default: pretty)",
    )
    parser.add_argument(
        "--log-level",
        "--level",
        dest="log_level",
        choices=("debug", "info", "warning", "error", "off"),
        default="info",
        help="Set Journey diagnostic logging level (default: info)",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = _JourneyArgumentParser(
        prog="journey",
        description="replay and verify real user journeys for agentic coding loops",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Core commands:\n"
            "  journey loop <step_label> --file journeys/<feature>_journey.py\n"
            "      Rerun one replayable journey step while an agent edits code.\n"
            "  journey verify --step <step_label> --file journeys/<feature>_journey.py\n"
            "      Freshly verify the selected case after the focused loop passes.\n"
            "  journey verify --file journeys/<feature>_journey.py\n"
            "      Freshly verify the whole journey before finishing.\n"
            "  journey evidence --step <step_label>\n"
            "      Inspect traces, videos, structured logs, and touchpoint payloads.\n"
            "  journey discover <url> --file journeys/discovered_journey.py\n"
            "      Crawl an app URL and generate a draft branched Journey spec.\n"
            "  journey touchpoints browser|docker|email|webhook|http|all\n"
            "      Print packaged touchpoint references for helpers used inside steps.\n"
            "\n"
            "Self-healing agent loop:\n"
            "  - Read `What happened`, `Try this`, `Next commands`, and `Retry failed step:` lines.\n"
            "  - Copy `Retry failed step:` when present; otherwise use `journey loop <failed_label>`.\n"
            "  - Inspect artifacts with `journey evidence --help`, `journey evidence --list-scopes`, and `journey evidence --paths` or `--show`.\n"
            "  - Rerun the focused loop command until it passes, then broaden to fresh `journey verify`."
        ),
    )
    parser.add_argument(
        "--output",
        choices=("pretty", "jsonl"),
        default="pretty",
        help="Set Journey output format (default: pretty)",
    )
    parser.add_argument(
        "--log-level",
        "--level",
        dest="log_level",
        choices=("debug", "info", "warning", "error", "off"),
        default="info",
        help="Set Journey diagnostic logging level (default: info)",
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
    level = (
        _extract_option_value(argv, "--log-level")
        or _extract_option_value(argv, "--level")
        or "info"
    )
    output = _extract_option_value(argv, "--output")
    output_format = output if output in {"pretty", "jsonl"} else "pretty"
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
    if raw_argv and raw_argv[0] == "evidence":
        parser = build_logs_parser(prog="journey evidence")
        args = parser.parse_args(raw_argv[1:])
        configure_logging(args.log_level, output_format=args.output)
        return _cmd_recordings(args)
    if raw_argv and raw_argv[0] == "touchpoints":
        parser = build_touchpoints_parser()
        args = parser.parse_args(raw_argv[1:])
        configure_logging(args.log_level, output_format=args.output)
        return _cmd_touchpoint_docs(args)
    if raw_argv and raw_argv[0] == "discover":
        parser = build_discover_parser()
        args = parser.parse_args(raw_argv[1:])
        configure_logging(args.log_level, output_format=args.output)
        return _cmd_discover(args)
    if raw_argv and raw_argv[0] == "loop":
        parser = build_loop_parser()
        args = parser.parse_args(raw_argv[1:])
        configure_logging(args.log_level, output_format=args.output)
        return _cmd_loop(args)
    if raw_argv and raw_argv[0] == "verify":
        parser = build_verify_parser()
        args = parser.parse_args(raw_argv[1:])
        configure_logging(args.log_level, output_format=args.output)
        return _cmd_verify(args)
    if raw_argv and raw_argv[0] == "agent":
        parser = build_agent_parser()
        args = parser.parse_args(raw_argv[1:])
        configure_logging(args.log_level, output_format=args.output)
        if args.force and not args.install:
            parser.error("--force requires --install")
        return _cmd_agent(args)
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, output_format=args.output)
    parser.error(
        "missing command: use journey loop, journey verify, journey evidence, "
        "journey discover, journey touchpoints, or journey agent"
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
