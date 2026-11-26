"""Payload schemas (v1): the table exists but nothing checks it."""

from __future__ import annotations

PAYLOADS: dict[str, dict] = {
    "greet": {"name": str, "loud": bool},
    "echo": {"message": str},
    "put": {"key": str, "value": int},
}


def validate(kind: str, payload) -> None:
    return None
