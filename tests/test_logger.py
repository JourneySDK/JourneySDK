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


def test_logger_writes_pretty_format_to_current_stdout(capsys: pytest.CaptureFixture[str]):
    get_logger("executor").info(
        "step_success",
        "  step prepare attempt=1 ok duration=0.012s",
        journey="flow",
        case="case_1",
        step="prepare",
        attempt=1,
        duration="0.012s",
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "      prepare                       ok attempt=1 duration=0.012s\n"


def test_logger_pretty_renders_plan_as_readable_timeline(capsys: pytest.CaptureFixture[str]):
    logger = get_logger("cli")

    logger.info("plan_start", "Plan")
    logger.info(
        "plan_journey",
        "Journey test.py:flow",
        display_file="test.py",
        journey="flow",
        cases=1,
        function_ref="module:flow",
        journey_id="flow",
    )
    logger.info(
        "plan_metadata",
        "journey_id=flow function_ref=module:flow",
        display_file="test.py",
        journey="flow",
        function_ref="module:flow",
        journey_id="flow",
    )
    logger.info(
        "plan_case",
        "- case_1 branch_env={} labels=['first', 'second']",
        case="case_1",
        branch_env={},
        labels=["first", "second"],
    )
    logger.info(
        "plan_summary",
        "Summary: 1 journey planned, 1 case planned, 0 failed",
        journeys=1,
        cases=1,
        failures=0,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        "Plan\n"
        "  test.py:flow\n"
        "    case_1  labels: first, second\n"
        "  Summary: 1 journey planned, 1 case planned, 0 failed\n"
    )


def test_logger_pretty_renders_browser_and_ai_prompt_activity(
    capsys: pytest.CaptureFixture[str],
):
    browser = get_logger("playwright")
    prompt = get_logger("playwright-prompt")

    browser.info(
        "open_page_start",
        "opening Playwright page",
        browser="chromium",
        headless=False,
        url="https://app.example/login",
    )
    browser.info(
        "open_page_success",
        "Playwright page opened",
        browser="chromium",
        url="https://app.example/dashboard",
    )
    prompt.info(
        "prompt_start",
        "prompt start: instruction='Sign in using password \"1111\".'",
        instruction='Sign in using password "1111".',
        model="claude-sonnet-4-6",
        max_steps=15,
        timeout="5s",
        active="page 0 'Login' at https://app.example/login",
    )
    prompt.info(
        "prompt_action",
        "step 1/15: AI will click selector '#sign-in'",
        step=1,
        max_steps=15,
        action="click selector '#sign-in'",
    )
    prompt.info(
        "prompt_code",
        'step 1/15 code: page.locator("#sign-in").click()',
        step_label="step 1/15",
        code='page.locator("#sign-in").click()',
    )
    prompt.info(
        "prompt_step_success",
        "step 1/15: succeeded on page 0 'Login'",
        step=1,
        max_steps=15,
        page=0,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        "        Browser                     opening chromium https://app.example/login headless=false\n"
        "        Browser                     opened chromium https://app.example/dashboard\n"
        "        AI prompt                   model=claude-sonnet-4-6 max_steps=15 timeout=5s\n"
        "          instruction               Sign in using password \"[redacted]\".\n"
        "          page                      page 0 'Login' at https://app.example/login\n"
        "          1/15 action               click selector '#sign-in'\n"
        "          1/15 code                 page.locator(\"#sign-in\").click()\n"
        "          1/15 ok                   page 0 'Login'\n"
    )


def test_logger_writes_structured_format_to_current_stdout(capsys: pytest.CaptureFixture[str]):
    configure_logging("info", output_format="structured")

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
    assert captured.out == "Warning: using fallback\n"
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
    configure_logging("debug", stream=stream, output_format="structured")

    get_logger("docker").debug("cli_start", "running command", command="docker ps")

    assert stream.flush_count == 1
    assert "level=DEBUG" in stream.getvalue()
    assert 'command="docker ps"' in stream.getvalue()


def test_logger_redacts_sensitive_fields(capsys: pytest.CaptureFixture[str]):
    configure_logging("info", output_format="structured")

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


def test_logger_redacts_sensitive_fields_in_pretty(capsys: pytest.CaptureFixture[str]):
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
    assert "api_key=[redacted]" in output
    assert "authorization=[redacted]" in output
    assert "password=[redacted]" in output
    assert "route=/v1/test" in output


def test_logger_colors_pretty_output_for_tty_streams() -> None:
    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    stream = TtyStream()
    configure_logging("info", stream=stream)

    get_logger("executor").warning("step_retry", "retrying step", step="prepare")

    output = stream.getvalue()
    assert "\x1b[" in output
    assert "Warning: prepare retrying" in output


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


def test_logger_rejects_invalid_output_format() -> None:
    with pytest.raises(ValueError):
        configure_logging("info", output_format="text")  # type: ignore[arg-type]
