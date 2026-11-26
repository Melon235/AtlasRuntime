"""Payload schemas (v2): required keys and their types before dispatch."""

from __future__ import annotations

from typing import Any

from wire.errors import InvalidPayload

PAYLOADS: dict[str, dict[str, Any]] = {
    "greet": {"name": str, "loud": bool},
    "echo": {"message": str},
    "put": {"key": str, "value": int},
}

OPTIONAL: dict[str, frozenset[str]] = {"greet": frozenset({"loud"})}


def _matches(expected: type, value: object) -> bool:
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, float) and not isinstance(value, bool)
    return isinstance(value, expected)


def validate(kind: str, payload: object) -> None:
    schema = PAYLOADS.get(kind)
    if schema is None:
        return
    if not isinstance(payload, dict):
        raise InvalidPayload(f"{kind}: payload must be an object")
    optional = OPTIONAL.get(kind, frozenset())
    for key, expected in schema.items():
        path = f"{kind}.{key}"
        if key not in payload:
            if key in optional:
                continue
            raise InvalidPayload(f"{path}: missing required key")
        if not _matches(expected, payload[key]):
            raise InvalidPayload(
                f"{path}: expected {expected.__name__}, got {type(payload[key]).__name__}"
            )


__all__ = ["OPTIONAL", "PAYLOADS", "validate"]
