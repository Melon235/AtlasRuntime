"""Jobs, records and attempt outcomes (frozen)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATES = frozenset({"queued", "running", "failed", "done", "dead", "cancelled"})
TERMINAL = frozenset({"done", "dead", "cancelled"})


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    created_at: float
    deadline: float | None = None
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("job id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("job kind must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("job payload must be a dict")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)):
            raise ValueError("created_at must be a number")
        if self.deadline is not None and (
            isinstance(self.deadline, bool) or not isinstance(self.deadline, (int, float))
        ):
            raise ValueError("deadline must be a number or None")
        if self.max_attempts is not None and (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer or None")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "deadline": self.deadline,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> Job:
        return cls(
            id=document["id"],
            kind=document["kind"],
            payload=dict(document["payload"]),
            created_at=document["created_at"],
            deadline=document.get("deadline"),
            max_attempts=document.get("max_attempts"),
        )


@dataclass(frozen=True)
class JobRecord:
    job: Job
    state: str = "queued"
    attempts: int = 0
    next_run_at: float = 0.0
    result: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r}")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise ValueError("attempts must be an integer")
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.as_dict(),
            "state": self.state,
            "attempts": self.attempts,
            "next_run_at": self.next_run_at,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> JobRecord:
        return cls(
            job=Job.from_dict(document["job"]),
            state=document["state"],
            attempts=document["attempts"],
            next_run_at=document["next_run_at"],
            result=document.get("result"),
            error=document.get("error"),
        )


@dataclass(frozen=True)
class AttemptOutcome:
    state: str
    attempts: int
    result: Any = None
    reason: str | None = None
    replayed: bool = False
