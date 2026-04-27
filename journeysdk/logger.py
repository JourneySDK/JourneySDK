"""Central logging helpers for Journey SDK diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sys
from threading import Lock
from typing import Literal, TextIO

JourneyLogLevel = Literal["debug", "info", "warning", "error", "off"]

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
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")
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
        line = _format_log_line(
            level=level,
            component=self._component,
            event=_normalize_event(event),
            message=str(message),
            fields=fields,
        )
        stream = _active_stream()
        print(line, file=stream, flush=True)


def configure_logging(
    level: JourneyLogLevel = "info",
    stream: TextIO | None = None,
) -> None:
    """Configure process-wide Journey SDK diagnostic logging."""

    _require_level(level)
    global _configured_level, _configured_stream
    with _config_lock:
        _configured_level = level
        _configured_stream = stream


def get_logger(component: str) -> JourneyLogger:
    """Return a logger for one Journey SDK component."""

    return JourneyLogger(component)


def _format_log_line(
    *,
    level: JourneyLogLevel,
    component: str,
    event: str,
    message: str,
    fields: dict[str, object],
) -> str:
    parts = [
        "[journey]",
        f"time={_format_timestamp()}",
        f"level={_LEVEL_NAMES[level]}",
        f"component={_format_value(component)}",
        f"event={_format_value(event)}",
        f"message={_format_value(message)}",
    ]
    for key in sorted(fields):
        parts.append(f"{key}={_format_field_value(key, fields[key])}")
    return " ".join(parts)


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


def _should_emit(level: JourneyLogLevel) -> bool:
    with _config_lock:
        configured = _configured_level
    return _LEVEL_VALUES[level] >= _LEVEL_VALUES[configured]


def _active_stream() -> TextIO:
    with _config_lock:
        stream = _configured_stream
    return stream if stream is not None else sys.stderr


__all__ = [
    "JourneyLogLevel",
    "JourneyLogger",
    "configure_logging",
    "get_logger",
]
