"""Explicit task revisions. The Stage F catalog continues to resolve revision 1."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from experiments.evaluation.closure2.catalog import task_digests
from experiments.stage_b_plus.authority import digest_of
from experiments.stage_f.tasks import Task, task_by_id
from experiments.stage_f.tasks.fixtures import FIXTURE_ROOT, _read_tree

C1 = "f-c1-config-lifecycle"
P2 = "f-p2-protocol-adapter"
X1 = "f-x1-api-compat-audit"
X2 = "f-x2-distributed-diagnosis"
REVISIONS = {C1: 2, P2: 2, X1: 2, X2: 2}

_OVERLAYS = {C1: "c1-r2", P2: "p2-r2", X1: "x1-r2", X2: "x2-r2"}
_REFERENCE_FILES = {C1: {"configkit/profile.py", "configkit/validate.py"}}
_ASSIGNMENTS = {
    C1: {
        "sources": "Follow the documented exact prefix and ParseError code and path rules.",
        "resolved": "Implement the complete preset table, independent copies and TLS error paths "
        "from docs/INTERFACE.md.",
    },
    P2: {"json": "Apply the documented JSON v1 ID default, preservation and type rules."},
    X1: {
        "billing": "Use the real audit API with keyword arguments and keep settlement on ledger.post.",
        "reports": "Use the real audit API with keyword arguments, including through wrappers.",
        "surfaces": "Use the real audit API and the injected notification sink with keyword arguments.",
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
    if set(reference) != _REFERENCE_FILES.get(task_id, set()):
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


def task_identity(task: Task, revision: int) -> dict[str, Any]:
    digests = task_digests(task)
    digests["public_specification_digest"] = digest_of(task.seed["docs/INTERFACE.md"])
    return {
        "revision": revision,
        **digests,
        "task_digest": digest_of({"task_id": task.task_id, "revision": revision, **digests}),
    }
