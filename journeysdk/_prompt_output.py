"""Shared prompt-output helpers for AI-driven Journey touchpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

PromptOutputFieldSpec = str | Mapping[str, object]
PromptOutputSpec = Mapping[str, PromptOutputFieldSpec]


@dataclass(frozen=True)
class PromptOutputSchema:
    fields: tuple[str, ...]
    properties: dict[str, object]
    json_schema: dict[str, object]
    prompt_text: str


def normalize_prompt_output_spec(
    output: PromptOutputSpec | None,
    *,
    owner: str,
) -> PromptOutputSchema | None:
    """Return a strict structured-output schema for an optional output spec."""

    if output is None:
        return None
    if not isinstance(output, Mapping):
        raise TypeError(f"{owner} output must be a mapping of field names to specs.")
    if not output:
        raise ValueError(f"{owner} output must define at least one field.")

    properties: dict[str, object] = {}
    for raw_name, raw_spec in output.items():
        name = _normalize_field_name(raw_name, owner=owner)
        if name in properties:
            raise ValueError(f"{owner} output field {name!r} is defined more than once.")
        properties[name] = _normalize_field_schema(
            raw_spec,
            owner=owner,
            field_name=name,
        )

    json_schema = {
        "title": "journey_prompt_output",
        "description": "Structured output for JourneyPlaywrightPage.prompt(...).",
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    prompt_text = json.dumps(properties, sort_keys=True, indent=2)
    return PromptOutputSchema(
        fields=tuple(properties),
        properties=properties,
        json_schema=json_schema,
        prompt_text=prompt_text,
    )


def parse_prompt_structured_output(
    text: str,
    schema: PromptOutputSchema,
    *,
    owner: str,
) -> dict[str, object]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{owner} expected the final model response to be JSON matching output=..."
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"{owner} expected the final model response to be a JSON object."
        )
    return validate_prompt_structured_output(parsed, schema, owner=owner)


def validate_prompt_structured_output(
    output: dict[str, object],
    schema: PromptOutputSchema,
    *,
    owner: str,
) -> dict[str, object]:
    expected = set(schema.fields)
    actual = set(output)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing fields {missing!r}")
        if extra:
            details.append(f"unexpected fields {extra!r}")
        raise RuntimeError(
            f"{owner} final structured output did not match output=...: "
            + "; ".join(details)
        )
    return dict(output)


def _normalize_field_name(name: object, *, owner: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"{owner} output field names must be strings.")
    if not name:
        raise ValueError(f"{owner} output field names must be non-empty.")
    if name != name.strip():
        raise ValueError(
            f"{owner} output field names must not start or end with whitespace."
        )
    return name


def _normalize_field_schema(
    spec: object,
    *,
    owner: str,
    field_name: str,
) -> dict[str, object]:
    if isinstance(spec, str):
        description = spec.strip()
        if not description:
            raise ValueError(
                f"{owner} output field {field_name!r} description must be non-empty."
            )
        return {
            "type": "string",
            "description": description,
        }
    if not isinstance(spec, Mapping):
        raise TypeError(
            f"{owner} output field {field_name!r} must be a string description "
            "or a JSON-schema mapping."
        )
    if not spec:
        raise ValueError(
            f"{owner} output field {field_name!r} schema must be non-empty."
        )
    normalized = dict(spec)
    try:
        json.dumps(normalized)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{owner} output field {field_name!r} schema must be JSON-serializable."
        ) from exc
    return normalized
