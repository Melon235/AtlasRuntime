"""Failure vocabulary for the resource package.

The split matters for the scheduler: a *malformed* resource rule is an exception, while a task that
merely asks for more than the pool can ever give is classified as unschedulable.
"""

from __future__ import annotations

from typing import ClassVar


class ResourceError(Exception):
    """Base class for everything this package raises."""

    code: ClassVar[str] = "resource_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = dict(context)

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.context}


class InvalidPoolError(ResourceError):
    """A pool is malformed: empty name, negative or non-integer capacity, duplicate name."""

    code: ClassVar[str] = "invalid_pool"


class InvalidWindowError(ResourceError):
    """An availability window is malformed: negative start, end not after start, overlapping."""

    code: ClassVar[str] = "invalid_window"


class InvalidDemandError(ResourceError):
    """A task demand is malformed: negative units, or units that are not integers."""

    code: ClassVar[str] = "invalid_demand"


class UnknownResourceError(ResourceError):
    """A task demands a resource that no pool declares."""

    code: ClassVar[str] = "unknown_resource"


class DemandExceedsCapacityError(ResourceError):
    """A task demands more units of one resource than the pool can ever supply."""

    code: ClassVar[str] = "demand_exceeds_capacity"
