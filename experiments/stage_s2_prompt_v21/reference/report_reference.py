"""Rendering a schedule as text.

The text form exists so a plan can be pasted into a build log and diffed between two runs. It is
derived from the plan only -- it never recomputes anything.
"""

from __future__ import annotations

from workgrid.scheduler.contracts import SchedulePlan


def render_plan(plan: SchedulePlan) -> str:
    """Return the deterministic text form described in the module docstring."""

    lines = [f"plan {str(plan.status)} makespan={plan.makespan}"]
    for task in plan.tasks:
        usage = ",".join(f"{name}={int(task.units[name])}" for name in sorted(task.units))
        lines.append(f"{task.start:04d}-{task.end:04d} {task.task_id} {usage or '-'}")
    if plan.unschedulable:
        lines.append("unschedulable " + ",".join(item.task_id for item in plan.unschedulable))
    return "\n".join(lines) + "\n"


__all__ = ["render_plan"]
