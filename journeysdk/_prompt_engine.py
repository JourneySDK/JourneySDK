"""Generic prompt engine helpers for AI-driven Journey touchpoints."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread, get_ident
from typing import TypeVar, cast

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler

from journeysdk._prompt_memory import (
    PromptMemoryEntry,
    PromptMemorySection,
    load_prompt_memory_entry,
    prompt_memory_entry_from_result,
    prompt_memory_updates_disabled,
    truncate_prompt_memory_text,
    write_prompt_memory_entry,
)
from journeysdk._prompt_output import (
    PromptOutputSchema,
    parse_prompt_structured_output,
    validate_prompt_structured_output,
)
from journeysdk.logger import (
    JourneyLogRecord,
    JourneyLogger,
    PrettyLine,
    PrettyStyle,
    pretty_row,
)

_T = TypeVar("_T")
_PROMPT_DETAIL_INDENT = 10
_PROMPT_LABEL_WIDTH = 25
PromptModelCall = Callable[[str, Callable[[dict[str, object]], object]], object]
_OBSERVATION_RECORDS_MARKER = "Observation records JSON:"
_COMPACTED_OBSERVATION_TEXT = (
    "Previous Journey observation omitted to reduce prompt history size. "
    "Use the latest observation message for the current page state; prior "
    "action and tool messages remain in this conversation."
)
_SCREENSHOT_TOOL_NAME = "journey_screenshot"
_INSPECT_DOM_TOOL_NAME = "journey_inspect_dom"
_COMPACTED_SCREENSHOT_TEXT = (
    "Previous Journey screenshot omitted to reduce prompt history size. "
    "Use newer observations and action results for the current page state."
)
_COMPACTED_INSPECT_DOM_TEXT = (
    "Previous Journey DOM inspection omitted to reduce prompt history size. "
    "Use newer observations and action results for the current page state."
)


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


@dataclass(frozen=True)
class PromptMemoryReplayResult:
    final_output: str | dict[str, object]


@dataclass(frozen=True)
class PromptMemoryDraft:
    sections: tuple[PromptMemorySection, ...]


@dataclass(frozen=True)
class PromptMemoryCompileContext:
    component: str
    instruction: str
    observation_signature: str
    final_output: str | dict[str, object]
    log_records: tuple[JourneyLogRecord, ...]
    final_observation: PromptObservation
    call_model: PromptModelCall | None = None

    def invoke_model(
        self,
        operation: str,
        callback: Callable[[dict[str, object]], _T],
    ) -> _T:
        if self.call_model is None:
            return callback({})
        return cast(_T, self.call_model(operation, callback))


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


class _PromptObservationCompactionMiddleware(AgentMiddleware):
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(
            request.override(
                messages=_compact_prompt_observation_messages(request.messages),
            )
        )


_PROMPT_PROVIDER_ENV_VARS = {
    "anthropic": ("ANTHROPIC_API_KEY", "Anthropic"),
    "openai": ("OPENAI_API_KEY", "OpenAI"),
}

_AUTH_FAILURE_TERMS = (
    "api_key",
    "auth_token",
    "authentication",
    "authorization",
    "credentials",
    "x-api-key",
)

_INPUT_TOKEN_KEYS = ("input_tokens", "prompt_tokens")
_OUTPUT_TOKEN_KEYS = ("output_tokens", "completion_tokens")
_TOTAL_TOKEN_KEYS = ("total_tokens",)


@dataclass(frozen=True)
class _PromptUsageRecord:
    operation: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class _PromptUsageTotals:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    model_calls: int


@dataclass(frozen=True)
class _PromptPayloadStats:
    text_chars: int
    approximate_tokens: int
    image_count: int
    section_char_counts: tuple[tuple[str, int], ...]


class _PromptUsageCallbackHandler(BaseCallbackHandler):
    def __init__(
        self,
        tracker: _PromptUsageTracker,
        *,
        operation: str,
        model: str,
        logger: JourneyLogger,
    ) -> None:
        super().__init__()
        self._tracker = tracker
        self._operation = operation
        self._model = model
        self._logger = logger

    def on_chat_model_start(
        self,
        serialized: object,
        messages: object,
        **kwargs: object,
    ) -> None:
        for message_batch in _iter_chat_message_batches(messages):
            self._emit_payload_stats(message_batch)

    def on_llm_start(
        self,
        serialized: object,
        prompts: object,
        **kwargs: object,
    ) -> None:
        if not isinstance(prompts, list):
            return
        prompt_messages = [
            {"role": "user", "content": prompt}
            for prompt in prompts
            if isinstance(prompt, str)
        ]
        if prompt_messages:
            self._emit_payload_stats(prompt_messages)

    def on_llm_end(self, response: object, **kwargs: object) -> None:
        self._tracker.collect(
            operation=self._operation,
            result=response,
            configured_model=self._model,
        )
        self._tracker.emit_unemitted(logger=self._logger)

    def _emit_payload_stats(self, messages: list[object]) -> None:
        stats = _prompt_payload_stats(messages)
        sections = dict(stats.section_char_counts)
        self._logger.info(
            "prompt_model_payload",
            (
                f"{self._operation} payload "
                f"text_chars={stats.text_chars} "
                f"approx_tokens={stats.approximate_tokens} "
                f"images={stats.image_count}"
            ),
            pretty=pretty_row(
                "model payload",
                (
                    f"{self._operation} "
                    f"text_chars={stats.text_chars} "
                    f"approx_tokens={stats.approximate_tokens} "
                    f"images={stats.image_count}"
                ),
                indent=_PROMPT_DETAIL_INDENT,
                label_width=_PROMPT_LABEL_WIDTH,
                style="accent",
            ),
            operation=self._operation,
            model=self._model,
            text_chars=stats.text_chars,
            approximate_tokens=stats.approximate_tokens,
            image_count=stats.image_count,
            section_char_counts=sections,
        )


class _PromptUsageTracker:
    def __init__(self) -> None:
        self._records: list[_PromptUsageRecord] = []
        self._emitted_count = 0
        self._lock = Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def call(
        self,
        *,
        operation: str,
        configured_model: str,
        logger: JourneyLogger,
        callback: Callable[[dict[str, object]], _T],
    ) -> _T:
        start_index = self.count
        config = {
            "callbacks": [
                _PromptUsageCallbackHandler(
                    self,
                    operation=operation,
                    model=configured_model,
                    logger=logger,
                )
            ]
        }
        try:
            result = callback(config)
        except BaseException:
            self.emit_unemitted(logger=logger)
            raise
        if self.count == start_index:
            self.collect(
                operation=operation,
                result=result,
                configured_model=configured_model,
            )
        self.emit_unemitted(logger=logger)
        return result

    def collect(
        self,
        *,
        operation: str,
        result: object,
        configured_model: str,
    ) -> None:
        for value in _iter_usage_values(result):
            record = _prompt_usage_record(
                operation=operation,
                configured_model=configured_model,
                value=value,
            )
            if record is not None:
                with self._lock:
                    self._records.append(record)

    def emit_unemitted(self, *, logger: JourneyLogger) -> None:
        while True:
            with self._lock:
                if self._emitted_count >= len(self._records):
                    return
                record = self._records[self._emitted_count]
                self._emitted_count += 1
            logger.info(
                "prompt_model_usage",
                f"model usage: {_format_prompt_usage(record)}",
                pretty=pretty_row(
                    "model usage",
                    _format_prompt_usage(record),
                    indent=_PROMPT_DETAIL_INDENT,
                    label_width=_PROMPT_LABEL_WIDTH,
                    style="accent",
                ),
                operation=record.operation,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                total_tokens=record.total_tokens,
            )

    def totals(self) -> _PromptUsageTotals:
        with self._lock:
            return _aggregate_prompt_usage(list(self._records))


class PromptActionContext:
    def __init__(self, session: PromptEngineSession) -> None:
        self._session = session

    def next_step_index(self) -> int:
        return self._session.next_step_index()

    def record_action(self, record: JourneyLogRecord) -> None:
        self._session.record_action(record)

    def record_memory_log(self, record: JourneyLogRecord) -> None:
        self._session.record_memory_log(record)

    def observation_or_stop(self, *, step_index: int) -> list[dict[str, object]]:
        return self._session.action_observation_or_stop(step_index=step_index)

    def raise_if_step_limit_reached(self, *, step_index: int) -> None:
        self._session.raise_if_step_limit_reached(step_index=step_index)

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
        build_actions: Callable[[PromptActionContext], list[object]],
        build_final_observation: Callable[[], PromptObservation] | None = None,
        load_model: Callable[[str], object] | None = None,
        create_agent: Callable[..., object] | None = None,
        before_final_observation: Callable[[], None] | None = None,
        replay_memory: Callable[[PromptMemoryEntry], PromptMemoryReplayResult | None]
        | None = None,
        compile_memory: Callable[[PromptMemoryCompileContext], PromptMemoryDraft | None]
        | None = None,
        format_memory: Callable[[PromptMemoryEntry, str | None], str] | None = None,
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
        self._build_actions = build_actions
        self._build_final_observation = build_final_observation
        self._load_model = load_model or _load_langchain_model
        self._create_agent = create_agent or _create_langchain_agent
        self._before_final_observation = before_final_observation
        self._memory_observation_signature: str | None = None
        self._memory_entry: PromptMemoryEntry | None = None
        self._memory_loaded = False
        self._memory_replay_error: str | None = None
        self._replay_memory = replay_memory
        self._compile_memory = compile_memory
        self._format_memory = format_memory or _format_prompt_memory_for_prompt
        self._prompt_model: object | None = None
        self._usage_tracker = _PromptUsageTracker()
        self._action_records: list[JourneyLogRecord] = []
        self._memory_log_records: list[JourneyLogRecord] = []
        self._prompt_thread_id = get_ident()
        self._prompt_thread_calls: Queue[_PromptThreadCall] = Queue()

    def run(self) -> str | dict[str, object]:
        observation = self._build_observation()
        memory_entry = self._memory_for_observation(observation)
        replay_result = self._try_replay_memory(memory_entry)
        if replay_result is not None:
            return replay_result.final_output
        if self._memory_replay_error is not None:
            observation = self._build_observation()
        self._prompt_model = self._load_model(self._model)
        agent = self._create_agent(
            self._prompt_model,
            tools=self._build_actions(PromptActionContext(self)),
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

    def action_observation_or_stop(
        self,
        *,
        step_index: int,
    ) -> list[dict[str, object]]:
        observation = self._build_observation()
        self.raise_if_step_limit_reached(step_index=step_index)
        return self._build_observation_content(
            observation=observation,
            include_memory=False,
        )

    def raise_if_step_limit_reached(self, *, step_index: int) -> None:
        if step_index >= self._max_steps:
            self._raise_max_steps()

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
                self._format_memory(
                    memory_entry,
                    self._memory_replay_error,
                ),
            ]
        if self._output_schema is None:
            output_section = [
                "",
                "Final return value requested: plain text.",
            ]
        else:
            output_section = [
                "",
                "Final return value requested: JSON object with these output fields:",
                self._output_schema.prompt_text,
            ]
        prompt_text = "\n".join(
            [
                f"Instruction:\n{self._instruction}",
                *memory_section,
                "",
                "Observation records JSON:",
                _json_prompt(self._observation_record_dicts(observation)),
                *self._render_text_sections(observation.sections),
                *output_section,
                "",
                "Use an available action to continue work, or return a concise completion signal "
                "when no more action is needed. The structured finalizer will decide whether "
                "success criteria are met and produce the returned output.",
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
            response = self._call_model(
                "action_loop",
                lambda config: self._invoke_agent_on_worker(
                    lambda: agent.invoke(
                        {"messages": messages},
                        config={
                            **config,
                            "recursion_limit": max(6, self._max_steps * 3 + 3),
                        },
                    )
                )
            )
        except PromptControlError:
            raise
        except Exception as exc:
            if _is_langchain_recursion_limit_error(exc):
                self._raise_max_steps()
            raise _runtime_error_with_hint(
                f"{self._owner} failed to call model {self._model!r}: {exc}",
                hint=_prompt_model_failure_hint(self._model, exc),
            ) from exc
        return response

    def _call_model(
        self,
        operation: str,
        callback: Callable[[dict[str, object]], _T],
    ) -> _T:
        return self._usage_tracker.call(
            operation=operation,
            configured_model=self._model,
            logger=self._logger,
            callback=callback,
        )

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
        try:
            while not done.is_set():
                self._run_next_prompt_thread_call(timeout=0.05)
        except BaseException as exc:
            self._cancel_pending_prompt_thread_calls(exc)
            thread.join(timeout=0.25)
            raise
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
            if not isinstance(exc, Exception):
                raise
        finally:
            call.done.set()

    def _cancel_pending_prompt_thread_calls(self, exc: BaseException) -> None:
        while True:
            try:
                call = self._prompt_thread_calls.get_nowait()
            except Empty:
                return
            call.error = exc
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
        final_observation = (
            self._build_final_observation()
            if self._build_final_observation is not None
            else self._build_observation()
        )
        step_index = len(self._action_records) + 1
        final_output = self._finalize_output(
            response_text,
            observation=final_observation,
            step_index=step_index,
        )
        try:
            self._write_memory(
                final_output=final_output,
                final_observation=final_observation,
            )
        except Exception:
            self._log_finish(step_index=step_index, final_output=final_output)
            raise
        self._log_finish(step_index=step_index, final_output=final_output)
        return final_output

    def _log_finish(
        self,
        *,
        step_index: int,
        final_output: str | dict[str, object],
    ) -> None:
        output_summary = _prompt_output_summary(final_output)
        usage = self._usage_tracker.totals()
        usage_detail = _format_prompt_usage_totals(usage)
        self._logger.info(
            "prompt_finish",
            f"step {step_index}/{self._max_steps}: finished with output: "
            f"{output_summary}; {usage_detail}",
            pretty=pretty_row(
                f"{step_index}/{self._max_steps} finish",
                f"{output_summary} {usage_detail}",
                indent=10,
                label_width=25,
                style="success",
            ),
            step=step_index,
            max_steps=self._max_steps,
            output=output_summary,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            model_calls=usage.model_calls,
        )

    def _finalize_output(
        self,
        response_text: str,
        *,
        observation: PromptObservation,
        step_index: int,
    ) -> str | dict[str, object]:
        structured_response = self._request_finalization(
            completion_text=response_text,
            observation=observation,
        )
        finalization_schema = self._finalization_schema()
        finalization = _normalize_structured_output(
            structured_response,
            schema=finalization_schema,
            owner=self._owner,
        )
        success_criteria_met = finalization.get("success_criteria_met")
        failure_reason = finalization.get("failure_reason")
        if not isinstance(success_criteria_met, bool) or not isinstance(
            failure_reason,
            str,
        ):
            raise RuntimeError(
                f"{self._owner} final structured output must include boolean "
                "success_criteria_met and string failure_reason fields."
            )
        if not success_criteria_met:
            self.raise_prompt_failed(
                step_index=step_index,
                reason=failure_reason,
            )

        raw_output = finalization.get("output")
        if self._output_schema is None:
            if not isinstance(raw_output, str):
                raise RuntimeError(
                    f"{self._owner} final structured output field 'output' "
                    "must be a string when output=... is omitted."
                )
            return raw_output
        if not isinstance(raw_output, dict):
            raise RuntimeError(
                f"{self._owner} final structured output field 'output' must "
                "be an object matching output=...."
            )
        structured_output = _normalize_structured_output(
            raw_output,
            schema=self._output_schema,
            owner=self._owner,
        )
        return _fill_visible_message_fields(
            structured_output,
            schema=self._output_schema,
            visible_text=observation.visible_text,
        )

    def _request_finalization(
        self,
        *,
        completion_text: str,
        observation: PromptObservation,
    ) -> object:
        if self._prompt_model is None:
            self._prompt_model = self._load_model(self._model)
        try:
            structured_model = self._prompt_model.with_structured_output(
                self._finalization_schema().json_schema,
                method="json_schema",
            )
            messages = self._build_finalization_messages(
                completion_text=completion_text,
                observation=observation,
            )
            return self._call_model(
                "finalization",
                lambda config: structured_model.invoke(
                    messages,
                    config=config,
                )
            )
        except Exception as exc:
            raise _runtime_error_with_hint(
                f"{self._owner} failed to call structured output model "
                f"{self._model!r}: {exc}",
                hint=_prompt_model_failure_hint(self._model, exc),
            ) from exc

    def _build_finalization_messages(
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
                _json_prompt(self._observation_record_dicts(observation)),
                *self._render_finalization_sections(observation.sections),
                "",
                "Return the finalization object using this schema:",
                _json_prompt(self._finalization_schema().properties),
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
                    "Finalize a Journey prompt task with structured output.\n\n"
                    "Use the current visible state as the source of truth. Decide whether the original "
                    "instruction and all success criteria are satisfied. Treat expectation wording such "
                    "as 'Expect ...', 'should ...', and 'must ...' as required success criteria.\n\n"
                    "Set success_criteria_met=false when the visible page state contradicts a success "
                    "criterion, a required state cannot be confirmed, or the completion signal admits "
                    "that a criterion was not met. When false, set failure_reason to the visible "
                    "blocking message or a concise expected-vs-observed explanation.\n\n"
                    "When success_criteria_met=true, set failure_reason to an empty string and set "
                    "output to the requested return value. If a requested output field asks for an "
                    "error, validation message, warning, status, or problem, copy the visible message "
                    "exactly when present.\n\n"
                    "Do not mention implementation details, hidden reasoning, or unavailable metadata."
                ),
            },
            {
                "role": "user",
                "content": content,
            },
        ]

    def _render_finalization_sections(
        self,
        sections: tuple[PromptTextSection, ...],
    ) -> list[str]:
        return self._render_text_sections(
            tuple(section for section in sections if section.tag == "visible-text")
        )

    def _finalization_schema(self) -> PromptOutputSchema:
        if self._output_schema is None:
            output_property: dict[str, object] = {
                "type": "string",
                "description": (
                    "Plain-text value to return when all success criteria are met."
                ),
            }
        else:
            output_property = {
                "type": "object",
                "description": (
                    "Output object to return when all success criteria are met."
                ),
                "properties": dict(self._output_schema.properties),
                "required": list(self._output_schema.fields),
                "additionalProperties": False,
            }
        properties: dict[str, object] = {
            "success_criteria_met": {
                "type": "boolean",
                "description": (
                    "Whether the original instruction and all success criteria are satisfied."
                ),
            },
            "failure_reason": {
                "type": "string",
                "description": (
                    "Visible blocking message or concise expected-vs-observed explanation "
                    "when success_criteria_met is false; otherwise an empty string."
                ),
            },
            "output": output_property,
        }
        return PromptOutputSchema(
            fields=("success_criteria_met", "failure_reason", "output"),
            properties=properties,
            json_schema={
                "title": "journey_prompt_finalization",
                "description": (
                    "Structured finalization for a Journey prompt task."
                ),
                "type": "object",
                "properties": properties,
                "required": ["success_criteria_met", "failure_reason", "output"],
                "additionalProperties": False,
            },
            prompt_text=json.dumps(properties, sort_keys=True, indent=2),
        )

    def _memory_for_observation(
        self,
        observation: PromptObservation,
    ) -> PromptMemoryEntry | None:
        if self._memory_path is None:
            return None
        if not self._memory_loaded:
            self._memory_loaded = True
            self._memory_observation_signature = observation.signature
            self._memory_entry = load_prompt_memory_entry(
                self._memory_path,
                component=self._component,
                instruction=self._instruction,
                observation_signature=observation.signature,
            )
            if self._memory_entry is not None:
                self._logger.info(
                    "prompt_memory_loaded",
                    f"loaded prompt memory from {self._memory_path}",
                    pretty=_prompt_memory_row(f"loaded from {self._memory_path}"),
                    path=str(self._memory_path),
                )
        return self._memory_entry

    def _try_replay_memory(
        self,
        memory_entry: PromptMemoryEntry | None,
    ) -> PromptMemoryReplayResult | None:
        if memory_entry is None or self._replay_memory is None:
            return None
        try:
            replay_result = self._replay_memory(memory_entry)
        except Exception as exc:
            self._memory_replay_error = str(exc)
            return None
        return replay_result

    def _write_memory(
        self,
        *,
        final_output: str | dict[str, object],
        final_observation: PromptObservation,
    ) -> None:
        if (
            self._memory_path is None
            or self._memory_observation_signature is None
            or prompt_memory_updates_disabled()
            or self._compile_memory is None
        ):
            return
        draft = self._compile_memory(
            PromptMemoryCompileContext(
                component=self._component,
                instruction=self._instruction,
                observation_signature=self._memory_observation_signature,
                final_output=final_output,
                log_records=tuple(self._memory_log_records or self._action_records),
                final_observation=final_observation,
                call_model=self._call_model,
            )
        )
        if draft is None:
            return
        entry = prompt_memory_entry_from_result(
            component=self._component,
            instruction=self._instruction,
            observation_signature=self._memory_observation_signature,
            final_output=final_output,
            sections=draft.sections,
        )
        run_count = write_prompt_memory_entry(
            self._memory_path,
            entry,
        )
        self._logger.info(
            "prompt_memory_saved",
            f"wrote prompt memory to {self._memory_path}",
            pretty=_prompt_memory_row(
                f"wrote to {self._memory_path}",
                style="success",
            ),
            path=str(self._memory_path),
            run_count=run_count,
        )


def _format_prompt_memory_for_prompt(
    entry: PromptMemoryEntry,
    replay_error: str | None = None,
) -> str:
    replay_error_section: list[str] = []
    if replay_error:
        replay_error_section = [
            "",
            "Prior memory replay failed before fallback:",
            replay_error.strip(),
        ]
    rendered_sections: list[str] = []
    for section in entry.sections:
        rendered_sections.extend(["", section.heading + ":"])
        if section.language is None:
            rendered_sections.append(section.body)
        else:
            rendered_sections.extend(
                [
                    f"```{section.language}",
                    section.body,
                    "```",
                ]
            )
    return "\n".join(
        [
            "Prompt memory:",
            "Use this prior successful memory as a hint, but trust the current observation.",
            *replay_error_section,
            *rendered_sections,
        ]
    )


def _json_prompt(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _compact_prompt_observation_messages(messages: list[object]) -> list[object]:
    observation_indexes = [
        index
        for index, message in enumerate(messages)
        if _is_prompt_observation_message(message)
    ]
    latest_observation_index = observation_indexes[-1] if observation_indexes else -1
    stale_screenshot_indexes = _stale_screenshot_tool_result_indexes(
        messages,
        latest_observation_index=latest_observation_index,
    )
    stale_inspect_dom_indexes = _stale_inspect_dom_tool_result_indexes(
        messages,
        latest_observation_index=latest_observation_index,
    )
    if (
        len(observation_indexes) <= 1
        and not stale_screenshot_indexes
        and not stale_inspect_dom_indexes
    ):
        return messages

    compacted = list(messages)
    if len(observation_indexes) > 1:
        initial_observation_index = observation_indexes[0]
        for index in observation_indexes[:-1]:
            compacted[index] = _compact_prompt_observation_message(
                messages[index],
                preserve_initial_context=index == initial_observation_index,
            )
    for index in stale_screenshot_indexes:
        compacted[index] = _compact_screenshot_tool_result_message(messages[index])
    for index in stale_inspect_dom_indexes:
        compacted[index] = _compact_inspect_dom_tool_result_message(messages[index])
    return compacted


def _is_prompt_observation_message(message: object) -> bool:
    content = _message_content(message)
    return any(_OBSERVATION_RECORDS_MARKER in text for text in _content_texts(content))


def _compact_prompt_observation_message(
    message: object,
    *,
    preserve_initial_context: bool,
) -> object:
    compacted_text = _compacted_observation_text(
        _first_observation_text(_message_content(message)),
        preserve_initial_context=preserve_initial_context,
    )
    return _with_message_content(message, [{"type": "text", "text": compacted_text}])


def _message_content(message: object) -> object:
    if isinstance(message, Mapping):
        return message.get("content")
    return getattr(message, "content", None)


def _content_texts(content: object) -> Iterator[str]:
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, str):
            yield item
            continue
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if isinstance(text, str):
            yield text


def _first_observation_text(content: object) -> str:
    for text in _content_texts(content):
        if _OBSERVATION_RECORDS_MARKER in text:
            return text
    return ""


def _compacted_observation_text(
    text: str,
    *,
    preserve_initial_context: bool,
) -> str:
    if preserve_initial_context:
        prefix = text.split(_OBSERVATION_RECORDS_MARKER, 1)[0].rstrip()
        if prefix:
            return "\n\n".join([prefix, _COMPACTED_OBSERVATION_TEXT])
    return _COMPACTED_OBSERVATION_TEXT


def _stale_screenshot_tool_result_indexes(
    messages: list[object],
    *,
    latest_observation_index: int,
) -> list[int]:
    return _stale_tool_result_indexes(
        messages,
        latest_observation_index=latest_observation_index,
        tool_name=_SCREENSHOT_TOOL_NAME,
        require_image=True,
    )


def _stale_inspect_dom_tool_result_indexes(
    messages: list[object],
    *,
    latest_observation_index: int,
) -> list[int]:
    return _stale_tool_result_indexes(
        messages,
        latest_observation_index=latest_observation_index,
        tool_name=_INSPECT_DOM_TOOL_NAME,
        require_image=False,
    )


def _stale_tool_result_indexes(
    messages: list[object],
    *,
    latest_observation_index: int,
    tool_name: str,
    require_image: bool,
) -> list[int]:
    if latest_observation_index < 0:
        return []
    tool_call_ids = _tool_call_ids(messages, tool_name=tool_name)
    if not tool_call_ids:
        return []
    indexes: list[int] = []
    for index, message in enumerate(messages[:latest_observation_index]):
        tool_call_id = _message_tool_call_id(message)
        if tool_call_id not in tool_call_ids:
            continue
        if (
            not require_image
            or _content_has_image(_message_content(message))
        ):
            indexes.append(index)
    return indexes


def _tool_call_ids(messages: list[object], *, tool_name: str) -> set[str]:
    tool_call_ids: set[str] = set()
    for message in messages:
        for tool_call in _message_tool_calls(message):
            if tool_call.get("name") != tool_name:
                continue
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and tool_call_id:
                tool_call_ids.add(tool_call_id)
    return tool_call_ids


def _message_tool_calls(message: object) -> Iterator[Mapping[str, object]]:
    raw_tool_calls = None
    if isinstance(message, Mapping):
        raw_tool_calls = message.get("tool_calls")
    else:
        raw_tool_calls = getattr(message, "tool_calls", None)
    if not isinstance(raw_tool_calls, list):
        return
    for tool_call in raw_tool_calls:
        if isinstance(tool_call, Mapping):
            yield tool_call


def _message_tool_call_id(message: object) -> str | None:
    if isinstance(message, Mapping):
        value = message.get("tool_call_id")
    else:
        value = getattr(message, "tool_call_id", None)
    return value if isinstance(value, str) and value else None


def _content_has_image(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("type") == "image_url"
        for item in content
    )


def _compact_screenshot_tool_result_message(message: object) -> object:
    return _with_message_content(
        message,
        [{"type": "text", "text": _COMPACTED_SCREENSHOT_TEXT}],
    )


def _compact_inspect_dom_tool_result_message(message: object) -> object:
    return _with_message_content(
        message,
        [{"type": "text", "text": _COMPACTED_INSPECT_DOM_TEXT}],
    )


def _with_message_content(message: object, content: object) -> object:
    if isinstance(message, Mapping):
        updated = dict(message)
        updated["content"] = content
        return updated
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"content": content})
    copy_method = getattr(message, "copy", None)
    if callable(copy_method):
        return copy_method(update={"content": content})
    return message


def _iter_chat_message_batches(messages: object) -> Iterator[list[object]]:
    if not isinstance(messages, list):
        return
    if messages and all(isinstance(item, list) for item in messages):
        for batch in messages:
            if isinstance(batch, list):
                yield list(batch)
        return
    yield list(messages)


def _prompt_payload_stats(messages: list[object]) -> _PromptPayloadStats:
    text_chars = 0
    image_count = 0
    section_char_counts: dict[str, int] = {}
    for message in messages:
        content = _message_content(message)
        for text in _content_texts(content):
            text_chars += len(text)
            for section, char_count in _section_char_counts(text):
                section_char_counts[section] = (
                    section_char_counts.get(section, 0) + char_count
                )
        image_count += _content_image_count(content)
    return _PromptPayloadStats(
        text_chars=text_chars,
        approximate_tokens=max(1, (text_chars + 3) // 4) if text_chars else 0,
        image_count=image_count,
        section_char_counts=tuple(sorted(section_char_counts.items())),
    )


def _section_char_counts(text: str) -> Iterator[tuple[str, int]]:
    current_section = "message"
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _is_prompt_section_heading(stripped):
            current_section = stripped.removesuffix(":")
        yield current_section, len(line)


def _is_prompt_section_heading(line: str) -> bool:
    if not line.endswith(":") or line.startswith("<") or len(line) > 100:
        return False
    if not line:
        return False
    return all(character.isalnum() or character in " _-/()." for character in line[:-1])


def _content_image_count(content: object) -> int:
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "image_url"
    )


def _iter_usage_values(result: object) -> Iterator[object]:
    messages = list(_iter_result_messages(result))
    for message in messages:
        yield message
    if any(_has_usage_payload(message) for message in messages):
        return
    if _has_usage_payload(result):
        yield result


def _iter_result_messages(result: object) -> Iterator[object]:
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            for message in messages:
                yield message
    generations = getattr(result, "generations", None)
    if isinstance(generations, list):
        for generation_group in generations:
            if not isinstance(generation_group, list):
                continue
            for generation in generation_group:
                message = getattr(generation, "message", None)
                if message is not None:
                    yield message


def _has_usage_payload(value: object) -> bool:
    sources = _usage_sources(value)
    if not sources:
        return False
    return (
        _first_int(sources, _INPUT_TOKEN_KEYS) is not None
        or _first_int(sources, _OUTPUT_TOKEN_KEYS) is not None
        or _first_int(sources, _TOTAL_TOKEN_KEYS) is not None
    )


def _prompt_usage_record(
    *,
    operation: str,
    configured_model: str,
    value: object,
) -> _PromptUsageRecord | None:
    sources = _usage_sources(value)
    if not sources:
        return None
    input_tokens = _first_int(sources, _INPUT_TOKEN_KEYS)
    output_tokens = _first_int(sources, _OUTPUT_TOKEN_KEYS)
    total_tokens = _first_int(sources, _TOTAL_TOKEN_KEYS)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    model = _usage_model(value, configured_model=configured_model, sources=sources)
    if (
        input_tokens is None
        and output_tokens is None
        and total_tokens is None
    ):
        return None
    return _PromptUsageRecord(
        operation=operation,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _usage_sources(value: object) -> list[Mapping[str, object]]:
    sources: list[Mapping[str, object]] = []
    for candidate in (
        getattr(value, "usage_metadata", None),
        getattr(value, "response_metadata", None),
        getattr(value, "llm_output", None),
        value if isinstance(value, Mapping) else None,
    ):
        mapping = _as_mapping(candidate)
        if mapping is not None:
            sources.append(mapping)
    for source in tuple(sources):
        for key in ("usage_metadata", "usage", "token_usage", "usage_details"):
            mapping = _as_mapping(source.get(key))
            if mapping is not None:
                sources.append(mapping)
    return sources


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _usage_model(
    value: object,
    *,
    configured_model: str,
    sources: list[Mapping[str, object]],
) -> str:
    for source in sources:
        for key in ("model_name", "model", "ls_model_name"):
            model = source.get(key)
            if isinstance(model, str) and model.strip():
                return model.strip()
    response_metadata = _as_mapping(getattr(value, "response_metadata", None))
    if response_metadata is not None:
        model = response_metadata.get("model_name")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return configured_model


def _first_int(sources: list[Mapping[str, object]], keys: tuple[str, ...]) -> int | None:
    for source in sources:
        for key in keys:
            value = _int_value(source.get(key))
            if value is not None:
                return value
    return None


def _int_value(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _aggregate_prompt_usage(
    records: list[_PromptUsageRecord],
) -> _PromptUsageTotals:
    return _PromptUsageTotals(
        input_tokens=_sum_known_int(record.input_tokens for record in records),
        output_tokens=_sum_known_int(record.output_tokens for record in records),
        total_tokens=_sum_known_int(record.total_tokens for record in records),
        model_calls=len(records),
    )


def _sum_known_int(values: Iterator[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known)


def _format_prompt_usage(record: _PromptUsageRecord) -> str:
    return (
        f"{record.operation} model={record.model} "
        f"{_format_prompt_usage_totals(record)}"
    )


def _format_prompt_usage_totals(
    usage: _PromptUsageRecord | _PromptUsageTotals,
) -> str:
    return (
        "tokens="
        f"input:{_format_optional_int(usage.input_tokens)} "
        f"output:{_format_optional_int(usage.output_tokens)} "
        f"total:{_format_optional_int(usage.total_tokens)}"
    )


def _format_optional_int(value: int | None) -> str:
    if value is None:
        return "unknown"
    return str(value)


def _prompt_output_summary(value: str | dict[str, object]) -> str:
    if isinstance(value, str):
        return truncate_prompt_memory_text(value)
    return truncate_prompt_memory_text(json.dumps(value, sort_keys=True))


def _prompt_memory_row(detail: object, *, style: PrettyStyle = "accent") -> PrettyLine:
    return pretty_row(
        "prompt memory",
        detail,
        indent=_PROMPT_DETAIL_INDENT,
        label_width=_PROMPT_LABEL_WIDTH,
        style=style,
    )


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
        raise _runtime_error_with_hint(
            f"prompt failed to initialize LangChain model {model!r}: {exc}",
            hint=_prompt_model_failure_hint(model, exc),
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
        middleware=(_PromptObservationCompactionMiddleware(),),
    )


def resolve_prompt_model(
    model: str | None,
    *,
    env_var: str,
    owner: str,
    default_model: str | None = None,
) -> str:
    if model is not None and model.strip():
        return model.strip()
    env_model = os.environ.get(env_var, "").strip()
    if env_model:
        return env_model
    if default_model is not None and default_model.strip():
        return default_model.strip()
    raise _runtime_error_with_hint(
        f"{owner} requires model=... or the {env_var} environment variable.",
        hint=(
            f"Set {env_var} before running the journey, for example "
            f"`export {env_var}=openai:gpt-4.1-mini`. In shells, use a "
            "space after `export`: `export NAME=value`, not `export=NAME=value`."
        ),
    )


def _runtime_error_with_hint(message: str, *, hint: str | None = None) -> RuntimeError:
    error = RuntimeError(message)
    if hint is not None:
        setattr(error, "hint", hint)
    return error


def _exception_hint(exc: BaseException) -> str | None:
    hint = getattr(exc, "hint", None)
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    return None


def _prompt_model_failure_hint(model: str, exc: BaseException) -> str | None:
    existing_hint = _exception_hint(exc)
    if existing_hint is not None:
        return existing_hint
    provider = _model_provider(model)
    provider_config = _PROMPT_PROVIDER_ENV_VARS.get(provider)
    if provider_config is None or not _looks_like_auth_failure(exc):
        return None
    env_var, provider_name = provider_config
    return (
        f"Set {env_var} for {provider_name} models, then rerun. For example: "
        f"`export {env_var}=...` and "
        f"`export JOURNEY_BROWSER_PROMPT_MODEL={model}`."
    )


def _model_provider(model: str) -> str:
    provider, separator, _ = model.partition(":")
    if separator:
        return provider.strip().lower()
    return ""


def _looks_like_auth_failure(exc: BaseException) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(term in message for term in _AUTH_FAILURE_TERMS)
