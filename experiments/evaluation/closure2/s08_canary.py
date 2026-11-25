# ruff: noqa: E501
"""Closure-2 S07: the S08 canary contract, prepared and **not** authorized.

The 2026-11-18 connectivity canary proved one thing well — provider, sandbox, tools, isolated oracle
and cleanup all work — and it was single-worker by design. It cannot show that a child is created,
given a context and graded, and it says nothing about the native engine or about the router refusing a
plan. S08 therefore needs a different scope, not a status edit on the consumed one:

* it executes **seven real benchmark cells** of the frozen P1/P2/N1 set, one attempt, no repetition;
* it covers native single, native multi, Atlas single, Atlas multi with both prompt layouts and the
  selective arm, plus the negative task's router refusal;
* it is **structure evidence**, never a benchmark sample: its cells are excluded from S11, and the
  exclusion is declared here, before anything runs;
* its approval is ``PENDING_OWNER_APPROVAL`` and its template is the only way to grant it. The
  benchmark-definition approval, the S06 authorization and the three consumed 2026-11-18 scopes
  cannot authorize it.

Why these seven cells and this order: each cell is the cheapest one that can falsify one layer of the
chain, and the order walks the chain from the smallest failure to the most expensive one. A failure at
cell 1 costs one native single run and localizes the fault to provider/sandbox/oracle; a failure at
cell 5 costs one Atlas multi run and localizes it to the DAG or the context plane.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.candidate_budget import CANDIDATE_PROFILE
from experiments.evaluation.closure2.pilot_runner import (
    ARM_ORDER,
    S08_CANARY_PLAN_ID,
    TASK_ORDER,
    Cell,
    PilotPlan,
)
from experiments.evaluation.closure2.protocol import C2A_R1_PREREGISTRATION_DIGEST

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "s08-canary-contract.json"

#: Candidate identity of the canary scope. It is inside the plan digest.
CANARY_PLAN_REVISION = "closure-2-s08-canary-1"

#: The execution order, and with it the falsification order. See the module docstring.
CANARY_ORDER: tuple[str, ...] = (
    "closure2-pipeline-migration::N0",
    "closure2-pipeline-migration::A0",
    "closure2-pipeline-migration::N1",
    "closure2-pipeline-migration::A1-L",
    "closure2-pipeline-migration::A1-P",
    "closure2-pipeline-migration::A2-P",
    "closure2-state-machine-refactor::A2-P",
)

#: What each cell has to prove. Requirements are named, so a coverage gap is a missing key instead of
#: a paragraph somebody has to re-read.
CANARY_COVERAGE: dict[str, tuple[str, ...]] = {
    "closure2-pipeline-migration::N0": ("native_single", "provider_sandbox_oracle_chain"),
    "closure2-pipeline-migration::A0": ("atlas_single", "context_plane_task_scoped"),
    "closure2-pipeline-migration::N1": ("native_multi_dag", "independent_write_sets"),
    "closure2-pipeline-migration::A1-L": ("atlas_multi_dag", "legacy_prompt_layout"),
    "closure2-pipeline-migration::A1-P": (
        "atlas_multi_dag",
        "shared_prefix_prompt_layout",
        "worker_creation",
        "worker_capability",
        "artifact_handoff",
        "worker_usage_counted",
    ),
    "closure2-pipeline-migration::A2-P": ("selective_arm_admits_positive_task", "router_audit"),
    "closure2-state-machine-refactor::A2-P": (
        "selective_arm_refuses_negative_task",
        "gate_2_dependency_coupling",
        "shared_write_risk",
    ),
}

REQUIRED_COVERAGE: tuple[str, ...] = (
    "native_single",
    "native_multi_dag",
    "atlas_single",
    "atlas_multi_dag",
    "legacy_prompt_layout",
    "shared_prefix_prompt_layout",
    "independent_write_sets",
    "worker_creation",
    "worker_capability",
    "artifact_handoff",
    "worker_usage_counted",
    "selective_arm_refuses_negative_task",
)

#: Acceptance criteria. Each one is checkable against a field of the run evidence, and the criterion
#: names that field. A canary that fails one of these fails the stage; it is not "mostly green".
ACCEPTANCE_CRITERIA: tuple[dict[str, str], ...] = (
    {
        "id": "A1",
        "criterion": "all seven cells are dispatched and observed, none is not_started",
        "evidence": "summary.cell_count == 7, summary.not_started_cells == []",
    },
    {
        "id": "A2",
        "criterion": "the durable record confirms every cell",
        "evidence": "summary.execution_integrity_status == 'COMPLETE'",
    },
    {
        "id": "A3",
        "criterion": "the positive multi path really creates workers",
        "evidence": (
            "cell P1::A1-P: executed_worker_count == 3, multi_worker_execution is True, and the run's "
            "lifecycle evidence lists three distinct worker ids with the two wave-0 workers before "
            "the dependent one"
        ),
    },
    {
        "id": "A4",
        "criterion": "a worker is able to write, inside its own declared paths",
        "evidence": (
            "P1::A1-P tool-call evidence contains at least one file_write/file_edit by a worker, and "
            "no path outside that worker's owned_paths; the ownership guard is not assumed, it is "
            "read off the tool calls"
        ),
    },
    {
        "id": "A5",
        "criterion": "the artifact handoff reaches the downstream node",
        "evidence": (
            "P1::A1-P request evidence shows the 'integrate' request carrying both dependency "
            "outputs; if it does not, the run records HANDOFF_NOT_OBSERVED and this criterion fails"
        ),
    },
    {
        "id": "A6",
        "criterion": "the positive wave is observable as a wave",
        "evidence": (
            "two wave-0 workers with no dependency between them, both starting before 'integrate' "
            "starts. Wall-clock overlap is NOT observable (parallel_overlap_evidence is null); the "
            "canary records the wave structure and never reports an overlap number"
        ),
    },
    {
        "id": "A7",
        "criterion": "the selective arm refuses the negative task",
        "evidence": (
            "N1::A2-P: resolved_route == 'direct', executed_worker_count == 1, and the router audit "
            "carries SHARED_WRITE_RISK plus a blocked GATE_2_DEPENDENCY_COUPLING"
        ),
    },
    {
        "id": "A8",
        "criterion": "the selective arm admits the positive task",
        "evidence": (
            "P1::A2-P: resolved_route in {'delegate_parallel', 'delegate'} with "
            "executed_worker_count == 3, i.e. the router did not collapse a parallelisable plan"
        ),
    },
    {
        "id": "A9",
        "criterion": "worker usage is inside the run totals",
        "evidence": (
            "every cell records provider_prompt_tokens and its cache split status; the multi cells "
            "report several times the single cell's request count, and no field claims a per-agent "
            "token split the runtime does not measure"
        ),
    },
    {
        "id": "A10",
        "criterion": "the oracle is independent",
        "evidence": (
            "oracle_mode == 'sandbox' for all seven cells; each cell records oracle_passed next to "
            "the executor verdict, and no cell is reclassified because the two disagree"
        ),
    },
    {
        "id": "A11",
        "criterion": "sandboxes are cleaned up",
        "evidence": (
            "agent_sandbox_runs_confirmed_min == 7, confirmed_not_created == 0, unknown == 0, and no "
            "residual container is left on the endpoint"
        ),
    },
    {
        "id": "A12",
        "criterion": "the evidence is complete and outside the repository",
        "evidence": (
            "per-cell raw run directories plus a summary and an evidence index of byte lengths and "
            "SHA-256, held outside the checkout; the repository keeps only the sanitized aggregate"
        ),
    },
    {
        "id": "A13",
        "criterion": "the canary is never counted as a benchmark sample",
        "evidence": (
            "the summary and the published table carry canary_id and is_benchmark_sample == False; "
            "no canary row appears in the S11 aggregate"
        ),
    },
)

EVIDENCE_CONTRACT: dict[str, tuple[str, ...]] = {
    "per_cell": (
        "cell_id, task_id, arm_id, topology, runner, prompt_layout, requested_route",
        "resolved_route and reason codes, when the arm runs the router",
        "executed_worker_count and single_worker_downgrade",
        "oracle_passed, oracle_failure_kind, task_success, failure_class",
        "provider_prompt_tokens, provider_completion_tokens",
        "cache_hit_tokens, cache_miss_tokens, cache_split_status",
        "estimated_cost_usd and its state, model_calls and their source",
        "budget verdict, basis and admission rejections",
        "sandbox creation status and destruction status",
        "raw_result_path and run_dir",
    ),
    "per_run": (
        "pilot_plan_id, pilot_plan_digest, approved_plan_digest, approval_status",
        "budget_profile revision and digest",
        "cell_count, execution_claim (attempt id, ledger path, CONSUMED)",
        "execution_integrity_status, stopped_reason, not_started_cells",
        "cost_policy, cost ceiling state and breach flag",
        "agent sandbox counts, confirmed/not-created/unknown",
    ),
    "published": (
        "a sanitized aggregate built from the summary, never the raw workspace",
        "an evidence index of byte lengths and SHA-256 for every published file",
        "the sentence 'not a benchmark sample' next to every canary number",
    ),
    "never_published": (
        "hidden oracle tests",
        "API keys or provider payloads",
        "the raw container filesystem or a patch of the hidden fixtures",
    ),
}


class CanaryContractError(RuntimeError):
    """The canary contract does not cover what it claims to cover."""


def canary_cells() -> tuple[Cell, ...]:
    """The seven cells, in execution (and falsification) order."""

    cells: list[Cell] = []
    for cell_id in CANARY_ORDER:
        task_id, separator, arm_id = cell_id.partition("::")
        if not separator or task_id not in TASK_ORDER or arm_id not in ARM_ORDER:
            raise CanaryContractError(f"{cell_id!r} is not <task>::<arm> of the frozen matrix")
        cells.append(Cell(task_id, arm_id))
    return tuple(cells)


def cost_block() -> dict[str, Any]:
    """The canary's cost numbers, from the one estimator both S07 artifacts share."""

    from experiments.evaluation.closure2.stage_costs import estimate_payload

    canary = estimate_payload()["stages"]["canary"]
    return {
        "expected": canary["expected"],
        "high": canary["high"],
        "nominal_capacity": canary["nominal_capacity"],
        "proposed_stop": canary["proposed_stop"],
    }


def canary_plan() -> PilotPlan:
    """The canary as an approvable scope, built with the same dataclass the dispatcher uses."""

    cost = cost_block()
    return PilotPlan(
        plan_id=S08_CANARY_PLAN_ID,
        # A canary scope of its own: it is not a benchmark sample and must never be aggregated as
        # one, so it does not claim the benchmark id either.
        benchmark_id="closure2_s08_canary_scope",
        preregistration_digest=C2A_R1_PREREGISTRATION_DIGEST,
        runner_revision=CANARY_PLAN_REVISION,
        cells=tuple(cell.cell_id for cell in canary_cells()),
        repetitions=(0,),
        max_cell_executions=len(CANARY_ORDER),
        estimated_cost_ceiling_usd=cost["proposed_stop"]["usd"],
        budget_profile_digest=CANDIDATE_PROFILE.digest(),
    )


def authorization_template() -> dict[str, Any]:
    """The only record that could authorize the canary. Pending by construction."""

    plan = canary_plan()
    return {
        "status": "PENDING_OWNER_APPROVAL",
        "approved_plan_digest": plan.digest(),
        "approved_by": None,
        "approval_commit": None,
        "approved_at": None,
        "scope": {
            "plan_id": plan.plan_id,
            "cells": list(plan.cells),
            "repetitions": list(plan.repetitions),
            "max_cell_executions": plan.max_cell_executions,
            "estimated_cost_stop_usd": plan.estimated_cost_ceiling_usd,
            "budget_profile_digest": plan.budget_profile_digest,
            "worker_max_turns": CANDIDATE_PROFILE.multi_worker_max_turns,
            "per_cell_token_budget": CANDIDATE_PROFILE.cell_token_budget,
            "attempts": 1,
        },
        "cannot_be_satisfied_by": [
            "protocol.APPROVAL_RECORD: it approves the benchmark definition, not an execution scope",
            "the S06 authorization: a different stage, a different digest, already spent",
            "closure2-pilot-1 / closure2-routing-pair-1 / closure2-connectivity-canary-v1: consumed",
            "any of the three formal plans: a different digest and a different cell list",
        ],
        "granting_requires": (
            "a new commit that sets status=APPROVED with an approver, that commit's hash, a timestamp "
            "and exactly the digest above"
        ),
    }


#: The record the dispatcher reads. Pending, so the shipped gate refuses the canary.
CANARY_APPROVAL: dict[str, Any] = authorization_template()


def coverage_matrix() -> dict[str, list[str]]:
    """Requirement -> the cells that would prove it. A requirement with no cell is a gap."""

    matrix: dict[str, list[str]] = {name: [] for name in REQUIRED_COVERAGE}
    for cell_id, requirements in CANARY_COVERAGE.items():
        for requirement in requirements:
            matrix.setdefault(requirement, []).append(cell_id)
    return matrix


def check_contract() -> list[str]:
    """Everything the canary must satisfy before it may be proposed for authorization."""

    problems: list[str] = []
    cells = canary_cells()
    if len(cells) != len(CANARY_ORDER):
        problems.append(f"{len(cells)} cells, {len(CANARY_ORDER)} planned")
    if len({cell.cell_id for cell in cells}) != len(cells):
        problems.append("the canary lists the same cell twice")
    if set(CANARY_COVERAGE) != set(CANARY_ORDER):
        missing = sorted(set(CANARY_ORDER) - set(CANARY_COVERAGE))
        extra = sorted(set(CANARY_COVERAGE) - set(CANARY_ORDER))
        problems.append(f"coverage table disagrees with the order: missing={missing} extra={extra}")
    matrix = coverage_matrix()
    for requirement in REQUIRED_COVERAGE:
        if not matrix.get(requirement):
            problems.append(f"no cell covers {requirement!r}")
    # The two layouts and the two engines have to be present, not merely different arms.
    arm_ids = {cell.arm_id for cell in cells}
    for required in ("N0", "N1", "A0", "A1-L", "A1-P", "A2-P"):
        if required not in arm_ids:
            problems.append(f"arm {required} has no canary cell")
    plan = canary_plan()
    if plan.budget_profile_digest != CANDIDATE_PROFILE.digest():
        problems.append("the canary does not bind the candidate budget profile")
    if plan.estimated_cost_ceiling_usd is None or plan.estimated_cost_ceiling_usd <= 0:
        problems.append("the canary has no usable cost stop")
    if CANARY_APPROVAL["status"] == "APPROVED":
        problems.append("the canary approval must ship pending")
    for key in ("approved_by", "approval_commit", "approved_at"):
        if CANARY_APPROVAL.get(key) is not None:
            problems.append(f"the canary template must leave {key} empty")
    # The negative task must be the state-machine refactor, and the selective arm must be the one
    # that is expected to refuse it.
    if not any(
        cell.task_id == "closure2-state-machine-refactor" and cell.arm_id == "A2-P"
        for cell in cells
    ):
        problems.append("the negative-control cell is not N1::A2-P")
    if not any(
        cell.task_id == "closure2-pipeline-migration" and cell.arm_id == "A2-P" for cell in cells
    ):
        problems.append("the positive selective cell is not P1::A2-P")
    return problems


def contract_payload() -> dict[str, Any]:
    """The offline artifact S08 can be authorized against, and nothing else."""

    plan = canary_plan()
    return {
        "kind": "closure2_s08_canary_contract",
        "stage": "closure-2-s07",
        "status": "PREPARED_PENDING_AUTHORIZATION",
        "canary_id": S08_CANARY_PLAN_ID,
        "plan_id": plan.plan_id,
        "plan_digest": plan.digest(),
        "plan_revision": plan.runner_revision,
        "authorization_status": CANARY_APPROVAL["status"],
        "authorized": False,
        "live_runnable_reason": (
            "the plan is wired and refused: pilot_authorization_gate rejects it because the record "
            "is pending, and no other approval in the repository names this digest"
        ),
        "benchmark_id": plan.benchmark_id,
        "preregistration_digest": plan.preregistration_digest,
        "budget_profile": {
            **CANDIDATE_PROFILE.payload(),
            "digest": CANDIDATE_PROFILE.digest(),
        },
        "worker_max_turns": CANDIDATE_PROFILE.multi_worker_max_turns,
        "per_cell_token_budget": CANDIDATE_PROFILE.cell_token_budget,
        "single_agent_max_turns": CANDIDATE_PROFILE.single_max_turns,
        "attempts": 1,
        "repetitions": list(plan.repetitions),
        "cell_count": len(CANARY_ORDER),
        "execution_order": list(CANARY_ORDER),
        "cells": [
            {
                "order": index + 1,
                "cell_id": cell.cell_id,
                "task_id": cell.task_id,
                "arm_id": cell.arm_id,
                "engine": cell.spec.runner,
                "topology": cell.spec.topology,
                "prompt_layout": cell.spec.prompt_layout,
                "route": cell.spec.requested_route,
                "covers": list(CANARY_COVERAGE[cell.cell_id]),
            }
            for index, cell in enumerate(canary_cells())
        ],
        "coverage": coverage_matrix(),
        "coverage_complete": all(coverage_matrix().get(name) for name in REQUIRED_COVERAGE),
        "acceptance_criteria": [dict(item) for item in ACCEPTANCE_CRITERIA],
        "evidence_contract": {key: list(values) for key, values in EVIDENCE_CONTRACT.items()},
        "cost": cost_block(),
        "exclusions": {
            "declared_before_execution": True,
            "statement": (
                "canary cells are structure evidence. They are excluded from the S11 aggregate even "
                "when they pass, because they are a single unreplicated observation of a path the "
                "formal matrix measures three times."
            ),
            "formal_identity_note": (
                "the canary repeats the (cell, repetition 0) identities of formal r1. The formal plan "
                "still runs them: a canary result is never reused as a formal sample, and the formal "
                "r1 claim is a separate one-shot claim on a separate plan digest."
            ),
        },
        "not_authorized": [
            "no model request is made by this module",
            "no sandbox is created by this module",
            "preparing the contract authorizes nothing: S08 stays closed until the record above is "
            "approved by the owner in its own commit",
        ],
        "authorization_template": CANARY_APPROVAL,
        "contract_problems": check_contract(),
        "model_calls": 0,
        "sandbox_runs": 0,
    }


def write_contract(path: Path) -> dict[str, Any]:
    payload = contract_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "closure-2 S08 canary contract: offline preparation only. "
            "This writes a contract; it never starts a run."
        )
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    payload = write_contract(Path(args.out))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "authorized": payload["authorized"],
                "contract_problems": payload["contract_problems"],
                "coverage_complete": payload["coverage_complete"],
                "cells": payload["cell_count"],
                "plan_digest": payload["plan_digest"],
                "proposed_stop_usd": payload["cost"]["proposed_stop"]["usd"],
                "model_calls": 0,
                "sandbox_runs": 0,
                "out": str(args.out),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not payload["contract_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACCEPTANCE_CRITERIA",
    "CANARY_APPROVAL",
    "CANARY_COVERAGE",
    "CANARY_ORDER",
    "CANARY_PLAN_REVISION",
    "EVIDENCE_CONTRACT",
    "REQUIRED_COVERAGE",
    "CanaryContractError",
    "authorization_template",
    "canary_cells",
    "canary_plan",
    "check_contract",
    "contract_payload",
    "coverage_matrix",
    "write_contract",
]
