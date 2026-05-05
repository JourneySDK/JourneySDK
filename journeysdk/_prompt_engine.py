"""Generic prompt engine helpers for AI-driven Journey tools."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread, get_ident
from typing import TypeVar, cast

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from journeysdk._prompt_memory import (
    load_prompt_memory_entry,
    prompt_memory_entry_from_result,
    prompt_memory_key,
    prompt_memory_updates_disabled,
    truncate_prompt_memory_text,
    write_prompt_memory_entry,
)
from journeysdk._prompt_output import (
    PromptOutputSchema,
    parse_prompt_structured_output,
    validate_prompt_structured_output,
)
from journeysdk.logger import JourneyLogRecord, JourneyLogger, pretty_row

_T = TypeVar("_T")


@dataclass(frozen=True)
class PromptTextSection:
    heading: str
    text: str
    tag: str | None = None


@dataclass(frozen=True)
class PromptImage:
    data_url: str


@dataclass(frozen=True)
class PromptObservation:
    signature: str
    records: tuple[JourneyLogRecord, ...]
    sections: tuple[PromptTextSection, ...] = ()
    images: tuple[PromptImage, ...] = ()
    visible_text: str = ""


@dataclass
class _PromptThreadCall:
    callback: Callable[[], object]
    done: Event
    result: object | None = None
    error: BaseException | None = None


class PromptControlError(RuntimeError):
    """Internal prompt control-flow error that should reach callers as-is."""


class PromptFailedError(PromptControlError):
    """The prompt reached a visible terminal failure."""


class PromptMaxStepsError(PromptControlError):
    """The prompt exhausted the configured step budget."""


class PromptToolContext:
    def __init__(self, session: PromptEngineSession) -> None:
        self._session = session

    def next_step_index(self) -> int:
        return self._session.next_step_index()

    def record_action(self, record: JourneyLogRecord) -> None:
        self._session.record_action(record)

    def record_memory_log(self, record: JourneyLogRecord) -> None:
        self._session.record_memory_log(record)

    def observation_or_stop(self, *, step_index: int) -> list[dict[str, object]]:
        return self._session.tool_observation_or_stop(step_index=step_index)

    def fail_session(self, *, step_index: int, reason: str) -> None:
        self._session.raise_prompt_failed(step_index=step_index, reason=reason)

    def run_on_prompt_thread(self, callback: Callable[[], _T]) -> _T:
        return self._session.run_on_prompt_thread(callback)


class PromptEngineSession:
    def __init__(
        self,
        *,
        component: str,
        owner: str,
        instruction: str,
        model: str,
        max_steps: int,
        memory_path: Path | None,
        output_schema: PromptOutputSchema | None,
        system_prompt: str,
        logger: JourneyLogger,
        build_observation: Callable[[], PromptObservation],
        build_tools: Callable[[PromptToolContext], list[object]],
        load_model: Callable[[str], object] | None = None,
        create_agent: Callable[..., object] | None = None,
        before_final_observation: Callable[[], None] | None = None,
    ) -> None:
        self._component = component
        self._owner = owner
        self._instruction = instruction
        self._model = model
        self._max_steps = max_steps
        self._memory_path = memory_path
        self._output_schema = output_schema
        self._system_prompt = system_prompt
        self._logger = logger
        self._build_observation = build_observation
        self._build_tools = build_tools
        self._load_model = load_model or _load_langchain_model
        self._create_agent = create_agent or _create_langchain_agent
        self._before_final_observation = before_final_observation
        self._memory_key: str | None = None
        self._memory_observation_signature: str | None = None
        self._memory_entry: dict[str, object] | None = None
        self._memory_loaded = False
        self._prompt_model = self._load_model(model)
        self._action_records: list[JourneyLogRecord] = []
        self._memory_log_records: list[JourneyLogRecord] = []
        self._prompt_thread_id = get_ident()
        self._prompt_thread_calls: Queue[_PromptThreadCall] = Queue()

    def run(self) -> str | dict[str, object]:
        observation = self._build_observation()
        agent = self._create_agent(
            self._prompt_model,
            tools=self._build_tools(PromptToolContext(self)),
            system_prompt=self._system_prompt,
        )
        result = self._request_agent_result(
            agent=agent,
            messages=[
                self._build_observation_message(
                    observation=observation,
                    include_memory=True,
                )
            ],
        )
        return self._finish_from_response(_final_agent_message(result))

    def next_step_index(self) -> int:
        return len(self._action_records) + 1

    def record_action(self, record: JourneyLogRecord) -> None:
        self._action_records.append(record)

    def record_memory_log(self, record: JourneyLogRecord) -> None:
        self._memory_log_records.append(record)

    def tool_observation_or_stop(
        self,
        *,
        step_index: int,
    ) -> list[dict[str, object]]:
        observation = self._build_observation()
        if step_index >= self._max_steps:
            self._raise_max_steps()
        return self._build_observation_content(
            observation=observation,
            include_memory=False,
        )

    def raise_prompt_failed(self, *, step_index: int, reason: str) -> None:
        normalized_reason = reason.strip() or "The requested task could not be completed."
        message = f"{self._owner} could not complete instruction: {normalized_reason}"
        self._logger.warning(
            "prompt_failed",
            f"step {step_index}/{self._max_steps}: prompt failed: {normalized_reason}",
            pretty=pretty_row(
                "AI prompt",
                f"failed at {step_index}/{self._max_steps}: {normalized_reason}",
                indent=8,
                label_width=27,
                style="warning",
            ),
            step=step_index,
            max_steps=self._max_steps,
            reason=normalized_reason,
        )
        raise PromptFailedError(message)

    def run_on_prompt_thread(self, callback: Callable[[], _T]) -> _T:
        if get_ident() == self._prompt_thread_id:
            return callback()
        call = _PromptThreadCall(callback=callback, done=Event())
        self._prompt_thread_calls.put(call)
        call.done.wait()
        if call.error is not None:
            raise call.error
        return cast(_T, call.result)

    def _build_observation_message(
        self,
        *,
        observation: PromptObservation,
        include_memory: bool,
    ) -> dict[str, object]:
        return {
            "role": "user",
            "content": self._build_observation_content(
                observation=observation,
                include_memory=include_memory,
            ),
        }

    def _build_observation_content(
        self,
        *,
        observation: PromptObservation,
        include_memory: bool,
    ) -> list[dict[str, object]]:
        memory_entry = (
            self._memory_for_observation(observation) if include_memory else None
        )
        memory_section: list[str] = []
        if memory_entry is not None:
            memory_section = [
                "",
                "Prompt memory JSON:",
                json.dumps(memory_entry, sort_keys=True, indent=2),
            ]
        if self._output_schema is None:
            output_section = [
                "",
                "When the task is complete, return the final answer as plain text.",
            ]
        else:
            output_section = [
                "",
                "When the task is complete, return the final answer using these output fields JSON:",
                self._output_schema.prompt_text,
            ]
        prompt_text = "\n".join(
            [
                f"Instruction:\n{self._instruction}",
                *memory_section,
                "",
                "Observation records JSON:",
                json.dumps(
                    self._observation_record_dicts(observation),
                    sort_keys=True,
                    indent=2,
                ),
                *self._render_text_sections(observation.sections),
                *output_section,
                "",
                "Call a tool to continue work, or return the final answer directly when complete.",
            ]
        )
        content = [
            {
                "type": "text",
                "text": prompt_text,
            }
        ]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url},
            }
            for image in observation.images
        )
        return content

    def _render_text_sections(
        self,
        sections: tuple[PromptTextSection, ...],
    ) -> list[str]:
        rendered: list[str] = []
        for section in sections:
            rendered.extend(["", section.heading + ":"])
            if section.tag is None:
                rendered.append(section.text)
            else:
                rendered.extend(
                    [
                        f"<journey-{section.tag}>",
                        section.text,
                        f"</journey-{section.tag}>",
                    ]
                )
        return rendered

    def _observation_record_dicts(
        self,
        observation: PromptObservation,
    ) -> list[dict[str, object]]:
        return [
            record.to_dict()
            for record in (*observation.records, *tuple(self._action_records))
        ]

    def _request_agent_result(
        self,
        *,
        agent: object,
        messages: list[object],
    ) -> object:
        try:
            response = self._invoke_agent_on_worker(
                lambda: agent.invoke(
                    {"messages": messages},
                    config={"recursion_limit": max(6, self._max_steps * 3 + 3)},
                )
            )
        except PromptControlError:
            raise
        except Exception as exc:
            if _is_langchain_recursion_limit_error(exc):
                self._raise_max_steps()
            raise RuntimeError(
                f"{self._owner} failed to call model {self._model!r}: {exc}"
            ) from exc
        return response

    def _invoke_agent_on_worker(self, callback: Callable[[], _T]) -> _T:
        if get_ident() != self._prompt_thread_id:
            return callback()

        result: object | None = None
        error: BaseException | None = None
        done = Event()

        def worker() -> None:
            nonlocal result, error
            try:
                result = callback()
            except BaseException as exc:
                error = exc
            finally:
                done.set()

        thread = Thread(
            target=worker,
            name="journey-prompt-agent",
            daemon=True,
        )
        thread.start()
        while not done.is_set():
            self._run_next_prompt_thread_call(timeout=0.05)
        thread.join()
        if error is not None:
            raise error
        return cast(_T, result)

    def _run_next_prompt_thread_call(self, *, timeout: float) -> None:
        try:
            call = self._prompt_thread_calls.get(timeout=timeout)
        except Empty:
            return
        try:
            call.result = call.callback()
        except BaseException as exc:
            call.error = exc
        finally:
            call.done.set()

    def _raise_max_steps(self) -> None:
        message = (
            f"{self._owner} reached max_steps={self._max_steps} "
            "without a final response."
        )
        if self._action_records:
            last_record = self._action_records[-1].to_dict()
            message += (
                f" Last action was {last_record.get('status')}: "
                f"{last_record.get('action_type', last_record.get('event'))} "
                f"{last_record.get('target', '')!r} ({last_record.get('detail', '')})."
            )
        self._logger.warning(
            "prompt_stopped",
            f"prompt stopped: {message}",
            pretty=pretty_row(
                "AI prompt",
                message,
                indent=8,
                label_width=27,
                style="warning",
            ),
            max_steps=self._max_steps,
        )
        raise PromptMaxStepsError(message)

    def _finish_from_response(self, response: object) -> str | dict[str, object]:
        response_text = _extract_langchain_text(response, owner=self._owner).strip()
        if self._before_final_observation is not None:
            self._before_final_observation()
        final_observation = self._build_observation()
        final_output = self._parse_final_output(
            response_text,
            observation=final_observation,
        )
        step_index = len(self._action_records) + 1
        self._logger.info(
            "prompt_finish",
            f"step {step_index}/{self._max_steps}: finished with output: "
            f"{_prompt_output_summary(final_output)}",
            pretty=pretty_row(
                f"{step_index}/{self._max_steps} finish",
                _prompt_output_summary(final_output),
                indent=10,
                label_width=25,
                style="success",
            ),
            step=step_index,
            max_steps=self._max_steps,
            output=_prompt_output_summary(final_output),
        )
        self._write_memory(final_output=final_output)
        return final_output

    def _parse_final_output(
        self,
        response_text: str,
        *,
        observation: PromptObservation,
    ) -> str | dict[str, object]:
        if self._output_schema is None:
            return response_text
        structured_response = self._request_structured_output(
            completion_text=response_text,
            observation=observation,
        )
        structured_output = _normalize_structured_output(
            structured_response,
            schema=self._output_schema,
            owner=self._owner,
        )
        return _fill_visible_message_fields(
            structured_output,
            schema=self._output_schema,
            visible_text=observation.visible_text,
        )

    def _request_structured_output(
        self,
        *,
        completion_text: str,
        observation: PromptObservation,
    ) -> object:
        if self._output_schema is None:
            raise RuntimeError(f"{self._owner} structured output was not configured.")
        try:
            structured_model = self._prompt_model.with_structured_output(
                self._output_schema.json_schema,
                method="json_schema",
            )
            return structured_model.invoke(
                self._build_structured_output_messages(
                    completion_text=completion_text,
                    observation=observation,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"{self._owner} failed to call structured output model "
                f"{self._model!r}: {exc}"
            ) from exc

    def _build_structured_output_messages(
        self,
        *,
        completion_text: str,
        observation: PromptObservation,
    ) -> list[dict[str, object]]:
        completion_section: list[str] = []
        if completion_text:
            completion_section = [
                "",
                "Completion signal from the action loop:",
                completion_text,
            ]
        prompt_text = "\n".join(
            [
                f"Instruction:\n{self._instruction}",
                *completion_section,
                "",
                "Observation records JSON:",
                json.dumps(
                    self._observation_record_dicts(observation),
                    sort_keys=True,
                    indent=2,
                ),
                *self._render_text_sections(observation.sections),
                "",
                "Return values for these output fields:",
                self._output_schema.prompt_text,
            ]
        )
        content = [
            {
                "type": "text",
                "text": prompt_text,
            }
        ]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url},
            }
            for image in observation.images
        )
        return [
            {
                "role": "system",
                "content": (
                    "Return structured output for a completed Journey prompt task.\n\n"
                    "Use the current visible state as the source of truth. If a requested output field "
                    "asks for an error, validation message, warning, status, or problem, copy the visible "
                    "message exactly when present.\n\n"
                    "Do not mention implementation details, hidden reasoning, or unavailable metadata."
                ),
            },
            {
                "role": "user",
                "content": content,
            },
        ]

    def _memory_for_observation(
        self,
        observation: PromptObservation,
    ) -> dict[str, object] | None:
        if self._memory_path is None:
            return None
        if not self._memory_loaded:
            self._memory_loaded = True
            self._memory_observation_signature = observation.signature
            self._memory_key = prompt_memory_key(
                component=self._component,
                instruction=self._instruction,
                observation_signature=observation.signature,
            )
            self._memory_entry = load_prompt_memory_entry(
                self._memory_path,
                self._memory_key,
            )
            if self._memory_entry is not None:
                self._logger.info(
                    "prompt_memory_loaded",
                    f"loaded prompt memory from {self._memory_path}",
                    path=str(self._memory_path),
                    key=self._memory_key,
                )
        return self._memory_entry

    def _write_memory(self, *, final_output: str | dict[str, object]) -> None:
        if (
            self._memory_path is None
            or self._memory_key is None
            or self._memory_observation_signature is None
            or prompt_memory_updates_disabled()
        ):
            return
        memory_records = tuple(self._memory_log_records or self._action_records)
        entry = prompt_memory_entry_from_result(
            component=self._component,
            instruction=self._instruction,
            observation_signature=self._memory_observation_signature,
            final_output=final_output,
            log_records=memory_records,
        )
        run_count = write_prompt_memory_entry(
            self._memory_path,
            self._memory_key,
            entry,
        )
        self._logger.info(
            "prompt_memory_saved",
            f"wrote prompt memory to {self._memory_path}",
            path=str(self._memory_path),
            key=self._memory_key,
            run_count=run_count,
        )


def _prompt_output_summary(value: str | dict[str, object]) -> str:
    if isinstance(value, str):
        return truncate_prompt_memory_text(value)
    return truncate_prompt_memory_text(json.dumps(value, sort_keys=True))


def _final_agent_message(result: object) -> object:
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list) and messages:
            return messages[-1]
    return result


def _is_langchain_recursion_limit_error(exc: Exception) -> bool:
    if type(exc).__name__ == "GraphRecursionError":
        return True
    message = str(exc).lower()
    return "recursion limit" in message and "langgraph" in message


def _normalize_structured_output(
    response: object,
    *,
    schema: PromptOutputSchema,
    owner: str,
) -> dict[str, object]:
    if isinstance(response, dict):
        if "parsed" in response and response.get("parsing_error") is None:
            parsed = response["parsed"]
            if isinstance(parsed, dict):
                return validate_prompt_structured_output(parsed, schema, owner=owner)
        return validate_prompt_structured_output(response, schema, owner=owner)
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return validate_prompt_structured_output(dumped, schema, owner=owner)
    return parse_prompt_structured_output(
        _extract_langchain_text(response, owner=owner),
        schema,
        owner=owner,
    )


_VISIBLE_MESSAGE_FIELD_TERMS = (
    "error",
    "validation",
    "warning",
    "problem",
    "issue",
    "failure",
    "failed",
    "invalid",
    "incorrect",
)
_VISIBLE_MESSAGE_TEXT_TERMS = (
    "cannot",
    "denied",
    "error",
    "expired",
    "failed",
    "failure",
    "incorrect",
    "invalid",
    "not found",
    "required",
    "try again",
    "unable",
    "wrong",
)


def _fill_visible_message_fields(
    output: dict[str, object],
    *,
    schema: PromptOutputSchema,
    visible_text: str,
) -> dict[str, object]:
    fallback_message = _extract_visible_message(visible_text)
    if not fallback_message:
        return output
    repaired = dict(output)
    for field_name in schema.fields:
        if repaired.get(field_name) != "":
            continue
        field_schema = schema.properties.get(field_name)
        description = ""
        if isinstance(field_schema, dict):
            raw_description = field_schema.get("description")
            if isinstance(raw_description, str):
                description = raw_description
        field_hint = f"{field_name} {description}".lower()
        if any(term in field_hint for term in _VISIBLE_MESSAGE_FIELD_TERMS):
            repaired[field_name] = fallback_message
    return repaired


def _extract_visible_message(visible_text: str) -> str:
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    for window_size in (1, 2, 3):
        for index in range(0, max(len(lines) - window_size + 1, 0)):
            candidate = " ".join(lines[index : index + window_size])
            normalized = candidate.lower()
            if any(term in normalized for term in _VISIBLE_MESSAGE_TEXT_TERMS):
                return candidate
    return ""


def _extract_langchain_text(response: object, *, owner: str) -> str:
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        joined = "".join(text_parts).strip()
        if joined:
            return joined
    raise RuntimeError(f"{owner} expected the model response to include text content.")


def _load_langchain_model(model: str) -> object:
    try:
        return init_chat_model(
            model,
            temperature=0.0,
            max_tokens=1000,
        )
    except Exception as exc:
        raise RuntimeError(
            f"prompt failed to initialize LangChain model {model!r}: {exc}"
        ) from exc


def _create_langchain_agent(
    model: object,
    *,
    tools: list[object],
    system_prompt: str,
) -> object:
    return create_agent(
        model,
        tools=tools,
        system_prompt=system_prompt,
    )


def resolve_prompt_model(
    model: str | None,
    *,
    env_var: str,
    owner: str,
) -> str:
    if model is not None and model.strip():
        return model.strip()
    env_model = os.environ.get(env_var, "").strip()
    if env_model:
        return env_model
    raise RuntimeError(
        f"{owner} requires model=... or the {env_var} environment variable."
    )
