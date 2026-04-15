"""Tutorial journeys that demonstrate --fail-fast."""

from __future__ import annotations

from journeysdk import journey, step


def raise_expected_failure() -> bool:
    raise RuntimeError("expected tutorial failure")


def finish_successfully() -> bool:
    return True


@journey
def broken_demo_journey() -> None:
    step(raise_expected_failure)


@journey
def good_demo_journey() -> None:
    step(finish_successfully)
