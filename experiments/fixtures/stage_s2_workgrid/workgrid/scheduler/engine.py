"""The shared scheduling engine.

This is the only module that knows a dependency graph and a resource calendar exist at the same
time. It reads from :mod:`workgrid.dependencies` and :mod:`workgrid.resources`; it must not change
either of them, and it must not re-implement their rules.

The contract, in the order it is applied per task:

1. **Order.** Visit tasks in :func:`workgrid.dependencies.ordering.topological_order` order, so a
   task is always considered after its dependencies.
2. **Blocked dependencies.** If any dependency is already unschedulable, this task is unschedulable
   with :attr:`~workgrid.scheduler.contracts.ReasonCode.DEPENDENCY_UNSCHEDULABLE`. Its own demand
   and window are not examined further.
3. **Impossible demand.** ``ResourceConstraints.validate_demand(demand)`` raises
   :class:`~workgrid.resources.errors.DemandExceedsCapacityError` or
   :class:`~workgrid.resources.errors.UnknownResourceError`; the task is unschedulable with the
   matching :class:`~workgrid.scheduler.contracts.ReasonCode`. These are *not* propagated as
   exceptions: one impossible task does not invalidate the plan.
4. **Placement.** ``ready = max(end of the scheduled dependencies, 0)``. Starting from
   ``ResourceConstraints.earliest_start(ready, duration)``, take the first candidate where
   ``ResourceConstraints.fits_at(candidate, duration, demand, placed)`` is true. When a candidate is
   too crowded, advance to the next open slot (``earliest_start(candidate + 1, duration)``) and try
   again. If candidates run out, the task is unschedulable with
   :attr:`~workgrid.scheduler.contracts.ReasonCode.NO_AVAILABLE_WINDOW`.
5. **Report.** Return :func:`~workgrid.scheduler.contracts.build_plan` with the scheduled tasks, the
   unschedulable tasks, ``peak_usage`` from ``ResourceConstraints.peak_usage(placed)`` and
   ``summary["usable_slots"]`` from ``Calendar.usable_slots()``.

A broken graph is different: :class:`~workgrid.dependencies.errors.CycleDetectedError` and
:class:`~workgrid.dependencies.errors.UnknownDependencyError` come from the dependency package
before any placement happens, and must propagate unchanged.
"""

from __future__ import annotations

from workgrid.dependencies.graph import TaskGraph
from workgrid.resources.constraints import ResourceConstraints
from workgrid.scheduler.contracts import SchedulePlan


def build_schedule(graph: TaskGraph, constraints: ResourceConstraints) -> SchedulePlan:
    """Place every task that can be placed, or report why it cannot be."""

    raise NotImplementedError("the scheduling engine is not implemented yet")
