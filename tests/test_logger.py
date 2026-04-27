from __future__ import annotations

from io import StringIO
import re

import pytest

from journeysdk.logger import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging("info")
    yield
    configure_logging("info")


def test_logger_writes_common_format_to_current_stderr(capsys: pytest.CaptureFixture[str]):
    get_logger("executor").info(
        "step_start",
        "starting step",
        journey="flow",
        case="case_1",
        step="prepare",
        attempt=1,
    )

    output = capsys.readouterr().err.strip()
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
    assert "discovery_warning" in capsys.readouterr().err

    configure_logging("off")
    logger.error("failure", "this should be suppressed")
    assert capsys.readouterr().err == ""


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

    output = capsys.readouterr().err
    assert "secret-api-key" not in output
    assert "Bearer token" not in output
    assert "secret-password" not in output
    assert 'api_key="[redacted]"' in output
    assert 'authorization="[redacted]"' in output
    assert 'password="[redacted]"' in output
    assert "route=/v1/test" in output


def test_logger_rejects_invalid_level() -> None:
    with pytest.raises(ValueError):
        configure_logging("verbose")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        get_logger("executor").log("verbose", "event", "message")  # type: ignore[arg-type]
