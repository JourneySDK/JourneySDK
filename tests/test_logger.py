from __future__ import annotations

import json
from io import StringIO
import re

import pytest

from journeysdk.logger import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging("info")
    yield
    configure_logging("info")


def test_logger_writes_common_format_to_current_stdout(capsys: pytest.CaptureFixture[str]):
    get_logger("executor").info(
        "step_start",
        "starting step",
        journey="flow",
        case="case_1",
        step="prepare",
        attempt=1,
    )

    captured = capsys.readouterr()
    output = captured.out.strip()
    assert captured.err == ""
    assert re.match(
        r'^\[journey\] time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z '
        r'level=INFO component=executor event=step_start message="starting step" '
        r"attempt=1 case=case_1 journey=flow step=prepare$",
        output,
    )


def test_logger_respects_level_filtering_and_off(capsys: pytest.CaptureFixture[str]):
    logger = get_logger("cli")
    configure_logging("warning")

    logger.info("discovery_start", "discovering journeys")
    logger.warning("discovery_warning", "using fallback")
    captured = capsys.readouterr()
    assert "discovery_warning" in captured.out
    assert captured.err == ""

    configure_logging("off")
    logger.error("failure", "this should be suppressed")
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
    configure_logging("debug", stream=stream)

    get_logger("docker").debug("cli_start", "running command", command="docker ps")

    assert stream.flush_count == 1
    assert "level=DEBUG" in stream.getvalue()
    assert 'command="docker ps"' in stream.getvalue()


def test_logger_redacts_sensitive_fields(capsys: pytest.CaptureFixture[str]):
    get_logger("cloud").info(
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


def test_logger_writes_jsonl_records_to_stdout(capsys: pytest.CaptureFixture[str]):
    configure_logging("info", output_format="jsonl")

    get_logger("cli").info(
        "execute_result",
        "execution result",
        payload={"journeys": [{"journey_name": "flow"}], "errors": []},
        api_token="secret-token",
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    record = json.loads(captured.out)
    assert record["level"] == "INFO"
    assert record["component"] == "cli"
    assert record["event"] == "execute_result"
    assert record["message"] == "execution result"
    assert record["payload"]["journeys"][0]["journey_name"] == "flow"
    assert record["api_token"] == "[redacted]"
    assert "secret-token" not in captured.out


def test_logger_rejects_invalid_level() -> None:
    with pytest.raises(ValueError):
        configure_logging("verbose")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        get_logger("executor").log("verbose", "event", "message")  # type: ignore[arg-type]
