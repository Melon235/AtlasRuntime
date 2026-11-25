"""Stage C1-B: one cell, one attempt, two mechanism fixes.

What changed and why
--------------------
Stage C1 ended A2. The audit (``experiments/stage_c1b/trace_audit.py``) shows the failure was not
model capability on the task and not the 20-turn cap by itself:

* the CSV worker finished both of its files by turn 5 and then spent 14 turns — 70% of its budget —
  on interpreter commands the ownership policy refuses (`python -c`, `py_compile`, `compileall`);
* the JSON worker finished by turn 6, refined twice more, tried two scratch files outside its
  declared paths, and lost its last turn to the cap while writing its closing sentence;
* the Main integrated, passed the public suite at turn 9, then spent five turns on the same refused
  interpreter commands and 116,664 input tokens (67% of its budget) after its last meaningful write,
  until the shared budget refused request 18.

So two contracts were under-specified, and both are fixed here:

1. the **worker completion contract** (`atlasos.workers.catalog.CODING_INSTRUCTIONS`): what to
   verify with, what a refusal means, what not to create, and the six-field closing report;
2. the **Main verification and termination discipline** (`main-v2.2-rc1-c1b`, one replaced prompt
   section): the public suite is the authorized execution path, interpreter shells are refused here,
   a pass is necessary but not sufficient, and turns must be reserved for delivery.

This is a **combined** intervention. A run on this plan cannot be read as a single-variable ablation
of either change; the stage's report says so.

What did not change
-------------------
Goal bytes, Main turns, worker turns, shared token budget, cost ceilings, task, fixture, public
suite, oracle, router, effective grant (`plugins/`), exposure and provider. The grant, the goal and
the task facts are *reused* from ``experiments.stage_c1.plan`` rather than restated, so the two
plans cannot drift apart on the parts that are supposed to be identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from experiments.evaluation.models import text_digest
from experiments.stage_b_plus import authority as authority_module
from experiments.stage_b_plus import contract
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import prompts

DESIGN_VERSION = "c1b-1.0.0"
STAGE_ID = "C1B"

PLAN_ID = "stage-c1b-coding-delegation-completion-v1"
SCOPE = "C1B_COMPLETION_CONTROL_ONLY"

TASK_ID = c1.TASK_ID
ARM_ID = c1.ARM_ID
REPETITION = 1
ATTEMPT_LIMIT = 1

#: The two interventions, stated once. ``combined`` is what the report must say: this plan does not
#: isolate either change.
INTERVENTION_KINDS: tuple[str, ...] = ("worker_completion_contract", "main_verification_discipline")
INTERVENTION_KIND = "combined_mechanism_fix"

DOES_NOT_MODIFY: tuple[str, ...] = (
    "experiments/stage_b_plus/contract.py::BUDGET",
    "experiments/stage_b_plus/contract.py::TASK_WRITE_SCOPE",
    "experiments/stage_b_plus/plan-canary.json",
    "experiments/stage_b_plus/plan-formal.json",
    "experiments/stage_b_plus/protocol.json",
    "experiments/evaluation/closure2/*",
    "src/atlasos/delegation/router.py",
    "src/atlasos/ownership/*",
)

#: Candidate envelope, unchanged from Stage C1 on purpose: the audit shows the token stop was
#: reached through refused verification, not through necessary implementation work.
ENVELOPE_CANDIDATE: dict[str, Any] = {
    **dict(c1.ENVELOPE_CANDIDATE),
    "basis": (
        "unchanged from Stage C1: the budget was exhausted by refused verification turns, so a "
        "larger ceiling would have bought more refused attempts rather than completion"
    ),
}


def task() -> Any:
    return c1.task()


def goal_text() -> str:
    """Byte-identical to Stage C1: the intervention is not the user goal this time."""

    return c1.goal_text()


def goal_digest() -> str:
    return c1.goal_digest()


def main_prompt() -> dict[str, str]:
    return {"version": prompts.V22_VERSION, "text": prompts.V22_TEXT}


def prompt_digest() -> str:
    return prompts.V22_DIGEST


def worker_contract() -> dict[str, Any]:
    """The worker prompt contract a cell renders, identified by source and by bytes."""

    from atlasos.workers.catalog import (
        CODING_CONTRACT_VERSION,
        CODING_INSTRUCTIONS,
        WorkerCatalog,
    )
    from atlasos.workers.models import WorkerTaskContext
    from atlasos.workers.renderer import render_worker_context

    profile = WorkerCatalog().profiles[contract.CODING]
    canonical = WorkerTaskContext(task_id="c1b-contract", objective="", background=None)
    rendered = render_worker_context(profile, canonical)
    return {
        "profile_id": contract.CODING,
        "contract_version": CODING_CONTRACT_VERSION,
        "instructions_digest": "sha256:" + text_digest(CODING_INSTRUCTIONS),
        "rendered_prompt_digest": "sha256:" + text_digest(rendered),
        "rendered_prompt_chars": len(rendered),
        "base_tools": list(profile.base_tools),
        "profile_max_turns": profile.max_turns,
        "source": "src/atlasos/workers/catalog.py",
    }


def cells() -> list[dict[str, Any]]:
    from experiments.stage_benchmark import matrix

    base = matrix.build_plan(
        [TASK_ID],
        [ARM_ID],
        repetitions=REPETITION,
        seed=int(contract.BUDGET["seed"]),
    )
    arm = contract.ARMS_BY_ID[ARM_ID]
    cells_out: list[dict[str, Any]] = []
    for order, cell in enumerate(base, start=1):
        cells_out.append(
            {
                **cell,
                "order": order,
                "cell_id": f"c1b.{cell['cell_id']}",
                "scope": SCOPE,
                "plan_id": PLAN_ID,
                "is_benchmark_sample": False,
                "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
                "goal_id": c1.GOAL_ID,
                "goal": goal_text(),
                "goal_digest": goal_digest(),
                "intervention_digest": c1.activation_digest(),
                "intervention_kind": INTERVENTION_KIND,
                "interventions": list(INTERVENTION_KINDS),
                # The prompt travels with the cell, so the plan digest covers the bytes that ran.
                "main_prompt": main_prompt(),
                "main_prompt_version": prompts.V22_VERSION,
                "main_prompt_digest": prompt_digest(),
                "worker_profile": arm.profile_id,
                "worker_exposure": arm.exposure,
                "worker_contract_digest": worker_contract()["rendered_prompt_digest"],
                "attempt_limit": ATTEMPT_LIMIT,
                "child_write_grants": c1.effective_grant(),
                "grant_normalization": c1.grant_record(),
            }
        )
    return cells_out


def plan_record() -> dict[str, Any]:
    from experiments.stage_benchmark import matrix

    plan_cells = cells()
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
        "claim_namespace": f"c1b-claim/{PLAN_ID}",
        "runs_subdir": f"runs/stage-c1b/{PLAN_ID}",
        "evidence_subdir": f"evidence/stage-c1b/{PLAN_ID}",
        "is_benchmark_sample": False,
        "is_diagnostic": True,
        "attempt_limit": ATTEMPT_LIMIT,
        "nominal_token_capacity": len(plan_cells) * int(contract.BUDGET["token_budget"]),
        "execution_contract": dict(contract.EXECUTION_CONTRACT),
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "intervention": {
            "kind": INTERVENTION_KIND,
            "parts": list(INTERVENTION_KINDS),
            "goal_changed": False,
            "main_prompt_version": prompts.V22_VERSION,
            "main_prompt_digest": prompt_digest(),
            "main_prompt_section_replaced": "## TESTING AND RECOVERY",
            "worker_contract": worker_contract(),
            "budget_changed": False,
            "turns_changed": False,
            "grant_changed": False,
            "router_changed": False,
            "single_variable_ablation": False,
        },
        "goal_id": c1.GOAL_ID,
        "goal_digest": goal_digest(),
        "write_grant": c1.grant_record(),
        "main_prompt": {
            "version": prompts.V22_VERSION,
            "digest": prompt_digest(),
            "chars": len(prompts.V22_TEXT),
            "derived_from": contract.BPLUS_VERSION,
            "derived_from_digest": contract.protocol_document()["prompts"]["p3-bplus"]["digest"],
        },
        "task": {
            "task_id": TASK_ID,
            "family": task().family,
            "write_scope": list(task().allowed_paths),
            "deliverables": list(task().deliverables),
            "forbidden_paths": list(task().forbidden_paths),
            "public_tests": sorted(task().public_tests),
        },
        "budget": dict(contract.BUDGET),
        "budget_digest": authority_module.digest_of(dict(contract.BUDGET)),
        "envelope_candidate": dict(ENVELOPE_CANDIDATE),
        "does_not_modify": list(DOES_NOT_MODIFY),
        "authorization_migrates": False,
    }


def validate(plan: Mapping[str, Any] | None = None) -> list[str]:
    record = dict(plan or plan_record())
    problems: list[str] = []

    if record.get("plan_id") != PLAN_ID or record.get("scope") != SCOPE:
        problems.append("the plan is not the C1-B completion-control identity")
    if record.get("stage") != STAGE_ID:
        problems.append("the plan does not declare its own stage")
    if record.get("is_benchmark_sample") is not False:
        problems.append("the C1-B cell would be counted as a benchmark sample")

    cells_value = list(record.get("cells") or [])
    if len(cells_value) != 1:
        return [*problems, f"the plan has {len(cells_value)} cells; the design is exactly one"]
    cell = cells_value[0]
    from experiments.stage_benchmark import matrix

    if record.get("digest") != matrix.plan_digest(cells_value):
        problems.append("the plan digest does not recompute from its cells")
    if cell.get("task_id") != TASK_ID or cell.get("arm_id") != ARM_ID:
        problems.append("the cell is not P2 on arm C3")
    if cell.get("scope") != SCOPE or cell.get("plan_id") != PLAN_ID:
        problems.append("the cell does not carry the plan's own identity")
    if cell.get("attempt_limit") != 1:
        problems.append("the cell does not declare one attempt")
    if cell.get("is_benchmark_sample") is not False:
        problems.append("a C1-B cell is marked as a benchmark sample")

    # the goal is untouched: this stage's intervention is not the user goal
    if cell.get("goal") != goal_text() or cell.get("goal_digest") != goal_digest():
        problems.append("the goal is not the frozen Stage C1 goal")
    if goal_digest() != "sha256:1496055dfef64496f2efb570b835cbb0936a32bf67ba2df5b97a1e1e1d508e07":
        problems.append("the goal digest moved away from the approved Stage C1 goal")

    # the prompt is the new bytes, and it is a *derived* edit of the B+ prompt
    prompt = cell.get("main_prompt") or {}
    if prompt.get("version") != prompts.V22_VERSION or prompt.get("text") != prompts.V22_TEXT:
        problems.append("the cell does not carry the C1-B Main prompt")
    if cell.get("main_prompt_digest") != prompt_digest():
        problems.append("the cell's prompt digest does not match the frozen prompt")
    if prompt_digest() == contract.protocol_document()["prompts"]["p3-bplus"]["digest"]:
        problems.append("the C1-B prompt is identical to the B+ prompt")
    if prompts.V22_TEXT.count(prompts.SECTION_START) != 1:
        problems.append("the C1-B prompt no longer has exactly one replaced section")
    base = contract.prompt_text("p3-bplus")
    if not prompts.V22_TEXT.endswith(base[base.index(prompts.SECTION_END) :]):
        problems.append("the C1-B prompt changed bytes after the replaced section")
    if not prompts.V22_TEXT.startswith(base[: base.index(prompts.SECTION_START)]):
        problems.append("the C1-B prompt changed bytes before the replaced section")

    # the worker contract is the new one, and the profile is unchanged apart from its instructions
    contract_now = worker_contract()
    if cell.get("worker_contract_digest") != contract_now["rendered_prompt_digest"]:
        problems.append("the cell does not carry the rendered worker prompt digest")
    if set(contract_now["base_tools"]) != set(contract_of_coding_tools()):
        problems.append("the coding profile's tool surface changed")
    if contract_now["profile_max_turns"] != 8:
        problems.append("the shipped profile turn cap changed")

    # the grant is the C1 grant, unchanged
    bound = {
        str(profile_id): tuple(str(path) for path in paths)
        for profile_id, paths in (cell.get("child_write_grants") or {}).items()
    }
    if bound != c1.effective_grant():
        problems.append("the effective grant is not the Stage C1 grant")
    if cell.get("grant_normalization") != c1.grant_record():
        problems.append("the grant normalisation record is not the Stage C1 record")

    # the budget and the envelope
    if record.get("budget") != dict(contract.BUDGET):
        problems.append("the plan does not run on the frozen Stage B+ budget")
    if record.get("budget_digest") != authority_module.digest_of(dict(contract.BUDGET)):
        problems.append("the budget digest does not recompute")
    envelope = record.get("envelope_candidate") or {}
    if envelope.get("approved") is not False:
        problems.append("the stage proposes a cost ceiling as if it were approved")
    for field in ("cost_stop_usd", "per_cell_cost_stop_usd"):
        value = envelope.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            problems.append(f"the candidate {field} is not finite and positive")
    if int(envelope.get("token_stop") or 0) != record.get("nominal_token_capacity"):
        problems.append("the candidate token stop is not the nominal capacity")

    # the intervention is declared combined, not an ablation
    intervention = record.get("intervention") or {}
    if intervention.get("kind") != INTERVENTION_KIND:
        problems.append("the intervention is not declared as a combined fix")
    if intervention.get("single_variable_ablation") is not False:
        problems.append("the intervention claims to be a single-variable ablation")
    if (
        intervention.get("budget_changed") is not False
        or intervention.get("turns_changed") is not False
    ):
        problems.append("the plan claims a budget or turn change it does not make")
    if record.get("authorization_migrates") is not False:
        problems.append("the plan claims an existing authorization migrates to it")
    return problems


def contract_of_coding_tools() -> tuple[str, ...]:
    """The tool surface the coding profile had before this stage; a change is not authorised."""

    return (
        "tool_search",
        "dispatch_action",
        "file_read",
        "file_write",
        "file_edit",
        "grep",
        "list_files",
        "bash",
    )


__all__ = [
    "ARM_ID",
    "ATTEMPT_LIMIT",
    "DESIGN_VERSION",
    "DOES_NOT_MODIFY",
    "ENVELOPE_CANDIDATE",
    "INTERVENTION_KIND",
    "INTERVENTION_KINDS",
    "PLAN_ID",
    "REPETITION",
    "SCOPE",
    "STAGE_ID",
    "TASK_ID",
    "cells",
    "contract_of_coding_tools",
    "goal_digest",
    "goal_text",
    "main_prompt",
    "plan_record",
    "prompt_digest",
    "task",
    "validate",
    "worker_contract",
]
