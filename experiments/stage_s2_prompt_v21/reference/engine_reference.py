"""The shared scheduling engine.

This is the only module that knows a dependency graph and a resource calendar exist at the same
time. It reads from :mod:`workgrid.dependencies` and :mod:`workgrid.resources`; it must not change
either of them, and it must not re-implement their rules.
"""

from __future__ import annotations

from workgrid.dependencies.graph import TaskGraph
from workgrid.dependencies.ordering import topological_order
from workgrid.resources.capacity import TaskDemand
from workgrid.resources.constraints import ResourceConstraints
from workgrid.resources.errors import DemandExceedsCapacityError, UnknownResourceError
from workgrid.scheduler.contracts import (
    ReasonCode,
    ScheduledTask,
    SchedulePlan,
    UnschedulableTask,
    build_plan,
)


def build_schedule(graph: TaskGraph, constraints: ResourceConstraints) -> SchedulePlan:
    """Place every task that can be placed, or report why it cannot be."""

    order = topological_order(graph)

    scheduled: list[ScheduledTask] = []
    blocked: list[UnschedulableTask] = []
    placed: list[tuple[int, int, dict[str, int]]] = []
    ends: dict[str, int] = {}
    failed: dict[str, str] = {}

    for task_id in order:
        node = graph.node(task_id)
        parents = graph.dependencies(task_id)

        blocked_parents = sorted(parent for parent in parents if parent in failed)
        if blocked_parents:
            blocked.append(
                UnschedulableTask(
                    task_id=task_id,
                    reason=ReasonCode.DEPENDENCY_UNSCHEDULABLE,
                    detail=f"dependency {blocked_parents[0]!r} is unschedulable",
                )
            )
            failed[task_id] = str(ReasonCode.DEPENDENCY_UNSCHEDULABLE)
            continue

        demand = TaskDemand(task_id=task_id, units=dict(node.demand))
        try:
            constraints.validate_demand(demand)
        except DemandExceedsCapacityError as error:
            blocked.append(
                UnschedulableTask(
                    task_id=task_id,
                    reason=ReasonCode.DEMAND_EXCEEDS_CAPACITY,
                    detail=error.message,
                )
            )
            failed[task_id] = str(ReasonCode.DEMAND_EXCEEDS_CAPACITY)
            continue
        except UnknownResourceError as error:
            blocked.append(
                UnschedulableTask(
                    task_id=task_id,
                    reason=ReasonCode.UNKNOWN_RESOURCE,
                    detail=error.message,
                )
            )
            failed[task_id] = str(ReasonCode.UNKNOWN_RESOURCE)
            continue

        ready = max((ends[parent] for parent in parents if parent in ends), default=0)
        start = constraints.earliest_start(ready, node.duration)
        while start is not None and not constraints.fits_at(start, node.duration, demand, placed):
            start = constraints.earliest_start(start + 1, node.duration)

        if start is None:
            blocked.append(
                UnschedulableTask(
                    task_id=task_id,
                    reason=ReasonCode.NO_AVAILABLE_WINDOW,
                    detail="no open window can hold the task",
                )
            )
            failed[task_id] = str(ReasonCode.NO_AVAILABLE_WINDOW)
            continue

        end = start + node.duration
        scheduled.append(
            ScheduledTask(
                task_id=task_id,
                start=start,
                end=end,
                units=dict(demand.units),
            )
        )
        placed.append((start, end, dict(demand.units)))
        ends[task_id] = end

    return build_plan(
        scheduled,
        blocked,
        peak_usage=constraints.peak_usage(placed),
        summary={"usable_slots": constraints.calendar.usable_slots()},
    )


__all__ = ["build_schedule"]
