"""Stage F task catalog: eight tasks, four families, one frozen ``Task`` dataclass.

S1 and S2 are the Closure-2 fixtures, reused byte for byte. P1, P2, X1, X2, C1 and C2 are the F2-B
formal fixtures: real multi-module business code authored under ``experiments/stage_f/fixtures`` and
loaded into the frozen dataclass, so the fixture writer, the digests and the sandboxed oracle keep
working unchanged.

The six formal tasks carry a public contract, a visible public suite, an independent hidden oracle,
a reference implementation used only to prove satisfiability, and a human-authored decomposition.
"""

from __future__ import annotations

from experiments.evaluation.closure2 import catalog as closure2_catalog
from experiments.stage_f.tasks.c import C1, C2
from experiments.stage_f.tasks.common import Task
from experiments.stage_f.tasks.p import P1, P2
from experiments.stage_f.tasks.x import X1, X2

FAMILY_OF: dict[str, str] = {
    "closure2-pipeline-migration": "S",
    "closure2-state-machine-refactor": "S",
    "f-p1-connector-migration": "P",
    "f-p2-protocol-adapter": "P",
    "f-x1-api-compat-audit": "X",
    "f-x2-distributed-diagnosis": "X",
    "f-c1-config-lifecycle": "C",
    "f-c2-scheduler-retry": "C",
}

TASKS: tuple[Task, ...] = (
    closure2_catalog.TASKS_BY_ID["closure2-pipeline-migration"],
    closure2_catalog.TASKS_BY_ID["closure2-state-machine-refactor"],
    P1,
    P2,
    X1,
    X2,
    C1,
    C2,
)

TASK_IDS: tuple[str, ...] = tuple(task.task_id for task in TASKS)
TASKS_BY_ID: dict[str, Task] = {task.task_id: task for task in TASKS}

#: The human-authored decomposition used by A2, one record per task. It is frozen into the plan and
#: disclosed in the report; it never reaches A0, A1 or A3.
STATIC_DAG: dict[str, tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]] = {
    task_id: tuple(
        (
            str(worker["task_id"]),
            tuple(worker["owned_paths"]),
            tuple(worker["depends_on"]),
        )
        for worker in task.workers
    )
    for task_id, task in TASKS_BY_ID.items()
}


def task_by_id(task_id: str) -> Task:
    if task_id not in TASKS_BY_ID:
        raise KeyError(f"unknown Stage F task {task_id!r}")
    return TASKS_BY_ID[task_id]


def family_of(task_id: str) -> str:
    return FAMILY_OF[task_id]


__all__ = [
    "FAMILY_OF",
    "STATIC_DAG",
    "TASKS",
    "TASKS_BY_ID",
    "TASK_IDS",
    "Task",
    "family_of",
    "task_by_id",
]
