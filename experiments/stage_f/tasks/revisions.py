"""Explicit task revisions. The Stage F catalog continues to resolve revision 1."""

from __future__ import annotations

from dataclasses import replace

from experiments.stage_f.tasks import Task, task_by_id
from experiments.stage_f.tasks.fixtures import FIXTURE_ROOT, _read_tree

C1 = "f-c1-config-lifecycle"
X2 = "f-x2-distributed-diagnosis"
REVISIONS = {C1: 2, X2: 2}

_OVERLAYS = {C1: "c1-r2", X2: "x2-r2"}
_REFERENCE_FILES = {C1: {"configkit/profile.py", "configkit/validate.py"}, X2: set()}
_ASSIGNMENTS = {
    C1: {
        "sources": "Follow the documented exact prefix and ParseError code and path rules.",
        "resolved": "Implement the complete preset table, independent copies and TLS error paths "
        "from docs/INTERFACE.md.",
    },
    X2: {
        "queue": "Reject non-Event arguments and persist the updated nack attempt count before "
        "returning, as specified in docs/INTERFACE.md.",
        "storage": "A reopened journal must expose the latest nack attempt count before redelivery.",
        "recovery": "Preserve the journal's latest attempt count even before any redelivery.",
    },
}


def task_at_revision(task_id: str, revision: int) -> Task:
    historical = task_by_id(task_id)
    if revision == 1:
        return historical
    if REVISIONS.get(task_id) != revision:
        raise KeyError(f"unknown contract revision {revision} for {task_id}")

    root = FIXTURE_ROOT / _OVERLAYS[task_id]
    seed = _read_tree(root / "seed")
    reference = _read_tree(root / "reference")
    oracle = _read_tree(root / "oracle")
    if set(seed) != {"docs/INTERFACE.md"} or set(oracle) != set(historical.oracle_tests):
        raise ValueError(f"missing or unexpected contract files for {task_id} revision {revision}")
    if set(reference) != _REFERENCE_FILES[task_id]:
        raise ValueError(f"missing or unexpected reference files for {task_id} revision {revision}")
    workers = []
    for worker in historical.workers:
        note = _ASSIGNMENTS[task_id].get(worker["task_id"])
        workers.append(
            {**worker, "assignment": f"{worker['assignment']}\n\n{note}"} if note else worker
        )
    return replace(
        historical,
        seed={**historical.seed, **seed},
        reference={**historical.reference, **reference},
        oracle_tests=oracle,
        workers=tuple(workers),
    )
