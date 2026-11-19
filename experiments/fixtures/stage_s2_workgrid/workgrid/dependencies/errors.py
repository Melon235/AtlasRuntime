"""Failure vocabulary for the dependency package.

Every failure a caller can get is a distinct type, and every type carries a stable ``code``.
The scheduling engine turns some of them into unschedulable entries; the rest stay exceptions,
because a cycle or a dangling reference is a broken plan, not a task that cannot be placed.
"""

from __future__ import annotations

from typing import ClassVar


class DependencyError(Exception):
    """Base class for everything this package raises."""

    code: ClassVar[str] = "dependency_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = dict(context)

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.context}


class InvalidTaskError(DependencyError):
    """A task specification is malformed: empty id, non-positive duration, bad dependency list."""

    code: ClassVar[str] = "invalid_task"


class DuplicateTaskError(DependencyError):
    """Two tasks share one id."""

    code: ClassVar[str] = "duplicate_task"


class UnknownDependencyError(DependencyError):
    """A task depends on an id that is not in the graph."""

    code: ClassVar[str] = "unknown_dependency"


class CycleDetectedError(DependencyError):
    """The dependency graph is not acyclic; ``cycle`` is the deterministic witness."""

    code: ClassVar[str] = "cycle_detected"

    def __init__(self, cycle: tuple[str, ...]) -> None:
        super().__init__(
            "dependency cycle: " + " -> ".join(cycle),
            cycle=list(cycle),
        )
        self.cycle = cycle
