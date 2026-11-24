# ruff: noqa: E501
"""Closure-2 S07: the candidate 54-cell formal matrix, offline and **not** authorized.

What this module is:

* an **order generator** — the frozen matrix is six arms x three tasks x three repetitions = 54
  executions, and this module fixes the interleaving rule so the order is reproducible instead of
  being decided on the day of the run;
* a **plan identity** — three repetition-scoped ``PilotPlan`` objects, each digestible and each
  approval-independent, built with the same dataclass the live entry point uses, so a digest computed
  here is the digest the executed plan would have;
* an **offline probe** — ``authorization_probe()`` runs the *shipped* gate against the candidate and
  reports the refusal, which is how this stage proves "prepared, not authorized" instead of asserting
  it.

What this module is not:

* not a new execution entry point. ``pilot_runner.plan_scope_ids()`` still returns exactly two plans,
  so nothing here can be dispatched from the CLI;
* not a re-freeze. The three plan digests are candidates until a later stage writes an approval that
  names them; this module writes no approval and mutates no record;
* not a second scheduler. The cell order is a list; running it is still ``run_live_pilot``'s job.

Why three plans rather than one plan with three repetitions: ``pilot_ledger.PilotLedger`` holds one
claim row per *plan digest* (``pilot_claims.plan_digest`` is the primary key) and never releases it.
A single 54-execution plan with ``repetitions=(0, 1, 2)`` could therefore only ever be dispatched
once, and ``run_live_pilot`` takes one repetition per call. Three repetition-scoped plans keep the
one-shot lease intact without changing the ledger's semantics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.pilot_runner import (
    ARM_ORDER,
    TASK_ORDER,
    Cell,
    PilotPlan,
    pilot_authorization_gate,
)
from experiments.evaluation.closure2.protocol import (
    BENCHMARK_ID,
    C2A_R1_PREREGISTRATION_DIGEST,
    EXECUTION_ORDER,
    preregistration_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "formal-plan-candidate.json"

#: Candidate identity. It is inside every plan digest, so settling it is part of freezing the plan.
FORMAL_PLAN_REVISION = "closure-2-s07-candidate-1"

#: Three paired repetitions per cell, as the preregistration's minimum.
FORMAL_REPETITIONS: tuple[int, ...] = (0, 1, 2)

#: Six arms, so a rotation of two places gives every arm three distinct positions across the three
#: repetitions and every position three distinct arms. No RNG is involved, which is stronger than a
#: recorded seed: the order cannot drift because nobody drew it.
FORMAL_ARM_ROTATION = 2

CELLS_PER_REPETITION = len(TASK_ORDER) * len(ARM_ORDER)
TOTAL_EXECUTIONS = CELLS_PER_REPETITION * len(FORMAL_REPETITIONS)

#: Advisory per-repetition ceiling. See ``docs/closure-2-s07-formal-evaluation-prep.md`` §I for the
#: estimate it is derived from. It is inside the plan digest, so approving the digest approves it.
FORMAL_COST_CEILING_USD = 1.00

#: The plan id prefix. ``formal_plan_id(r)`` is the id of repetition ``r``'s scope.
FORMAL_PLAN_ID_PREFIX = "closure2-formal-r"


class FormalPlanError(RuntimeError):
    """The candidate plan does not describe the frozen matrix. Never downgraded to a warning."""


def formal_plan_id(repetition: int) -> str:
    """The plan id of one repetition. Repetitions are 1-based in the id, 0-based in the payload."""

    _check_repetition(repetition)
    return f"{FORMAL_PLAN_ID_PREFIX}{repetition + 1}"


def _check_repetition(repetition: int) -> None:
    if repetition not in FORMAL_REPETITIONS:
        raise FormalPlanError(
            f"repetition {repetition!r} is not one of the three planned repetitions "
            f"{list(FORMAL_REPETITIONS)}"
        )


def rotated_arms(repetition: int) -> tuple[str, ...]:
    """The arm order used inside every task block of ``repetition``.

    ``ARM_ORDER`` rotated left by ``2 * repetition``: repetition 0 starts at ``N0``, repetition 1 at
    ``A0``, repetition 2 at ``A1-P``. Every arm keeps its frozen neighbours, only the start moves.
    """

    _check_repetition(repetition)
    offset = (FORMAL_ARM_ROTATION * repetition) % len(ARM_ORDER)
    return ARM_ORDER[offset:] + ARM_ORDER[:offset]


def repetition_cells(repetition: int) -> tuple[Cell, ...]:
    """The 18 cells of one repetition: tasks are the outer loop, arms the rotated inner loop.

    Tasks stay in frozen order. That is the preregistration's reading of "tasks x repetitions are the
    outer loop"; rotating the task order as well would control time-of-day drift better and is
    recorded as an owner decision rather than silently applied.
    """

    arms = rotated_arms(repetition)
    return tuple(Cell(task_id, arm_id) for task_id in TASK_ORDER for arm_id in arms)


def formal_plan(repetition: int) -> PilotPlan:
    """One repetition as an approvable scope. Identical in shape to the pilot and routing plans."""

    _check_repetition(repetition)
    return PilotPlan(
        plan_id=formal_plan_id(repetition),
        benchmark_id=BENCHMARK_ID,
        preregistration_digest=C2A_R1_PREREGISTRATION_DIGEST,
        runner_revision=FORMAL_PLAN_REVISION,
        cells=tuple(cell.cell_id for cell in repetition_cells(repetition)),
        repetitions=(repetition,),
        max_cell_executions=CELLS_PER_REPETITION,
        estimated_cost_ceiling_usd=FORMAL_COST_CEILING_USD,
    )


def candidate_approval(repetition: int) -> dict[str, Any]:
    """The record an owner would have to replace. Pending by construction, on purpose.

    It names the digest it would authorize, so the gap is a status and a signature — never an
    ambiguous "some approval exists". ``pilot_authorization_gate`` refuses it for exactly that reason.
    """

    plan = formal_plan(repetition)
    return {
        "status": "PENDING_OWNER_APPROVAL",
        "approved_plan_digest": plan.digest(),
        "approved_by": None,
        "approval_commit": None,
        "approved_at": None,
    }


def authorization_probe(repetition: int) -> dict[str, Any]:
    """Run the shipped gate against the candidate. The expected answer is a refusal.

    This is the whole offline claim: the plan is complete enough to be approved and still cannot run,
    because the approval layer — not this module — decides that.
    """

    cells = repetition_cells(repetition)
    gate = pilot_authorization_gate(
        cells=cells,
        repetition_id=repetition,
        plan=formal_plan(repetition),
        record=candidate_approval(repetition),
    )
    return {
        "plan_id": formal_plan_id(repetition),
        "plan_digest": gate["plan_digest"],
        "execution_ready": gate["execution_ready"],
        "problems": list(gate["problems"]),
        "cells": len(cells),
        "repetition": repetition,
    }


def _arm_counts(cells: tuple[Cell, ...]) -> dict[str, int]:
    counts = {arm: 0 for arm in ARM_ORDER}
    for cell in cells:
        counts[cell.arm_id] = counts.get(cell.arm_id, 0) + 1
    return counts


def check_candidate() -> list[str]:
    """Everything the candidate must satisfy before it can be proposed for approval."""

    problems: list[str] = []
    seen_cells = 0
    digests: dict[str, str] = {}
    for repetition in FORMAL_REPETITIONS:
        cells = repetition_cells(repetition)
        plan = formal_plan(repetition)
        if len(cells) != CELLS_PER_REPETITION:
            problems.append(
                f"repetition {repetition}: {len(cells)} cells, not {CELLS_PER_REPETITION}"
            )
        if len({cell.cell_id for cell in cells}) != len(cells):
            problems.append(f"repetition {repetition} lists the same cell twice")
        for task_id in TASK_ORDER:
            for arm_id in ARM_ORDER:
                if Cell(task_id, arm_id).cell_id not in {cell.cell_id for cell in cells}:
                    problems.append(f"repetition {repetition} is missing {task_id}::{arm_id}")
        counts = _arm_counts(cells)
        if set(counts.values()) != {len(TASK_ORDER)}:
            problems.append(f"repetition {repetition}: arms are not balanced: {counts}")
        if plan.cells != tuple(cell.cell_id for cell in cells):
            problems.append(f"repetition {repetition}: plan cells do not match the generated order")
        if plan.repetitions != (repetition,):
            problems.append(f"repetition {repetition}: plan authorizes the wrong repetition")
        if preregistration_digest() != plan.preregistration_digest:
            problems.append(
                f"repetition {repetition}: pinned to a different preregistration digest"
            )
        digest = plan.digest()
        if digest in digests:
            problems.append(f"repetitions {digests[digest]} and {repetition} share one plan digest")
        digests[digest] = str(repetition)
        seen_cells += len(cells)
    if seen_cells != TOTAL_EXECUTIONS:
        problems.append(f"{seen_cells} executions planned, {TOTAL_EXECUTIONS} expected")
    for repetition in FORMAL_REPETITIONS:
        position = {arm: rotated_arms(repetition).index(arm) for arm in ARM_ORDER}
        if sorted(position.values()) != list(range(len(ARM_ORDER))):
            problems.append(f"repetition {repetition}: the rotation is not a permutation")
    # Every arm must occupy a different position in every repetition, or the rotation is decorative.
    for arm in ARM_ORDER:
        positions = [rotated_arms(repetition).index(arm) for repetition in FORMAL_REPETITIONS]
        if len(set(positions)) != len(positions):
            problems.append(f"arm {arm} keeps the same position across repetitions")
    return problems


def formal_plan_payload() -> dict[str, Any]:
    """The offline artifact: order, identities, refusals and the rule that produced them."""

    problems = check_candidate()
    repetitions: list[dict[str, Any]] = []
    for repetition in FORMAL_REPETITIONS:
        plan = formal_plan(repetition)
        probe = authorization_probe(repetition)
        repetitions.append(
            {
                "repetition": repetition,
                "plan_id": plan.plan_id,
                "plan_digest": plan.digest(),
                "runner_revision": plan.runner_revision,
                "repetitions": list(plan.repetitions),
                "max_cell_executions": plan.max_cell_executions,
                "estimated_cost_ceiling_usd": plan.estimated_cost_ceiling_usd,
                "arm_order": list(rotated_arms(repetition)),
                "cells": [
                    {
                        "cell_id": cell.cell_id,
                        "task_id": cell.task_id,
                        "arm_id": cell.arm_id,
                        "topology": cell.spec.topology,
                        "runner": cell.spec.runner,
                        "prompt_layout": cell.spec.prompt_layout,
                        "route": cell.spec.requested_route,
                    }
                    for cell in repetition_cells(repetition)
                ],
                "candidate_approval": candidate_approval(repetition),
                "authorization_probe": probe,
            }
        )
    return {
        "kind": "closure2_formal_matrix_candidate",
        "stage": "closure-2-s07",
        "status": "CANDIDATE_NOT_FROZEN_NOT_AUTHORIZED",
        "live_runnable": False,
        "live_runnable_reason": (
            "pilot_runner.plan_scope_ids() exposes two plans and neither is a formal-matrix plan; "
            "the candidate is refused by pilot_authorization_gate until an owner approval record "
            "names its digest"
        ),
        "benchmark_id": BENCHMARK_ID,
        "preregistration_digest": preregistration_digest(),
        "frozen_preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
        "plan_revision": FORMAL_PLAN_REVISION,
        "order_rule": EXECUTION_ORDER["rule"],
        "order_implementation": (
            "tasks in frozen order, arms rotated left by "
            f"{FORMAL_ARM_ROTATION} places per repetition; no randomisation, so no seed is recorded"
        ),
        "seed": None,
        "randomised": False,
        "arms": list(ARM_ORDER),
        "tasks": list(TASK_ORDER),
        "repetitions_planned": len(FORMAL_REPETITIONS),
        "cells_per_repetition": CELLS_PER_REPETITION,
        "total_executions": TOTAL_EXECUTIONS,
        "contract_problems": problems,
        "contract_ok": not problems,
        "model_calls": 0,
        "sandbox_runs": 0,
        "repetitions": repetitions,
    }


def write_formal_plan(path: Path) -> dict[str, Any]:
    payload = formal_plan_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "closure-2 formal matrix candidate: offline order and identities. "
            "This writes a plan; it never starts a run."
        )
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    payload = write_formal_plan(Path(args.out))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "contract_ok": payload["contract_ok"],
                "contract_problems": payload["contract_problems"],
                "total_executions": payload["total_executions"],
                "plan_digests": {
                    item["plan_id"]: item["plan_digest"] for item in payload["repetitions"]
                },
                "execution_ready": [
                    item["authorization_probe"]["execution_ready"]
                    for item in payload["repetitions"]
                ],
                "model_calls": 0,
                "sandbox_runs": 0,
                "out": str(args.out),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if payload["contract_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CELLS_PER_REPETITION",
    "FORMAL_COST_CEILING_USD",
    "FORMAL_PLAN_REVISION",
    "FORMAL_REPETITIONS",
    "TOTAL_EXECUTIONS",
    "FormalPlanError",
    "authorization_probe",
    "candidate_approval",
    "check_candidate",
    "formal_plan",
    "formal_plan_id",
    "formal_plan_payload",
    "repetition_cells",
    "rotated_arms",
    "write_formal_plan",
]
