from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
import pytest

from journeysdk._prompt_memory import (
    PromptMemoryEntry,
    PromptMemorySection,
    load_prompt_memory_entry,
    write_prompt_memory_entry,
)
from journeysdk._prompt_engine import (
    PromptActionContext,
    PromptEngineSession,
    PromptMemoryCompileContext,
    PromptMemoryDraft,
    PromptMemoryReplayResult,
    PromptObservation,
    PromptTextSection,
    _compact_prompt_observation_messages,
)
from journeysdk.logger import (
    JourneyLogRecord,
    configure_logging,
    get_logger,
    make_log_record,
    pretty_row,
)


class _FakeAIMessage:
    def __init__(
        self,
        *,
        content: object = "",
        tool_calls: list[dict[str, object]] | None = None,
        usage_metadata: dict[str, object] | None = None,
        response_metadata: dict[str, object] | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.invalid_tool_calls: list[object] = []
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


class _FakeStructuredResponse(dict):
    def __init__(
        self,
        payload: dict[str, object],
        *,
        usage_metadata: dict[str, object],
        model_name: str,
    ) -> None:
        super().__init__(payload)
        self.usage_metadata = usage_metadata
        self.response_metadata = {"model_name": model_name}


class _FakeChatGeneration:
    def __init__(self, message: _FakeAIMessage | _FakeStructuredResponse) -> None:
        self.message = message


class _FakeLLMResult:
    def __init__(self, message: _FakeAIMessage | _FakeStructuredResponse) -> None:
        self.generations = [[_FakeChatGeneration(message)]]


class _FakePromptModel:
    def __init__(
        self,
        responses: list[str | dict[str, object]],
        *,
        structured_responses: list[object] | None = None,
        usage_metadata: list[dict[str, object] | None] | None = None,
        structured_usage_metadata: list[dict[str, object] | None] | None = None,
        model_name: str = "fake:model",
        emit_callbacks: bool = False,
    ) -> None:
        self._responses = list(responses)
        self._structured_responses = list(structured_responses or [])
        self._usage_metadata = list(usage_metadata or [])
        self._structured_usage_metadata = list(structured_usage_metadata or [])
        self.model_name = model_name
        self.emit_callbacks = emit_callbacks
        self.calls: list[dict[str, object]] = []
        self.structured_calls: list[dict[str, object]] = []
        self._response_index = 0

    def with_structured_output(
        self,
        schema: dict[str, object],
        *,
        method: str | None = None,
    ) -> _FakeStructuredPromptModel:
        return _FakeStructuredPromptModel(self, schema, method)

    def next_usage_metadata(self) -> dict[str, object] | None:
        if not self._usage_metadata:
            return None
        return self._usage_metadata.pop(0)

    def next_structured_usage_metadata(self) -> dict[str, object] | None:
        if not self._structured_usage_metadata:
            return None
        return self._structured_usage_metadata.pop(0)

    def emit_llm_end(
        self,
        config: dict[str, object] | None,
        message: _FakeAIMessage | _FakeStructuredResponse,
    ) -> None:
        if not self.emit_callbacks or config is None:
            return
        callbacks = config.get("callbacks")
        if not isinstance(callbacks, list):
            return
        result = _FakeLLMResult(message)
        for callback in callbacks:
            on_llm_end = getattr(callback, "on_llm_end", None)
            if callable(on_llm_end):
                on_llm_end(result)


class _FakeStructuredPromptModel:
    def __init__(
        self,
        prompt_model: _FakePromptModel,
        schema: dict[str, object],
        method: str | None,
    ) -> None:
        self._prompt_model = prompt_model
        self._schema = schema
        self._method = method

    def invoke(
        self,
        messages: list[object],
        *,
        config: dict[str, object] | None = None,
    ) -> object:
        self._prompt_model.structured_calls.append(
            {
                "messages": list(messages),
                "schema": self._schema,
                "method": self._method,
            }
        )
        if not self._prompt_model._structured_responses:
            raise AssertionError("No fake structured LLM responses remaining.")
        response = self._prompt_model._structured_responses.pop(0)
        usage_metadata = self._prompt_model.next_structured_usage_metadata()
        if usage_metadata is None:
            return response
        assert isinstance(response, dict)
        structured_response = _FakeStructuredResponse(
            response,
            usage_metadata=usage_metadata,
            model_name=self._prompt_model.model_name,
        )
        self._prompt_model.emit_llm_end(config, structured_response)
        return structured_response


class _FakeAgent:
    def __init__(
        self,
        model: _FakePromptModel,
        *,
        tools: list[object],
        system_prompt: str,
    ) -> None:
        self._model = model
        self._tools = {getattr(item, "name"): item for item in tools}
        self._system_prompt = system_prompt

    def invoke(
        self,
        payload: dict[str, object],
        *,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raw_messages = payload.get("messages")
        assert isinstance(raw_messages, list)
        messages: list[object] = [
            {"role": "system", "content": self._system_prompt},
            *raw_messages,
        ]
        while True:
            self._model.calls.append({"messages": list(messages)})
            response = self._model._responses.pop(0)
            self._model._response_index += 1
            usage_metadata = self._model.next_usage_metadata()
            response_metadata = (
                {"model_name": self._model.model_name}
                if usage_metadata is not None
                else None
            )
            if isinstance(response, str):
                ai_message = _FakeAIMessage(
                    content=response,
                    usage_metadata=usage_metadata,
                    response_metadata=response_metadata,
                )
            else:
                tool_calls = []
                for index, tool_call in enumerate(response.get("tool_calls", []), start=1):
                    assert isinstance(tool_call, dict)
                    normalized = dict(tool_call)
                    normalized.setdefault(
                        "id",
                        f"fake-call-{self._model._response_index}-{index}",
                    )
                    normalized.setdefault("type", "tool_call")
                    tool_calls.append(normalized)
                ai_message = _FakeAIMessage(
                    tool_calls=tool_calls,
                    usage_metadata=usage_metadata,
                    response_metadata=response_metadata,
                )
            self._model.emit_llm_end(config, ai_message)
            messages.append(ai_message)
            if not ai_message.tool_calls:
                return {"messages": messages}
            for tool_call in ai_message.tool_calls:
                tool_name = tool_call["name"]
                tool_item = self._tools[tool_name]
                tool_message = tool_item.invoke(tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "content": tool_message.content,
                        "tool_call_id": getattr(tool_message, "tool_call_id", ""),
                    }
                )


class _FailingAgent:
    def invoke(
        self,
        payload: dict[str, object],
        *,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise RuntimeError(
            "Could not resolve authentication method. Expected one of api_key, "
            "auth_token, or credentials to be set."
        )


def _fake_create_agent(
    model: object,
    *,
    tools: list[object],
    system_prompt: str,
) -> _FakeAgent:
    assert isinstance(model, _FakePromptModel)
    return _FakeAgent(model, tools=tools, system_prompt=system_prompt)


def _failing_create_agent(
    model: object,
    *,
    tools: list[object],
    system_prompt: str,
) -> _FailingAgent:
    return _FailingAgent()


def _action_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "content": "",
        "tool_calls": [
            {
                "name": name,
                "args": arguments,
            }
        ],
    }


def _finalization(
    output: str | dict[str, object],
    *,
    success: bool = True,
    reason: str = "",
) -> dict[str, object]:
    return {
        "success_criteria_met": success,
        "failure_reason": reason,
        "output": output,
    }


def _prompt_observation_content(
    label: str,
    *,
    prefix: str = "Instruction:\nfinish",
) -> list[dict[str, object]]:
    return [
        {
            "type": "text",
            "text": "\n".join(
                [
                    prefix,
                    "",
                    "Observation records JSON:",
                    json.dumps(
                        [
                            {
                                "event": "page",
                                "label": label,
                            }
                        ],
                        indent=2,
                    ),
                    "",
                    "Active page visible text:",
                    f"{label}-visible-text",
                    "",
                    "Active page rendered HTML:",
                    f"{label}-rendered-html",
                ]
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{label}"},
        },
    ]


def _prompt_screenshot_content(label: str) -> list[dict[str, object]]:
    return [
        {
            "type": "text",
            "text": f"Screenshot captured for {label}.",
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{label}"},
        },
    ]


def _content_text(content: object) -> str:
    assert isinstance(content, list)
    first = content[0]
    assert isinstance(first, dict)
    text = first.get("text")
    assert isinstance(text, str)
    return text


def _has_image_content(content: object) -> bool:
    assert isinstance(content, list)
    return any(
        isinstance(item, dict) and item.get("type") == "image_url"
        for item in content
    )


def test_prompt_engine_observation_compaction_leaves_single_observation_unchanged() -> None:
    messages: list[object] = [
        HumanMessage(content=_prompt_observation_content("initial")),
    ]

    compacted = _compact_prompt_observation_messages(messages)

    assert compacted is messages
    assert compacted[0] is messages[0]
    assert "initial-rendered-html" in _content_text(messages[0].content)
    assert _has_image_content(messages[0].content)


def test_prompt_engine_observation_compaction_keeps_only_latest_full_observation() -> None:
    messages: list[object] = [
        HumanMessage(
            content=_prompt_observation_content(
                "initial",
                prefix=(
                    "Instruction:\nfinish\n\n"
                    "Prompt memory:\nUse cached selectors from a successful run."
                ),
            )
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fake_echo",
                    "args": {"text": "first"},
                    "id": "call-1",
                }
            ],
        ),
        ToolMessage(
            content=_prompt_observation_content("old-tool"),
            tool_call_id="call-1",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fake_echo",
                    "args": {"text": "second"},
                    "id": "call-2",
                }
            ],
        ),
        ToolMessage(
            content=_prompt_observation_content("latest"),
            tool_call_id="call-2",
        ),
    ]

    compacted = _compact_prompt_observation_messages(messages)

    initial_text = _content_text(compacted[0].content)
    assert "Prompt memory:" in initial_text
    assert "initial-rendered-html" not in initial_text
    assert "Previous Journey observation omitted" in initial_text
    assert not _has_image_content(compacted[0].content)

    old_tool_text = _content_text(compacted[2].content)
    assert "old-tool-rendered-html" not in old_tool_text
    assert "Previous Journey observation omitted" in old_tool_text
    assert not _has_image_content(compacted[2].content)

    latest_text = _content_text(compacted[4].content)
    assert "latest-rendered-html" in latest_text
    assert _has_image_content(compacted[4].content)

    assert compacted[1].tool_calls[0]["id"] == "call-1"
    assert compacted[2].tool_call_id == "call-1"
    assert compacted[3].tool_calls[0]["id"] == "call-2"
    assert compacted[4].tool_call_id == "call-2"


def test_prompt_engine_observation_compaction_preserves_dict_message_pairing() -> None:
    messages: list[object] = [
        {
            "role": "user",
            "content": _prompt_observation_content("initial"),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "name": "fake_echo",
                    "args": {"text": "hello"},
                    "id": "call-1",
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": _prompt_observation_content("latest"),
        },
    ]

    compacted = _compact_prompt_observation_messages(messages)

    assert compacted[1] == messages[1]
    assert isinstance(compacted[0], dict)
    assert compacted[0]["role"] == "user"
    assert not _has_image_content(compacted[0]["content"])
    assert "initial-rendered-html" not in _content_text(compacted[0]["content"])
    assert isinstance(compacted[2], dict)
    assert compacted[2]["tool_call_id"] == "call-1"
    assert "latest-rendered-html" in _content_text(compacted[2]["content"])
    assert _has_image_content(compacted[2]["content"])


def test_prompt_engine_observation_compaction_keeps_current_screenshot_tool_result() -> None:
    messages: list[object] = [
        HumanMessage(content=_prompt_observation_content("initial")),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "journey_screenshot",
                    "args": {},
                    "id": "screenshot-1",
                }
            ],
        ),
        ToolMessage(
            content=_prompt_screenshot_content("current"),
            tool_call_id="screenshot-1",
        ),
    ]

    compacted = _compact_prompt_observation_messages(messages)

    assert compacted is messages
    assert _has_image_content(messages[2].content)
    assert "Screenshot captured for current." in _content_text(messages[2].content)


def test_prompt_engine_observation_compaction_strips_stale_screenshot_tool_result() -> None:
    messages: list[object] = [
        HumanMessage(content=_prompt_observation_content("initial")),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "journey_screenshot",
                    "args": {},
                    "id": "screenshot-1",
                }
            ],
        ),
        ToolMessage(
            content=_prompt_screenshot_content("old"),
            tool_call_id="screenshot-1",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fake_echo",
                    "args": {"text": "continue"},
                    "id": "call-2",
                }
            ],
        ),
        ToolMessage(
            content=_prompt_observation_content("latest"),
            tool_call_id="call-2",
        ),
    ]

    compacted = _compact_prompt_observation_messages(messages)

    assert compacted[1].tool_calls[0]["id"] == "screenshot-1"
    assert compacted[2].tool_call_id == "screenshot-1"
    assert not _has_image_content(compacted[2].content)
    assert "Previous Journey screenshot omitted" in _content_text(compacted[2].content)
    assert "old" not in _content_text(compacted[2].content)
    assert "latest-rendered-html" in _content_text(compacted[4].content)
    assert _has_image_content(compacted[4].content)


def test_prompt_engine_runs_action_adapter_and_persists_action_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel(
        [_action_call("fake_echo", {"text": "hello"}), "done"],
        structured_responses=[
            _finalization("done"),
        ],
    )
    action_records: list[JourneyLogRecord] = []

    def build_observation() -> PromptObservation:
        return PromptObservation(
            signature="fake://ready",
            records=(
                make_log_record(
                    "fake-action",
                    "state",
                    "fake action ready",
                    status="ready",
                ),
            ),
            sections=(
                PromptTextSection(
                    heading="Fake visible text",
                    tag="fake-visible-text",
                    text="Ready",
                ),
            ),
        )

    def build_actions(context: PromptActionContext) -> list[object]:
        @tool("fake_echo")
        def fake_echo(text: str) -> list[dict[str, object]]:
            """Echo text into the fake action."""

            def run() -> list[dict[str, object]]:
                step = context.next_step_index()
                record = make_log_record(
                    "fake-action",
                    "action",
                    f"echoed {text}",
                    step=step,
                    status="ok",
                    target=text,
                )
                action_records.append(record)
                context.record_action(record)
                return context.observation_or_stop(step_index=step)

            return context.run_on_prompt_thread(run)

        return [fake_echo]

    result = PromptEngineSession(
        component="fake-action",
        owner="fake.prompt(...)",
        instruction="echo hello",
        model="fake:model",
        max_steps=3,
        memory_path=tmp_path / "fake.memory.md",
        output_schema=None,
        system_prompt="Use fake_echo when more work is needed.",
        logger=get_logger("fake-prompt"),
        build_observation=build_observation,
        build_actions=build_actions,
        load_model=lambda model_name: model,
        create_agent=_fake_create_agent,
        compile_memory=lambda context: PromptMemoryDraft(
            sections=(
                PromptMemorySection(
                    heading="Fake replay",
                    body='fake_echo("hello")',
                    language="python",
                ),
                PromptMemorySection(
                    heading="Fake notes",
                    body=f"Compiled from {len(context.log_records)} log record.",
                ),
            ),
        ),
    ).run()

    assert result == "done"
    first_prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert "Observation records JSON:" in first_prompt_text
    assert "Known pages JSON:" not in first_prompt_text
    assert "Executed steps JSON:" not in second_prompt_text
    assert '"event": "action"' in second_prompt_text
    assert "fake-visible-text" in first_prompt_text
    assert len(model.structured_calls) == 1
    assert model.structured_calls[0]["schema"]["title"] == "journey_prompt_finalization"

    entry = load_prompt_memory_entry(
        tmp_path / "fake.memory.md",
        component="fake-action",
        instruction="echo hello",
        observation_signature="fake://ready",
    )
    assert entry is not None
    assert entry.component == "fake-action"
    assert entry.observation_signature == "fake://ready"
    assert entry.final_output == "done"
    assert entry.sections == (
        PromptMemorySection(
            heading="Fake replay",
            body='fake_echo("hello")',
            language="python",
        ),
        PromptMemorySection(
            heading="Fake notes",
            body="Compiled from 1 log record.",
        ),
    )
    log_output = capsys.readouterr().out
    expected = (
        f"          prompt memory             wrote to {tmp_path / 'fake.memory.md'}"
    )
    assert expected in log_output


def test_prompt_engine_logs_model_usage_breakdown_in_pretty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel(
        [_action_call("fake_echo", {"text": "hello"}), "done"],
        structured_responses=[_finalization("done")],
        usage_metadata=[
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_cost_usd": 0.0001,
                "output_cost_usd": 0.00004,
            },
            {
                "input_tokens": 80,
                "output_tokens": 10,
                "total_tokens": 90,
                "total_cost_usd": 0.00008,
            },
        ],
        structured_usage_metadata=[
            {
                "input_tokens": 50,
                "output_tokens": 5,
                "total_tokens": 55,
                "input_cost_usd": 0.00005,
                "output_cost_usd": 0.00002,
            }
        ],
        model_name="fake-model-v1",
    )

    def build_observation() -> PromptObservation:
        return PromptObservation(signature="fake://ready", records=())

    def build_actions(context: PromptActionContext) -> list[object]:
        @tool("fake_echo")
        def fake_echo(text: str) -> list[dict[str, object]]:
            """Echo text into the fake action."""

            def run() -> list[dict[str, object]]:
                step = context.next_step_index()
                context.record_action(
                    make_log_record(
                        "fake-action",
                        "action",
                        f"echoed {text}",
                        step=step,
                        status="ok",
                    )
                )
                return context.observation_or_stop(step_index=step)

            return context.run_on_prompt_thread(run)

        return [fake_echo]

    result = PromptEngineSession(
        component="fake-action",
        owner="fake.prompt(...)",
        instruction="echo hello",
        model="fake:model",
        max_steps=3,
        memory_path=None,
        output_schema=None,
        system_prompt="Use fake_echo when more work is needed.",
        logger=get_logger("fake-prompt"),
        build_observation=build_observation,
        build_actions=build_actions,
        load_model=lambda model_name: model,
        create_agent=_fake_create_agent,
    ).run()

    assert result == "done"
    log_output = capsys.readouterr().out
    assert log_output.count("model usage") == 3
    assert (
        "action_loop model=fake-model-v1 "
        "tokens=input:100 output:20 total:120"
    ) in log_output
    assert (
        "finalization model=fake-model-v1 "
        "tokens=input:50 output:5 total:55"
    ) in log_output
    assert "tokens=input:230 output:35 total:265" in log_output
    assert "cost=" not in log_output


def test_prompt_engine_streams_callback_usage_before_tool_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel(
        [_action_call("fake_echo", {"text": "hello"}), "done"],
        structured_responses=[_finalization("done")],
        usage_metadata=[
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
        ],
        structured_usage_metadata=[
            {"input_tokens": 6, "output_tokens": 1, "total_tokens": 7}
        ],
        model_name="fake-model-v1",
        emit_callbacks=True,
    )
    logger = get_logger("fake-prompt")

    def build_actions(context: PromptActionContext) -> list[object]:
        @tool("fake_echo")
        def fake_echo(text: str) -> list[dict[str, object]]:
            """Echo text into the fake action."""

            def run() -> list[dict[str, object]]:
                logger.info(
                    "fake_action",
                    f"fake action received {text}",
                    pretty=pretty_row(
                        "fake action",
                        f"received {text}",
                        indent=10,
                        label_width=25,
                    ),
                )
                step = context.next_step_index()
                context.record_action(
                    make_log_record(
                        "fake-action",
                        "action",
                        f"echoed {text}",
                        step=step,
                        status="ok",
                    )
                )
                return context.observation_or_stop(step_index=step)

            return context.run_on_prompt_thread(run)

        return [fake_echo]

    result = PromptEngineSession(
        component="fake-action",
        owner="fake.prompt(...)",
        instruction="echo hello",
        model="fake:model",
        max_steps=3,
        memory_path=None,
        output_schema=None,
        system_prompt="Use fake_echo when more work is needed.",
        logger=logger,
        build_observation=lambda: PromptObservation(
            signature="fake://ready",
            records=(),
        ),
        build_actions=build_actions,
        load_model=lambda model_name: model,
        create_agent=_fake_create_agent,
    ).run()

    assert result == "done"
    log_output = capsys.readouterr().out
    first_usage_index = log_output.index(
        "action_loop model=fake-model-v1 tokens=input:10 output:2 total:12"
    )
    action_index = log_output.index("fake action")
    assert first_usage_index < action_index
    assert log_output.count("action_loop model=fake-model-v1") == 2
    assert "tokens=input:24 output:5 total:29" in log_output
    assert "cost=" not in log_output


def test_prompt_engine_logs_model_usage_in_jsonl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel(
        ["done"],
        structured_responses=[_finalization("done")],
        usage_metadata=[
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "total_cost_usd": 0.00012,
            }
        ],
        structured_usage_metadata=[
            {
                "input_tokens": 13,
                "output_tokens": 5,
                "total_tokens": 18,
                "total_cost_usd": 0.00008,
            }
        ],
        model_name="fake-model-v1",
        emit_callbacks=True,
    )

    configure_logging("info", output_format="jsonl")
    try:
        result = PromptEngineSession(
            component="fake-action",
            owner="fake.prompt(...)",
            instruction="finish",
            model="fake:model",
            max_steps=3,
            memory_path=None,
            output_schema=None,
            system_prompt="Return done.",
            logger=get_logger("fake-prompt"),
            build_observation=lambda: PromptObservation(
                signature="fake://ready",
                records=(),
            ),
            build_actions=lambda context: [],
            load_model=lambda model_name: model,
            create_agent=_fake_create_agent,
        ).run()
    finally:
        configure_logging("info", output_format="pretty")

    assert result == "done"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    usage_records = [
        record for record in records if record["event"] == "prompt_model_usage"
    ]
    assert [record["operation"] for record in usage_records] == [
        "action_loop",
        "finalization",
    ]
    interesting_events = [
        record["event"]
        for record in records
        if record["event"] in {"prompt_model_usage", "prompt_finish"}
    ]
    assert interesting_events == [
        "prompt_model_usage",
        "prompt_model_usage",
        "prompt_finish",
    ]
    assert usage_records[0]["model"] == "fake-model-v1"
    assert usage_records[0]["input_tokens"] == 11
    assert usage_records[0]["output_tokens"] == 7
    assert usage_records[0]["total_tokens"] == 18
    assert "total_cost_usd" not in usage_records[0]
    assert "cost_status" not in usage_records[0]
    finish = next(record for record in records if record["event"] == "prompt_finish")
    assert finish["input_tokens"] == 24
    assert finish["output_tokens"] == 12
    assert finish["total_tokens"] == 36
    assert "total_cost_usd" not in finish
    assert "cost_status" not in finish
    assert finish["model_calls"] == 2


def test_prompt_engine_logs_token_usage_without_cost_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel(
        ["done"],
        structured_responses=[_finalization("done")],
        usage_metadata=[
            {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12}
        ],
        structured_usage_metadata=[
            {"input_tokens": 6, "output_tokens": 2, "total_tokens": 8}
        ],
    )

    configure_logging("info", output_format="jsonl")
    try:
        PromptEngineSession(
            component="fake-action",
            owner="fake.prompt(...)",
            instruction="finish",
            model="fake:model",
            max_steps=3,
            memory_path=None,
            output_schema=None,
            system_prompt="Return done.",
            logger=get_logger("fake-prompt"),
            build_observation=lambda: PromptObservation(
                signature="fake://ready",
                records=(),
            ),
            build_actions=lambda context: [],
            load_model=lambda model_name: model,
            create_agent=_fake_create_agent,
        ).run()
    finally:
        configure_logging("info", output_format="pretty")

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    usage_records = [
        record for record in records if record["event"] == "prompt_model_usage"
    ]
    assert all("cost_status" not in record for record in usage_records)
    assert all("total_cost_usd" not in record for record in usage_records)
    finish = next(record for record in records if record["event"] == "prompt_finish")
    assert finish["input_tokens"] == 15
    assert finish["output_tokens"] == 5
    assert finish["total_tokens"] == 20
    assert "total_cost_usd" not in finish
    assert "cost_status" not in finish


def test_prompt_engine_includes_memory_compile_usage_in_finish(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel(
        ["done"],
        structured_responses=[_finalization("done")],
        usage_metadata=[
            {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}
        ],
        structured_usage_metadata=[
            {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
        ],
        model_name="fake-model-v1",
    )

    def compile_memory(context: PromptMemoryCompileContext) -> PromptMemoryDraft:
        context.invoke_model(
            "memory_compile",
            lambda config: _FakeAIMessage(
                content="compiled",
                usage_metadata={
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                },
                response_metadata={"model_name": "fake-model-v1"},
            ),
        )
        return PromptMemoryDraft(
            sections=(
                PromptMemorySection(
                    heading="Fake replay",
                    body='fake_echo("hello")',
                    language="python",
                ),
            )
        )

    PromptEngineSession(
        component="fake-action",
        owner="fake.prompt(...)",
        instruction="finish",
        model="fake:model",
        max_steps=3,
        memory_path=tmp_path / "fake.memory.md",
        output_schema=None,
        system_prompt="Return done.",
        logger=get_logger("fake-prompt"),
        build_observation=lambda: PromptObservation(
            signature="fake://ready",
            records=(),
        ),
        build_actions=lambda context: [],
        load_model=lambda model_name: model,
        create_agent=_fake_create_agent,
        compile_memory=compile_memory,
    ).run()

    log_output = capsys.readouterr().out
    assert "memory_compile model=fake-model-v1" in log_output
    assert "tokens=input:20 output:9 total:29" in log_output
    assert "cost=" not in log_output


def test_prompt_engine_propagates_keyboard_interrupt_from_prompt_thread_action(
    tmp_path: Path,
) -> None:
    model = _FakePromptModel([_action_call("fake_interrupt", {})])

    def build_observation() -> PromptObservation:
        return PromptObservation(
            signature="fake://ready",
            records=(),
            sections=(
                PromptTextSection(
                    heading="Fake visible text",
                    tag="fake-visible-text",
                    text="Ready",
                ),
            ),
        )

    def build_actions(context: PromptActionContext) -> list[object]:
        @tool("fake_interrupt")
        def fake_interrupt() -> list[dict[str, object]]:
            """Raise KeyboardInterrupt on the prompt thread."""

            def run() -> list[dict[str, object]]:
                raise KeyboardInterrupt()

            return context.run_on_prompt_thread(run)

        return [fake_interrupt]

    with pytest.raises(KeyboardInterrupt):
        PromptEngineSession(
            component="fake-action",
            owner="fake.prompt(...)",
            instruction="interrupt",
            model="fake:model",
            max_steps=3,
            memory_path=tmp_path / "fake.memory.md",
            output_schema=None,
            system_prompt="Use fake_interrupt when more work is needed.",
            logger=get_logger("fake-prompt"),
            build_observation=build_observation,
            build_actions=build_actions,
            load_model=lambda model_name: model,
            create_agent=_fake_create_agent,
        ).run()

    assert len(model.calls) == 1


def test_prompt_engine_logs_loaded_prompt_memory_as_nested_pretty_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    memory_path = tmp_path / "long-memory-name-for-sign-in.memory.md"
    write_prompt_memory_entry(
        memory_path,
        PromptMemoryEntry(
            component="fake-action",
            instruction="echo hello",
            observation_signature="fake://ready",
            sections=(
                PromptMemorySection(
                    heading="Fake replay",
                    body='fake_echo("hello")',
                    language="python",
                ),
            ),
            final_output="done from memory",
        ),
    )

    def build_observation() -> PromptObservation:
        return PromptObservation(
            signature="fake://ready",
            records=(),
            sections=(
                PromptTextSection(
                    heading="Fake visible text",
                    text="Ready",
                ),
            ),
        )

    result = PromptEngineSession(
        component="fake-action",
        owner="fake.prompt(...)",
        instruction="echo hello",
        model="fake:model",
        max_steps=3,
        memory_path=memory_path,
        output_schema=None,
        system_prompt="Use fake_echo when more work is needed.",
        logger=get_logger("fake-prompt"),
        build_observation=build_observation,
        build_actions=lambda context: [],
        load_model=lambda model_name: object(),
        create_agent=_fake_create_agent,
        replay_memory=lambda entry: PromptMemoryReplayResult(
            final_output=entry.final_output
        ),
    ).run()

    assert result == "done from memory"
    log_output = capsys.readouterr().out
    assert f"          prompt memory             loaded from {memory_path}" in log_output
    assert "loaded prompt memory from" not in log_output


def test_prompt_engine_model_call_auth_failure_has_actionable_hint() -> None:
    def build_observation() -> PromptObservation:
        return PromptObservation(
            signature="fake://ready",
            records=(),
            sections=(
                PromptTextSection(
                    heading="Fake visible text",
                    text="Ready",
                ),
            ),
        )

    with pytest.raises(RuntimeError, match="failed to call model") as exc_info:
        PromptEngineSession(
            component="fake-action",
            owner="fake.prompt(...)",
            instruction="echo hello",
            model="anthropic:claude-haiku-4-5",
            max_steps=3,
            memory_path=None,
            output_schema=None,
            system_prompt="Use fake_echo when more work is needed.",
            logger=get_logger("fake-prompt"),
            build_observation=build_observation,
            build_actions=lambda context: [],
            load_model=lambda model_name: object(),
            create_agent=_failing_create_agent,
        ).run()

    hint = getattr(exc_info.value, "hint", "")
    assert "ANTHROPIC_API_KEY" in hint
    assert "JOURNEY_BROWSER_PROMPT_MODEL=anthropic:claude-haiku-4-5" in hint


def test_prompt_memory_round_trips_markdown_entry(tmp_path: Path) -> None:
    memory_path = tmp_path / "fake.memory.md"
    entry = PromptMemoryEntry(
        component="fake-action",
        instruction="remember this",
        observation_signature="fake://ready",
        sections=(
            PromptMemorySection(
                heading="Recipe",
                body="Reuse the cached fake-action handle.",
            ),
            PromptMemorySection(
                heading="Fixture data",
                body='{"selector": "#cached"}',
                language="json",
            ),
        ),
        final_output={"status": "done"},
    )

    run_count = write_prompt_memory_entry(memory_path, entry)

    assert run_count == 1
    assert memory_path.read_text(encoding="utf-8").startswith("# Journey Prompt Memory\n")
    loaded = load_prompt_memory_entry(
        memory_path,
        component="fake-action",
        instruction="remember this",
        observation_signature="fake://ready",
    )
    assert loaded == PromptMemoryEntry(
        component="fake-action",
        instruction="remember this",
        observation_signature="fake://ready",
        sections=(
            PromptMemorySection(
                heading="Recipe",
                body="Reuse the cached fake-action handle.",
            ),
            PromptMemorySection(
                heading="Fixture data",
                body='{"selector": "#cached"}',
                language="json",
            ),
        ),
        final_output={"status": "done"},
        run_count=1,
        updated_at=loaded.updated_at if loaded is not None else "",
    )


def test_prompt_memory_ignores_mismatched_instruction_or_observation(
    tmp_path: Path,
) -> None:
    memory_path = tmp_path / "sign-in.memory.md"
    write_prompt_memory_entry(
        memory_path,
        PromptMemoryEntry(
            component="browser",
            instruction="sign in",
            observation_signature="login-page",
            sections=(
                PromptMemorySection(
                    heading="Action state",
                    body="This section belongs to the action.",
                ),
            ),
            final_output="Signed in.",
        ),
    )

    assert (
        load_prompt_memory_entry(
            memory_path,
            component="browser",
            instruction="sign out",
            observation_signature="login-page",
        )
        is None
    )
    assert (
        load_prompt_memory_entry(
            memory_path,
            component="browser",
            instruction="sign in",
            observation_signature="dashboard-page",
        )
        is None
    )


def test_prompt_memory_allows_entries_without_browser_sections(
    tmp_path: Path,
) -> None:
    memory_path = tmp_path / "generic.memory.md"
    write_prompt_memory_entry(
        memory_path,
        PromptMemoryEntry(
            component="fake-action",
            instruction="cache generic state",
            observation_signature="fake://ready",
            sections=(
                PromptMemorySection(
                    heading="Action-specific state",
                    body="not executable code",
                ),
            ),
            final_output="Cached.",
        ),
    )

    loaded = load_prompt_memory_entry(
        memory_path,
        component="fake-action",
        instruction="cache generic state",
        observation_signature="fake://ready",
    )

    assert loaded is not None
    assert loaded.sections == (
        PromptMemorySection(
            heading="Action-specific state",
            body="not executable code",
        ),
    )


def test_prompt_memory_write_cleans_tmp_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    memory_path = tmp_path / "sign-in.memory.md"
    temp_paths: list[Path] = []

    def fail_replace(source: object, target: object) -> None:
        temp_paths.append(Path(source))
        raise OSError("replace failed")

    monkeypatch.setattr("journeysdk._prompt_memory.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_prompt_memory_entry(
            memory_path,
            PromptMemoryEntry(
                component="browser",
                instruction="sign in",
                observation_signature="login-page",
                sections=(
                    PromptMemorySection(
                        heading="Action-specific state",
                        body="replay details are not shared-memory concepts",
                    ),
                ),
                final_output="Signed in.",
            ),
        )

    assert temp_paths
    assert all(not path.exists() for path in temp_paths)
