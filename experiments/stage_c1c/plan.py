"""Stage C1-C: the completion headroom control — one cell, one attempt, one resource parameter.

What this stage is
------------------
Stage C1-B ran the P2 coding-delegation cell once and ended **A3**
(``WORKER_COMPLETED_BUT_INTEGRATION_FAILED``) for a reason that is not the delegation mechanism:

* two coding workers were admitted, both completed, both produced a consumable completion report;
* the Main read both handoffs, consumed the worker code, integrated it, and passed the public suite;
* it kept editing afterwards, so that pass did not describe the exported workspace;
* the independent oracle scored the export and returned **FAIL**;
* the Main was refused its seventeenth request by the shared budget: 485,340 of 500,000 tokens used,
  14,660 left, the request estimated at 26,501.

L1-L5 therefore held and the run died on *headroom*, not on delegation. This stage changes exactly
one runtime parameter — the shared nominal token budget, 500,000 → 750,000 — and nothing else. It
exists to answer one question: with enough budget to finish the integration it had already started,
does the existing mechanism reach **A4**?

What is *reused*, not restated
------------------------------
Goal bytes, the task, the arm, the Main prompt, the worker completion contract, the effective grant
and every other run parameter are taken from :mod:`experiments.stage_c1b.plan` and
:mod:`experiments.stage_c1.plan` rather than copied, so the two plans cannot drift apart on the
parts that are supposed to be identical. :func:`validate` re-checks the reused digests against the
frozen C1-B values.

What the budget seam is (and is not)
------------------------------------
``contract.BUDGET`` stays at the historical 500,000: Stage B+, C1 and C1-B keep their frozen budget
and their frozen budget digest. The C1-C cell binds the *complete* effective budget record into
itself (:func:`budget`), so it is inside the plan digest, and
``contract.token_budget_for(cell)`` is the single place the number becomes
``GoalRunOptions.token_budget`` — the value ``run_goal`` turns into
``RunBudgetState(limit_tokens=…)``. No environment variable, no monkeypatch, no second executor.

This is one cell and one attempt. It is a mechanism diagnostic: it does not support a
statistical claim, and it does not "prove" that 750,000 is the right number for anything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from experiments.stage_b_plus import authority as authority_module
from experiments.stage_b_plus import contract
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import plan as c1b
from experiments.stage_c1b import prompts

DESIGN_VERSION = "c1c-1.0.0"
STAGE_ID = "C1C"

PLAN_ID = "stage-c1c-completion-headroom-control-v1"
SCOPE = "C1C_HEADROOM_CONTROL_ONLY"

TASK_ID = c1.TASK_ID
ARM_ID = c1.ARM_ID
REPETITION = 1
ATTEMPT_LIMIT = 1

INTERVENTION_KIND = "resource_headroom_control"
INTERVENTION_KIND_PARTS: tuple[str, ...] = ("shared_token_budget",)
RESOURCE = "shared_nominal_token_budget"

#: The one value that moves, and the value it moves from.
TOKEN_BUDGET_BEFORE = int(contract.BUDGET["token_budget"])
TOKEN_BUDGET_AFTER = 750_000

#: Cost stops are the matching authorization boundary, kept separate from the token decision. They
#: stay candidates: the Owner's choice of 750,000 tokens is not an execution authorization.
COST_STOP_CANDIDATE = 0.25
PER_CELL_COST_STOP_CANDIDATE = 0.25

#: Stage C1-B's measured numbers, quoted so the rationale is inside the frozen identity rather than
#: only in prose. A later edit that changes the story has to change the digest.
C1B_OBSERVED: dict[str, Any] = {
    "plan_id": "stage-c1b-coding-delegation-completion-v1",
    "plan_digest": "sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c",
    "outcome": "A3_WORKER_COMPLETED_BUT_INTEGRATION_FAILED",
    "system_tokens": 485_340,
    "shared_budget": 500_000,
    "remaining_tokens": 14_660,
    "next_request_estimated_tokens": 26_501,
    "rejected_request": 17,
    "worker_turns": [15, 14],
    "admitted_workers": 2,
    "completed_workers": 2,
    "valid_completion_reports": 2,
    "handoff_reads": 2,
    "ownership_refusals": 4,
    "main_peak_context_tokens": 19_393,
    "cost_usd": 0.104162,
    "wall_clock_seconds": 128.89,
    "independent_oracle": "FAIL",
    "execution_integrity": "PASS",
}

DOES_NOT_MODIFY: tuple[str, ...] = (
    "experiments/stage_b_plus/contract.py::BUDGET",
    "experiments/stage_b_plus/contract.py::TASK_WRITE_SCOPE",
    "experiments/stage_b_plus/plan-canary.json",
    "experiments/stage_b_plus/plan-formal.json",
    "experiments/stage_b_plus/protocol.json",
    "experiments/stage_c1/plan.json",
    "experiments/stage_c1b/plan.json",
    "experiments/stage_c1b/prompts.py",
    "experiments/evaluation/closure2/*",
    "src/atlasos/delegation/router.py",
    "src/atlasos/ownership/*",
    "src/atlasos/workers/catalog.py",
    "src/atlasos/app/main_driven.py",
)


# -- the one intervention ---------------------------------------------------------------------


def intervention_record() -> dict[str, Any]:
    """The intervention, stated once, with every parameter that did *not* move."""

    return {
        "kind": INTERVENTION_KIND,
        "parts": list(INTERVENTION_KIND_PARTS),
        "resource": RESOURCE,
        "shared_token_budget_before": TOKEN_BUDGET_BEFORE,
        "shared_token_budget_after": TOKEN_BUDGET_AFTER,
        "main_prompt_changed": False,
        "worker_contract_changed": False,
        "goal_changed": False,
        "task_changed": False,
        "turns_changed": False,
        "router_changed": False,
        "grant_changed": False,
        "oracle_changed": False,
        "single_variable_ablation": True,
        "statistical_claim": False,
        "inference": (
            "one cell, one attempt: a mechanism diagnostic on whether more headroom converts the "
            "C1-B A3 into a delivered run. It is not a significance claim and not an estimate of a "
            "causal effect of budget size"
        ),
        "cost_stop": {
            "changed": True,
            "before": float(c1.ENVELOPE_CANDIDATE["cost_stop_usd"]),
            "after_candidate": COST_STOP_CANDIDATE,
            "per_cell_before": float(c1.ENVELOPE_CANDIDATE["per_cell_cost_stop_usd"]),
            "per_cell_after_candidate": PER_CELL_COST_STOP_CANDIDATE,
            "approved": False,
            "note": "the matching authorization boundary for the resource change; a candidate",
        },
        "basis": (
            "C1-B used 485,340 of 500,000 tokens and was refused request 17 with 14,660 tokens "
            "left against an estimate of 26,501; the workers had already completed and the Main "
            "had already integrated, so the stop was headroom, not mechanism. 750,000 is +50% "
            "over the nominal budget and leaves 264,660 tokens over the observed consumption"
        ),
    }


def intervention_digest() -> str:
    return authority_module.digest_of(intervention_record())


# -- budget -----------------------------------------------------------------------------------


def budget() -> dict[str, Any]:
    """The complete effective run configuration for this cell.

    ``contract.BUDGET`` with one value replaced. Complete rather than a delta: the cell then states
    every parameter it runs with, and a reader does not have to apply an inheritance rule to know
    what the provider, the turns or the sandbox were.
    """

    record = dict(contract.BUDGET)
    record["token_budget"] = TOKEN_BUDGET_AFTER
    return record


def budget_digest() -> str:
    return authority_module.digest_of(budget())


# -- reused identities ------------------------------------------------------------------------


def task() -> Any:
    return c1.task()


def goal_text() -> str:
    """Byte-identical to Stage C1 and Stage C1-B: the goal is not the intervention."""

    return c1.goal_text()


def goal_digest() -> str:
    return c1.goal_digest()


def main_prompt() -> dict[str, str]:
    """Byte-identical to Stage C1-B: the prompt is not the intervention."""

    return c1b.main_prompt()


def prompt_digest() -> str:
    return c1b.prompt_digest()


def worker_contract() -> dict[str, Any]:
    """Byte-identical to Stage C1-B: the worker contract is not the intervention."""

    return c1b.worker_contract()


def delegation_record() -> dict[str, Any]:
    config = contract.delegation_config()
    return {
        "max_parallel_children": config.max_parallel_children,
        "max_children_per_call": config.max_children_per_call,
        "max_delegation_rounds": config.max_delegation_rounds,
        "child_max_turns": config.child_max_turns,
        "child_timeout_seconds": config.child_timeout_seconds,
        "result_preview_chars": config.result_preview_chars,
    }


# -- plan -------------------------------------------------------------------------------------


def cells() -> list[dict[str, Any]]:
    from experiments.stage_benchmark import matrix

    base = matrix.build_plan(
        [TASK_ID],
        [ARM_ID],
        repetitions=REPETITION,
        seed=int(contract.BUDGET["seed"]),
    )
    arm = contract.ARMS_BY_ID[ARM_ID]
    declared = budget()
    digest = budget_digest()
    cells_out: list[dict[str, Any]] = []
    for order, cell in enumerate(base, start=1):
        cells_out.append(
            {
                **cell,
                "order": order,
                "cell_id": f"c1c.{cell['cell_id']}",
                "scope": SCOPE,
                "plan_id": PLAN_ID,
                "is_benchmark_sample": False,
                "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
                # The goal, the prompt, the worker contract and the grant travel with the cell, so
                # the plan digest covers the bytes that ran and the budget that ran them.
                "goal_id": c1.GOAL_ID,
                "goal": goal_text(),
                "goal_digest": goal_digest(),
                "activation_digest": c1.activation_digest(),
                "intervention_kind": INTERVENTION_KIND,
                "interventions": list(INTERVENTION_KIND_PARTS),
                "intervention_digest": intervention_digest(),
                "main_prompt": main_prompt(),
                "main_prompt_version": prompts.V22_VERSION,
                "main_prompt_digest": prompt_digest(),
                "worker_profile": arm.profile_id,
                "worker_exposure": arm.exposure,
                "worker_contract_digest": worker_contract()["rendered_prompt_digest"],
                "attempt_limit": ATTEMPT_LIMIT,
                "child_write_grants": c1.effective_grant(),
                "grant_normalization": c1.grant_record(),
                # The effective budget and its identity, inside the digest.
                "budget": declared,
                "budget_digest": digest,
                "token_budget": TOKEN_BUDGET_AFTER,
            }
        )
    return cells_out


def plan_record() -> dict[str, Any]:
    from experiments.stage_benchmark import matrix

    plan_cells = cells()
    declared = budget()
    digest = budget_digest()
    return {
        "stage": STAGE_ID,
        "design_version": DESIGN_VERSION,
        "plan_id": PLAN_ID,
        "scope": SCOPE,
        "tasks": [TASK_ID],
        "arms": [ARM_ID],
        "repetitions": REPETITION,
        "cells_count": len(plan_cells),
        "cells": plan_cells,
        "digest": matrix.plan_digest(plan_cells),
        "balance": contract.balance(plan_cells, (ARM_ID,)),
        "claim_namespace": f"c1c-claim/{PLAN_ID}",
        "runs_subdir": f"runs/stage-c1c/{PLAN_ID}",
        "evidence_subdir": f"evidence/stage-c1c/{PLAN_ID}",
        "is_benchmark_sample": False,
        "is_diagnostic": True,
        "attempt_limit": ATTEMPT_LIMIT,
        # One cell, one shared nominal budget: the capacity *is* the effective budget.
        "nominal_token_capacity": len(plan_cells) * int(declared["token_budget"]),
        "execution_contract": dict(contract.EXECUTION_CONTRACT),
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "intervention": intervention_record(),
        "goal_id": c1.GOAL_ID,
        "goal_digest": goal_digest(),
        "write_grant": c1.grant_record(),
        "main_prompt": {
            "version": prompts.V22_VERSION,
            "digest": prompt_digest(),
            "chars": len(prompts.V22_TEXT),
            "reused_from": "stage-c1b",
            "unchanged_from_c1b": True,
        },
        "worker_contract": worker_contract(),
        "delegation": delegation_record(),
        "task": {
            "task_id": TASK_ID,
            "family": task().family,
            "write_scope": list(task().allowed_paths),
            "deliverables": list(task().deliverables),
            "forbidden_paths": list(task().forbidden_paths),
            "public_tests": sorted(task().public_tests),
            "oracle_tests": sorted(task().oracle_tests),
        },
        "budget": declared,
        "budget_digest": digest,
        "budget_intervention": {
            "before": TOKEN_BUDGET_BEFORE,
            "after": TOKEN_BUDGET_AFTER,
            "delta": TOKEN_BUDGET_AFTER - TOKEN_BUDGET_BEFORE,
            "relative": round(TOKEN_BUDGET_AFTER / TOKEN_BUDGET_BEFORE - 1.0, 6),
        },
        "c1b_observed": dict(C1B_OBSERVED),
        "envelope_candidate": {
            "cost_stop_usd": COST_STOP_CANDIDATE,
            "per_cell_cost_stop_usd": PER_CELL_COST_STOP_CANDIDATE,
            "token_stop": TOKEN_BUDGET_AFTER,
            "approved": False,
            "basis": (
                "one cell, one attempt; the cost ceiling is about 2.4x the C1-B observed cost "
                "(USD 0.104162) and both cost fields stay candidates until the Owner signs"
            ),
        },
        "does_not_modify": list(DOES_NOT_MODIFY),
        "authorization_migrates": False,
    }


# -- validation -------------------------------------------------------------------------------


def _budget_problems(record: Mapping[str, Any], cell: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    declared = record.get("budget") or {}
    expected = budget()
    if declared != expected:
        problems.append("the plan does not declare the effective 750K budget record")
    if declared.get("token_budget") != TOKEN_BUDGET_AFTER:
        problems.append("the plan's token budget is not 750000")
    if int(contract.BUDGET["token_budget"]) != TOKEN_BUDGET_BEFORE:
        problems.append("the frozen Stage B+ budget moved; it must stay at 500000")
    differences = {
        key: value for key, value in expected.items() if contract.BUDGET.get(key) != value
    }
    if set(differences) != {"token_budget"}:
        problems.append(f"the effective budget changes more than the token budget: {differences}")
    if record.get("budget_digest") != authority_module.digest_of(dict(declared)):
        problems.append("the budget digest does not recompute from the declared budget")
    if record.get("budget_digest") == authority_module.digest_of(dict(contract.BUDGET)):
        problems.append(
            "the budget digest is the frozen 500K digest; the intervention is not bound"
        )
    if int(record.get("nominal_token_capacity") or 0) != TOKEN_BUDGET_AFTER:
        problems.append("the nominal token capacity is not one cell's 750000")
    if cell.get("budget") != declared:
        problems.append("the cell and the plan disagree about the effective budget")
    if cell.get("budget_digest") != record.get("budget_digest"):
        problems.append("the cell does not carry the plan's budget digest")
    if int(cell.get("token_budget") or 0) != TOKEN_BUDGET_AFTER:
        problems.append("the cell's token budget is not 750000")
    if contract.token_budget_for(cell) != TOKEN_BUDGET_AFTER:
        problems.append("the production resolver does not read 750000 from this cell")
    if contract.token_budget_for({}) != TOKEN_BUDGET_BEFORE:
        problems.append("a cell without a budget no longer resolves to the frozen 500000")
    return problems


def _unchanged_problems(cell: Mapping[str, Any]) -> list[str]:
    """Every parameter that must be byte-identical to Stage C1-B."""

    problems: list[str] = []
    c1b_plan = c1b.plan_record()
    c1b_cell = c1b_plan["cells"][0]

    if goal_digest() != "sha256:1496055dfef64496f2efb570b835cbb0936a32bf67ba2df5b97a1e1e1d508e07":
        problems.append("the goal digest moved away from the approved Stage C1 goal")
    if cell.get("goal") != c1b_cell.get("goal") or cell.get("goal_digest") != goal_digest():
        problems.append("the executed goal is not the Stage C1-B goal")
    if goal_text() != c1b.goal_text():
        problems.append("the goal bytes differ from Stage C1-B")

    if prompt_digest() != c1b_plan["main_prompt"]["digest"]:
        problems.append("the Main prompt is not the Stage C1-B prompt")
    if cell.get("main_prompt") != c1b_cell.get("main_prompt"):
        problems.append("the cell does not carry the Stage C1-B Main prompt bytes")
    if cell.get("main_prompt_digest") != c1b_cell.get("main_prompt_digest"):
        problems.append("the cell's prompt digest is not the Stage C1-B prompt digest")
    if prompts.V22_TEXT.count(prompts.SECTION_START) != 1:
        problems.append("the reused prompt no longer has exactly one replaced section")

    if worker_contract() != (c1b_plan.get("intervention") or {}).get("worker_contract"):
        problems.append("the worker completion contract is not the Stage C1-B contract")
    if cell.get("worker_contract_digest") != c1b_cell.get("worker_contract_digest"):
        problems.append("the cell does not carry the Stage C1-B rendered worker prompt digest")

    if dict(cell.get("child_write_grants") or {}) != c1b_cell.get("child_write_grants"):
        problems.append("the Host grant is not the Stage C1-B grant")
    if cell.get("grant_normalization") != c1b_cell.get("grant_normalization"):
        problems.append("the grant normalisation record is not the Stage C1-B record")

    if cell.get("task_id") != TASK_ID or cell.get("arm_id") != ARM_ID:
        problems.append("the cell is not P2 on arm C3")
    if cell.get("worker_profile") != c1b_cell.get("worker_profile"):
        problems.append("the worker profile moved away from the Stage C1-B arm")
    if cell.get("worker_exposure") != c1b_cell.get("worker_exposure"):
        problems.append("the exposure level moved away from the Stage C1-B arm")

    # turns, concurrency, router policy and the sandbox: the frozen values, read from the plan
    declared = cell.get("budget") or {}
    for key in (
        "provider",
        "model",
        "main_max_turns",
        "child_max_turns",
        "worker_max_turns",
        "max_concurrent_children",
        "max_children_per_call",
        "max_delegation_rounds",
        "public_test_max_calls",
        "result_preview_chars",
        "tool_error_mode",
        "network",
        "cpu",
        "memory_gib",
        "sandbox_provider",
        "image",
        "router_policy",
        "sandbox_timeout_minutes",
        "matrix_concurrency",
        "seed",
    ):
        if declared.get(key) != contract.BUDGET.get(key):
            problems.append(f"{key} moved away from the frozen value {contract.BUDGET.get(key)!r}")
    if (
        int(declared.get("main_max_turns") or 0) != 20
        or int(declared.get("worker_max_turns") or 0) != 20
    ):
        problems.append("the 20-turn cap is not preserved")
    return problems


def validate(plan: Mapping[str, Any] | None = None) -> list[str]:
    """Every invariant this design promises, checked on the record itself."""

    record = dict(plan or plan_record())
    problems: list[str] = []

    if record.get("plan_id") != PLAN_ID or record.get("scope") != SCOPE:
        problems.append("the plan is not the C1-C headroom-control identity")
    if record.get("stage") != STAGE_ID:
        problems.append("the plan does not declare its own stage")
    if record.get("is_benchmark_sample") is not False:
        problems.append("the C1-C cell would be counted as a benchmark sample")
    if record.get("is_diagnostic") is not True:
        problems.append("the plan is not marked diagnostic")
    if record.get("attempt_limit") != 1:
        problems.append("the plan does not declare one attempt")

    plan_cells = list(record.get("cells") or [])
    if len(plan_cells) != 1:
        return [*problems, f"the plan has {len(plan_cells)} cells; the design is exactly one"]
    cell = plan_cells[0]
    from experiments.stage_benchmark import matrix

    if record.get("digest") != matrix.plan_digest(plan_cells):
        problems.append("the plan digest does not recompute from its cells")
    if cell.get("scope") != SCOPE or cell.get("plan_id") != PLAN_ID:
        problems.append("the cell does not carry the plan's own identity")
    if cell.get("cell_id") != f"c1c.{TASK_ID}.{ARM_ID}.r1":
        problems.append(f"the cell id is {cell.get('cell_id')!r}")
    if cell.get("attempt_limit") != 1:
        problems.append("the cell does not declare one attempt")
    if cell.get("is_benchmark_sample") is not False:
        problems.append("a C1-C cell is marked as a benchmark sample")
    if cell.get("execution_contract") != contract.EXECUTION_CONTRACT_DIGEST:
        problems.append("the cell is not bound to the current execution contract")

    problems.extend(_budget_problems(record, cell))
    problems.extend(_unchanged_problems(cell))

    intervention = record.get("intervention") or {}
    if intervention.get("kind") != INTERVENTION_KIND:
        problems.append("the intervention is not declared as a resource headroom control")
    if intervention.get("shared_token_budget_before") != TOKEN_BUDGET_BEFORE:
        problems.append("the intervention does not state the budget it starts from")
    if intervention.get("shared_token_budget_after") != TOKEN_BUDGET_AFTER:
        problems.append("the intervention does not state the budget it applies")
    for flag in (
        "main_prompt_changed",
        "worker_contract_changed",
        "goal_changed",
        "task_changed",
        "turns_changed",
        "router_changed",
        "grant_changed",
        "oracle_changed",
    ):
        if intervention.get(flag) is not False:
            problems.append(f"the intervention claims it changes {flag}")
    if intervention.get("statistical_claim") is not False:
        problems.append("the intervention claims a statistical result")
    if cell.get("intervention_digest") != intervention_digest():
        problems.append("the cell's intervention digest does not match the frozen record")
    if cell.get("intervention_kind") != INTERVENTION_KIND:
        problems.append("the cell does not declare the intervention kind")

    cost_stop = intervention.get("cost_stop") or {}
    if cost_stop.get("approved") is not False:
        problems.append("the cost stop is presented as approved")
    if float(cost_stop.get("after_candidate") or 0) != COST_STOP_CANDIDATE:
        problems.append("the candidate cost stop differs from the declared one")

    envelope = record.get("envelope_candidate") or {}
    if envelope.get("approved") is not False:
        problems.append("the stage proposes a cost ceiling as if it were approved")
    if int(envelope.get("token_stop") or 0) != TOKEN_BUDGET_AFTER:
        problems.append("the candidate token stop is not the effective 750000")
    for field in ("cost_stop_usd", "per_cell_cost_stop_usd"):
        value = envelope.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            problems.append(f"the candidate {field} is not finite and positive")
    if float(envelope.get("per_cell_cost_stop_usd") or 0) > float(
        envelope.get("cost_stop_usd") or 0
    ):
        problems.append("the per-cell stop is larger than the cumulative stop")

    if list(record.get("tasks") or []) != [TASK_ID] or list(record.get("arms") or []) != [ARM_ID]:
        problems.append("the plan's task/arm lists are not the single P2/C3 cell")
    if record.get("authorization_migrates") is not False:
        problems.append("the plan claims an existing authorization migrates to it")

    # the plan must not be able to impersonate an earlier one
    earlier = {
        "c1": c1.plan_record()["digest"],
        "c1b": c1b.plan_record()["digest"],
        "bplus-canary": contract.scope_plan(contract.SCOPE_CANARY)["digest"],
        "bplus-formal": contract.scope_plan(contract.SCOPE_FORMAL)["digest"],
    }
    if record.get("digest") in set(earlier.values()):
        problems.append("the C1-C plan digest collides with an earlier plan")
    if record.get("budget_digest") in {
        c1.plan_record()["budget_digest"],
        c1b.plan_record()["budget_digest"],
    }:
        problems.append("the C1-C budget digest collides with an earlier plan's")
    return problems


__all__ = [
    "ARM_ID",
    "ATTEMPT_LIMIT",
    "C1B_OBSERVED",
    "COST_STOP_CANDIDATE",
    "DESIGN_VERSION",
    "DOES_NOT_MODIFY",
    "INTERVENTION_KIND",
    "INTERVENTION_KIND_PARTS",
    "PER_CELL_COST_STOP_CANDIDATE",
    "PLAN_ID",
    "REPETITION",
    "RESOURCE",
    "SCOPE",
    "STAGE_ID",
    "TASK_ID",
    "TOKEN_BUDGET_AFTER",
    "TOKEN_BUDGET_BEFORE",
    "budget",
    "budget_digest",
    "cells",
    "delegation_record",
    "goal_digest",
    "goal_text",
    "intervention_digest",
    "intervention_record",
    "main_prompt",
    "plan_record",
    "prompt_digest",
    "task",
    "validate",
    "worker_contract",
]
