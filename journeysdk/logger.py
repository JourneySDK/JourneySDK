"""Central logging helpers for Journey SDK diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sys
from threading import Lock
from typing import Any, Literal, TextIO

JourneyLogLevel = Literal["debug", "info", "warning", "error", "off"]
JourneyOutputFormat = Literal["pretty", "structured", "jsonl"]

_LEVEL_VALUES: dict[JourneyLogLevel, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "off": 100,
}
_LEVEL_NAMES: dict[JourneyLogLevel, str] = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "off": "OFF",
}
_OUTPUT_FORMATS: set[JourneyOutputFormat] = {"pretty", "structured", "jsonl"}
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")
_PRETTY_STEP_LABEL_WIDTH = 29
_PRETTY_TOOL_LABEL_WIDTH = 27
_PRETTY_PROMPT_LABEL_WIDTH = 25
_PASSWORD_QUOTED_RE = re.compile(r"(?i)(password\s*)([\"'])(.*?)(\2)")
_PASSWORD_BARE_RE = re.compile(r"(?i)(password\s+)(?![\"'])([^\s,;.)]+)")
_SENSITIVE_FIELD_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)

_configured_level: JourneyLogLevel = "info"
_configured_stream: TextIO | None = None
_configured_output_format: JourneyOutputFormat = "pretty"
_config_lock = Lock()


class JourneyLogger:
    """Component-scoped logger for Journey SDK diagnostics."""

    def __init__(self, component: str) -> None:
        self._component = _normalize_component(component)

    @property
    def component(self) -> str:
        return self._component

    def debug(self, event: str, message: str, **fields: object) -> None:
        self.log("debug", event, message, **fields)

    def info(self, event: str, message: str, **fields: object) -> None:
        self.log("info", event, message, **fields)

    def warning(self, event: str, message: str, **fields: object) -> None:
        self.log("warning", event, message, **fields)

    def error(self, event: str, message: str, **fields: object) -> None:
        self.log("error", event, message, **fields)

    def log(
        self,
        level: JourneyLogLevel,
        event: str,
        message: str,
        **fields: object,
    ) -> None:
        if level == "off":
            raise ValueError("JourneyLogger.log(...) does not accept level='off'.")
        _require_level(level)
        if not _should_emit(level):
            return
        timestamp = _format_timestamp()
        normalized_event = _normalize_event(event)
        output_format = _active_output_format()
        stream = _active_stream()
        if output_format == "jsonl":
            line = _format_json_line(
                timestamp=timestamp,
                level=level,
                component=self._component,
                event=normalized_event,
                message=str(message),
                fields=fields,
            )
        elif output_format == "structured":
            line = _format_log_line(
                timestamp=timestamp,
                level=level,
                component=self._component,
                event=normalized_event,
                message=str(message),
                fields=fields,
            )
        else:
            line = _format_pretty_line(
                level=level,
                component=self._component,
                event=normalized_event,
                message=str(message),
                fields=fields,
                stream=stream,
            )
            if line is None:
                return
        print(line, file=stream, flush=True)


def configure_logging(
    level: JourneyLogLevel = "info",
    stream: TextIO | None = None,
    output_format: JourneyOutputFormat = "pretty",
) -> None:
    """Configure process-wide Journey SDK output."""

    _require_level(level)
    _require_output_format(output_format)
    global _configured_level, _configured_stream, _configured_output_format
    with _config_lock:
        _configured_level = level
        _configured_stream = stream
        _configured_output_format = output_format


def get_logger(component: str) -> JourneyLogger:
    """Return a logger for one Journey SDK component."""

    return JourneyLogger(component)


def _format_log_line(
    *,
    timestamp: str,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str:
    parts = [
        "[journey]",
        f"time={timestamp}",
        f"level={_LEVEL_NAMES[level]}",
        f"component={_format_value(component)}",
        f"event={_format_value(event)}",
        f"message={_format_value(message)}",
    ]
    for key in sorted(fields):
        parts.append(f"{key}={_format_field_value(key, fields[key])}")
    return " ".join(parts)


def _format_json_line(
    *,
    timestamp: str,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str:
    record: dict[str, Any] = {
        "time": timestamp,
        "level": _LEVEL_NAMES[level],
        "component": component,
        "event": event,
        "message": message,
    }
    for key in sorted(fields):
        record[key] = _json_field_value(key, fields[key])
    return json.dumps(record, ensure_ascii=True, default=str, separators=(",", ":"))


def _format_pretty_line(
    *,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
    stream: TextIO,
) -> str | None:
    line = _pretty_render_event(
        level=level,
        component=component,
        event=event,
        message=message,
        fields=fields,
    )
    if line is None:
        return None
    return _colorize_pretty(line, level=level, stream=stream)


def _pretty_render_event(
    *,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str | None:
    if level == "debug":
        return _pretty_debug(component=component, event=event, message=message, fields=fields)
    if component == "cli":
        return _pretty_cli(level=level, event=event, message=message, fields=fields)
    if component == "executor":
        return _pretty_executor(level=level, event=event, message=message, fields=fields)
    if component == "playwright":
        return _pretty_playwright(level=level, event=event, message=message, fields=fields)
    if component == "playwright-prompt":
        return _pretty_prompt(level=level, event=event, message=message, fields=fields)
    return _pretty_generic(
        level=level,
        component=component,
        event=event,
        message=message,
        fields=fields,
    )


def _pretty_cli(
    *,
    level: JourneyLogLevel,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str | None:
    if event == "discovery_start":
        root = fields.get("root")
        return f"Discovering journeys: {_pretty_text(root or message)}"
    if event == "discovery_complete":
        discovered = _pretty_count(fields.get("discovered"), "journey")
        errors = fields.get("errors")
        if isinstance(errors, int) and errors:
            return f"Found {discovered}, {errors} failed"
        return f"Found {discovered}"
    if event in {"compile_start", "compile_success"}:
        return None
    if event == "compile_failure":
        return None
    if event == "plan_start":
        return "Plan"
    if event == "plan_journey":
        return f"  {_pretty_target(fields)}"
    if event == "plan_metadata":
        return None
    if event == "plan_case":
        case = fields.get("case") or _first_word_after(message, "- ") or "case"
        labels = _pretty_labels(fields.get("labels"))
        branches = _pretty_branch_env(fields.get("branch_env"))
        details = []
        if labels:
            details.append(f"labels: {labels}")
        if branches:
            details.append(f"branches: {branches}")
        suffix = f"  {'; '.join(details)}" if details else ""
        return f"    {_pretty_text(case)}{suffix}"
    if event in {"plan_summary", "execute_summary"}:
        return f"  {_pretty_text(message)}"
    if event == "execution_section":
        return "Execution"
    if event == "execution_start":
        return None
    if event == "execution_success":
        return None
    if event == "execution_failure":
        target = _pretty_target(fields)
        error = fields.get("error")
        return _pretty_problem(level, f"{target} failed: {_pretty_text(error)}")
    if event in {"develop_step_stopped", "develop_step_reload"}:
        return _pretty_text(message)
    if event == "execute_result":
        return None
    if event == "execution_interrupted":
        return "Interrupted: Journey execution was interrupted before it finished."
    if event == "interrupt_summary":
        hint = fields.get("hint")
        if hint is None:
            return None
        return f"Hint: {_pretty_text(hint)}"
    if event in {"interrupt_message", "interrupt_hint"}:
        return None
    if event == "graceful_interrupt_requested":
        return _pretty_problem(level, message)
    if event == "pause_prompt":
        return _pretty_text(message)
    if event == "pause_invalid_choice":
        return _pretty_text(message)
    if event == "command_error":
        phase = fields.get("phase")
        location = fields.get("location")
        error_type = fields.get("error_type")
        return _pretty_problem(
            level,
            f"{_pretty_text(error_type)} during {_pretty_text(phase)} at {_pretty_text(location)}",
        )
    if event == "command_error_message":
        return _pretty_text(message)
    if event == "command_error_hint":
        return _pretty_text(message)
    if event == "parser_error":
        return _pretty_problem(level, message)
    if event == "parser_output":
        return _pretty_text(message)
    return _pretty_generic(level=level, component="cli", event=event, message=message, fields=fields)


def _pretty_executor(
    *,
    level: JourneyLogLevel,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str | None:
    if event == "journey_start":
        return f"  {_pretty_target(fields)}"
    if event == "journey_complete":
        return None
    if event in {"case_start", "case_resume"}:
        case = fields.get("case") or _first_word_after(message, "- ") or "case"
        action = " resume" if event == "case_resume" else ""
        details = []
        branches = fields.get("branches")
        if branches not in (None, "{}", {}):
            details.append(f"branches={_pretty_text(branches)}")
        replay_anchor = fields.get("replay_anchor")
        if replay_anchor is not None:
            details.append(f"replay_anchor={_pretty_text(replay_anchor)}")
        replay_from = fields.get("replay_from_index")
        if replay_from is not None:
            details.append(f"replay_from={_pretty_text(replay_from)}")
        suffix = f"  {' '.join(details)}" if details else ""
        return f"    {_pretty_text(case)}{action}{suffix}"
    if event == "step_start":
        return _pretty_step_row(fields, _step_detail("start", fields, include_attempt=True))
    if event == "step_success":
        return _pretty_step_row(fields, _step_detail("ok", fields, include_attempt=True))
    if event == "step_retry":
        return _pretty_step_problem(level, "retry", fields, fallback="retrying")
    if event == "step_failure":
        return _pretty_step_problem(level, "failed", fields, fallback="failed")
    if event == "step_interrupted":
        return _pretty_step_problem(level, "interrupted", fields, fallback="interrupted")
    if event == "branch_select":
        label = f"branch {fields.get('branch_group', '')}".strip()
        return _pretty_row(
            indent=6,
            label=label,
            detail=_pretty_text(fields.get("branch") or message),
            width=_PRETTY_STEP_LABEL_WIDTH,
        )
    if event == "case_complete":
        case = fields.get("case") or _first_word_after(message, "- ") or "case"
        detail_parts = ["done"]
        for key in ("steps", "duration", "stopped_at", "replay_anchor"):
            value = fields.get(key)
            if value is not None:
                detail_parts.append(f"{key}={_format_pretty_field_value(key, value)}")
        return f"    {_pretty_text(case)} {' '.join(detail_parts)}"
    if event == "develop_state_restart":
        return _pretty_problem(level, message)
    return _pretty_generic(
        level=level,
        component="executor",
        event=event,
        message=message,
        fields=fields,
    )


def _pretty_playwright(
    *,
    level: JourneyLogLevel,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str | None:
    browser = _pretty_text(fields.get("browser") or "browser")
    url = fields.get("url")
    if event == "open_page_start":
        detail = f"opening {browser}"
        if url is not None:
            detail += f" {_pretty_text(url)}"
        if fields.get("headless") is False:
            detail += " headless=false"
        return _pretty_tool_row("Browser", detail)
    if event == "open_page_success":
        detail = f"opened {browser}"
        if url is not None:
            detail += f" {_pretty_text(url)}"
        return _pretty_tool_row("Browser", detail)
    if event == "open_page_failure":
        detail = f"Browser failed to open {browser}"
        if url is not None:
            detail += f" {_pretty_text(url)}"
        if fields.get("error") is not None:
            detail += f": {_pretty_text(fields.get('error'))}"
        return _pretty_problem(level, detail)
    if event == "browser_install_check_start":
        return _pretty_tool_row("Browser", f"checking {browser} installation")
    if event == "browser_install_check_success":
        return _pretty_tool_row("Browser", f"{browser} installation available")
    if event == "browser_install_start":
        return _pretty_tool_row("Browser", f"installing {browser}")
    if event == "browser_install_success":
        return _pretty_tool_row("Browser", f"installed {browser}")
    if event.startswith("browser_install_"):
        return _pretty_problem(level, message)
    return _pretty_tool_row("Browser", _pretty_message_with_extras(event, message, fields))


def _pretty_prompt(
    *,
    level: JourneyLogLevel,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str | None:
    if event == "prompt_start":
        first_line = _pretty_tool_row(
            "AI prompt",
            " ".join(
                part
                for part in (
                    _pretty_key_value("model", fields.get("model")),
                    _pretty_key_value("max_steps", fields.get("max_steps")),
                    _pretty_key_value("timeout", fields.get("timeout")),
                )
                if part
            )
            or "starting",
        )
        lines = [first_line]
        instruction = fields.get("instruction")
        if instruction is not None:
            lines.append(
                _pretty_prompt_row("instruction", _pretty_text(instruction))
            )
        active = fields.get("active")
        if active is not None:
            lines.append(_pretty_prompt_row("page", _pretty_text(active)))
        return "\n".join(lines)
    if event == "prompt_action":
        return _pretty_prompt_row(
            f"{_pretty_prompt_step_ref(fields)} action",
            _pretty_text(fields.get("action") or message),
        )
    if event == "prompt_code":
        step_label = _pretty_prompt_step_ref(fields)
        code = fields.get("code")
        if code is None:
            return _pretty_prompt_row(f"{step_label} code", "")
        if message.startswith("  "):
            return _pretty_prompt_continuation(_pretty_text(code))
        return _pretty_prompt_row(f"{step_label} code", _pretty_text(code))
    if event == "prompt_step_success":
        return _pretty_prompt_row(
            f"{_pretty_prompt_step_ref(fields)} ok",
            _pretty_text(_after(message, "succeeded on ") or message),
        )
    if event == "page_discovered":
        return _pretty_prompt_row(
            "page discovered",
            _pretty_text(_page_detail(fields) or _after(message, "discovered ") or message),
        )
    if event == "active_page_change":
        return _pretty_prompt_row(
            "active page",
            _pretty_text(fields.get("page_summary") or _after(message, "active page changed to ") or message),
        )
    if event == "prompt_finish":
        return _pretty_prompt_row(
            f"{_pretty_prompt_step_ref(fields)} finish",
            _pretty_text(fields.get("output") or _after(message, "finished with output: ") or message),
        )
    if event == "prompt_rejected":
        return _pretty_prompt_row(
            f"{_pretty_prompt_step_ref(fields)} rejected",
            _pretty_text(fields.get("detail") or message),
        )
    if event == "prompt_failed":
        return _pretty_tool_row(
            "AI prompt",
            f"failed at {_pretty_prompt_step_ref(fields)}: {_pretty_text(fields.get('reason') or message)}",
        )
    if event == "prompt_stopped":
        return _pretty_tool_row("AI prompt", _pretty_text(_strip_prefix(message, "prompt stopped: ")))
    if event == "prompt_memory_loaded":
        return _pretty_prompt_row("memory", f"loaded {_pretty_text(fields.get('path') or '')}".rstrip())
    if event == "prompt_memory_saved":
        detail = f"saved {_pretty_text(fields.get('path') or '')}".rstrip()
        run_count = fields.get("run_count")
        if run_count is not None:
            detail += f" run_count={_pretty_text(run_count)}"
        return _pretty_prompt_row("memory", detail)
    return _pretty_tool_row("AI prompt", _pretty_message_with_extras(event, message, fields))


def _pretty_generic(
    *,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str:
    detail = _pretty_message_with_extras(event, message, fields)
    if level in {"warning", "error"}:
        return _pretty_problem(level, detail)
    label = _pretty_component_label(component)
    if component in {
        "cloud",
        "docker",
        "email",
        "email-cloud",
        "webhook",
        "webhook-cloud",
    }:
        return _pretty_tool_row(label, detail)
    return _pretty_text(detail)


def _pretty_debug(
    *,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str:
    detail = _pretty_message_with_extras(event, message, fields)
    return f"Debug: {component}:{event} | {detail}"


def _pretty_problem(level: JourneyLogLevel, detail: object) -> str:
    prefix = "Error" if level == "error" else "Warning"
    return f"{prefix}: {_pretty_text(detail)}"


def _pretty_step_problem(
    level: JourneyLogLevel,
    action: str,
    fields: dict[str, object],
    *,
    fallback: str,
) -> str:
    step = _pretty_text(fields.get("step") or "step")
    duration = fields.get("duration")
    error = fields.get("error")
    if duration is not None:
        detail = f"{step} {action} after {_pretty_text(duration)}"
        if error is not None:
            detail += f" ({_pretty_text(error)})"
        return _pretty_problem(level, detail)
    return _pretty_problem(level, f"{step} {fallback}")


def _pretty_step_row(fields: dict[str, object], detail: str) -> str:
    return _pretty_row(
        indent=6,
        label=_pretty_text(fields.get("step") or "step"),
        detail=detail,
        width=_PRETTY_STEP_LABEL_WIDTH,
    )


def _pretty_tool_row(label: str, detail: object) -> str:
    return _pretty_row(
        indent=8,
        label=label,
        detail=_pretty_text(detail),
        width=_PRETTY_TOOL_LABEL_WIDTH,
    )


def _pretty_prompt_row(label: str, detail: object) -> str:
    return _pretty_row(
        indent=10,
        label=label,
        detail=_pretty_text(detail),
        width=_PRETTY_PROMPT_LABEL_WIDTH,
    )


def _pretty_prompt_continuation(detail: object) -> str:
    return f"{' ' * 10}{' ' * (_PRETTY_PROMPT_LABEL_WIDTH + 1)}{_pretty_text(detail)}"


def _pretty_row(*, indent: int, label: str, detail: object, width: int) -> str:
    text = _pretty_text(detail)
    if not text:
        return f"{' ' * indent}{label}"
    return f"{' ' * indent}{label:<{width}} {text}"


def _step_detail(action: str, fields: dict[str, object], *, include_attempt: bool) -> str:
    parts = [action]
    attempt = fields.get("attempt")
    if include_attempt and attempt is not None:
        parts.append(f"attempt={_pretty_text(attempt)}")
    duration = fields.get("duration")
    if duration is not None:
        parts.append(f"duration={_pretty_text(duration)}")
    return " ".join(parts)


def _pretty_message_with_extras(
    event: str,
    message: str,
    fields: dict[str, object],
) -> str:
    detail = _pretty_text(message)
    extras = _pretty_extra_fields(event=event, fields=fields)
    if extras:
        return f"{detail} {extras}" if detail else extras
    return detail


def _pretty_target(fields: dict[str, object]) -> str:
    display_file = fields.get("display_file") or fields.get("file")
    journey = fields.get("journey") or fields.get("journey_id")
    if display_file is not None and journey is not None:
        return f"{_pretty_text(display_file)}:{_pretty_text(journey)}"
    if journey is not None:
        return _pretty_text(journey)
    if display_file is not None:
        return _pretty_text(display_file)
    return "journey"


def _pretty_count(value: object, singular: str) -> str:
    if isinstance(value, int):
        noun = singular if value == 1 else f"{singular}s"
        return f"{value} {noun}"
    return f"0 {singular}s"


def _pretty_labels(value: object) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(_pretty_text(item) for item in value)
    if value is None:
        return ""
    return _pretty_text(value)


def _pretty_branch_env(value: object) -> str:
    if value in (None, {}, "{}"):
        return ""
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_pretty_text(key)}={_pretty_text(nested)}"
            for key, nested in value.items()
        ) + "}"
    return _pretty_text(value)


def _pretty_key_value(key: str, value: object) -> str:
    if value is None:
        return ""
    return f"{key}={_pretty_text(value)}"


def _pretty_prompt_step_ref(fields: dict[str, object]) -> str:
    step = fields.get("step")
    max_steps = fields.get("max_steps")
    if step is not None and max_steps is not None:
        return f"{_pretty_text(step)}/{_pretty_text(max_steps)}"
    step_label = fields.get("step_label")
    if step_label is not None:
        return _strip_prefix(_pretty_text(step_label), "step ")
    return "step"


def _page_detail(fields: dict[str, object]) -> str:
    page = fields.get("page")
    title = fields.get("title")
    url = fields.get("url")
    if page is None and title is None and url is None:
        return ""
    detail = f"page {_pretty_text(page)}" if page is not None else "page"
    if title is not None:
        detail += f" '{_pretty_text(title)}'"
    if url is not None:
        detail += f" at {_pretty_text(url)}"
    return detail


def _pretty_component_label(component: str) -> str:
    labels = {
        "cloud": "Cloud",
        "docker": "Docker",
        "email": "Email",
        "email-cloud": "Email",
        "webhook": "Webhook",
        "webhook-cloud": "Webhook",
    }
    return labels.get(component, component.replace("-", " ").capitalize())


def _first_word_after(text: str, prefix: str) -> str:
    if prefix not in text:
        return ""
    remainder = text.split(prefix, 1)[1].strip()
    return remainder.split(maxsplit=1)[0] if remainder else ""


def _after(text: str, marker: str) -> str:
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].strip()


def _strip_prefix(text: str, prefix: str) -> str:
    return text[len(prefix) :] if text.startswith(prefix) else text


def _pretty_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict | list | tuple):
        return json.dumps(
            _json_safe_value(value),
            ensure_ascii=True,
            default=str,
            separators=(",", ":"),
        )
    return _redact_pretty_text(str(value))


def _redact_pretty_text(text: str) -> str:
    def quoted_replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[redacted]{match.group(2)}"

    redacted = _PASSWORD_QUOTED_RE.sub(quoted_replacement, text)
    return _PASSWORD_BARE_RE.sub(r"\1[redacted]", redacted)


def _pretty_extra_fields(
    *,
    event: str,
    fields: dict[str, object],
) -> str:
    hidden = _pretty_hidden_fields(event=event, fields=fields)
    parts = []
    for key in sorted(fields):
        if key in hidden:
            continue
        if fields[key] is None:
            continue
        value = _format_pretty_field_value(key, fields[key])
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _pretty_hidden_fields(*, event: str, fields: dict[str, object]) -> set[str]:
    if (
        event == "step_success"
        and fields.get("step") is not None
        and fields.get("duration") is not None
    ):
        return {"attempt", "case", "duration", "file", "journey", "node_index", "step"}
    return set()


def _format_pretty_field_value(key: str, value: object) -> str:
    if _is_sensitive_field(key):
        return "[redacted]"
    safe_value = _json_safe_value(value)
    if safe_value is None:
        return "null"
    if isinstance(safe_value, bool):
        return "true" if safe_value else "false"
    if isinstance(safe_value, int | float) and not isinstance(safe_value, bool):
        return str(safe_value)
    if isinstance(safe_value, str):
        return _redact_pretty_text(safe_value)
    return json.dumps(
        safe_value,
        ensure_ascii=True,
        default=str,
        separators=(",", ":"),
    )


def _colorize_pretty(line: str, *, level: JourneyLogLevel, stream: TextIO) -> str:
    if not _stream_supports_color(stream):
        return line
    colors = {
        "debug": "\033[2m",
        "warning": "\033[33m",
        "error": "\033[31m",
    }
    color = colors.get(level)
    if color is None:
        return line
    return f"{color}{line}\033[0m"


def _stream_supports_color(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty is not None and isatty())


def _format_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _format_field_value(key: str, value: object) -> str:
    if _is_sensitive_field(key):
        return _format_value("[redacted]")
    return _format_value(value)


def _json_field_value(key: str, value: object) -> object:
    if _is_sensitive_field(key):
        return "[redacted]"
    return _json_safe_value(value)


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_field_value(str(key), nested)
            for key, nested in value.items()
        }
    if isinstance(value, list | tuple):
        return [_json_safe_value(item) for item in value]
    return str(value)


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if _SAFE_VALUE_RE.fullmatch(text):
        return text
    return json.dumps(text, ensure_ascii=True)


def _is_sensitive_field(key: str) -> bool:
    normalized = key.lower()
    return any(fragment in normalized for fragment in _SENSITIVE_FIELD_FRAGMENTS)


def _normalize_component(component: str) -> str:
    stripped = component.strip()
    if not stripped:
        raise ValueError("get_logger(...) expects a non-empty component name.")
    return stripped


def _normalize_event(event: str) -> str:
    stripped = event.strip()
    if not stripped:
        raise ValueError("JourneyLogger expects a non-empty event name.")
    return stripped


def _require_level(level: str) -> None:
    if level not in _LEVEL_VALUES:
        choices = ", ".join(_LEVEL_VALUES)
        raise ValueError(f"Journey log level must be one of: {choices}.")


def _require_output_format(output_format: str) -> None:
    if output_format not in _OUTPUT_FORMATS:
        choices = ", ".join(sorted(_OUTPUT_FORMATS))
        raise ValueError(f"Journey output format must be one of: {choices}.")


def _should_emit(level: JourneyLogLevel) -> bool:
    with _config_lock:
        configured = _configured_level
    return _LEVEL_VALUES[level] >= _LEVEL_VALUES[configured]


def _active_output_format() -> JourneyOutputFormat:
    with _config_lock:
        output_format = _configured_output_format
    return output_format


def _active_stream() -> TextIO:
    with _config_lock:
        stream = _configured_stream
    return stream if stream is not None else sys.stdout


__all__ = [
    "JourneyLogLevel",
    "JourneyLogger",
    "JourneyOutputFormat",
    "configure_logging",
    "get_logger",
]
