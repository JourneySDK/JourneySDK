from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import tool

from journeysdk._prompt_engine import (
    PromptEngineSession,
    PromptObservation,
    PromptTextSection,
    PromptToolContext,
)
from journeysdk.logger import JourneyLogRecord, get_logger, make_log_record


class _FakeAIMessage:
    def __init__(
        self,
        *,
        content: object = "",
        tool_calls: list[dict[str, object]] | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.invalid_tool_calls: list[object] = []


class _FakePromptModel:
    def __init__(self, responses: list[str | dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self._response_index = 0


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
        del config
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
            if isinstance(response, str):
                ai_message = _FakeAIMessage(content=response)
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
                ai_message = _FakeAIMessage(tool_calls=tool_calls)
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


def _fake_create_agent(
    model: object,
    *,
    tools: list[object],
    system_prompt: str,
) -> _FakeAgent:
    assert isinstance(model, _FakePromptModel)
    return _FakeAgent(model, tools=tools, system_prompt=system_prompt)


def _tool_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "content": "",
        "tool_calls": [
            {
                "name": name,
                "args": arguments,
            }
        ],
    }


def test_prompt_engine_runs_tool_agnostic_adapter_and_persists_action_records(
    tmp_path: Path,
) -> None:
    model = _FakePromptModel([_tool_call("fake_echo", {"text": "hello"}), "done"])
    action_records: list[JourneyLogRecord] = []

    def build_observation() -> PromptObservation:
        return PromptObservation(
            signature="fake://ready",
            records=(
                make_log_record(
                    "fake-tool",
                    "state",
                    "fake tool ready",
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

    def build_tools(context: PromptToolContext) -> list[object]:
        @tool("fake_echo")
        def fake_echo(text: str) -> list[dict[str, object]]:
            """Echo text into the fake tool."""

            def run() -> list[dict[str, object]]:
                step = context.next_step_index()
                record = make_log_record(
                    "fake-tool",
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
        component="fake-tool",
        owner="fake.prompt(...)",
        instruction="echo hello",
        model="fake:model",
        max_steps=3,
        memory_path=tmp_path / "fake.memory.json",
        output_schema=None,
        system_prompt="Use fake_echo when more work is needed.",
        logger=get_logger("fake-prompt"),
        build_observation=build_observation,
        build_tools=build_tools,
        load_model=lambda model_name: model,
        create_agent=_fake_create_agent,
    ).run()

    assert result == "done"
    first_prompt_text = model.calls[0]["messages"][1]["content"][0]["text"]
    second_prompt_text = model.calls[1]["messages"][-1]["content"][0]["text"]
    assert "Observation records JSON:" in first_prompt_text
    assert "Known pages JSON:" not in first_prompt_text
    assert "Executed steps JSON:" not in second_prompt_text
    assert '"event": "action"' in second_prompt_text
    assert "fake-visible-text" in first_prompt_text

    memory_payload = json.loads((tmp_path / "fake.memory.json").read_text(encoding="utf-8"))
    entry = next(iter(memory_payload["entries"].values()))
    assert entry["component"] == "fake-tool"
    assert entry["observation_signature"] == "fake://ready"
    assert entry["final_output"] == "done"
    assert "action_records" not in entry
    assert entry["log_records"] == [
        {
            "level": "INFO",
            "component": "fake-tool",
            "event": "action",
            "message": "echoed hello",
            "step": 1,
            "status": "ok",
            "target": "hello",
        }
    ]
