"""Stage F — the 96-cell master manifest and its three frozen batch plans.

Order is generated once from a fixed seed and then frozen: the manifest records the seed, the task
order and the sequence assignment, and every batch plan must recompute from them. A batch whose
does not recompute is rejected before it can reach a dispatch.

With three repetitions and four sequences no task can visit all four positions; the design is an
incomplete counterbalance and says so both here and in the manifest.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract
from experiments.stage_f import arms, tasks

REPO_ROOT = Path(__file__).resolve().parents[2]

SCOPE = "F_UNIFIED_96_CELL_EVALUATION"
MASTER_ID = "stage-f-96-manifest-v1"
DESIGN_VERSION = "stage-f-1.0.0"

#: Fixed before any run. Changing it changes every plan digest.
ORDER_SEED = 20261125

BATCH_PLAN_IDS: dict[int, str] = {
    1: "stage-f-96-b1",
    2: "stage-f-96-b2",
    3: "stage-f-96-b3",
}

SEQUENCE_ORDER: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")


def task_order() -> tuple[str, ...]:
    """The one global task order; each batch rotates it so no task is always first."""

    order = list(tasks.TASK_IDS)
    random.Random(ORDER_SEED).shuffle(order)
    return tuple(order)


def batch_task_order(batch: int) -> tuple[str, ...]:
    order = task_order()
    offset = (batch - 1) * 3
    return tuple(order[(index + offset) % len(order)] for index in range(len(order)))


def batch_sequence_assignment(batch: int) -> dict[str, str]:
    """``sequence = SEQUENCES[(global_index + batch - 1) mod 4]``.

    The *global* index is used, not the position inside the rotated batch order. That keeps two
    properties at once: within a batch the eight global indices still cycle through the four
    sequences twice (two tasks per sequence), and across the three batches a task advances by one
    sequence each time, so it sees three distinct sequences instead of two. Using the rotated
    position instead collapses to two sequences because the rotation step (3) and the batch offset
    (1) both cancel modulo four.
    """

    global_order = task_order()
    assignment: dict[str, str] = {}
    for task_id in batch_task_order(batch):
        index = global_order.index(task_id)
        assignment[task_id] = SEQUENCE_ORDER[(index + batch - 1) % len(SEQUENCE_ORDER)]
    return assignment


def batch_cells(batch: int) -> list[dict[str, Any]]:
    """The 32 cells of one batch, in execution order."""

    assignment = batch_sequence_assignment(batch)
    plan_id = BATCH_PLAN_IDS[batch]
    cells: list[dict[str, Any]] = []
    order = 0
    for task_id in batch_task_order(batch):
        sequence = assignment[task_id]
        for arm_id in arms.QUATERNIONS[sequence]:
            order += 1
            cells.append(
                arms.cell_record(
                    task_id=task_id,
                    arm_id=arm_id,
                    repetition=batch,
                    sequence=sequence,
                    position=order,
                    batch_id=plan_id,
                    plan_id=plan_id,
                    scope=SCOPE,
                )
            )
    return cells


def plan_digest(cells: Sequence[Mapping[str, Any]]) -> str:
    from experiments.stage_benchmark import matrix

    return matrix.plan_digest(list(cells))


def batch_plan(batch: int) -> dict[str, Any]:
    cells = batch_cells(batch)
    return {
        "stage": "F",
        "design_version": DESIGN_VERSION,
        "master_id": MASTER_ID,
        "executor_version": _executor_identity()["executor_version"],
        "executor_digest": _executor_identity()["executor_digest"],
        "adapter_version": arms.ADAPTER_VERSION,
        "adapter_digest": adapter_identity()["normaliser_digest"],
        "pricing_digest": pricing_identity()["digest"],
        "settlement_digest": settlement_contract()["digest"],
        "plan_id": BATCH_PLAN_IDS[batch],
        "scope": SCOPE,
        "batch": batch,
        "repetition": batch,
        "repetitions": 1,
        "tasks": list(batch_task_order(batch)),
        "arms": list(arms.ARM_ORDER),
        "sequence_assignment": batch_sequence_assignment(batch),
        "cells_count": len(cells),
        "cells": cells,
        "digest": plan_digest(cells),
        "claim_namespace": f"stage-f-claim/{BATCH_PLAN_IDS[batch]}",
        "runs_subdir": f"runs/{BATCH_PLAN_IDS[batch]}",
        "evidence_subdir": f"evidence/{BATCH_PLAN_IDS[batch]}",
        "is_benchmark_sample": False,
        "attempt_limit": 1,
        "budget": arms.budget(),
        "budget_digest": arms.budget_digest(),
        "nominal_token_capacity": arms.TOKEN_STOP_PER_BATCH,
        "cost_stop_usd": arms.COST_STOP_PER_BATCH,
        "per_cell_cost_stop_usd": arms.COST_STOP_PER_CELL,
        "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
        "order_seed": ORDER_SEED,
        "incomplete_counterbalance": True,
    }


def batch_plans() -> dict[int, dict[str, Any]]:
    return {batch: batch_plan(batch) for batch in range(1, arms.BATCH_COUNT + 1)}


def _executor_identity() -> dict[str, Any]:
    """The executor identity, imported lazily: the executor resolves Stage F plans at call time."""

    from experiments.stage_b_plus import executor

    return executor.executor_identity(REPO_ROOT)


def adapter_identity() -> dict[str, Any]:
    """What turns a raw result into Unified Cell Evidence, and the schema it writes."""

    from experiments.stage_f import normalize

    files = {
        relative: "sha256:" + hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
        for relative in (
            "experiments/stage_f/normalize.py",
            "experiments/stage_f/runners.py",
        )
    }
    return {
        "adapter_version": arms.ADAPTER_VERSION,
        "schema_version": normalize.SCHEMA_VERSION,
        "normaliser_digest": authority.digest_of(files),
        "files": files,
    }


def pricing_identity() -> dict[str, Any]:
    """The frozen price block, with where it came from and what it is not."""

    from experiments.stage_b_plus import cost

    pricing = dict(cost.stage_b_estimate.pricing())
    return {
        **pricing,
        "currency": "USD",
        "model": "deepseek-chat",
        "model_alias_resolution": "deepseek-chat resolves to deepseek-flash",
        "digest": authority.digest_of(pricing),
        "is_an_estimate": True,
        "is_a_bill": False,
        "missing_cache_split": "price the whole input as a miss; never assume a hit",
    }


def settlement_contract() -> dict[str, Any]:
    """How a batch is settled against its predecessors, frozen with the matrix."""

    contract_block = {
        "rule": "min(frozen batch allowance, owner authorization, global remaining)",
        "per_batch_token_ceiling": arms.TOKEN_STOP_PER_BATCH,
        "global_token_ceiling": arms.TOKEN_STOP_TOTAL,
        "per_batch_cost_ceiling_usd": arms.COST_STOP_PER_BATCH,
        "global_cost_ceiling_usd": arms.COST_STOP_TOTAL,
        "refuses": (
            "a missing prior report, an unconsumed prior claim, a plan or executor identity "
            "mismatch, incomplete usage, a prior frozen stop, or no remaining headroom"
        ),
        "reader": "experiments.stage_f.cumulative.settlement",
        "entry": "executor.dispatch (pre-claim)",
    }
    return {**contract_block, "digest": authority.digest_of(contract_block)}


def visible_files(task_id: str) -> dict[str, str]:
    """The bytes a cell starts from: the seed plus the declared public suite.

    ``prepare_fixture`` materialises exactly this set and digests it, so the manifest and the
    per-cell fixture check compare the same object instead of two different ones.
    """

    task = tasks.task_by_id(task_id)
    return {**dict(task.seed), **dict(task.public_tests)}


def public_interface(task_id: str) -> dict[str, Any]:
    """The frozen interface the task is written against, as bytes plus its per-node bindings."""

    task = tasks.task_by_id(task_id)
    paths = sorted({path for bound in task.frozen_contracts.values() for path in bound})
    # A task may declare one interface document for the whole project instead of binding it per
    # node (S2 does). The bytes are then the interface the seed ships under ``docs/``.
    if not paths:
        paths = sorted(path for path in task.seed if path.startswith("docs/"))
    return {
        "files": {path: task.seed.get(path) for path in paths},
        "bindings": {node: list(bound) for node, bound in task.frozen_contracts.items()},
    }


def _task_facts(task_id: str) -> dict[str, Any]:
    task = tasks.task_by_id(task_id)
    interface = public_interface(task_id)
    return {
        "family": tasks.family_of(task_id),
        "title": task.title,
        "goal_digest": arms.goal_digest(task_id),
        "grant_digest": arms.grant_digest(task_id),
        "static_dag_digest": arms.static_dag_digest(task_id),
        # The decomposition itself, including each node's assignment text and the cap it is given.
        "static_dag": arms.static_dag(task_id),
        # The frozen fixture identity: a fixture edit after the freeze moves this digest.
        "seed_digest": authority.digest_of(dict(sorted(visible_files(task_id).items()))),
        "seed_files": len(visible_files(task_id)),
        # The four content identities a reader needs to know *what* was frozen. Names alone would
        # let the suite be edited while the manifest still looked current.
        "public_interface_digest": authority.digest_of(interface),
        "public_interface_paths": sorted(interface["files"]),
        "public_test_digest": authority.digest_of(dict(sorted(task.public_tests.items()))),
        "oracle_digest": authority.digest_of(dict(sorted(task.oracle_tests.items()))),
        "reference_digest": authority.digest_of(dict(sorted(task.reference.items()))),
        "paths_digest": authority.digest_of(
            {
                "allowed": list(task.allowed_paths),
                "forbidden": list(task.forbidden_paths),
            }
        ),
        "deliverables": list(task.deliverables),
        "public_tests": sorted(task.public_tests),
        "oracle_tests": sorted(task.oracle_tests),
        "reference_files": sorted(task.reference),
        "allowed_paths": list(task.allowed_paths),
        "forbidden_paths": list(task.forbidden_paths),
    }


def manifest() -> dict[str, Any]:
    orders = {str(batch): list(batch_task_order(batch)) for batch in range(1, 4)}
    assignments = {str(batch): batch_sequence_assignment(batch) for batch in range(1, 4)}
    plans = batch_plans()
    task_facts = {task_id: _task_facts(task_id) for task_id in tasks.TASK_IDS}
    return {
        "stage": "F",
        "design_version": DESIGN_VERSION,
        "master_id": MASTER_ID,
        "scope": SCOPE,
        "tasks": list(tasks.TASK_IDS),
        "families": {task_id: tasks.family_of(task_id) for task_id in tasks.TASK_IDS},
        "arms": arms.arm_summary(),
        "task_facts": task_facts,
        "order_seed": ORDER_SEED,
        "task_order": list(task_order()),
        "batch_task_order": orders,
        "batch_sequences": assignments,
        "sequences": {key: list(value) for key, value in arms.QUATERNIONS.items()},
        "batches": [
            {
                "batch": batch,
                "plan_id": plans[batch]["plan_id"],
                "plan_digest": plans[batch]["digest"],
                "cells": plans[batch]["cells_count"],
                "token_capacity": plans[batch]["nominal_token_capacity"],
                "cost_stop_usd": plans[batch]["cost_stop_usd"],
            }
            for batch in (1, 2, 3)
        ],
        "cells_total": sum(plans[batch]["cells_count"] for batch in (1, 2, 3)),
        "budget": arms.budget(),
        "budget_digest": arms.budget_digest(),
        "token_stop_total": arms.TOKEN_STOP_TOTAL,
        "cost_stop_total": arms.COST_STOP_TOTAL,
        "cost_stop_candidate": {
            "per_cell_usd": arms.COST_STOP_PER_CELL,
            "per_batch_usd": arms.COST_STOP_PER_BATCH,
            "total_usd": arms.COST_STOP_TOTAL,
            "approved": False,
        },
        "adapter_version": arms.ADAPTER_VERSION,
        "adapter": adapter_identity(),
        "executor_version": _executor_identity()["executor_version"],
        "executor_digest": _executor_identity()["executor_digest"],
        "executor_files": _executor_identity()["files"],
        "pricing": pricing_identity(),
        "settlement_contract": settlement_contract(),
        "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
        "main_prompt_digest": arms.main_prompt_digest(),
        "worker_contract_digest": arms.worker_contract()["rendered_prompt_digest"],
        "incomplete_counterbalance": (
            "three repetitions cannot visit all four execution positions; each task sees three of "
            "the four quaternions"
        ),
    }


def _digest_of(payload: Any) -> str:
    return authority.digest_of(payload)


def validate(record: Mapping[str, Any] | None = None) -> list[str]:
    """Every invariant of the 96-cell design, checked on the record itself."""

    manifest_record = dict(record or manifest())
    problems: list[str] = []
    plans = batch_plans()

    if manifest_record.get("master_id") != MASTER_ID or manifest_record.get("scope") != SCOPE:
        problems.append("the manifest is not the Stage F master identity")
    if manifest_record.get("cells_total") != 96:
        problems.append("the manifest does not hold 96 cells")
    if len(plans) != 3:
        problems.append("there are not exactly three batch plans")

    all_cells: list[dict[str, Any]] = []
    for batch, plan in plans.items():
        cells = list(plan["cells"])
        all_cells.extend(cells)
        if len(cells) != arms.CELLS_PER_BATCH:
            problems.append(f"batch {batch} has {len(cells)} cells, not 32")
        if plan["digest"] != plan_digest(cells):
            problems.append(f"batch {batch}: the plan digest does not recompute")
        if plan["budget_digest"] != arms.budget_digest():
            problems.append(f"batch {batch}: the budget digest is not the Stage F budget")
        if int(contract.BUDGET["token_budget"]) != 500_000:
            problems.append("the frozen Stage B+ budget moved")
        if contract.token_budget_for(cells[0]) != 1_000_000:
            problems.append(f"batch {batch}: the effective token budget is not one million")
        # every task once, every arm once per task, sequence carries two tasks
        per_task: dict[str, list[str]] = {}
        for cell in cells:
            per_task.setdefault(str(cell["task_id"]), []).append(str(cell["arm_id"]))
        if len(per_task) != 8:
            problems.append(f"batch {batch} does not cover eight tasks")
        for task_id, arm_ids in per_task.items():
            if sorted(arm_ids) != sorted(arms.ARM_ORDER):
                problems.append(f"batch {batch}: {task_id} does not run all four arms once")
        counts = {sequence: 0 for sequence in SEQUENCE_ORDER}
        for cell in cells:
            counts[str(cell["sequence"])] += 1
        if sorted(counts.values()) != [8, 8, 8, 8]:
            problems.append(f"batch {batch}: sequences are unbalanced {counts}")
        if [cell["order"] for cell in cells] != list(range(1, len(cells) + 1)):
            problems.append(f"batch {batch}: cell order is not 1..32")

    ids = [str(cell["cell_id"]) for cell in all_cells]
    if len(ids) != len(set(ids)):
        problems.append("duplicate cell ids across the matrix")
    per_arm: dict[str, int] = {arm_id: 0 for arm_id in arms.ARM_ORDER}
    per_task_arm: dict[tuple[str, str], int] = {}
    for cell in all_cells:
        per_arm[str(cell["arm_id"])] += 1
        key = (str(cell["task_id"]), str(cell["arm_id"]))
        per_task_arm[key] = per_task_arm.get(key, 0) + 1
        problems.extend(arms.validate_cell(cell))
    if any(count != 24 for count in per_arm.values()):
        problems.append(f"arm cell counts are {per_arm}, expected 24 each")
    if any(count != 3 for count in per_task_arm.values()):
        problems.append("a task/arm pair does not have three repetitions")

    # a task never repeats the same sequence across batches
    for task_id in tasks.TASK_IDS:
        seen = [str(cell["sequence"]) for cell in all_cells if cell["task_id"] == task_id]
        if len(set(seen)) != 3:
            problems.append(f"{task_id} sees {sorted(set(seen))}, not three distinct sequences")

    # each (task, repetition) has four distinct arms, so a pair is always complete
    pairs: dict[tuple[str, int], set[str]] = {}
    for cell in all_cells:
        pairs.setdefault((str(cell["task_id"]), int(cell["repetition"])), set()).add(
            str(cell["arm_id"])
        )
    if any(value != set(arms.ARM_ORDER) for value in pairs.values()):
        problems.append("a task/repetition pair is missing an arm")

    # Every task identity is explicit: the four content digests, the interface it was written
    # against, the decomposition with its assignments, and both path lists.
    frozen_facts = manifest_record.get("task_facts") or {}
    for task_id in tasks.TASK_IDS:
        facts = frozen_facts.get(task_id) or {}
        for key in (
            "goal_digest",
            "grant_digest",
            "static_dag_digest",
            "seed_digest",
            "public_interface_digest",
            "public_test_digest",
            "oracle_digest",
            "reference_digest",
            "paths_digest",
        ):
            if not str(facts.get(key) or "").startswith("sha256:"):
                problems.append(f"{task_id}: {key} is not a frozen digest")
        if not facts.get("public_interface_paths"):
            problems.append(f"{task_id}: the public interface is not bound")
        if not facts.get("reference_files"):
            problems.append(f"{task_id}: the reference content is not bound")
        if not (facts.get("allowed_paths") and facts.get("forbidden_paths")):
            problems.append(f"{task_id}: the path policy is not bound")
        if not all(node.get("assignment") for node in facts.get("static_dag") or []):
            problems.append(f"{task_id}: a static-DAG node carries no assignment")
    if manifest_record.get("batches") and [
        item["plan_digest"] for item in manifest_record["batches"]
    ] != [plans[batch]["digest"] for batch in (1, 2, 3)]:
        problems.append("the manifest does not carry the batch plan digests")
    if manifest_record.get("budget_digest") != arms.budget_digest():
        problems.append("the manifest budget digest is not the Stage F budget")
    if manifest_record.get("cost_stop_candidate", {}).get("approved") is not False:
        problems.append("the cost stop is presented as approved")
    return problems


def write(record: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Freeze the manifest and the three plans into ``experiments/stage_f``."""

    from pathlib import Path

    root = Path(__file__).resolve().parent
    plans = batch_plans()
    manifest_record = dict(record or manifest())
    contract.write_document(manifest_record, root / "manifest.json")
    for batch, plan in plans.items():
        contract.write_document(plan, root / f"plan-b{batch}.json")
    return manifest_record


def plan_scope(scope_or_id: str) -> dict[str, Any]:
    """Resolve a Stage F plan id or scope, for the dispatch entry."""

    if scope_or_id in (SCOPE, MASTER_ID):
        return manifest()
    for batch, plan_id in BATCH_PLAN_IDS.items():
        if scope_or_id == plan_id:
            return batch_plan(batch)
    raise ValueError(f"unknown Stage F plan {scope_or_id!r}")


__all__ = [
    "BATCH_PLAN_IDS",
    "DESIGN_VERSION",
    "MASTER_ID",
    "ORDER_SEED",
    "SCOPE",
    "batch_cells",
    "batch_plan",
    "batch_plans",
    "manifest",
    "plan_digest",
    "plan_scope",
    "task_order",
    "validate",
    "write",
]

#: ``hashlib`` is imported for callers that want a quick content digest of this module's output.
_ = hashlib
