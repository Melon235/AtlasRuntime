"""Worker capacity and task demand.

Capacity is per resource and constant over time; *when* a resource is available is the calendar's
question. Keeping the two apart is what lets a plan be checked pool by pool.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from workgrid.resources.errors import (
    DemandExceedsCapacityError,
    InvalidDemandError,
    InvalidPoolError,
    UnknownResourceError,
)


@dataclass(frozen=True)
class ResourcePool:
    """One resource with a constant capacity, e.g. ``workers`` with 2 slots."""

    name: str
    capacity: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise InvalidPoolError("pool name must be a non-empty string", name=self.name)
        if not isinstance(self.capacity, int) or isinstance(self.capacity, bool):
            raise InvalidPoolError("pool capacity must be an integer", pool=self.name)
        if self.capacity < 0:
            raise InvalidPoolError("pool capacity cannot be negative", pool=self.name)


@dataclass(frozen=True)
class PoolSet:
    """Every declared pool, ordered by name."""

    pools: tuple[ResourcePool, ...] = ()

    def __post_init__(self) -> None:
        names = [pool.name for pool in self.pools]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise InvalidPoolError("duplicate pool name", pools=duplicates)
        ordered = tuple(sorted(self.pools, key=lambda pool: pool.name))
        object.__setattr__(self, "pools", ordered)

    def names(self) -> tuple[str, ...]:
        return tuple(pool.name for pool in self.pools)

    def capacity(self, name: str) -> int:
        for pool in self.pools:
            if pool.name == name:
                return pool.capacity
        raise UnknownResourceError(f"no pool named {name!r}", resource=name)

    def has(self, name: str) -> bool:
        return any(pool.name == name for pool in self.pools)

    def total(self) -> int:
        return sum(pool.capacity for pool in self.pools)


@dataclass(frozen=True)
class TaskDemand:
    """How much of each resource one task holds while it runs."""

    task_id: str
    units: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, units in self.units.items():
            if not isinstance(name, str) or not name:
                raise InvalidDemandError(
                    "resource name must be a non-empty string", task=self.task_id
                )
            if not isinstance(units, int) or isinstance(units, bool):
                raise InvalidDemandError(
                    "demand units must be an integer", task=self.task_id, resource=name
                )
            if units < 0:
                raise InvalidDemandError(
                    "demand units cannot be negative", task=self.task_id, resource=name
                )
        object.__setattr__(self, "units", {name: self.units[name] for name in sorted(self.units)})

    def units_for(self, name: str) -> int:
        return int(self.units.get(name, 0))

    def resources(self) -> tuple[str, ...]:
        return tuple(self.units)

    def validate_against(self, pools: PoolSet) -> None:
        """Raise if this demand can never be satisfied, whatever the calendar says."""

        for name in self.units:
            capacity = pools.capacity(name)  # raises UnknownResourceError
            if self.units_for(name) > capacity:
                raise DemandExceedsCapacityError(
                    f"task {self.task_id!r} needs {self.units_for(name)} of {name!r}, "
                    f"pool capacity is {capacity}",
                    task=self.task_id,
                    resource=name,
                    demand=self.units_for(name),
                    capacity=capacity,
                )


def build_pools(spec: Iterable[Mapping[str, object]]) -> PoolSet:
    """Read the ``pools`` list of a plan document."""

    pools: list[ResourcePool] = []
    for index, raw in enumerate(spec):
        if not isinstance(raw, Mapping):
            raise InvalidPoolError("each pool must be a mapping", index=index)
        try:
            name = raw["name"]
            capacity = raw["capacity"]
        except KeyError as exc:
            raise InvalidPoolError(
                "a pool needs 'name' and 'capacity'", index=index, missing=str(exc)
            ) from exc
        pools.append(ResourcePool(name=str(name), capacity=capacity))  # type: ignore[arg-type]
    return PoolSet(tuple(pools))


def build_demand(task_id: str, units: Mapping[str, object] | None) -> TaskDemand:
    """Read the ``demand`` mapping of one task."""

    if not units:
        return TaskDemand(task_id=task_id, units={})
    return TaskDemand(task_id=task_id, units={str(name): units[name] for name in units})  # type: ignore[misc]
