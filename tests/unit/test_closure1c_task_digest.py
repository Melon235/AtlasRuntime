"""Closure-1C: the effective public task digest, and the fixture digest it must not disturb.

The historical ``BenchmarkTask.fixture_digest`` is frozen: it predates multi-worker tasks and does
not cover ``WorkerStep.shared_context``. Reworking it would invalidate every recorded run identity,
so the fix is additive — a second, versioned digest over the *effective public input* — and these
tests hold both halves in place.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from experiments.evaluation.final4.runner_entry import build_task
from experiments.evaluation.final4.validation_tasks import TASKS
from experiments.evaluation.models import (
    EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION,
    BenchmarkTask,
    WorkerStep,
    effective_public_task_digest,
    effective_public_task_payload,
)

SERIAL = next(task for task in TASKS if task.task_id == "validation-serial")
PARALLEL = next(task for task in TASKS if task.task_id == "validation-parallel")

#: Captured from the frozen revision-3 task set before this stage. If these move, historical run
#: identities no longer mean what they said.
FROZEN_FIXTURE_DIGESTS = {
    (
        "validation-serial",
        False,
    ): "86e9c4241c55c44fb676aed69224903141bb417bac1bd8b3c71389030c5edb38",
    ("validation-serial", True): "d78c8254d8bafe20b08a21c4e629d21133e9b9280ba0433487e51edc362aebc3",
    (
        "validation-parallel",
        False,
    ): "0b7c5af1e439939b2ec0bd93a0d301324e62d7cb86d877ef1147b4542a08cfd4",
    (
        "validation-parallel",
        True,
    ): "927076e53cb91a637b270f877a10021a773c681ca7aebbdbdd301b873c556f86",
}


def _first_step(task: BenchmarkTask) -> WorkerStep:
    return task.workers[0]


def test_the_digest_is_stable_across_two_computations() -> None:
    task = build_task(SERIAL, multi=True)
    assert effective_public_task_digest(task) == effective_public_task_digest(task)
    assert task.effective_public_task_digest == effective_public_task_digest(task)


def test_the_digest_is_stable_across_two_constructions() -> None:
    first = build_task(PARALLEL, multi=True)
    second = build_task(PARALLEL, multi=True)
    assert first.effective_public_task_digest == second.effective_public_task_digest


def test_changing_shared_context_changes_the_digest_but_not_the_fixture_digest() -> None:
    task = build_task(SERIAL, multi=True)
    changed = replace(
        task,
        workers=tuple(
            replace(worker, shared_context=worker.shared_context + "\nEXTRA PUBLIC RULE")
            for worker in task.workers
        ),
    )
    assert changed.effective_public_task_digest != task.effective_public_task_digest
    # The historical digest is blind to shared context — that is the defect, recorded not denied.
    assert changed.fixture_digest == task.fixture_digest


def test_changing_a_worker_objective_changes_the_digest() -> None:
    task = build_task(SERIAL, multi=True)
    changed = replace(
        task,
        workers=(
            replace(_first_step(task), prompt=_first_step(task).prompt + "\nDo it differently."),
            *task.workers[1:],
        ),
    )
    assert changed.effective_public_task_digest != task.effective_public_task_digest


def test_changing_a_declared_dependency_changes_the_digest() -> None:
    task = build_task(SERIAL, multi=True)
    changed = replace(
        task,
        workers=(
            task.workers[0],
            replace(task.workers[1], depends_on=()),
        ),
    )
    assert changed.effective_public_task_digest != task.effective_public_task_digest


def test_changing_the_hidden_tests_does_not_change_the_public_digest() -> None:
    task = build_task(SERIAL, multi=True)
    changed = replace(task, hidden_files={"test_hidden.py": "def test_anything():\n    pass\n"})
    assert changed.effective_public_task_digest == task.effective_public_task_digest
    # ...and the private payload really is excluded, not merely equal by accident.
    assert "test_hidden" not in str(effective_public_task_payload(task))


def test_changing_a_visible_file_changes_the_digest() -> None:
    task = build_task(SERIAL, multi=True)
    changed = replace(task, visible_files={**task.visible_files, "extra.py": "x = 1\n"})
    assert changed.effective_public_task_digest != task.effective_public_task_digest


def test_changing_a_public_execution_constraint_changes_the_digest() -> None:
    task = build_task(SERIAL, multi=True)
    changed = replace(task, max_turns=task.max_turns + 1)
    assert changed.effective_public_task_digest != task.effective_public_task_digest


@pytest.mark.parametrize(("task_id", "multi"), sorted(FROZEN_FIXTURE_DIGESTS))
def test_the_historical_fixture_digest_is_byte_identical(task_id: str, multi: bool) -> None:
    task = next(item for item in TASKS if item.task_id == task_id)
    assert build_task(task, multi=multi).fixture_digest == FROZEN_FIXTURE_DIGESTS[(task_id, multi)]


def test_the_digest_is_versioned() -> None:
    task = build_task(SERIAL, multi=True)
    payload = effective_public_task_payload(task)
    assert payload["version"] == EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION
    assert EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION == "effective-public-task-1"


def test_atlas_and_native_share_one_digest_for_one_task() -> None:
    """Both arms call the same function on the same task object; that is the contract."""

    for task in (SERIAL, PARALLEL):
        atlas_side = build_task(task, multi=True)
        native_side = build_task(task, multi=True)
        assert atlas_side.effective_public_task_digest == native_side.effective_public_task_digest

    # A single-worker arm of the same sample is a different public input, and must say so.
    assert (
        build_task(SERIAL, multi=False).effective_public_task_digest
        != build_task(SERIAL, multi=True).effective_public_task_digest
    )


def test_the_single_worker_arm_carries_the_shared_public_context_too() -> None:
    """The single arm's whole-task turn already contains the shared block, so nothing is missing."""

    single = build_task(SERIAL, multi=False)
    payload = effective_public_task_payload(single)
    assert payload["workers"][0]["shared_context"] == ""
    assert SERIAL.shared_context.strip()[:40] in payload["prompt"]
