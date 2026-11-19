"""The six Stage B tasks, in matrix order.

Two local tasks, two with independent investigations, two tightly coupled. The order is the one the
protocol uses; it carries no meaning beyond that.
"""

from __future__ import annotations

from . import cascade, ledgerclose, pager, retrygate, slugkit, zonelint
from .base import TaskSpec

SPECS: tuple[TaskSpec, ...] = (
    slugkit.SPEC,
    pager.SPEC,
    zonelint.SPEC,
    ledgerclose.SPEC,
    retrygate.SPEC,
    cascade.SPEC,
)

BY_ID: dict[str, TaskSpec] = {spec.task_id: spec for spec in SPECS}

TASK_IDS: tuple[str, ...] = tuple(spec.task_id for spec in SPECS)

STRUCTURES: tuple[str, ...] = ("LOCAL_DIRECT", "INDEPENDENT_INVESTIGATION", "COUPLED_UNCERTAIN")

#: Tasks from earlier stages. No Stage B task may share a name, a goal or a byte with them.
HISTORICAL_TASKS: tuple[str, ...] = ("calculator", "eventpipe", "releaseguard", "workgrid")

__all__ = ["BY_ID", "HISTORICAL_TASKS", "SPECS", "STRUCTURES", "TASK_IDS", "TaskSpec"]
