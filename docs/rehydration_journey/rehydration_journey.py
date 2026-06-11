"""Example journey showing step-started branch rehydration."""

from __future__ import annotations

from journeysdk import branch, journey, step
from docs._reset_state import reset_default_state

EVENTS: list[str] = []
_EXTERNAL_SEED_COUNTER = 0


def reset_demo_state() -> None:
    global _EXTERNAL_SEED_COUNTER
    EVENTS.clear()
    reset_default_state(__file__)
    _EXTERNAL_SEED_COUNTER = 0


def next_external_payload() -> dict[str, int]:
    global _EXTERNAL_SEED_COUNTER
    _EXTERNAL_SEED_COUNTER += 1
    return {"seed": _EXTERNAL_SEED_COUNTER}


def prepare_context(payload: dict[str, int]) -> dict[str, str]:
    EVENTS.append(f"prepare_{payload['seed']}")
    return {"token": f"seed-{payload['seed']}"}


def shared_after_anchor(context: dict[str, str]) -> dict[str, str]:
    EVENTS.append(f"shared_{context['token']}")
    return {"shared": context["token"]}


def complete_branch_a_from_anchor(shared: dict[str, str]) -> bool:
    EVENTS.append(f"branch_a_{shared['shared']}")
    return True


def complete_branch_b_from_anchor(shared: dict[str, str]) -> bool:
    EVENTS.append(f"branch_b_{shared['shared']}")
    return True


@journey
def rehydration_journey() -> None:
    payload = next_external_payload()
    context = step(prepare_context, payload)

    shared = step(shared_after_anchor, context)

    if branch(replay_from=context):
        step(complete_branch_a_from_anchor, shared)
    elif branch(replay_from=context):
        step(complete_branch_b_from_anchor, shared)
