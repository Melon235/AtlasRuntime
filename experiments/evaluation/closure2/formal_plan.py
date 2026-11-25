# ruff: noqa: E501
"""Closure-2 S07: the three formal plans of the 54-cell matrix, offline and **not** authorized.

What this module is:

* an **order generator** — the frozen matrix is six arms x three tasks x three repetitions = 54
  executions, and this module fixes the interleaving rule so the order is reproducible instead of
  being decided on the day of the run. Two schedules exist: the S07 ``frozen`` one (tasks in catalogue
  order) and the ``balanced`` one (tasks rotate too), which is what the plans below execute;
* a **plan identity** — three repetition-scoped ``PilotPlan`` objects, each digestible and each
  approval-independent, built with the same dataclass the live entry point uses, so a digest computed
  here is the digest the executed plan would have. Each plan also names the **budget profile** it
  executes under, so approving a plan approves its numbers and a candidate plan cannot be mistaken for
  the frozen 12 / 260 000 registration;
* an **offline probe** — ``authorization_probe()`` runs the *shipped* gate against the candidate and
  reports the refusal, which is how this stage proves "prepared, not authorized" instead of asserting
  it;
* a **cross-plan aggregator** — ``aggregate_plan_summaries()`` turns three live summaries into one
  54-execution table with a cost reconciliation, refusing to merge a summary that belongs to another
  plan or repeats an execution.

What this module is not:

* not a new execution entry point. The three plans are reachable through
  ``pilot_runner.pilot_scope()`` / ``plan_cells_for()``, and every one of them is refused by the
  shipped gate because no approval record names its digest;
* not a re-freeze. The plan digests are candidates until a later stage writes an approval that names
  them; this module writes no approval and mutates no record;
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

from experiments.evaluation.closure2.candidate_budget import CANDIDATE_PROFILE, BudgetProfile
from experiments.evaluation.closure2.pilot_runner import (
    ARM_ORDER,
    FORMAL_PLAN_ID_PREFIX,
    FORMAL_PLAN_IDS,
    FORMAL_REPETITIONS,
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
from experiments.evaluation.closure2.stage_costs import formal_plan_stop_usd

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "formal-plan-candidate.json"

#: Candidate identity. It is inside every plan digest, so settling it is part of freezing the plan.
#: Bumped from ``closure-2-s07-candidate-1``: the plans now bind a budget profile and execute the
#: balanced schedule, so the previous digests name a different scope.
FORMAL_PLAN_REVISION = "closure-2-s07-candidate-2"

#: Six arms, so a rotation of two places gives every arm three distinct positions across the three
#: repetitions and every position three distinct arms. No RNG is involved, which is stronger than a
#: recorded seed: the order cannot drift because nobody drew it.
FORMAL_ARM_ROTATION = 2

#: Three tasks, so a rotation of one place puts every task in each third of a repetition exactly once.
FORMAL_TASK_ROTATION = 1

#: ``frozen`` is what S07 proposed and the preflight assumes; ``balanced`` is the revision candidate
#: that removes the task/time-of-day confound. The wired plans use ``balanced``; the frozen schedule
#: stays reproducible so the difference can be audited rather than asserted.
SCHEDULE_FROZEN = "s07-frozen-task-order"
SCHEDULE_BALANCED = "balanced-double-rotation"
SCHEDULES: tuple[str, ...] = (SCHEDULE_FROZEN, SCHEDULE_BALANCED)
WIRED_SCHEDULE = SCHEDULE_BALANCED

CELLS_PER_REPETITION = len(TASK_ORDER) * len(ARM_ORDER)
TOTAL_EXECUTIONS = CELLS_PER_REPETITION * len(FORMAL_REPETITIONS)

#: Advisory per-repetition ceiling, derived in ``stage_costs`` from the zero-cache-hit price of one
#: repetition. It is inside the plan digest, so approving the digest approves it. Candidate only.
FORMAL_COST_CEILING_USD = formal_plan_stop_usd()

#: The budget profile every formal plan executes under.
FORMAL_BUDGET_PROFILE: BudgetProfile = CANDIDATE_PROFILE


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


def _check_schedule(schedule: str) -> None:
    if schedule not in SCHEDULES:
        raise FormalPlanError(
            f"unknown schedule {schedule!r}: known schedules are {list(SCHEDULES)}"
        )


def rotated_arms(repetition: int) -> tuple[str, ...]:
    """The base arm order of ``repetition``: ``ARM_ORDER`` rotated left by ``2 * repetition``.

    Repetition 0 starts at ``N0``, repetition 1 at ``A0``, repetition 2 at ``A1-P``. Every arm keeps
    its frozen neighbours inside a block; only the start moves.
    """

    _check_repetition(repetition)
    offset = (FORMAL_ARM_ROTATION * repetition) % len(ARM_ORDER)
    return ARM_ORDER[offset:] + ARM_ORDER[:offset]


def block_arms(
    repetition: int, block_index: int, *, schedule: str = WIRED_SCHEDULE
) -> tuple[str, ...]:
    """The arm order inside one task block.

    The base rotation alone leaves an arm in the *same* sixth of every repetition: the block is the
    outer loop, so an arm's position inside its block never varies and, per repetition, all three of
    its executions land in the same third of the session. Rotating the block's start by
    ``2 * (repetition + block_index)`` puts every arm in the early, the middle and the late third
    **once per repetition**, so no arm is systematically the one that runs while the provider is cold
    (or warm) inside a session.
    """

    _check_repetition(repetition)
    _check_schedule(schedule)
    rotation = repetition if schedule == SCHEDULE_BALANCED else 0
    offset = (FORMAL_ARM_ROTATION * (rotation + block_index)) % len(ARM_ORDER)
    return ARM_ORDER[offset:] + ARM_ORDER[:offset]


def rotated_tasks(repetition: int, *, schedule: str = WIRED_SCHEDULE) -> tuple[str, ...]:
    """The task order of ``repetition``.

    ``frozen`` keeps the catalogue order, which aliases time-of-day drift onto the task: P1 is always
    first, N1 always last. ``balanced`` rotates by one place, so every task occupies the early, the
    middle and the late third exactly once across the three repetitions.
    """

    _check_repetition(repetition)
    _check_schedule(schedule)
    if schedule == SCHEDULE_FROZEN:
        return TASK_ORDER
    offset = (FORMAL_TASK_ROTATION * repetition) % len(TASK_ORDER)
    return TASK_ORDER[offset:] + TASK_ORDER[:offset]


def repetition_cells(repetition: int, *, schedule: str = WIRED_SCHEDULE) -> tuple[Cell, ...]:
    """The 18 cells of one repetition: task blocks outer, arms the rotated inner loop."""

    cells: list[Cell] = []
    for block_index, task_id in enumerate(rotated_tasks(repetition, schedule=schedule)):
        for arm_id in block_arms(repetition, block_index, schedule=schedule):
            cells.append(Cell(task_id, arm_id))
    return tuple(cells)


def formal_plan(
    repetition: int, *, schedule: str = WIRED_SCHEDULE, budget_profile: BudgetProfile | None = None
) -> PilotPlan:
    """One repetition as an approvable scope. Identical in shape to the pilot and routing plans."""

    _check_repetition(repetition)
    profile = budget_profile or FORMAL_BUDGET_PROFILE
    return PilotPlan(
        plan_id=formal_plan_id(repetition),
        benchmark_id=BENCHMARK_ID,
        preregistration_digest=C2A_R1_PREREGISTRATION_DIGEST,
        runner_revision=FORMAL_PLAN_REVISION,
        cells=tuple(cell.cell_id for cell in repetition_cells(repetition, schedule=schedule)),
        repetitions=(repetition,),
        max_cell_executions=CELLS_PER_REPETITION,
        estimated_cost_ceiling_usd=FORMAL_COST_CEILING_USD,
        budget_profile_digest=profile.digest(),
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
    """Run the shipped gate against the candidate. The expected answer is a refusal."""

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


def _third(index: int, *, width: int = CELLS_PER_REPETITION) -> str:
    """Which third of a repetition an execution index falls in."""

    boundary = width // 3
    if index < boundary:
        return "early"
    if index < 2 * boundary:
        return "middle"
    return "late"


def schedule_audit(*, schedule: str = WIRED_SCHEDULE) -> dict[str, Any]:
    """The full order of the three plans, with the balance properties measured, not asserted."""

    _check_schedule(schedule)
    positions: dict[str, list[int]] = {arm: [] for arm in ARM_ORDER}
    block_positions: dict[str, list[int]] = {arm: [] for arm in ARM_ORDER}
    arm_thirds: dict[str, dict[str, int]] = {arm: {} for arm in ARM_ORDER}
    task_thirds: dict[str, dict[str, int]] = {task: {} for task in TASK_ORDER}
    arms_at_index: dict[int, set[str]] = {index: set() for index in range(CELLS_PER_REPETITION)}
    blocks: list[dict[str, Any]] = []
    for repetition in FORMAL_REPETITIONS:
        cells = repetition_cells(repetition, schedule=schedule)
        order: list[dict[str, Any]] = []
        for index, cell in enumerate(cells):
            block_index = index // len(ARM_ORDER)
            inside = block_arms(repetition, block_index, schedule=schedule).index(cell.arm_id)
            third = _third(index)
            order.append(
                {
                    "execution_index": index,
                    "block_index": block_index,
                    "position_in_block": inside,
                    "cell_id": cell.cell_id,
                    "task_id": cell.task_id,
                    "arm_id": cell.arm_id,
                    "third": third,
                }
            )
            positions[cell.arm_id].append(index)
            block_positions[cell.arm_id].append(inside)
            arms_at_index[index].add(cell.arm_id)
            arm_thirds[cell.arm_id][third] = arm_thirds[cell.arm_id].get(third, 0) + 1
        for block_index, task_id in enumerate(rotated_tasks(repetition, schedule=schedule)):
            third = _third(block_index * len(ARM_ORDER))
            task_thirds[task_id][third] = task_thirds[task_id].get(third, 0) + 1
        blocks.append(
            {
                "repetition": repetition,
                "plan_id": formal_plan_id(repetition),
                "task_order": list(rotated_tasks(repetition, schedule=schedule)),
                "block_arm_orders": [
                    list(block_arms(repetition, block_index, schedule=schedule))
                    for block_index in range(len(TASK_ORDER))
                ],
                "order": order,
            }
        )
    return {
        "schedule": schedule,
        "wired": schedule == WIRED_SCHEDULE,
        "rule": (
            "task blocks are the outer loop and arms the inner loop; the block starts rotate left by "
            f"{FORMAL_ARM_ROTATION} places per (repetition + block) and the task order by "
            f"{FORMAL_TASK_ROTATION if schedule == SCHEDULE_BALANCED else 0} per repetition; no "
            "randomisation, so no seed is recorded and the order cannot drift"
        ),
        "arm_rotation": FORMAL_ARM_ROTATION,
        "task_rotation": FORMAL_TASK_ROTATION if schedule == SCHEDULE_BALANCED else 0,
        "repetitions": blocks,
        "arm_execution_indices": positions,
        "arm_block_positions": block_positions,
        "arm_third_counts": arm_thirds,
        "task_third_counts": task_thirds,
        "every_task_in_each_third_once": all(
            sorted(counts.values()) == [1, 1, 1] and len(counts) == 3
            for counts in task_thirds.values()
        ),
        "every_arm_covers_every_third": all(
            sorted(counts.values()) == [len(FORMAL_REPETITIONS)] * 3 and len(counts) == 3
            for counts in arm_thirds.values()
        ),
        "every_arm_holds_three_block_positions": all(
            len(set(values)) >= 3 for values in block_positions.values()
        ),
        "every_index_is_held_by_three_distinct_arms": all(
            len(arms) == len(FORMAL_REPETITIONS) for arms in arms_at_index.values()
        ),
    }


def check_candidate(*, schedule: str = WIRED_SCHEDULE) -> list[str]:
    """Everything the candidate must satisfy before it can be proposed for approval."""

    problems: list[str] = []
    seen_cells = 0
    digests: dict[str, str] = {}
    for repetition in FORMAL_REPETITIONS:
        cells = repetition_cells(repetition, schedule=schedule)
        plan = formal_plan(repetition, schedule=schedule)
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
        if plan.budget_profile_digest != FORMAL_BUDGET_PROFILE.digest():
            problems.append(f"repetition {repetition}: does not bind the candidate budget profile")
        if plan.plan_id != FORMAL_PLAN_IDS[repetition]:
            problems.append(f"repetition {repetition}: plan id does not match the wired registry")
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
    for arm in ARM_ORDER:
        positions = [rotated_arms(repetition).index(arm) for repetition in FORMAL_REPETITIONS]
        if len(set(positions)) != len(positions):
            problems.append(f"arm {arm} keeps the same position across repetitions")
    audit = schedule_audit(schedule=schedule)
    for key, message in (
        ("every_task_in_each_third_once", "a task is not in the early, middle and late third once"),
        ("every_arm_covers_every_third", "an arm is not in the early, middle and late third once"),
        ("every_arm_holds_three_block_positions", "an arm repeats its position inside a block"),
        ("every_index_is_held_by_three_distinct_arms", "an execution index repeats an arm"),
    ):
        if not audit[key]:
            problems.append(f"the {schedule} schedule is not balanced: {message}")
    # Identities are unique across the *whole* matrix, not only inside one repetition.
    identities = {
        (cell.cell_id, repetition)
        for repetition in FORMAL_REPETITIONS
        for cell in repetition_cells(repetition, schedule=schedule)
    }
    if len(identities) != TOTAL_EXECUTIONS:
        problems.append(f"{len(identities)} unique (cell, repetition) identities, expected 54")
    return problems


# ------------------------------------------------------------------------------- cross-plan results


def _cell_identity(cell: dict[str, Any]) -> tuple[str, int | None]:
    return (str(cell.get("cell_id")), cell.get("repetition_id"))


def aggregate_plan_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge up to three formal summaries into one 54-execution table with a cost reconciliation.

    Refusals, in this order, because each one would corrupt the table if it were merged anyway:

    1. a summary whose plan id is not one of the three formal plans;
    2. two summaries of the same plan, or one plan digest approved twice;
    3. a repeated ``(cell, repetition)`` identity;
    4. a cell count that is not 18.

    Totals stay ``None`` when any term is unknown: a pilot with one unmeasured cell does not have a
    total, and neither does a matrix.
    """

    problems: list[str] = []
    seen_plans: dict[str, str] = {}
    seen_cells: dict[tuple[str, int | None], str] = {}
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        plan_id = str(summary.get("pilot_plan_id"))
        if plan_id not in FORMAL_PLAN_IDS:
            problems.append(f"{plan_id!r} is not one of the three formal plans")
            continue
        digest = str(summary.get("pilot_plan_digest"))
        if plan_id in seen_plans:
            problems.append(f"{plan_id} was summarized twice")
        seen_plans[plan_id] = digest
        cells = list(summary.get("cells") or [])
        if len(cells) != CELLS_PER_REPETITION:
            problems.append(f"{plan_id}: {len(cells)} cells, expected {CELLS_PER_REPETITION}")
        for cell in cells:
            identity = _cell_identity(cell)
            if identity in seen_cells:
                problems.append(f"{identity[0]} at repetition {identity[1]} appears twice")
            seen_cells[identity] = plan_id
            rows.append({"plan_id": plan_id, "plan_digest": digest, **cell})
    if problems:
        return {
            "status": "REFUSED",
            "problems": problems,
            "plans": sorted(seen_plans),
            "cells": len(rows),
        }

    def total(key: str) -> int | None:
        """A sum of counts, or ``None`` as soon as one term is unknown."""

        numbers: list[int] = []
        for row in rows:
            value = row.get(key)
            if value is None:
                return None
            numbers.append(int(value))
        return sum(numbers) if numbers else None

    def money(values: list[Any]) -> float | None:
        """A sum of costs, or ``None`` as soon as one term is unknown."""

        total_usd = 0.0
        for value in values:
            if value is None:
                return None
            total_usd += float(value)
        return round(total_usd, 4) if values else None

    by_plan: list[dict[str, Any]] = []
    for plan_id in FORMAL_PLAN_IDS:
        plan_rows = [row for row in rows if row["plan_id"] == plan_id]
        costs = [row.get("estimated_cost_usd") for row in plan_rows]
        by_plan.append(
            {
                "plan_id": plan_id,
                "present": plan_id in seen_plans,
                "plan_digest": seen_plans.get(plan_id),
                "cells": len(plan_rows),
                "oracle_passed": sum(1 for row in plan_rows if row.get("oracle_passed") is True),
                "executor_passed": sum(1 for row in plan_rows if row.get("status") == "PASSED"),
                "estimated_cost_usd": money(costs),
            }
        )
    ceilings = [
        summary.get("cost_ceiling_usd")
        for summary in summaries
        if summary.get("cost_ceiling_usd") is not None
    ]
    spent = [row["estimated_cost_usd"] for row in by_plan if row["estimated_cost_usd"] is not None]
    spend_total = money(spent)
    ceiling_total = money(ceilings) if len(ceilings) == len(seen_plans) else None
    return {
        "status": "AGGREGATED" if len(seen_plans) == len(FORMAL_PLAN_IDS) else "INCOMPLETE",
        "problems": [],
        "plans": by_plan,
        "plans_present": len(seen_plans),
        "cells_observed": len(rows),
        "cells_planned": TOTAL_EXECUTIONS,
        "oracle_passed": sum(1 for row in rows if row.get("oracle_passed") is True),
        "executor_passed": sum(1 for row in rows if row.get("status") == "PASSED"),
        "provider_prompt_tokens": total("provider_prompt_tokens"),
        "provider_completion_tokens": total("provider_completion_tokens"),
        "cache_hit_tokens": total("cache_hit_tokens"),
        "cache_miss_tokens": total("cache_miss_tokens"),
        "model_calls": total("model_calls"),
        "estimated_cost_usd": spend_total,
        "cost_reconciliation": {
            "per_plan_ceilings_usd": ceilings,
            "ceiling_total_usd": ceiling_total,
            "estimated_spend_usd": spend_total,
            "within_ceiling": (
                None
                if spend_total is None or ceiling_total is None
                else spend_total <= ceiling_total
            ),
            "rule": "a missing split has no cost, so a partial total is reported as None, never as 0",
        },
        "rows": rows,
    }


# ------------------------------------------------------------------------------------------ artifact


def formal_plan_payload() -> dict[str, Any]:
    """The offline artifact: order, identities, refusals and the rule that produced them."""

    problems = check_candidate()
    repetitions: list[dict[str, Any]] = []
    audit = schedule_audit()
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
                "budget_profile_digest": plan.budget_profile_digest,
                "task_order": list(rotated_tasks(repetition)),
                "block_arm_orders": [
                    list(block_arms(repetition, block_index))
                    for block_index in range(len(TASK_ORDER))
                ],
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
            "the three plans are wired into pilot_runner.plan_scope_ids() and plan_cells_for(), and "
            "every one of them is refused by pilot_authorization_gate because no approval record "
            "names its digest; the benchmark-definition approval alone authorizes no execution"
        ),
        "benchmark_id": BENCHMARK_ID,
        "preregistration_digest": preregistration_digest(),
        "frozen_preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
        "plan_revision": FORMAL_PLAN_REVISION,
        "budget_profile": {
            **FORMAL_BUDGET_PROFILE.payload(),
            "digest": FORMAL_BUDGET_PROFILE.digest(),
        },
        "order_rule": EXECUTION_ORDER["rule"],
        "schedule": audit,
        "schedule_alternatives": {
            schedule: {
                "task_orders": [
                    list(rotated_tasks(repetition, schedule=schedule))
                    for repetition in FORMAL_REPETITIONS
                ],
                "task_third_counts": schedule_audit(schedule=schedule)["task_third_counts"],
                "every_task_in_each_third_once": schedule_audit(schedule=schedule)[
                    "every_task_in_each_third_once"
                ],
                "every_arm_covers_every_third": schedule_audit(schedule=schedule)[
                    "every_arm_covers_every_third"
                ],
            }
            for schedule in SCHEDULES
        },
        "seed": None,
        "randomised": False,
        "arms": list(ARM_ORDER),
        "tasks": list(TASK_ORDER),
        "repetitions_planned": len(FORMAL_REPETITIONS),
        "cells_per_repetition": CELLS_PER_REPETITION,
        "total_executions": TOTAL_EXECUTIONS,
        "plan_ids": list(FORMAL_PLAN_IDS),
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
    "FORMAL_BUDGET_PROFILE",
    "FORMAL_COST_CEILING_USD",
    "FORMAL_PLAN_REVISION",
    "FORMAL_REPETITIONS",
    "SCHEDULE_BALANCED",
    "SCHEDULE_FROZEN",
    "SCHEDULES",
    "TOTAL_EXECUTIONS",
    "WIRED_SCHEDULE",
    "FormalPlanError",
    "aggregate_plan_summaries",
    "authorization_probe",
    "candidate_approval",
    "check_candidate",
    "formal_plan",
    "formal_plan_id",
    "formal_plan_payload",
    "repetition_cells",
    "rotated_arms",
    "rotated_tasks",
    "schedule_audit",
    "write_formal_plan",
]
