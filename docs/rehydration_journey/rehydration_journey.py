"""Example journey showing step-started branch rehydration."""

from __future__ import annotations

from journeysdk import branch, journey, step

EVENTS: list[str] = []
_EXTERNAL_SEED_COUNTER = 0


def reset_demo_state() -> None:
    global _EXTERNAL_SEED_COUNTER
    EVENTS.clear()
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


def assert_branch_a(shared: dict[str, str]) -> bool:
    EVENTS.append(f"branch_a_{shared['shared']}")
    return True


def assert_branch_b(shared: dict[str, str]) -> bool:
    EVENTS.append(f"branch_b_{shared['shared']}")
    return True


@journey
def rehydration_journey() -> None:
    payload = next_external_payload()
    context = step(prepare_context, payload)

    shared = step(shared_after_anchor, context)

    if branch(start_from=context):
        step(assert_branch_a, shared)
    elif branch(start_from=context):
        step(assert_branch_b, shared)
