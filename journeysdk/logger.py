"""Central logging helpers for Journey SDK diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sys
from threading import Lock
from typing import Any, Literal, Sequence, TextIO, TypeAlias

JourneyLogLevel = Literal["debug", "info", "warning", "error", "off"]
JourneyOutputFormat = Literal["pretty", "structured", "jsonl"]
PrettyStyle = Literal[
    "default",
    "heading",
    "context",
    "touchpoint",
    "accent",
    "code",
    "success",
    "warning",
    "error",
    "muted",
]

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
_ANSI_STYLES: dict[PrettyStyle, str] = {
    "default": "",
    "heading": "\033[1m",
    "context": "\033[36m",
    "touchpoint": "\033[36m",
    "accent": "\033[35m",
    "code": "\033[2m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "muted": "\033[2m",
}

_configured_level: JourneyLogLevel = "info"
_configured_stream: TextIO | None = None
_configured_output_format: JourneyOutputFormat = "pretty"
_config_lock = Lock()


@dataclass(frozen=True)
class PrettyLine:
    """One human-facing pretty output line with optional terminal styling."""

    text: str
    style: PrettyStyle | None = None


@dataclass(frozen=True)
class JourneyLogRecord:
    """One JSON-safe structured Journey diagnostic record."""

    level: JourneyLogLevel
    component: str
    event: str
    message: str
    fields: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        """Return the record in the same field shape as JSON log output."""

        return {
            "level": _LEVEL_NAMES[self.level],
            "component": self.component,
            "event": self.event,
            "message": self.message,
            **self.fields,
        }


PrettyValue: TypeAlias = str | PrettyLine | Sequence[str | PrettyLine] | bool | None


class JourneyLogger:
    """Component-scoped logger for Journey SDK diagnostics."""

    def __init__(self, component: str) -> None:
        self._component = _normalize_component(component)

    @property
    def component(self) -> str:
        return self._component

    def debug(
        self,
        event: str,
        message: str,
        *,
        pretty: PrettyValue = None,
        **fields: object,
    ) -> None:
        self.log("debug", event, message, pretty=pretty, **fields)

    def info(
        self,
        event: str,
        message: str,
        *,
        pretty: PrettyValue = None,
        **fields: object,
    ) -> None:
        self.log("info", event, message, pretty=pretty, **fields)

    def warning(
        self,
        event: str,
        message: str,
        *,
        pretty: PrettyValue = None,
        **fields: object,
    ) -> None:
        self.log("warning", event, message, pretty=pretty, **fields)

    def error(
        self,
        event: str,
        message: str,
        *,
        pretty: PrettyValue = None,
        **fields: object,
    ) -> None:
        self.log("error", event, message, pretty=pretty, **fields)

    def log(
        self,
        level: JourneyLogLevel,
        event: str,
        message: str,
        *,
        pretty: PrettyValue = None,
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
                pretty=pretty,
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


def make_log_record(
    component: str,
    event: str,
    message: object,
    *,
    level: JourneyLogLevel = "info",
    **fields: object,
) -> JourneyLogRecord:
    """Return one JSON-safe Journey log record without emitting it."""

    if level == "off":
        raise ValueError("make_log_record(...) does not accept level='off'.")
    _require_level(level)
    return JourneyLogRecord(
        level=level,
        component=_normalize_component(component),
        event=_normalize_event(event),
        message=str(message),
        fields={
            key: _json_field_value(key, fields[key])
            for key in sorted(fields)
            if fields[key] is not None
        },
    )


def pretty_line(text: object, *, indent: int = 0, style: PrettyStyle | None = None) -> PrettyLine:
    """Return one generic pretty line for human-facing output."""

    return PrettyLine(f"{' ' * _require_non_negative_int(indent, 'indent')}{text}", style)


def pretty_row(
    label: object,
    detail: object = "",
    *,
    indent: int = 0,
    label_width: int = 0,
    style: PrettyStyle | None = None,
) -> PrettyLine:
    """Return an aligned pretty row whose multiline detail stays aligned."""

    indent = _require_non_negative_int(indent, "indent")
    label_width = _require_non_negative_int(label_width, "label_width")
    rendered_label = str(label)
    rendered_detail = str(detail) if detail is not None else ""
    if not rendered_detail:
        return PrettyLine(f"{' ' * indent}{rendered_label}", style)
    if label_width:
        first_prefix = f"{' ' * indent}{rendered_label:<{label_width}} "
    else:
        first_prefix = f"{' ' * indent}{rendered_label} "
    return PrettyLine(
        _first_line_with_continuation(
            rendered_detail,
            first_prefix=first_prefix,
            continuation_prefix=" " * len(first_prefix),
        ),
        style,
    )


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
    pretty: PrettyValue,
    stream: TextIO,
) -> str | None:
    rendered = _pretty_lines(
        level=level,
        component=component,
        event=event,
        message=message,
        fields=fields,
        pretty=pretty,
    )
    if rendered is None:
        return None
    return "\n".join(_colorize_pretty(line, stream=stream) for line in rendered)


def _pretty_lines(
    *,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
    pretty: PrettyValue,
) -> list[PrettyLine] | None:
    if pretty is False:
        return None
    if pretty is None or pretty is True:
        text = _pretty_message_with_extras(message, fields)
        if level == "debug":
            return [PrettyLine(f"Debug: {component}:{event} | {text}", "muted")]
        if level in {"warning", "error"}:
            return [PrettyLine(_problem_text(level, text), _level_style(level))]
        return [PrettyLine(text, None)]
    if isinstance(pretty, PrettyLine):
        return [_normalize_pretty_line(pretty, level=level, prefix_problem=False)]
    if isinstance(pretty, str):
        return [_normalize_pretty_line(PrettyLine(pretty), level=level, prefix_problem=True)]
    return [
        _normalize_pretty_line(
            item if isinstance(item, PrettyLine) else PrettyLine(str(item)),
            level=level,
            prefix_problem=False,
        )
        for item in pretty
    ]


def _normalize_pretty_line(
    line: PrettyLine,
    *,
    level: JourneyLogLevel,
    prefix_problem: bool,
) -> PrettyLine:
    text = _redact_pretty_text(line.text)
    style = line.style
    if level in {"warning", "error"}:
        style = style or _level_style(level)
        if prefix_problem:
            text = _problem_text(level, text)
    return PrettyLine(text, style)


def _problem_text(level: JourneyLogLevel, text: str) -> str:
    prefix = "Error" if level == "error" else "Warning"
    if text.startswith(f"{prefix}:"):
        return text
    return f"{prefix}: {text}"


def _level_style(level: JourneyLogLevel) -> PrettyStyle | None:
    if level == "debug":
        return "muted"
    if level == "warning":
        return "warning"
    if level == "error":
        return "error"
    return None


def _pretty_message_with_extras(message: str, fields: dict[str, object]) -> str:
    detail = _pretty_text(message)
    extras = _pretty_extra_fields(fields)
    if extras:
        return f"{detail} {extras}" if detail else extras
    return detail


def _pretty_extra_fields(fields: dict[str, object]) -> str:
    parts = []
    for key in sorted(fields):
        if fields[key] is None:
            continue
        value = _format_pretty_field_value(key, fields[key])
        parts.append(f"{key}={value}")
    return " ".join(parts)


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


def _redact_pretty_text(text: str) -> str:
    def quoted_replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[redacted]{match.group(2)}"

    redacted = _PASSWORD_QUOTED_RE.sub(quoted_replacement, text)
    return _PASSWORD_BARE_RE.sub(r"\1[redacted]", redacted)


def _first_line_with_continuation(
    text: str,
    *,
    first_prefix: str,
    continuation_prefix: str,
) -> str:
    lines = text.splitlines() or [""]
    rendered = [f"{first_prefix}{lines[0]}"]
    rendered.extend(f"{continuation_prefix}{line}" for line in lines[1:])
    return "\n".join(rendered)


def _colorize_pretty(line: PrettyLine, *, stream: TextIO) -> str:
    if line.style is None or line.style == "default" or not _stream_supports_color(stream):
        return line.text
    color = _ANSI_STYLES[line.style]
    if not color:
        return line.text
    return f"{color}{line.text}\033[0m"


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


def _require_non_negative_int(value: int, name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


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
    "JourneyLogRecord",
    "JourneyLogger",
    "JourneyOutputFormat",
    "PrettyLine",
    "PrettyStyle",
    "configure_logging",
    "get_logger",
    "make_log_record",
    "pretty_line",
    "pretty_row",
]
