"""Tutorial journeys that demonstrate --fail-fast."""

from __future__ import annotations

import journey


def raise_expected_failure() -> bool:
    raise RuntimeError("expected tutorial failure")


def finish_successfully() -> bool:
    return True


@journey.journey
def broken_demo_journey() -> None:
    journey.step(raise_expected_failure)


@journey.journey
def good_demo_journey() -> None:
    journey.step(finish_successfully)
