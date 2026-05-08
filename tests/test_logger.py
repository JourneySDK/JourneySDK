from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
import re

import pytest

from journeysdk.logger import (
    configure_logging,
    get_logger,
    make_log_record,
    pretty_line,
    pretty_row,
)


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging("info")
    yield
    configure_logging("info")


def test_logger_writes_generic_pretty_fallback_to_current_stdout(
    capsys: pytest.CaptureFixture[str],
):
    get_logger("component").info(
        "event_name",
        "generic message",
        detail="visible",
        empty=None,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "generic message detail=visible\n"


def test_make_log_record_normalizes_json_safe_fields() -> None:
    record = make_log_record(
        "  component  ",
        " event_name ",
        "machine message",
        detail={"nested": ("a", Path("file.txt"))},
        count=3,
    )

    assert record.component == "component"
    assert record.event == "event_name"
    assert record.level == "info"
    assert record.message == "machine message"
    assert record.fields == {
        "count": 3,
        "detail": {"nested": ["a", "file.txt"]},
    }
    assert record.to_dict() == {
        "level": "INFO",
        "component": "component",
        "event": "event_name",
        "message": "machine message",
        "count": 3,
        "detail": {"nested": ["a", "file.txt"]},
    }


def test_make_log_record_redacts_sensitive_fields() -> None:
    record = make_log_record(
        "component",
        "event_name",
        "machine message",
        api_token="secret-token",
        nested={"password": "secret-password"},
    )

    assert record.fields == {
        "api_token": "[redacted]",
        "nested": {"password": "[redacted]"},
    }
    assert "secret" not in json.dumps(record.to_dict())


def test_logger_accepts_explicit_pretty_text(capsys: pytest.CaptureFixture[str]):
    get_logger("component").info(
        "event_name",
        "machine message",
        pretty="Human message using password \"secret\".",
        detail="machine-only",
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "Human message using password \"[redacted]\".\n"


def test_logger_pretty_false_suppresses_only_pretty_output(
    capsys: pytest.CaptureFixture[str],
):
    logger = get_logger("component")

    logger.info("event_name", "machine message", pretty=False, detail="value")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    configure_logging("info", output_format="structured")
    logger.info("event_name", "machine message", pretty=False, detail="value")
    captured = capsys.readouterr()
    assert "message=\"machine message\"" in captured.out
    assert "detail=value" in captured.out
    assert "pretty" not in captured.out


def test_logger_pretty_row_aligns_multiline_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_logger("component").info(
        "event_name",
        "machine message",
        pretty=pretty_row(
            "1/15 rejected",
            (
                "TimeoutError: Locator.click: Timeout 5000ms exceeded.\n"
                "Call log:\n"
                "- waiting for get_by_role(\"button\", name=\"Log in\")"
            ),
            indent=10,
            label_width=25,
            style="warning",
        ),
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        "          1/15 rejected             TimeoutError: Locator.click: Timeout 5000ms exceeded.\n"
        "                                    Call log:\n"
        "                                    - waiting for get_by_role(\"button\", name=\"Log in\")\n"
    )


def test_logger_pretty_sequence_renders_multiple_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_logger("component").info(
        "event_name",
        "machine message",
        pretty=[
            pretty_line("Plan", style="heading"),
            pretty_row("case_1", "labels: first", indent=4, label_width=8),
        ],
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "Plan\n    case_1   labels: first\n"


def test_logger_styles_pretty_output_for_tty_streams() -> None:
    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    stream = TtyStream()
    configure_logging("info", stream=stream)

    logger = get_logger("component")
    logger.info("heading_event", "machine", pretty=pretty_line("Plan", style="heading"))
    logger.info(
        "touchpoint_event",
        "machine",
        pretty=pretty_row("Browser", "opening chromium", indent=8, label_width=27, style="touchpoint"),
    )
    logger.info(
        "success_event",
        "machine",
        pretty=pretty_row("prepare", "ok", indent=6, label_width=29, style="success"),
    )
    logger.warning("warning_event", "machine", pretty="prepare retrying")
    logger.error("error_event", "machine", pretty="prepare failed")

    output = stream.getvalue()
    assert "\x1b[1mPlan\x1b[0m" in output
    assert "\x1b[36m        Browser" in output
    assert "\x1b[32m      prepare" in output
    assert "\x1b[33mWarning: prepare retrying\x1b[0m" in output
    assert "\x1b[31mError: prepare failed\x1b[0m" in output


def test_logger_writes_structured_format_to_current_stdout(capsys: pytest.CaptureFixture[str]):
    configure_logging("info", output_format="structured")

    get_logger("component").info(
        "event_name",
        "starting step",
        pretty=pretty_line("Human only"),
        journey="flow",
        case="case_1",
        step="prepare",
        attempt=1,
    )

    captured = capsys.readouterr()
    output = captured.out.strip()
    assert captured.err == ""
    assert "pretty" not in output
    assert "Human only" not in output
    assert re.match(
        r'^\[journey\] time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z '
        r'level=INFO component=component event=event_name message="starting step" '
        r"attempt=1 case=case_1 journey=flow step=prepare$",
        output,
    )


def test_logger_respects_level_filtering_and_off(capsys: pytest.CaptureFixture[str]):
    logger = get_logger("component")
    configure_logging("warning")

    logger.info("info_event", "hidden")
    logger.warning("warning_event", "using fallback")
    captured = capsys.readouterr()
    assert captured.out == "Warning: using fallback\n"
    assert captured.err == ""

    configure_logging("off")
    logger.error("error_event", "this should be suppressed")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_logger_accepts_configured_stream_and_flushes() -> None:
    class FlushRecordingStream(StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1
            super().flush()

    stream = FlushRecordingStream()
    configure_logging("debug", stream=stream, output_format="structured")

    get_logger("component").debug("cli_start", "running command", command="docker ps")

    assert stream.flush_count == 1
    assert "level=DEBUG" in stream.getvalue()
    assert 'command="docker ps"' in stream.getvalue()


def test_logger_redacts_sensitive_fields(capsys: pytest.CaptureFixture[str]):
    configure_logging("info", output_format="structured")

    get_logger("component").info(
        "request_start",
        "calling cloud",
        api_key="secret-api-key",
        authorization="Bearer token",
        password="secret-password",
        route="/v1/test",
    )

    output = capsys.readouterr().out
    assert "secret-api-key" not in output
    assert "Bearer token" not in output
    assert "secret-password" not in output
    assert 'api_key="[redacted]"' in output
    assert 'authorization="[redacted]"' in output
    assert 'password="[redacted]"' in output
    assert "route=/v1/test" in output


def test_logger_redacts_sensitive_fields_in_pretty(capsys: pytest.CaptureFixture[str]):
    get_logger("component").info(
        "request_start",
        "calling cloud",
        api_key="secret-api-key",
        authorization="Bearer token",
        password="secret-password",
        route="/v1/test",
    )

    output = capsys.readouterr().out
    assert "secret-api-key" not in output
    assert "Bearer token" not in output
    assert "secret-password" not in output
    assert "api_key=[redacted]" in output
    assert "authorization=[redacted]" in output
    assert "password=[redacted]" in output
    assert "route=/v1/test" in output


def test_logger_writes_jsonl_records_to_stdout(capsys: pytest.CaptureFixture[str]):
    configure_logging("info", output_format="jsonl")

    get_logger("component").info(
        "execute_result",
        "execution result",
        pretty=pretty_line("Human only"),
        payload={"journeys": [{"journey_name": "flow"}], "errors": []},
        api_token="secret-token",
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    record = json.loads(captured.out)
    assert "pretty" not in record
    assert record["level"] == "INFO"
    assert record["component"] == "component"
    assert record["event"] == "execute_result"
    assert record["message"] == "execution result"
    assert record["payload"]["journeys"][0]["journey_name"] == "flow"
    assert record["api_token"] == "[redacted]"
    assert "secret-token" not in captured.out
    assert "Human only" not in captured.out


def test_logger_rejects_invalid_level() -> None:
    with pytest.raises(ValueError):
        configure_logging("verbose")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        get_logger("component").log("verbose", "event", "message")  # type: ignore[arg-type]


def test_logger_rejects_invalid_output_format() -> None:
    with pytest.raises(ValueError):
        configure_logging("info", output_format="text")  # type: ignore[arg-type]


def test_logger_has_no_component_or_event_specific_pretty_knowledge() -> None:
    source = Path("journeysdk/logger.py").read_text(encoding="utf-8")

    forbidden_tokens = [
        "plan_start",
        "plan_journey",
        "step_success",
        "open_page_start",
        "prompt_code",
        "playwright-prompt",
        "docker",
        "webhook",
        "email-cloud",
    ]
    for token in forbidden_tokens:
        assert token not in source
