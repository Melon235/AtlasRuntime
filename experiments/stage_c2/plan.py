"""Stage C2: coding-aware natural delegation decision — four cells, one decision policy.

What this stage is
------------------
C1, C1-B and C1-C all answered "can the Main delegate when it is *told* to": the user goal
carried an explicit appendix asking for coding workers. C1-C measured the headroom question and
left one thing untested — whether the Main chooses a delegation route on its own when the goal
says nothing about delegation at all.

Stage C2 therefore removes the appendix and varies exactly one thing *inside a task*: the Main
decision prompt.

* **P+** — a task whose two implementation workstreams are genuinely independent
  (``closure2-pipeline-migration``): separate modules, a published frozen interface, disjoint
  writable paths, an integration seam, a public suite and an oracle.
* **N−** — the coupled control (``closure2-state-machine-refactor``): both candidate steps must edit
  one shared state table, so the runtime's own router must refuse a split.
* **B0** — ``main-v2.2-rc1-c1b``, byte for byte: the prompt every C1 cell ran under.
* **B1** — ``main-v3.0-rc1-coding-aware``: the same bytes with three decision sections replaced.

Four cells, one attempt each, fixed order: P+/B0, N−/B0, P+/B1, N−/B1. Each task's pair differs only
in the Main prompt; the cell records are compared field by field and the difference set is checked.

What this stage deliberately does not do
----------------------------------------
No model call, no sandbox, no claim, no new executor, no router or ownership change, no worker
contract change, no new planner or decision agent, no new evaluation framework. The task,
fixture, public suite, oracle, grant, turns, budget, router policy and delegation limits are the
frozen ones; only the prompt and the plan identity are new. The grant is the task's own frozen
write scope, converted to the representation the router's matcher accepts by the repository's
own ``normalize_declared_path`` — the same seam Stage C1 established, applied to two tasks
instead of one.

Why P1 and N1 are reused rather than a new fixture is written up in
:func:`suitability_report`, which measures the selection instead of asserting it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from experiments.stage_b_plus import authority as authority_module
from experiments.stage_b_plus import contract
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import plan as c1b
from experiments.stage_c2 import prompts

DESIGN_VERSION = "c2-1.0.0"
STAGE_ID = "C2"

PLAN_ID = "stage-c2-coding-aware-natural-delegation-v1"
SCOPE = "C2_NATURAL_DELEGATION_DIAGNOSTIC_ONLY"

#: The positive control and the coupled control. Both are frozen Closure-2 fixtures; neither is
#: a new task and neither answer is written by this stage.
P_PLUS_TASK_ID = "closure2-pipeline-migration"
N_MINUS_TASK_ID = "closure2-state-machine-refactor"
TASK_IDS: tuple[str, ...] = (P_PLUS_TASK_ID, N_MINUS_TASK_ID)

ARM_ID = "C3"
PROMPT_ARMS: tuple[str, ...] = ("B0", "B1")
PROMPT_VERSIONS: dict[str, str] = {
    "B0": prompts.B0_VERSION,
    "B1": prompts.V30_VERSION,
}
PROMPT_TEXTS: dict[str, str] = {"B0": prompts.B0_TEXT, "B1": prompts.V30_TEXT}
PROMPT_DIGESTS: dict[str, str] = {"B0": prompts.B0_DIGEST, "B1": prompts.V30_DIGEST}

REPETITION = 1
ATTEMPT_LIMIT = 1

#: Fixed before any run, and not amended afterwards. Grouped by prompt arm so the two baselines are
#: executed first; no cell may be repeated or reconfigured because of another cell's result.
CELL_ORDER: tuple[tuple[str, str], ...] = (
    (P_PLUS_TASK_ID, "B0"),
    (N_MINUS_TASK_ID, "B0"),
    (P_PLUS_TASK_ID, "B1"),
    (N_MINUS_TASK_ID, "B1"),
)

INTERVENTION_KIND = "main_decision_prompt_ablation"
INTERVENTION_KIND_PARTS: tuple[str, ...] = ("main_decision_prompt",)
DECISION_AXIS = "main_decision_prompt"
#: The only cell fields a task's two cells may differ in. ``order`` and ``round`` are execution
#: position, not configuration: they are fixed by the plan, not chosen per arm.
PROMPT_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "order",
        "round",
        "cell_id",
        "prompt_arm",
        "main_prompt",
        "main_prompt_version",
        "main_prompt_digest",
    }
)

#: The frozen shared budget with one value raised: 750,000 per cell, the C1-C headroom value, so the
#: decision question is not confounded with the C1-B budget stop.
TOKEN_BUDGET = 750_000
FROZEN_TOKEN_BUDGET = int(contract.BUDGET["token_budget"])
NOMINAL_TOKEN_CAPACITY = len(CELL_ORDER) * TOKEN_BUDGET
#: Candidate only. The Owner signs a number; this stage proposes one.
COST_STOP_CANDIDATE = 1.00
PER_CELL_COST_STOP_CANDIDATE = 0.25

#: The workspace root every tool, ownership check and child sees inside a project sandbox.
WORKSPACE_ROOT = c1.WORKSPACE_ROOT
GRANT_NORMALIZER = c1.GRANT_NORMALIZER

DOES_NOT_MODIFY: tuple[str, ...] = (
    "experiments/stage_b_plus/contract.py::BUDGET",
    "experiments/stage_b_plus/contract.py::TASK_WRITE_SCOPE",
    "experiments/stage_b_plus/contract.py::EXECUTION_CONTRACT",
    "experiments/stage_b_plus/plan-canary.json",
    "experiments/stage_b_plus/plan-formal.json",
    "experiments/stage_b_plus/protocol.json",
    "experiments/stage_c1/plan.json",
    "experiments/stage_c1b/plan.json",
    "experiments/stage_c1b/prompts.py",
    "experiments/stage_c1c/plan.json",
    "experiments/evaluation/closure2/*",
    "src/atlasos/delegation/router.py",
    "src/atlasos/workers/catalog.py",
    "src/atlasos/ownership/*",
    "src/atlasos/app/main_driven.py",
)

#: How the eventual report must read a run. Not a preference: each entry names the field the runtime
#: actually writes, and :mod:`experiments.stage_c2.checks` verifies those fields exist.
REPORTING_CONTRACT: dict[str, Any] = {
    "version": "c2-reporting-1.0",
    "final_correctness": {
        "source": "the cell audit's oracle verdict (executor CellRun.oracle.status / .passed)",
        "not": (
            "experiments.stage_benchmark.reconstruct verification.oracle / "
            "L6_goal_correctness, which is the in-run static acceptance and reads PASS while an "
            "independent oracle fails"
        ),
        "reported_separately": [
            "static_acceptance",
            "public_test",
            "independent_oracle",
            "main_completion",
            "execution_integrity",
        ],
    },
    "cache": {
        "main": ["main_cache_hit_tokens", "main_cache_miss_tokens"],
        "children": ["delegation.totals.cache_hit_tokens", "delegation.totals.cache_miss_tokens"],
        "rule": (
            "hit and miss are the provider's own usage fields and are part of prompt tokens; they "
            "are never added on top of a prompt total, and UNKNOWN is not zero"
        ),
        "not": "reconstruct child_cache_visibility, which reports UNKNOWN with a split available",
    },
    "handoff": {
        "preview_delivered": [
            "delegation.batches[].result_chars",
            "delegation.batches[].result_truncated",
        ],
        "full_reference_read": "delegate.read calls in the Main transcript",
        "shared_file_consumption": (
            "Main file_read/file_edit on worker-owned paths after the batch; a shared workspace "
            "makes this possible without a delegate.read call"
        ),
        "rule": "the three are counted apart; one is never reported as another",
    },
    "delegation_decision": {
        "natural_proposal": "a delegate_tasks call the goal did not ask for",
        "first_proposal_turn": "the Main turn of that call",
        "not_a_target": "the number of delegations; Direct is a valid decision on either task",
    },
}


# -- budget -----------------------------------------------------------------------------------


def budget() -> dict[str, Any]:
    """The complete effective run configuration shared by all four cells."""

    record = dict(contract.BUDGET)
    record["token_budget"] = TOKEN_BUDGET
    return record


def budget_digest() -> str:
    return authority_module.digest_of(budget())


# -- tasks, goals, grants ---------------------------------------------------------------------


def task(task_id: str) -> Any:
    return contract.TASKS_BY_ID[task_id]


def goal_text(task_id: str) -> str:
    """The task's own public requirement. No appendix, no activation sentence, no hint."""

    return contract.GOAL_OF[task_id]


def goal_digest(task_id: str) -> str:
    return "sha256:" + hashlib.sha256(goal_text(task_id).encode("utf-8")).hexdigest()


def raw_grant(task_id: str) -> dict[str, tuple[str, ...]]:
    """The frozen task write scope, as the B+ contract states it."""

    return {contract.CODING: tuple(contract.TASK_WRITE_SCOPE[task_id])}


def _normalize(grants: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    from atlasos.ownership.paths import normalize_declared_path

    return {
        profile_id: tuple(
            normalize_declared_path(path, workspace_root=WORKSPACE_ROOT) for path in paths
        )
        for profile_id, paths in grants.items()
    }


def effective_grant(task_id: str) -> dict[str, tuple[str, ...]]:
    """The same scope in the representation the runtime's matcher accepts."""

    return _normalize(raw_grant(task_id))


def grant_covers(task_id: str, target: str) -> bool:
    from atlasos.ownership.models import path_is_within
    from atlasos.ownership.paths import normalize_workspace_path

    normalized = normalize_workspace_path(target, workspace_root=WORKSPACE_ROOT)
    granted = effective_grant(task_id)[contract.CODING]
    return any(path_is_within(declared, normalized) for declared in granted)


def grant_record(task_id: str) -> dict[str, Any]:
    """Both representations, the normaliser, and what did not change. Nothing was widened."""

    raw = raw_grant(task_id)
    effective = effective_grant(task_id)
    frozen = task(task_id)
    forbidden = list(frozen.forbidden_paths)
    return {
        "task_id": task_id,
        "raw": {profile_id: list(paths) for profile_id, paths in raw.items()},
        "effective": {profile_id: list(paths) for profile_id, paths in effective.items()},
        "normalizer": GRANT_NORMALIZER,
        "workspace_root": WORKSPACE_ROOT,
        "changes": [
            {"raw": raw_path, "effective": effective_path}
            for profile_id in sorted(raw)
            for raw_path, effective_path in zip(raw[profile_id], effective[profile_id], strict=True)
            if raw_path != effective_path
        ],
        "scope_changed": False,
        "profile_changed": False,
        "matches_frozen_task_scope": set(raw.get(contract.CODING, ()))
        == set(contract.TASK_WRITE_SCOPE[task_id]),
        "denied_by_runtime": [path for path in forbidden if not grant_covers(task_id, path)],
        "forbidden_inside_grant": [path for path in forbidden if grant_covers(task_id, path)],
        "forbidden_inside_grant_protection": (
            "task instruction plus independent oracle, not ownership"
            if any(grant_covers(task_id, path) for path in forbidden)
            else None
        ),
    }


# -- prompts ----------------------------------------------------------------------------------


def main_prompt(prompt_arm: str) -> dict[str, str]:
    if prompt_arm not in PROMPT_ARMS:
        raise KeyError(f"unknown prompt arm {prompt_arm!r}")
    return {"version": PROMPT_VERSIONS[prompt_arm], "text": PROMPT_TEXTS[prompt_arm]}


def prompt_digest(prompt_arm: str) -> str:
    if prompt_arm not in PROMPT_ARMS:
        raise KeyError(f"unknown prompt arm {prompt_arm!r}")
    return PROMPT_DIGESTS[prompt_arm]


def worker_contract() -> dict[str, Any]:
    """The C1-B worker completion contract, unchanged; only its identity is read here."""

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


# -- suitability --------------------------------------------------------------------------------


def _line_count(files: Mapping[str, str]) -> int:
    return sum(len(text.splitlines()) for text in files.values())


def _char_count(files: Mapping[str, str]) -> int:
    return sum(len(text) for text in files.values())


def _independent_pairs(task_id: str) -> list[dict[str, Any]]:
    """Worker pairs that are mutually independent, own disjoint paths, and both own deliverables.

    This is the structural question the P+ selection rests on, computed from the frozen task rather
    than asserted. ``depends_on`` is read in both directions: a pair where either side waits for the
    other is not independent, whatever its write sets look like.
    """

    frozen = task(task_id)
    workers = list(frozen.workers)
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(workers):
        for right in workers[index + 1 :]:
            left_paths = set(left["owned_paths"])
            right_paths = set(right["owned_paths"])
            overlap = sorted(left_paths & right_paths)
            left_deps = set(left["depends_on"])
            right_deps = set(right["depends_on"])
            independent = (
                not overlap
                and right["task_id"] not in left_deps
                and left["task_id"] not in right_deps
            )
            pairs.append(
                {
                    "left": str(left["task_id"]),
                    "right": str(right["task_id"]),
                    "shared_write_paths": overlap,
                    "declared_dependency": sorted(
                        dependency
                        for dependency in (left_deps | right_deps)
                        if dependency in {str(left["task_id"]), str(right["task_id"])}
                    ),
                    "independent": independent,
                    "left_owned": sorted(left_paths),
                    "right_owned": sorted(right_paths),
                }
            )
    return pairs


def suitability_report() -> dict[str, Any]:
    """P+ / N− evidence: scale, dependencies, interface stability, write sets and workload.

    Every number is read from the frozen catalog. What is *assessed* rather than measured is marked
    as such, because a fixture of this size can be argued about and the argument must stay visible.
    """

    out: dict[str, Any] = {"basis": "experiments.evaluation.closure2.catalog (frozen)", "tasks": {}}
    for task_id in TASK_IDS:
        frozen = task(task_id)
        pairs = _independent_pairs(task_id)
        independent = [pair for pair in pairs if pair["independent"]]
        deliverables = set(frozen.deliverables)
        owned_by_independent = {
            path for pair in independent for path in (*pair["left_owned"], *pair["right_owned"])
        }
        integration_seam = sorted(deliverables - owned_by_independent)
        reference_lines = _line_count(frozen.reference)
        out["tasks"][task_id] = {
            "family": frozen.family,
            "role": "P+" if task_id == P_PLUS_TASK_ID else "N-",
            "seed_files": len(frozen.seed),
            "seed_lines": _line_count(frozen.seed),
            "seed_chars": _char_count(frozen.seed),
            "reference_files": len(frozen.reference),
            "reference_lines": reference_lines,
            "public_tests": len(frozen.public_tests),
            "public_test_functions": sum(
                text.count("def test_") for text in frozen.public_tests.values()
            ),
            "oracle_files": len(frozen.oracle_tests),
            "oracle_test_functions": sum(
                text.count("def test_") for text in frozen.oracle_tests.values()
            ),
            "deliverables": len(frozen.deliverables),
            "workers": len(frozen.workers),
            "worker_ownership": {
                str(worker["task_id"]): {
                    "owned_paths": sorted(worker["owned_paths"]),
                    "depends_on": sorted(worker["depends_on"]),
                }
                for worker in frozen.workers
            },
            "worker_pairs": pairs,
            "independent_pairs": [pair["left"] + "+" + pair["right"] for pair in independent],
            "shared_write_paths": sorted(
                {path for pair in pairs for path in pair["shared_write_paths"]}
            ),
            "frozen_interface_files": sorted(set(frozen.forbidden_paths) & set(frozen.seed)),
            "interface_chars": sum(
                len(frozen.seed[path]) for path in set(frozen.forbidden_paths) & set(frozen.seed)
            ),
            "integration_seam_after_the_independent_pair": integration_seam,
            "grant_admits_every_deliverable": all(
                grant_covers(task_id, path) for path in frozen.deliverables
            ),
            "context_load_chars": (
                _char_count(frozen.seed)
                + len(frozen.shared_context or "")
                + sum(len(text) for text in frozen.public_tests.values())
            ),
            "public_suite_available": bool(frozen.public_tests),
            "independent_oracle_available": bool(frozen.oracle_tests),
            "reference_implementation_available": bool(frozen.reference),
            "simple_fixture": reference_lines <= 200,
        }
        out["tasks"][task_id]["criteria"] = {
            "two_substantive_independent_workstreams": bool(independent),
            "stable_public_interface": bool(set(frozen.forbidden_paths) & set(frozen.seed)),
            "exclusive_writable_scope_for_an_independent_pair": bool(independent),
            "no_worker_communication_needed": bool(independent),
            "main_integration_need": bool(integration_seam),
            "context_load_present": out["tasks"][task_id]["context_load_chars"] > 0,
            "direct_mode_feasible": reference_lines > 0,
            "delegated_mode_feasible": bool(independent) and bool(integration_seam),
            "public_test_and_oracle_available": bool(frozen.public_tests)
            and bool(frozen.oracle_tests),
        }
        out["tasks"][task_id]["assessment"] = (
            "structural: derived from the frozen write sets and dependency edges. The fixture is "
            "small in absolute line count; P+'s suitability rests on disjoint ownership plus a "
            "published interface plus a remaining integration seam, not on file count. Whether a "
            "fixture this size *should* be delegated is the question this stage measures, not a "
            "property of the fixture"
        )
    out["selection"] = (
        f"P+ = {P_PLUS_TASK_ID} (independent pair admitted by structure); "
        f"N- = {N_MINUS_TASK_ID} (both candidate steps own one shared module)"
    )
    return out


# -- cells ------------------------------------------------------------------------------------


def _cell(task_id: str, prompt_arm: str, order: int) -> dict[str, Any]:
    arm = contract.ARMS_BY_ID[ARM_ID]
    declared = budget()
    frozen = task(task_id)
    return {
        "cell_id": f"c2.{task_id}.{ARM_ID}.{prompt_arm}.r{REPETITION}",
        "task_id": task_id,
        "task_family": frozen.family,
        "arm_id": ARM_ID,
        "prompt_arm": prompt_arm,
        "decision_axis": DECISION_AXIS,
        "repetition": REPETITION,
        "round": order,
        "order": order,
        "scope": SCOPE,
        "plan_id": PLAN_ID,
        "is_benchmark_sample": False,
        "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
        "attempt_limit": ATTEMPT_LIMIT,
        # The goal is the task's own requirement: no activation appendix, no decomposition.
        "goal_id": f"{task_id}::natural-requirement",
        "goal": goal_text(task_id),
        "goal_digest": goal_digest(task_id),
        "intervention_kind": INTERVENTION_KIND,
        "interventions": list(INTERVENTION_KIND_PARTS),
        # The prompt travels with the cell, so the plan digest covers the bytes that run.
        "main_prompt": main_prompt(prompt_arm),
        "main_prompt_version": PROMPT_VERSIONS[prompt_arm],
        "main_prompt_digest": prompt_digest(prompt_arm),
        "worker_profile": arm.profile_id,
        "worker_exposure": arm.exposure,
        "worker_contract_digest": worker_contract()["rendered_prompt_digest"],
        "child_write_grants": effective_grant(task_id),
        "grant_normalization": grant_record(task_id),
        "budget": declared,
        "budget_digest": budget_digest(),
        "token_budget": TOKEN_BUDGET,
    }


def cells() -> list[dict[str, Any]]:
    return [
        _cell(task_id, prompt_arm, order)
        for order, (task_id, prompt_arm) in enumerate(CELL_ORDER, start=1)
    ]


def published_requirement(task_id: str) -> str:
    """The bytes the model sees as its goal, for the leak check."""

    return goal_text(task_id)


def plan_record() -> dict[str, Any]:
    from experiments.stage_benchmark import matrix

    plan_cells = cells()
    declared = budget()
    return {
        "stage": STAGE_ID,
        "design_version": DESIGN_VERSION,
        "plan_id": PLAN_ID,
        "scope": SCOPE,
        "tasks": list(TASK_IDS),
        "arms": [ARM_ID],
        "prompt_arms": list(PROMPT_ARMS),
        "decision_axis": DECISION_AXIS,
        "repetitions": REPETITION,
        "cells_count": len(plan_cells),
        "cell_order": [cell["cell_id"] for cell in plan_cells],
        "cells": plan_cells,
        "digest": matrix.plan_digest(plan_cells),
        "balance": contract.balance(plan_cells, (ARM_ID,)),
        "claim_namespace": f"c2-claim/{PLAN_ID}",
        "runs_subdir": f"runs/stage-c2/{PLAN_ID}",
        "evidence_subdir": f"evidence/stage-c2/{PLAN_ID}",
        "is_benchmark_sample": False,
        "is_diagnostic": True,
        "attempt_limit": ATTEMPT_LIMIT,
        "nominal_token_capacity": NOMINAL_TOKEN_CAPACITY,
        "execution_contract": dict(contract.EXECUTION_CONTRACT),
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "intervention": intervention_record(),
        "prompts": {
            arm: {
                "version": PROMPT_VERSIONS[arm],
                "digest": PROMPT_DIGESTS[arm],
                "chars": len(PROMPT_TEXTS[arm]),
            }
            for arm in PROMPT_ARMS
        },
        "prompt_changes": [
            {key: value for key, value in record.items() if key not in {"before", "after"}}
            for record in prompts.change_records()
        ],
        "prompt_unchanged_sections": prompts.unchanged_sections(),
        "worker_contract": worker_contract(),
        "delegation": delegation_record(),
        "grants": {task_id: grant_record(task_id) for task_id in TASK_IDS},
        "task_facts": {
            task_id: {
                "task_id": task_id,
                "family": task(task_id).family,
                "write_scope": list(task(task_id).allowed_paths),
                "deliverables": list(task(task_id).deliverables),
                "forbidden_paths": list(task(task_id).forbidden_paths),
                "public_tests": sorted(task(task_id).public_tests),
                "oracle_tests": sorted(task(task_id).oracle_tests),
                "goal_digest": goal_digest(task_id),
            }
            for task_id in TASK_IDS
        },
        "suitability": suitability_report(),
        "reporting": dict(REPORTING_CONTRACT),
        "budget": declared,
        "budget_digest": budget_digest(),
        "budget_change": {
            "before": FROZEN_TOKEN_BUDGET,
            "after": TOKEN_BUDGET,
            "delta": TOKEN_BUDGET - FROZEN_TOKEN_BUDGET,
            "relative": round(TOKEN_BUDGET / FROZEN_TOKEN_BUDGET - 1.0, 6),
        },
        "envelope_candidate": {
            "cost_stop_usd": COST_STOP_CANDIDATE,
            "per_cell_cost_stop_usd": PER_CELL_COST_STOP_CANDIDATE,
            "token_stop": NOMINAL_TOKEN_CAPACITY,
            "attempt_limit": ATTEMPT_LIMIT,
            "approved": False,
            "basis": (
                "four cells, one attempt each, 750,000 shared tokens per cell; the cumulative stop "
                "is 4 x the per-cell cost candidate. C1-C measured USD 0.086984 for one cell, so "
                "the candidate is about 2.9x that per cell. Every field stays a candidate until "
                "the Owner signs it"
            ),
        },
        "does_not_modify": list(DOES_NOT_MODIFY),
        "authorization_migrates": False,
        "prior_stages": {
            "c1c_plan_digest": (
                "sha256:4481bba2d3a0a9e0e3b7b04c5de3c851c70f1d4d03892043e23046782ab727bb"
            ),
            "c1c_outcome": "A3_ORACLE_ONLY_FAILURE",
            "c1c_system_tokens": 352_919,
            "c1c_shared_budget": 750_000,
            "c1c_budget_rejections": 0,
            "delegation_choice_verified": False,
        },
    }


# -- intervention ------------------------------------------------------------------------------


def intervention_record() -> dict[str, Any]:
    """The one thing that moves inside a task, and everything that must not."""

    pairs = {
        task_id: [cell["cell_id"] for cell in cells() if cell["task_id"] == task_id]
        for task_id in TASK_IDS
    }
    return {
        "kind": INTERVENTION_KIND,
        "parts": list(INTERVENTION_KIND_PARTS),
        "axis": DECISION_AXIS,
        "baseline": {
            "arm": "B0",
            "version": prompts.B0_VERSION,
            "digest": prompts.B0_DIGEST,
            "source": "experiments/stage_c1b/prompts.py (byte-identical reuse)",
        },
        "treatment": {
            "arm": "B1",
            "version": prompts.V30_VERSION,
            "digest": prompts.V30_DIGEST,
            "changed_sections": [entry["start"] for entry in prompts.REPLACEMENTS],
        },
        "cells_per_task": pairs,
        "goal_changed": False,
        "task_changed": False,
        "worker_contract_changed": False,
        "worker_profile_changed": False,
        "router_changed": False,
        "ownership_changed": False,
        "budget_changed_against_c1c": False,
        "turns_changed": False,
        "grant_changed": False,
        "oracle_changed": False,
        "activation_appendix_removed": True,
        "single_variable_ablation": True,
        "single_variable_scope": "within each task; the two arms share every other parameter",
        "statistical_claim": False,
        "inference": (
            "four cells, one attempt each: a decision diagnostic. It can show that a route was "
            "chosen under one prompt and not the other on these two tasks; it cannot estimate a "
            "rate, a causal effect, or a benefit of delegation"
        ),
        "cost_stop_candidate": {
            "cost_stop_usd": COST_STOP_CANDIDATE,
            "per_cell_cost_stop_usd": PER_CELL_COST_STOP_CANDIDATE,
            "approved": False,
        },
        "basis": (
            "C1/C1-B/C1-C all ran a goal with an explicit delegation appendix, so none of them "
            "measured whether the Main proposes delegation on its own. Removing the appendix and "
            "varying only the decision prompt is the smallest change that makes that question "
            "observable"
        ),
    }


def intervention_digest() -> str:
    return authority_module.digest_of(intervention_record())


# -- validation --------------------------------------------------------------------------------


def _prompt_only_problems(plan_cells: Sequence[Mapping[str, Any]]) -> list[str]:
    """Within each task the two cells differ in the prompt fields and nothing else."""

    problems: list[str] = []
    by_task: dict[str, list[Mapping[str, Any]]] = {}
    for cell in plan_cells:
        by_task.setdefault(str(cell.get("task_id")), []).append(cell)
    for task_id, pair in by_task.items():
        if len(pair) != len(PROMPT_ARMS):
            problems.append(f"{task_id} has {len(pair)} cells; the design is one per prompt arm")
            continue
        left, right = pair
        keys = set(left) | set(right)
        differing = {key for key in keys if left.get(key) != right.get(key)}
        extra = differing - PROMPT_ONLY_FIELDS
        if extra:
            problems.append(f"{task_id}: the cells differ in {sorted(extra)} as well as the prompt")
        if left.get("main_prompt_digest") == right.get("main_prompt_digest"):
            problems.append(f"{task_id}: both cells carry the same prompt digest")
        if {left.get("prompt_arm"), right.get("prompt_arm")} != set(PROMPT_ARMS):
            problems.append(f"{task_id}: the pair is not the B0/B1 comparison")
    return problems


def _leak_problems(plan_cells: Sequence[Mapping[str, Any]]) -> list[str]:
    """The goal must be the published requirement, not the answer, the split or the experiment.

    Byte equality with ``contract.GOAL_OF`` is the primary guarantee: whatever the frozen catalog
    publishes is what the model sees, and nothing this stage composes is appended. The two extra
    probes cover the strings the catalog *does* hold but must not publish: a worker's prepared
    assignment and the hidden test file names. Deliverable paths are not leaks — they are part of
    the task's own published requirement.
    """

    problems: list[str] = []
    for cell in plan_cells:
        goal = str(cell.get("goal") or "")
        task_id = str(cell["task_id"])
        frozen = task(task_id)
        if goal != contract.GOAL_OF[task_id]:
            problems.append(f"{cell['cell_id']}: the goal is not the frozen requirement")
        if c1.ACTIVATION_INSTRUCTION in goal:
            problems.append(f"{cell['cell_id']}: the goal still carries the C1 activation appendix")
        for leaked in (
            "[YOUR ASSIGNMENT",
            *(str(worker["assignment"]) for worker in frozen.workers),
            *sorted(frozen.oracle_tests),
        ):
            if leaked and leaked in goal:
                problems.append(f"{cell['cell_id']}: the goal carries {leaked[:40]!r}")
        lowered = goal.lower()
        for phrase in contract.FORBIDDEN_GOAL_PHRASES:
            if phrase in lowered:
                problems.append(f"{cell['cell_id']}: the goal says {phrase!r}")
    return problems


def validate(plan: Mapping[str, Any] | None = None) -> list[str]:
    """Every invariant this design promises, checked on the record itself."""

    record = dict(plan or plan_record())
    problems: list[str] = []

    if record.get("plan_id") != PLAN_ID or record.get("scope") != SCOPE:
        problems.append("the plan is not the C2 natural-delegation identity")
    if record.get("stage") != STAGE_ID:
        problems.append("the plan does not declare its own stage")
    if record.get("is_benchmark_sample") is not False:
        problems.append("a C2 cell would be counted as a benchmark sample")
    if record.get("is_diagnostic") is not True:
        problems.append("the plan is not marked diagnostic")
    if record.get("attempt_limit") != 1:
        problems.append("the plan does not declare one attempt")
    if record.get("authorization_migrates") is not False:
        problems.append("the plan claims an existing authorization migrates to it")

    plan_cells = list(record.get("cells") or [])
    if len(plan_cells) != len(CELL_ORDER):
        return [*problems, f"the plan has {len(plan_cells)} cells; the design is exactly four"]
    from experiments.stage_benchmark import matrix

    if record.get("digest") != matrix.plan_digest(plan_cells):
        problems.append("the plan digest does not recompute from its cells")
    if [cell["cell_id"] for cell in plan_cells] != [
        f"c2.{task_id}.{ARM_ID}.{arm}.r{REPETITION}" for task_id, arm in CELL_ORDER
    ]:
        problems.append("the cells are not in the fixed, pre-registered order")
    if [(cell["task_id"], cell["prompt_arm"]) for cell in plan_cells] != list(CELL_ORDER):
        problems.append("the cell task/prompt pairs are not the frozen design")
    tasks_declared = list(record.get("tasks") or [])
    arms_declared = list(record.get("arms") or [])
    if tasks_declared != list(TASK_IDS) or arms_declared != [ARM_ID]:
        problems.append("the plan's task/arm lists are not the two-task, one-arm design")
    if list(record.get("prompt_arms") or []) != list(PROMPT_ARMS):
        problems.append("the plan does not declare the B0/B1 prompt arms")

    arm = contract.ARMS_BY_ID[ARM_ID]
    if arm.profile_id != contract.CODING or arm.exposure != "card":
        problems.append("arm C3 is no longer coding_worker with the full card")

    for cell in plan_cells:
        task_id = str(cell["task_id"])
        if task_id not in TASK_IDS:
            problems.append(f"{cell['cell_id']}: unknown task")
            continue
        if cell.get("arm_id") != ARM_ID:
            problems.append(f"{cell['cell_id']}: not arm C3")
        if cell.get("scope") != SCOPE or cell.get("plan_id") != PLAN_ID:
            problems.append(f"{cell['cell_id']}: does not carry the plan's own identity")
        if cell.get("execution_contract") != contract.EXECUTION_CONTRACT_DIGEST:
            problems.append(f"{cell['cell_id']}: not bound to the current execution contract")
        if cell.get("attempt_limit") != 1 or cell.get("is_benchmark_sample") is not False:
            problems.append(f"{cell['cell_id']}: attempt or sample flag moved")
        if cell.get("worker_profile") != arm.profile_id:
            problems.append(f"{cell['cell_id']}: worker profile is not the arm's")
        if cell.get("worker_exposure") != arm.exposure:
            problems.append(f"{cell['cell_id']}: exposure is not the arm's")
        if cell.get("worker_contract_digest") != worker_contract()["rendered_prompt_digest"]:
            problems.append(f"{cell['cell_id']}: the worker contract is not the C1-B contract")

        # goal: the frozen requirement, nothing appended
        if cell.get("goal") != goal_text(task_id) or cell.get("goal_digest") != goal_digest(
            task_id
        ):
            problems.append(f"{cell['cell_id']}: the goal is not the frozen requirement")

        # prompt: the arm's bytes, and inside the cell
        prompt_arm = str(cell["prompt_arm"])
        if prompt_arm not in PROMPT_ARMS:
            problems.append(f"{cell['cell_id']}: unknown prompt arm")
            continue
        prompt = cell.get("main_prompt") or {}
        if prompt.get("version") != PROMPT_VERSIONS[prompt_arm]:
            problems.append(f"{cell['cell_id']}: prompt version is not the arm's")
        if prompt.get("text") != PROMPT_TEXTS[prompt_arm]:
            problems.append(f"{cell['cell_id']}: prompt bytes are not the arm's")
        if cell.get("main_prompt_digest") != PROMPT_DIGESTS[prompt_arm]:
            problems.append(f"{cell['cell_id']}: prompt digest is not the arm's")

        # grant: two representations of one frozen scope
        bound = {
            str(profile_id): tuple(str(path) for path in paths)
            for profile_id, paths in (cell.get("child_write_grants") or {}).items()
        }
        if bound != effective_grant(task_id):
            problems.append(f"{cell['cell_id']}: the Host grant is not the task's frozen grant")
        if cell.get("grant_normalization") != grant_record(task_id):
            problems.append(f"{cell['cell_id']}: the grant record is not the task's")
        if not all(grant_covers(task_id, path) for path in task(task_id).deliverables):
            problems.append(f"{cell['cell_id']}: the grant does not admit a deliverable")

        # budget: the effective record, bound into the cell
        if cell.get("budget") != budget() or cell.get("budget_digest") != budget_digest():
            problems.append(f"{cell['cell_id']}: the effective budget is not bound")
        if int(cell.get("token_budget") or 0) != TOKEN_BUDGET:
            problems.append(f"{cell['cell_id']}: token budget is not the effective value")
        if contract.token_budget_for(cell) != TOKEN_BUDGET:
            problems.append(f"{cell['cell_id']}: the resolver does not read the effective budget")

    problems.extend(_prompt_only_problems(plan_cells))
    problems.extend(_leak_problems(plan_cells))

    # budget and identity
    if int(contract.BUDGET["token_budget"]) != FROZEN_TOKEN_BUDGET:
        problems.append("the frozen Stage B+ budget moved")
    differences = {
        key: value for key, value in budget().items() if contract.BUDGET.get(key) != value
    }
    if set(differences) != {"token_budget"}:
        problems.append(f"the effective budget changes more than the token budget: {differences}")
    if record.get("budget") != budget():
        problems.append("the plan does not declare the effective budget record")
    if record.get("budget_digest") != budget_digest():
        problems.append("the budget digest does not recompute from the declared budget")
    if record.get("budget_digest") == authority_module.digest_of(dict(contract.BUDGET)):
        problems.append("the budget digest is the frozen 500K digest")
    if int(record.get("nominal_token_capacity") or 0) != NOMINAL_TOKEN_CAPACITY:
        problems.append("the nominal token capacity is not four cells x 750000")
    if contract.token_budget_for({}) != FROZEN_TOKEN_BUDGET:
        problems.append("a cell without a budget no longer resolves to the frozen value")
    if authority_module.plan_facts(record)["budget_digest"] != budget_digest():
        problems.append("the authority does not read the plan's budget digest")

    envelope = record.get("envelope_candidate") or {}
    if envelope.get("approved") is not False:
        problems.append("the stage proposes a cost ceiling as if it were approved")
    for field in ("cost_stop_usd", "per_cell_cost_stop_usd"):
        value = envelope.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            problems.append(f"the candidate {field} is not finite and positive")
    if float(envelope.get("per_cell_cost_stop_usd") or 0) > float(
        envelope.get("cost_stop_usd") or 0
    ):
        problems.append("the per-cell stop is larger than the cumulative stop")
    if int(envelope.get("token_stop") or 0) != NOMINAL_TOKEN_CAPACITY:
        problems.append("the candidate token stop is not the nominal capacity")
    if envelope.get("attempt_limit") != 1:
        problems.append("the candidate envelope does not carry one attempt")

    intervention = record.get("intervention") or {}
    if intervention.get("kind") != INTERVENTION_KIND:
        problems.append("the intervention is not declared as a Main-decision prompt ablation")
    if intervention.get("single_variable_ablation") is not True:
        problems.append("the intervention does not claim a within-task single variable")
    if intervention.get("statistical_claim") is not False:
        problems.append("the intervention claims a statistical result")
    if intervention.get("activation_appendix_removed") is not True:
        problems.append("the intervention does not declare the appendix removed")
    for flag in (
        "goal_changed",
        "task_changed",
        "worker_contract_changed",
        "worker_profile_changed",
        "router_changed",
        "ownership_changed",
        "budget_changed_against_c1c",
        "turns_changed",
        "grant_changed",
        "oracle_changed",
    ):
        if intervention.get(flag) is not False:
            problems.append(f"the intervention claims it changes {flag}")
    if intervention.get("cost_stop_candidate", {}).get("approved") is not False:
        problems.append("the cost stop is presented as approved")

    # the prompt pair itself
    problems.extend(prompts.validate())

    # the reporting contract must be present and versioned
    reporting = record.get("reporting") or {}
    for key in ("final_correctness", "cache", "handoff", "delegation_decision"):
        if key not in reporting:
            problems.append(f"the reporting contract has no {key!r} entry")
    if "L6" not in str(reporting.get("final_correctness", {}).get("not", "")):
        problems.append(
            "the reporting contract does not name the analyzer's L6/oracle field as excluded"
        )

    # the plan must not be able to impersonate an earlier one
    from experiments.stage_c1c import plan as c1c_plan

    earlier = {
        "c1": c1.plan_record()["digest"],
        "c1b": c1b.plan_record()["digest"],
        "c1c": c1c_plan.plan_record()["digest"],
        "bplus-canary": contract.scope_plan(contract.SCOPE_CANARY)["digest"],
        "bplus-formal": contract.scope_plan(contract.SCOPE_FORMAL)["digest"],
    }
    if record.get("digest") in set(earlier.values()):
        problems.append("the C2 plan digest collides with an earlier plan")
    if record.get("budget_digest") in {
        authority_module.digest_of(dict(contract.BUDGET)),
        c1b.plan_record()["budget_digest"],
    }:
        problems.append("the C2 budget digest collides with an earlier plan's")
    return problems


__all__ = [
    "ARM_ID",
    "ATTEMPT_LIMIT",
    "CELL_ORDER",
    "COST_STOP_CANDIDATE",
    "DECISION_AXIS",
    "DESIGN_VERSION",
    "DOES_NOT_MODIFY",
    "INTERVENTION_KIND",
    "INTERVENTION_KIND_PARTS",
    "NOMINAL_TOKEN_CAPACITY",
    "N_MINUS_TASK_ID",
    "PER_CELL_COST_STOP_CANDIDATE",
    "PLAN_ID",
    "PROMPT_ARMS",
    "PROMPT_DIGESTS",
    "PROMPT_ONLY_FIELDS",
    "PROMPT_TEXTS",
    "PROMPT_VERSIONS",
    "P_PLUS_TASK_ID",
    "REPORTING_CONTRACT",
    "REPETITION",
    "SCOPE",
    "STAGE_ID",
    "TASK_IDS",
    "TOKEN_BUDGET",
    "budget",
    "budget_digest",
    "cells",
    "delegation_record",
    "effective_grant",
    "goal_digest",
    "goal_text",
    "grant_covers",
    "grant_record",
    "intervention_digest",
    "intervention_record",
    "main_prompt",
    "plan_record",
    "prompt_digest",
    "published_requirement",
    "raw_grant",
    "suitability_report",
    "task",
    "validate",
    "worker_contract",
]
