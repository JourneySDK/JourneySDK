"""Value-level replay storage helpers."""

from __future__ import annotations

import pickle
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, Self, runtime_checkable

from .utils import callable_ref, resolve_ref


@dataclass(frozen=True)
class JourneyStoreContext:
    """Context passed to custom value storage hooks."""

    artifact_root: Path
    boundary_kind: str
    boundary_id: str

    def child(self, segment: str) -> "JourneyStoreContext":
        return replace(self, artifact_root=self.artifact_root / segment)


@dataclass(frozen=True)
class JourneyRestoreContext:
    """Context passed to custom value restore hooks."""

    artifact_root: Path
    boundary_kind: str
    boundary_id: str

    def child(self, segment: str) -> "JourneyRestoreContext":
        return replace(self, artifact_root=self.artifact_root / segment)


@runtime_checkable
class RehydratableValue(Protocol):
    """Public protocol for values that need custom replay storage."""

    def __store__(self, context: JourneyStoreContext) -> object:
        ...

    @classmethod
    def __restore__(cls, payload: object, context: JourneyRestoreContext) -> Self:
        ...


@dataclass(frozen=True)
class StoredValue:
    kind: str
    payload: bytes | None = None
    items: tuple["StoredValue", ...] = ()
    entries: tuple[tuple[bytes, "StoredValue"], ...] = ()
    type_ref: str | None = None


class StoredValueSerializationError(Exception):
    """Raised when replay state cannot be serialized."""


class StoredValueRestoreError(Exception):
    """Raised when replay state cannot be restored."""


def store_value(
    value: Any,
    *,
    context: JourneyStoreContext,
    description: str,
) -> StoredValue:
    """Serialize one replayable value into a stored envelope."""

    if isinstance(value, tuple):
        return StoredValue(
            kind="tuple",
            items=tuple(
                store_value(
                    item,
                    context=context.child(f"tuple-{index}"),
                    description=f"{description}[{index}]",
                )
                for index, item in enumerate(value)
            ),
        )
    if isinstance(value, list):
        return StoredValue(
            kind="list",
            items=tuple(
                store_value(
                    item,
                    context=context.child(f"list-{index}"),
                    description=f"{description}[{index}]",
                )
                for index, item in enumerate(value)
            ),
        )
    if isinstance(value, dict):
        entries: list[tuple[bytes, StoredValue]] = []
        for index, (key, item) in enumerate(value.items()):
            try:
                stored_key = pickle.dumps(key)
            except Exception as exc:  # pragma: no cover - exercised through callers
                raise StoredValueSerializationError(
                    f"Could not store {description} key {key!r}: {exc}"
                ) from exc
            entries.append(
                (
                    stored_key,
                    store_value(
                        item,
                        context=context.child(f"dict-{index}"),
                        description=f"{description}[{key!r}]",
                    ),
                )
            )
        return StoredValue(kind="dict", entries=tuple(entries))

    value_type = _rehydratable_type(value)
    if value_type is not None:
        type_ref = callable_ref(value_type)
        if "<locals>" in type_ref:
            raise StoredValueSerializationError(
                f"Could not store {description}: custom replay values must use importable top-level classes."
            )
        try:
            payload = value.__store__(context)
            payload_bytes = pickle.dumps(payload)
        except Exception as exc:  # pragma: no cover - exercised through callers
            raise StoredValueSerializationError(
                f"Could not store {description}: {exc}"
            ) from exc
        return StoredValue(
            kind="rehydratable",
            payload=payload_bytes,
            type_ref=type_ref,
        )

    try:
        payload = pickle.dumps(value)
    except Exception as exc:  # pragma: no cover - exercised through callers
        raise StoredValueSerializationError(
            f"Could not store {description}: {exc}"
        ) from exc
    return StoredValue(kind="pickle", payload=payload)


def restore_value(
    stored: StoredValue,
    *,
    context: JourneyRestoreContext,
    description: str,
) -> Any:
    """Restore one replayable value from its stored envelope."""

    if stored.kind == "tuple":
        return tuple(
            restore_value(
                item,
                context=context.child(f"tuple-{index}"),
                description=f"{description}[{index}]",
            )
            for index, item in enumerate(stored.items)
        )
    if stored.kind == "list":
        return [
            restore_value(
                item,
                context=context.child(f"list-{index}"),
                description=f"{description}[{index}]",
            )
            for index, item in enumerate(stored.items)
        ]
    if stored.kind == "dict":
        restored: dict[Any, Any] = {}
        for index, (stored_key, item) in enumerate(stored.entries):
            try:
                key = pickle.loads(stored_key)
            except Exception as exc:  # pragma: no cover - exercised through callers
                raise StoredValueRestoreError(
                    f"Could not restore {description} key: {exc}"
                ) from exc
            restored[key] = restore_value(
                item,
                context=context.child(f"dict-{index}"),
                description=f"{description}[{key!r}]",
            )
        return restored
    if stored.kind == "rehydratable":
        try:
            payload = pickle.loads(_require_payload(stored, description=description))
            value_type = resolve_ref(_require_type_ref(stored, description=description))
            restore = getattr(value_type, "__restore__", None)
            if not callable(restore):
                raise TypeError(
                    f"{value_type!r} does not define a callable __restore__(...) hook."
                )
            return restore(payload, context)
        except Exception as exc:  # pragma: no cover - exercised through callers
            raise StoredValueRestoreError(
                f"Could not restore {description}: {exc}"
            ) from exc
    if stored.kind == "pickle":
        try:
            return pickle.loads(_require_payload(stored, description=description))
        except Exception as exc:  # pragma: no cover - exercised through callers
            raise StoredValueRestoreError(
                f"Could not restore {description}: {exc}"
            ) from exc
    raise StoredValueRestoreError(
        f"Could not restore {description}: unknown stored value kind {stored.kind!r}."
    )


def _rehydratable_type(value: Any) -> type[Any] | None:
    value_type = type(value)
    store = getattr(value, "__store__", None)
    restore = getattr(value_type, "__restore__", None)
    if callable(store) and callable(restore):
        return value_type
    return None


def _require_payload(stored: StoredValue, *, description: str) -> bytes:
    if stored.payload is None:
        raise StoredValueRestoreError(
            f"Could not restore {description}: stored payload is missing."
        )
    return stored.payload


def _require_type_ref(stored: StoredValue, *, description: str) -> str:
    if stored.type_ref is None:
        raise StoredValueRestoreError(
            f"Could not restore {description}: stored type reference is missing."
        )
    return stored.type_ref


__all__ = [
    "JourneyRestoreContext",
    "JourneyStoreContext",
    "RehydratableValue",
]
