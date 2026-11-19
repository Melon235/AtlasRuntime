"""The shared scheduling layer.

It composes the dependency and resource packages into one deterministic plan. Consumers import from
here, so the package surface is part of the contract.
"""

from workgrid.scheduler.contracts import (
    PlanStatus,
    ReasonCode,
    ScheduledTask,
    SchedulePlan,
    UnschedulableTask,
    build_plan,
)
from workgrid.scheduler.engine import build_schedule
from workgrid.scheduler.report import render_plan

__all__ = [
    "PlanStatus",
    "ReasonCode",
    "SchedulePlan",
    "ScheduledTask",
    "UnschedulableTask",
    "build_plan",
    "build_schedule",
    "render_plan",
]
