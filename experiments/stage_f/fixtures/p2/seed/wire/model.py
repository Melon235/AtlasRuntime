"""Decoded wire values (frozen)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Message:
    """A decoded JSON message."""

    version: int
    kind: str
    identifier: str
    body: dict[str, Any]
    checksum: int | None = None


@dataclass(frozen=True)
class Frame:
    """A decoded binary frame."""

    version: int
    kind: str
    payload: bytes
    checksum: int
