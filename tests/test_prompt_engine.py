from __future__ import annotations

from pathlib import Path

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
    PromptMemoryDraft,
    PromptMemoryReplayResult,
    PromptObservation,
    PromptTextSection,
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


class _FailingAgent:
    def invoke(
        self,
        payload: dict[str, object],
        *,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del payload, config
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
    del model, tools, system_prompt
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


def test_prompt_engine_runs_action_adapter_and_persists_action_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _FakePromptModel([_action_call("fake_echo", {"text": "hello"}), "done"])
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
            model="anthropic:claude-sonnet-4-6",
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
    assert "JOURNEY_BROWSER_PROMPT_MODEL=anthropic:claude-sonnet-4-6" in hint


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
        del target
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
