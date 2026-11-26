"""Events and envelopes (frozen)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    payload: dict[str, Any]
    created_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("event id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("event kind must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("event payload must be a dict")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)):
            raise ValueError("event created_at must be a number")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> Event:
        return cls(
            id=document["id"],
            kind=document["kind"],
            payload=dict(document["payload"]),
            created_at=float(document["created_at"]),
        )


@dataclass(frozen=True)
class Envelope:
    event: Event
    attempts: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise ValueError("attempts must be an integer")
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")

    def next_attempt(self) -> Envelope:
        return Envelope(event=self.event, attempts=self.attempts + 1)
