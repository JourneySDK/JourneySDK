"""Public typing helpers for Journey SDK APIs."""

from __future__ import annotations

from collections.abc import Callable
from typing import ParamSpec, TypeAlias, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

JourneyFunction: TypeAlias = Callable[P, R]
JourneyEntrypoint: TypeAlias = Callable[[], object]
StepFunction: TypeAlias = Callable[P, R]

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

__all__ = [
    "JourneyEntrypoint",
    "JourneyFunction",
    "JsonObject",
    "JsonPrimitive",
    "JsonValue",
    "StepFunction",
]
