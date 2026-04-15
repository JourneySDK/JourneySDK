from __future__ import annotations

import _thread
import sys
import threading
from collections.abc import Callable
from typing import Any

import journeysdk as journey
from journeysdk.models import StepNode

INTERRUPT_PROMPT_PREFIX = "[tutorial] Press Ctrl-C during the next"


class LiveStderr:
    def __init__(
        self,
        wrapped: Any,
        *,
        trigger_text: str = INTERRUPT_PROMPT_PREFIX,
    ) -> None:
        self._wrapped = wrapped
        self._trigger_text = trigger_text
        self._buffer = ""
        self.prompt_seen = threading.Event()
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            if self._trigger_text in self._buffer:
                self.prompt_seen.set()
        return self._wrapped.write(text)

    def flush(self) -> None:
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def install_live_stderr(monkeypatch: Any) -> LiveStderr:
    live_stderr = LiveStderr(sys.stderr)
    monkeypatch.setattr(sys, "stderr", live_stderr)
    return live_stderr


def configured_pause_seconds(
    journey_fn: Callable[..., Any],
    *,
    step_label: str,
) -> float:
    plan = journey.compile_journey(journey_fn)
    for node in plan.case_plans[0].nodes:
        if not isinstance(node, StepNode) or node.label != step_label:
            continue
        pause_seconds = node.args[-1]
        if isinstance(pause_seconds, bool) or not isinstance(
            pause_seconds,
            int | float,
        ):
            raise AssertionError(
                f"Expected the last argument for step {step_label!r} to be a pause number."
            )
        return float(pause_seconds)
    raise AssertionError(f"Could not find step {step_label!r} in compiled tutorial plan.")


def start_interrupt_on_prompt(
    live_stderr: LiveStderr,
    *,
    pause_seconds: float,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def worker() -> None:
        if not live_stderr.prompt_seen.wait(timeout=max(pause_seconds + 2.0, 5.0)):
            return
        delay_seconds = min(0.25, max(pause_seconds / 4.0, 0.05))
        if stop_event.wait(delay_seconds):
            return
        _thread.interrupt_main()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return stop_event, thread
