"""The published shape of a schedule.

A plan is read by tools, diffed between runs and stored next to a build, so this module is the
stable part: status values, reason codes, the dataclasses themselves, and the canonical JSON form.
Nothing here decides anything -- :mod:`workgrid.scheduler.engine` does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class PlanStatus(StrEnum):
    """Overall outcome of one scheduling run."""

    SCHEDULED = "scheduled"
    PARTIAL = "partial"
    EMPTY = "empty"


class ReasonCode(StrEnum):
    """Why one task could not be placed."""

    DEMAND_EXCEEDS_CAPACITY = "demand_exceeds_capacity"
    UNKNOWN_RESOURCE = "unknown_resource"
    NO_AVAILABLE_WINDOW = "no_available_window"
    DEPENDENCY_UNSCHEDULABLE = "dependency_unschedulable"


@dataclass(frozen=True)
class ScheduledTask:
    """One task placed on the timeline."""

    task_id: str
    start: int
    end: int
    units: Mapping[str, int] = field(default_factory=dict)

    @property
    def duration(self) -> int:
        return self.end - self.start

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "start": self.start,
            "end": self.end,
            "units": {name: int(self.units[name]) for name in sorted(self.units)},
        }


@dataclass(frozen=True)
class UnschedulableTask:
    """One task that has no valid placement, with a machine-readable reason."""

    task_id: str
    reason: str
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"task_id": self.task_id, "reason": str(self.reason), "detail": self.detail}


@dataclass(frozen=True)
class SchedulePlan:
    """A complete, deterministic answer for one plan document."""

    status: PlanStatus
    tasks: tuple[ScheduledTask, ...] = ()
    unschedulable: tuple[UnschedulableTask, ...] = ()
    makespan: int = 0
    peak_usage: Mapping[str, int] = field(default_factory=dict)
    summary: Mapping[str, int] = field(default_factory=dict)

    def scheduled_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks)

    def unschedulable_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.unschedulable)

    def to_dict(self) -> dict[str, object]:
        """The canonical form: sorted keys, sorted resources, no sets, no floats."""

        return {
            "status": str(self.status),
            "makespan": int(self.makespan),
            "summary": {name: int(self.summary[name]) for name in sorted(self.summary)},
            "peak_usage": {name: int(self.peak_usage[name]) for name in sorted(self.peak_usage)},
            "tasks": [task.as_dict() for task in self.tasks],
            "unschedulable": [item.as_dict() for item in self.unschedulable],
        }


def build_plan(
    tasks: Iterable[ScheduledTask],
    unschedulable: Iterable[UnschedulableTask],
    *,
    peak_usage: Mapping[str, int] | None = None,
    summary: Mapping[str, int] | None = None,
) -> SchedulePlan:
    """Assemble a plan and derive the status, the makespan and the summary.

    Ordering is fixed here so every caller -- engine, report, tests -- sees the same plan:
    scheduled tasks by ``(start, task_id)``, unschedulable tasks by ``task_id``.
    """

    ordered = tuple(sorted(tasks, key=lambda task: (task.start, task.task_id)))
    blocked = tuple(sorted(unschedulable, key=lambda item: item.task_id))
    makespan = max((task.end for task in ordered), default=0)
    if not ordered and not blocked:
        status = PlanStatus.EMPTY
    elif blocked:
        status = PlanStatus.PARTIAL
    else:
        status = PlanStatus.SCHEDULED
    derived = {
        "tasks": len(ordered),
        "scheduled": len(ordered),
        "unschedulable": len(blocked),
        "makespan": int(makespan),
    }
    if summary:
        derived.update({name: int(value) for name, value in summary.items()})
    return SchedulePlan(
        status=status,
        tasks=ordered,
        unschedulable=blocked,
        makespan=makespan,
        peak_usage={name: int(peak_usage[name]) for name in sorted(peak_usage or {})},
        summary={name: int(derived[name]) for name in sorted(derived)},
    )


__all__ = [
    "PlanStatus",
    "ReasonCode",
    "SchedulePlan",
    "ScheduledTask",
    "UnschedulableTask",
    "build_plan",
]
