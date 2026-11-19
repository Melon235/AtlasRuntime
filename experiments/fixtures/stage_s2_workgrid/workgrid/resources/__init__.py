"""Resource rules: capacity, availability windows, and the constraints they imply.

Like the dependency package, this one knows nothing about scheduling. It answers questions about
one task and one placement at a time.
"""

from workgrid.resources.calendar import AvailabilityWindow, Calendar, build_calendar
from workgrid.resources.capacity import PoolSet, ResourcePool, TaskDemand, build_demand, build_pools
from workgrid.resources.constraints import Placement, ResourceConstraints
from workgrid.resources.errors import (
    DemandExceedsCapacityError,
    InvalidDemandError,
    InvalidPoolError,
    InvalidWindowError,
    ResourceError,
    UnknownResourceError,
)

__all__ = [
    "AvailabilityWindow",
    "Calendar",
    "DemandExceedsCapacityError",
    "InvalidDemandError",
    "InvalidPoolError",
    "InvalidWindowError",
    "Placement",
    "PoolSet",
    "ResourceConstraints",
    "ResourceError",
    "ResourcePool",
    "TaskDemand",
    "UnknownResourceError",
    "build_calendar",
    "build_demand",
    "build_pools",
]
