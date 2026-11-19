"""Putting capacity and the calendar together.

A placement is a triple ``(start, end, units)``. Nothing here schedules anything; the functions
answer "would this task fit if it started now?" without mutating their inputs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from workgrid.resources.calendar import Calendar
from workgrid.resources.capacity import PoolSet, TaskDemand

Placement = tuple[int, int, Mapping[str, int]]


@dataclass(frozen=True)
class ResourceConstraints:
    """The immutable rules a plan must satisfy."""

    pools: PoolSet
    calendar: Calendar
    resource_names: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.resource_names:
            object.__setattr__(self, "resource_names", self.pools.names())

    def validate_demand(self, demand: TaskDemand) -> None:
        """Raise when the demand can never be satisfied, whatever the start time is."""

        demand.validate_against(self.pools)

    def earliest_start(self, not_before: int, duration: int) -> int | None:
        return self.calendar.earliest_start(not_before, duration)

    def usage_at(self, slot: int, placed: Iterable[Placement]) -> dict[str, int]:
        """Units of each resource held during ``slot`` by the placements that cover it."""

        usage = {name: 0 for name in self.resource_names}
        for start, end, units in placed:
            if start <= slot < end:
                for name, amount in units.items():
                    usage[name] = usage.get(name, 0) + int(amount)
        return usage

    def fits_at(
        self,
        start: int,
        duration: int,
        demand: TaskDemand,
        placed: Iterable[Placement],
    ) -> bool:
        """True when ``demand`` can be added for ``duration`` slots starting at ``start``."""

        if not self.calendar.fits(start, duration):
            return False
        placed = tuple(placed)
        for slot in range(start, start + duration):
            usage = self.usage_at(slot, placed)
            for name, amount in demand.units.items():
                if usage.get(name, 0) + amount > self.pools.capacity(name):
                    return False
        return True

    def peak_usage(self, placed: Iterable[Placement]) -> dict[str, int]:
        """The highest concurrent usage of each resource over the whole plan."""

        placed = tuple(placed)
        peak = {name: 0 for name in self.resource_names}
        for start, end, _ in placed:
            for slot in range(start, end):
                usage = self.usage_at(slot, placed)
                for name, amount in usage.items():
                    peak[name] = max(peak[name], amount)
        return peak
