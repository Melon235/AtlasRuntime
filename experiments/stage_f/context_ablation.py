"""Frozen, independently authorized Direct-only context ablation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from experiments.stage_f import arms

from atlasos.app.main_driven import GoalContextExperiment
from atlasos.history_compaction import COMPACTION_OFF, COMPACTION_ROLLING_2
from atlasos.tool_result_offload import TOOL_RESULT_INGRESS_OFFLOAD, TOOL_RESULT_OFF

EXPERIMENT = "stage-fr-context-ablation-v1"
SCOPE = "F_R_CONTEXT_ABLATION_V1"
BATCH_IDS = {1: f"{EXPERIMENT}-b1"}
TASKS = ("f-c1-config-lifecycle", "f-x2-distributed-diagnosis")
TREATMENTS = {
    "C0": {"compaction": COMPACTION_OFF, "offloading": TOOL_RESULT_OFF},
    "C1": {"compaction": COMPACTION_OFF, "offloading": TOOL_RESULT_INGRESS_OFFLOAD},
    "C2": {"compaction": COMPACTION_ROLLING_2, "offloading": TOOL_RESULT_INGRESS_OFFLOAD},
}
ORDERS = (
    ("C0", "C1", "C2"),
    ("C1", "C2", "C0"),
    ("C2", "C0", "C1"),
    ("C2", "C1", "C0"),
)
ANALYSIS = {
    "treatments": TREATMENTS,
    "contrasts": [["C0", "C1"], ["C1", "C2"], ["C0", "C2"]],
    "paired_by": ["task_id", "repetition"],
    "subsets": ["all valid pairs", "pairs with successful delivery in both treatments"],
    "require_observed_trigger_for_mechanism_inference": True,
    "factorial_interaction_estimable": False,
    "reuse_historical_runs": False,
    "evidence": {
        "provider_input_output_cache_latency": "provider_requests.json records[].provider_usage",
        "mapped_context_json": "provider_requests.json records[].context_input",
        "peak_and_cumulative_input": "evidence.json context",
        "archive_reads_tool_counts": "evidence.json context_experiment",
        "budget_and_oracle": "evidence.json cell_budget_ledger and correctness",
        "estimated_cost_and_end_to_end_latency": "matrix-report.json cells",
    },
}


def base_plan() -> dict[str, Any]:
    cells = []
    for index, (task, rep) in enumerate((t, r) for r in (1, 2) for t in TASKS):
        order = ORDERS[index]
        for treatment in order:
            cell = arms.cell_record(
                task_id=task,
                arm_id="A1",
                repetition=rep,
                sequence="-".join(order),
                position=len(cells) + 1,
                batch_id=BATCH_IDS[1],
                plan_id=BATCH_IDS[1],
                scope=SCOPE,
            )
            cell.update(
                cell_id=f"context.{task}.{treatment}.r{rep}",
                context_treatment=treatment,
                context_intervention=dict(TREATMENTS[treatment]),
            )
            cells.append(cell)
    return {
        "batch": 1,
        "tasks": list(TASKS),
        "arms": ["A1"],
        "repetitions": 2,
        "cells_count": len(cells),
        "cells": cells,
        "budget": arms.budget(),
        "nominal_token_capacity": len(cells) * arms.CELL_BUDGET["token_budget"],
        "attempt_limit": 1,
        "is_benchmark_sample": False,
        "context_analysis": ANALYSIS,
    }


def options_for_cell(cell: Mapping[str, Any]) -> GoalContextExperiment | None:
    if cell.get("release_experiment") != EXPERIMENT:
        return None
    modes = TREATMENTS.get(str(cell.get("context_treatment")))
    if (
        modes is None
        or cell.get("context_intervention") != modes
        or cell.get("arm_id") != "A1"
        or cell.get("execution_mode") != arms.arm_spec("A1").execution_mode
        or (cell.get("delegation") or {}).get("enabled") is not False
        or cell.get("task_id") not in TASKS
        or cell.get("task_revision") != 2
        or cell.get("max_turns") != 40
        or cell.get("token_budget") != 1_000_000
    ):
        raise ValueError("cell does not match the frozen context intervention")
    return GoalContextExperiment(**modes)
