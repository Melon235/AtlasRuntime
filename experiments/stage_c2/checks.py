"""Stage C2 targeted verification: the four-cell decision design, offline.

``python -m experiments.stage_c2.checks`` writes ``plan.json``, ``authorization.json``,
``executor.json`` and ``offline.json`` next to this module, and nothing else in the repository.

The thirteen checks are the stage's Phase C list, in order:

C00  the P+/N− selection is measured on the frozen catalog: independent pair, disjoint write sets,
     shared write set, interface file, integration seam, workload — not asserted from file count
C01  the plan id and scope resolve through ``contract.scope_plan``; B+, C1, C1-B and C1-C are
     unchanged; the superseded canary ids stay refused
C02  within each task the two cells differ in the Main prompt and the execution position only
C03  every cell executes its own task's goal, grant, public suite and oracle — nothing is shared
     between the two tasks by accident
C04  the effective 750,000-token budget and the arm's prompt reach ``GoalRunOptions``, and a real
     ``run_goal`` builds a ``RunBudgetState`` whose limit is 750,000 — checked where it is consumed
C05  the frozen router admits the P+ independent pair, still refuses the N− shared write (and the
     frozen N− DAG, for its own reason), and the five boundary shapes are unchanged
C06  the real ``executor.dispatch(PLAN_ID, mode=MODE_OFFLINE_PROOF)`` runs all four cells through
     resolution → authority → claim → synthetic cell → audit → matrix report
C07  the pre-claim production admission reports READY for all four cells
C08  the older plan identities, the protocol digest and the C1-C artefacts are still the frozen ones
C09  six historical production claims are intact, no C2 claim exists, and the C1-C evidence index is
     untouched
C10  the pending authorization authorizes nothing and binds this plan's identity
C11  the plan's own design invariants hold
C12  the reporting contract names fields the runtime actually writes, and names the analyzer fields
     it must not be read from
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2 import catalog as closure2
from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_b_plus.offline import synthetic_cell_run
from experiments.stage_c1 import checks as c1_checks
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import plan as c1b
from experiments.stage_c1c import checks as c1c_checks
from experiments.stage_c1c import plan as c1c
from experiments.stage_c2 import plan, prompts

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

#: Identities recorded by earlier stages. Constants, so a later edit that moves one fails a check
#: instead of quietly re-baselining the run this stage is measured against.
C1_PLAN_DIGEST = "sha256:537508bf0fc10056987ef9342d9899008609aa9ba9c1734f7de53a3d3bc7ecd2"
C1B_PLAN_DIGEST = "sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c"
C1C_PLAN_DIGEST = "sha256:4481bba2d3a0a9e0e3b7b04c5de3c851c70f1d4d03892043e23046782ab727bb"
C1C_EXECUTOR_DIGEST = "sha256:59152911c7465b4252763c5e9d07ca4bac26cd5fe23b70d6d5bf5b4ae163714a"
C1C_EXECUTOR_VERSION = "bplus-executor-1.6"
FROZEN_BUDGET_DIGEST = "sha256:a6ea43679649cb39c6c4f55f3bbb915b8a0287abcddce511a725ec6abee23eb9"

BPLUS_CANARY_PLAN_DIGEST = c1_checks.BPLUS_CANARY_PLAN_DIGEST
BPLUS_FORMAL_PLAN_DIGEST = c1_checks.BPLUS_FORMAL_PLAN_DIGEST
BPLUS_PROTOCOL_DIGEST = c1_checks.BPLUS_PROTOCOL_DIGEST

#: The six consumed production claims, oldest last. A claim is consumed for ever.
CONSUMED_CLAIMS: tuple[tuple[str, str], ...] = (
    *c1c_checks.CONSUMED_CLAIMS,
    ("c1c", C1C_PLAN_DIGEST),
)

#: Stage C1-C custody, read-only. Read by digest, never rewritten.
C1C_EVIDENCE_ROOT = Path(r"D:\Work\AtlasRuntime-runs\stage-c1c-headroom-control")
C1C_EVIDENCE_INDEX_DIGEST = (
    "sha256:5defb4cd5bdfe27c0d54c24acc5ed563ab6a5fbbccf9bc85d01f2fef1f01d57b"
)
C1C_EVIDENCE_FILES = 35

REJECTED_PLAN_IDS: tuple[str, ...] = (
    contract.CANARY_PLAN_ID_V1,
    contract.CANARY_PLAN_ID_V2,
    "stage-c2-unknown-plan",
    "C2_UNKNOWN_SCOPE",
)


def _check(check_id: str, ok: bool, detail: str, **evidence: Any) -> dict[str, Any]:
    return {"check_id": check_id, "ok": bool(ok), "detail": detail, **evidence}


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _production_options(cell: Mapping[str, Any], root: Path) -> Any:
    return executor.production_goal_options(
        cell,
        evidence_root=root,
        fixture_dir=root / "fixture",
        config=executor.RunnerConfig(),
        run_id="c2-offline-preparation",
    )


# -- C00: the selection ------------------------------------------------------------------------


def _check_suitability(plan_record: Mapping[str, Any]) -> dict[str, Any]:
    report = plan.suitability_report()
    positive = report["tasks"][plan.P_PLUS_TASK_ID]
    negative = report["tasks"][plan.N_MINUS_TASK_ID]
    problems: list[str] = []

    if set(plan.TASK_IDS) - set(contract.TASKS_BY_ID):
        problems.append("a C2 task is not in the frozen Closure-2 catalog")
    if len(closure2.TASKS) != 3:
        problems.append("the frozen catalog no longer holds three tasks")
    if not positive["independent_pairs"]:
        problems.append("P+ has no independent pair, so it is not a positive control")
    if negative["independent_pairs"]:
        problems.append("N- has an independent pair, so it is not a coupled control")
    if not negative["shared_write_paths"]:
        problems.append("N- has no shared write path")
    if not positive["criteria"]["delegated_mode_feasible"]:
        problems.append("P+ cannot be delegated by its own structure")
    if negative["criteria"]["delegated_mode_feasible"]:
        problems.append("N- is structurally delegable, which contradicts the control")
    for label, task in (("P+", positive), ("N-", negative)):
        if not task["grant_admits_every_deliverable"]:
            problems.append(f"{label}: the frozen grant does not admit a deliverable")
        if not task["criteria"]["public_test_and_oracle_available"]:
            problems.append(f"{label}: the public suite or the oracle is missing")
        if not task["reference_implementation_available"]:
            problems.append(f"{label}: no reference implementation, so the oracle is unproven")
        if not task["frozen_interface_files"]:
            problems.append(f"{label}: no frozen interface file")

    ok = not problems
    return _check(
        "C00_selection_is_measured_on_the_frozen_catalog",
        ok,
        "; ".join(problems)
        or (
            f"P+ {plan.P_PLUS_TASK_ID}: {positive['seed_files']} seed files / "
            f"{positive['seed_lines']} lines, independent pair {positive['independent_pairs']}, "
            f"no shared write, integration seam "
            f"{positive['integration_seam_after_the_independent_pair']}; "
            f"N- {plan.N_MINUS_TASK_ID}: shared write {negative['shared_write_paths']}, no "
            f"independent pair; both grants admit every deliverable and both have a public suite "
            f"and an oracle"
        ),
        selection=report["selection"],
        tasks=report["tasks"],
        catalog_unchanged=len(closure2.TASKS) == 3,
    )


# -- C01: the entry the dispatch calls first ---------------------------------------------------


def _check_entry_resolution(plan_record: Mapping[str, Any]) -> dict[str, Any]:
    from experiments.stage_benchmark import matrix

    problems: list[str] = []
    by_id = contract.scope_plan(plan.PLAN_ID)
    by_scope = contract.scope_plan(plan.SCOPE)

    if by_id["digest"] != plan_record["digest"]:
        problems.append("the resolved plan digest is not the record's")
    if matrix.plan_digest(plan_record["cells"]) != plan_record["digest"]:
        problems.append("the plan digest does not recompute from its cells")
    if by_scope["digest"] != by_id["digest"] or by_scope["plan_id"] != by_id["plan_id"]:
        problems.append("the scope and the plan id resolve to different plans")
    if by_id["plan_id"] != plan.PLAN_ID or by_id["scope"] != plan.SCOPE:
        problems.append("the resolved plan is not the C2 identity")
    if by_id["cells_count"] != 4:
        problems.append("the resolved plan does not hold four cells")

    rejected: list[str] = []
    for plan_id in REJECTED_PLAN_IDS:
        try:
            contract.scope_plan(plan_id)
        except ValueError:
            rejected.append(plan_id)
        else:
            problems.append(f"{plan_id} resolved; it must be rejected")

    unchanged = {
        "c1": (contract.scope_plan(c1.PLAN_ID)["digest"], C1_PLAN_DIGEST),
        "c1b": (contract.scope_plan(c1b.PLAN_ID)["digest"], C1B_PLAN_DIGEST),
        "c1c": (contract.scope_plan(c1c.PLAN_ID)["digest"], C1C_PLAN_DIGEST),
        "bplus-canary": (
            contract.scope_plan(contract.SCOPE_CANARY)["digest"],
            BPLUS_CANARY_PLAN_DIGEST,
        ),
        "bplus-formal": (
            contract.scope_plan(contract.SCOPE_FORMAL)["digest"],
            BPLUS_FORMAL_PLAN_DIGEST,
        ),
    }
    for label, (actual, expected) in unchanged.items():
        if actual != expected:
            problems.append(f"the {label} entry moved to {actual}, expected {expected}")

    ok = not problems
    return _check(
        "C01_plan_entry_resolves_the_c2_plan_and_nothing_else_moved",
        ok,
        "; ".join(problems)
        or (
            f"{plan.PLAN_ID} and its scope both resolve to {plan_record['digest'][:19]}…; C1, "
            f"C1-B, C1-C and both B+ entries are unchanged; {len(rejected)} ids remain rejected"
        ),
        resolved_digest=by_id["digest"],
        rejected=rejected,
        unchanged={label: value[0] for label, value in unchanged.items()},
    )


# -- C02: within a task only the prompt moves ---------------------------------------------------


def _check_prompt_only(plan_record: Mapping[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    pairs: dict[str, Any] = {}
    cells = list(plan_record["cells"])
    for task_id in plan.TASK_IDS:
        pair = [cell for cell in cells if cell["task_id"] == task_id]
        arms = sorted(cell["prompt_arm"] for cell in pair)
        if len(pair) != 2 or arms != sorted(plan.PROMPT_ARMS):
            problems.append(f"{task_id}: the pair is {arms}")
            continue
        left, right = pair
        keys = set(left) | set(right)
        differing = sorted(key for key in keys if left.get(key) != right.get(key))
        extra = sorted(set(differing) - set(plan.PROMPT_ONLY_FIELDS))
        if extra:
            problems.append(f"{task_id}: also differs in {extra}")
        if left["main_prompt"]["text"] == right["main_prompt"]["text"]:
            problems.append(f"{task_id}: both cells carry the same prompt text")
        if left["main_prompt_digest"] == right["main_prompt_digest"]:
            problems.append(f"{task_id}: both cells carry the same prompt digest")
        for field in (
            "goal",
            "goal_digest",
            "child_write_grants",
            "grant_normalization",
            "worker_profile",
            "worker_exposure",
            "worker_contract_digest",
            "budget",
            "budget_digest",
            "token_budget",
            "attempt_limit",
            "execution_contract",
        ):
            if left.get(field) != right.get(field):
                problems.append(f"{task_id}: {field} differs between the two arms")
        pairs[task_id] = {
            "arms": arms,
            "differing_fields": differing,
            "prompt_digests": {
                left["prompt_arm"]: left["main_prompt_digest"],
                right["prompt_arm"]: right["main_prompt_digest"],
            },
        }

    by_arm = {cell["prompt_arm"]: cell for cell in cells}
    if by_arm["B0"]["main_prompt_digest"] != prompts.B0_DIGEST:
        problems.append("the B0 cells do not carry the C1-B prompt digest")
    if by_arm["B1"]["main_prompt_digest"] != prompts.V30_DIGEST:
        problems.append("the B1 cells do not carry the coding-aware prompt digest")
    if prompts.B0_DIGEST != c1b.prompts.V22_DIGEST:
        problems.append("B0 is not the C1-B prompt at all")

    ok = not problems
    return _check(
        "C02_within_a_task_only_the_main_prompt_moves",
        ok,
        "; ".join(problems)
        or (
            "each task's two cells differ only in the Main prompt (and the fixed execution "
            f"position); B0 = {prompts.B0_VERSION} ({prompts.B0_DIGEST[:19]}…), B1 = "
            f"{prompts.V30_VERSION} ({prompts.V30_DIGEST[:19]}…)"
        ),
        pairs=pairs,
        prompt_only_fields=sorted(plan.PROMPT_ONLY_FIELDS),
    )


# -- C03: every cell executes its own task ------------------------------------------------------


def _check_cells_match_tasks(plan_record: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    problems: list[str] = []
    fixtures: dict[str, Any] = {}
    for task_id in plan.TASK_IDS:
        frozen = contract.TASKS_BY_ID[task_id]
        fixture = executor.prepare_fixture(frozen, scratch / f"fixture-{task_id}")
        digests = closure2.task_digests(frozen)
        leaked = sorted(set(frozen.oracle_tests) & set(fixture["files_list"]))
        fixtures[task_id] = {
            "matches_frozen_seed": bool(fixture["matches_frozen_seed"]),
            "seed_digest": fixture["seed_digest"],
            "frozen_seed_digest": digests["seed_digest"],
            "files": fixture["files"],
            "public_suite_present": "test_public.py" in fixture["files_list"],
            "hidden_files_in_fixture": leaked,
        }
        if not fixture["matches_frozen_seed"]:
            problems.append(f"{task_id}: the fixture is not the frozen seed")
        if "test_public.py" not in fixture["files_list"]:
            problems.append(f"{task_id}: the public suite is not in the fixture")
        if leaked:
            problems.append(f"{task_id}: a hidden test is in the workspace")

    per_task: dict[str, Any] = {}
    for cell in plan_record["cells"]:
        task_id = str(cell["task_id"])
        frozen = contract.TASKS_BY_ID[task_id]
        arm = contract.ARMS_BY_ID[str(cell["arm_id"])]
        if cell["goal"] != contract.GOAL_OF[task_id]:
            problems.append(f"{cell['cell_id']}: the goal is not the task's requirement")
        if cell["goal_digest"] != plan.goal_digest(task_id):
            problems.append(f"{cell['cell_id']}: the goal digest does not recompute")
        bound = {
            str(profile_id): tuple(str(path) for path in paths)
            for profile_id, paths in cell["child_write_grants"].items()
        }
        if bound != plan.effective_grant(task_id):
            problems.append(f"{cell['cell_id']}: the grant is not the task's frozen scope")
        if not cell["grant_normalization"]["matches_frozen_task_scope"]:
            problems.append(f"{cell['cell_id']}: the grant record is not the frozen scope")
        if cell["worker_profile"] != arm.profile_id or cell["worker_exposure"] != arm.exposure:
            problems.append(f"{cell['cell_id']}: the arm's profile or exposure moved")
        if cell["worker_contract_digest"] != plan.worker_contract()["rendered_prompt_digest"]:
            problems.append(f"{cell['cell_id']}: the worker contract is not the C1-B contract")
        if cell["prompt_arm"] == "B0" and cell["main_prompt"]["text"] != prompts.B0_TEXT:
            problems.append(f"{cell['cell_id']}: B0 bytes moved")
        if cell["prompt_arm"] == "B1" and cell["main_prompt"]["text"] != prompts.V30_TEXT:
            problems.append(f"{cell['cell_id']}: B1 bytes moved")
        per_task.setdefault(task_id, {"family": frozen.family, "cells": []})["cells"].append(
            {
                "cell_id": cell["cell_id"],
                "prompt_arm": cell["prompt_arm"],
                "goal_digest": cell["goal_digest"],
                "grant": {key: list(value) for key, value in bound.items()},
                "public_tests": sorted(frozen.public_tests),
                "oracle_tests": sorted(frozen.oracle_tests),
            }
        )

    if len(plan_record["cells"]) != 4:
        problems.append("the plan does not hold four cells")
    if set(per_task) != set(plan.TASK_IDS):
        problems.append(f"the cells do not cover both tasks: {sorted(per_task)}")

    ok = not problems
    return _check(
        "C03_every_cell_executes_its_own_task",
        ok,
        "; ".join(problems)
        or (
            "four cells over two frozen tasks; each cell carries its own task's requirement "
            "bytes, normalised grant, public suite and oracle, and each task's fixture is the "
            "frozen seed with no hidden test in it"
        ),
        tasks=per_task,
        fixtures=fixtures,
    )


# -- C04: the options, and the budget state they become -----------------------------------------


def _check_production_options(plan_record: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    problems: list[str] = []
    observed: dict[str, Any] = {}
    expected_public_calls = int(contract.BUDGET["public_test_max_calls"])

    for cell in plan_record["cells"]:
        task_id = str(cell["task_id"])
        options = _production_options(cell, scratch / f"options-{cell['order']:02d}")
        grant = {key: list(value) for key, value in options.child_write_grants.items()}
        observed[cell["cell_id"]] = {
            "token_budget": options.token_budget,
            "max_turns": options.max_turns,
            "worker_max_turns": options.worker_max_turns,
            "main_prompt_version": options.main_prompt.version,
            "main_prompt_digest": options.main_prompt.digest,
            "grant": grant,
            "main_write_paths": list(options.main_write_paths),
            "delegation": {
                "max_parallel_children": options.delegation.max_parallel_children,
                "max_children_per_call": options.delegation.max_children_per_call,
                "max_delegation_rounds": options.delegation.max_delegation_rounds,
                "child_max_turns": options.delegation.child_max_turns,
                "result_preview_chars": options.delegation.result_preview_chars,
            },
            "public_test_max_calls": options.public_tests.max_calls,
        }
        if options.token_budget != plan.TOKEN_BUDGET:
            problems.append(f"{cell['cell_id']}: GoalRunOptions.token_budget moved")
        if options.max_turns != int(contract.BUDGET["main_max_turns"]):
            problems.append(f"{cell['cell_id']}: the Main turn cap moved")
        if options.worker_max_turns != int(contract.BUDGET["worker_max_turns"]):
            problems.append(f"{cell['cell_id']}: the worker turn cap moved")
        if options.main_prompt.version != cell["main_prompt_version"]:
            problems.append(f"{cell['cell_id']}: the executed prompt is not the cell's arm")
        if options.main_prompt.digest != cell["main_prompt_digest"]:
            problems.append(f"{cell['cell_id']}: the executed prompt digest moved")
        if options.main_prompt.text != plan.PROMPT_TEXTS[str(cell["prompt_arm"])]:
            problems.append(f"{cell['cell_id']}: the executed prompt bytes moved")
        if options.goal != plan.goal_text(task_id):
            problems.append(f"{cell['cell_id']}: the executed goal is not the requirement")
        expected_grant = [str(path) for path in plan.effective_grant(task_id)[contract.CODING]]
        if grant != {contract.CODING: expected_grant}:
            problems.append(f"{cell['cell_id']}: the child grant moved")
        if list(options.main_write_paths) != list(contract.TASKS_BY_ID[task_id].allowed_paths):
            problems.append(f"{cell['cell_id']}: the Main write scope moved")
        if options.worker_profiles != (contract.CODING,) or options.worker_exposure != "card":
            problems.append(f"{cell['cell_id']}: the worker profile or exposure moved")
        for name, expected in (
            ("max_parallel_children", 2),
            ("max_children_per_call", 2),
            ("max_delegation_rounds", 3),
            ("child_max_turns", 20),
            ("result_preview_chars", 900),
        ):
            if getattr(options.delegation, name) != expected:
                problems.append(f"{cell['cell_id']}: delegation {name} moved")
        if options.public_tests.max_calls != expected_public_calls:
            problems.append(f"{cell['cell_id']}: the public-suite call cap moved")

    # The budget is checked where it is consumed: one real run_goal per prompt arm.
    proofs: dict[str, Any] = {}
    for prompt_arm in plan.PROMPT_ARMS:
        cell = next(item for item in plan_record["cells"] if item["prompt_arm"] == prompt_arm)
        try:
            proof = c1c_checks.run_budget_proof(
                {"cells": [cell]}, scratch / f"budget-proof-{prompt_arm}"
            )
        except Exception as error:  # noqa: BLE001 - a raised proof is a failed check
            problems.append(f"{prompt_arm}: the run-budget proof raised {type(error).__name__}")
            proofs[prompt_arm] = {"error": f"{type(error).__name__}: {error}"[:200]}
            continue
        proofs[prompt_arm] = proof
        if proof.get("run_budget_limit_tokens") != plan.TOKEN_BUDGET:
            problems.append(
                f"{prompt_arm}: run_goal's RunBudgetState is "
                f"{proof.get('run_budget_limit_tokens')!r}, not {plan.TOKEN_BUDGET}"
            )
        if proof.get("options_token_budget") != plan.TOKEN_BUDGET:
            problems.append(f"{prompt_arm}: the proof's options did not carry the budget")

    ok = not problems
    return _check(
        "C04_budget_prompt_and_permissions_take_effect",
        ok,
        "; ".join(problems)
        or (
            f"all four cells build GoalRunOptions with {plan.TOKEN_BUDGET} tokens, 20/20 turns, "
            "their own arm's prompt bytes, their own task's goal and grant, and the frozen "
            f"delegation limits; a real run_goal builds "
            f"RunBudgetState(limit_tokens={plan.TOKEN_BUDGET}) for B0 and B1"
        ),
        options=observed,
        run_budget=proofs,
    )


# -- C05: the router boundary ------------------------------------------------------------------


def _spec(
    key: str,
    objective: str,
    paths: tuple[str, ...],
    *,
    depends_on: tuple[str, ...] = (),
    profile_id: str | None = None,
) -> Any:
    from atlasos.delegation.models import DelegatedTaskSpec

    return DelegatedTaskSpec(
        key=key,
        objective=objective,
        background=(
            "The project is at /workspace/project. The task's published interface is a workspace "
            "file; test_public.py is present and must not be edited."
        ),
        profile_id=profile_id or contract.CODING,
        write_paths=paths,
        depends_on=depends_on,
    )


def _frozen_pair(task_id: str) -> tuple[Any, ...]:
    """The Host's own frozen decomposition of the task, as a proposal.

    Only the workers that declare no dependency are used: a batch is one parallel wave, so a frozen
    step that waits for another is not part of it.
    """

    frozen = contract.TASKS_BY_ID[task_id]
    return tuple(
        _spec(
            str(worker["task_id"]),
            str(worker["objective"]),
            tuple(worker["owned_paths"]),
        )
        for worker in frozen.workers
        if not worker["depends_on"]
    )


def _shared_write_pair(task_id: str) -> tuple[Any, ...]:
    """Both frozen steps of one task, each owning the module they both have to edit."""

    frozen = contract.TASKS_BY_ID[task_id]
    shared = sorted(
        {
            path
            for worker in frozen.workers
            for path in worker["owned_paths"]
            if sum(1 for other in frozen.workers if path in other["owned_paths"]) > 1
        }
    )
    return tuple(
        _spec(str(worker["task_id"]), str(worker["objective"]), tuple(shared))
        for worker in frozen.workers
    )


def _frozen_dag_pair(task_id: str) -> tuple[Any, ...]:
    frozen = contract.TASKS_BY_ID[task_id]
    return tuple(
        _spec(
            str(worker["task_id"]),
            str(worker["objective"]),
            tuple(worker["owned_paths"]),
            depends_on=tuple(worker["depends_on"]),
        )
        for worker in frozen.workers
    )


def _decide(options: Any, tasks: tuple[Any, ...]) -> dict[str, Any]:
    from atlasos.delegation.models import DelegateTasksInput
    from atlasos.delegation.router import DelegationRouter

    request = DelegateTasksInput(tasks=tasks, reason="Stage C2 offline admission check")
    decision = DelegationRouter().decide(request, context=c1_checks._plan_context(options))
    return {
        "route": decision.route.value,
        "reason_codes": list(decision.reason_codes),
        "blocked_gates": list(decision.blocked_gates),
        "admitted_children": [child.key for child in decision.children if child.admitted],
        "max_wave_width": decision.max_wave_width,
        "policy_version": decision.policy_version,
    }


def _check_router(plan_record: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    problems: list[str] = []
    positive_cell = next(
        cell
        for cell in plan_record["cells"]
        if cell["task_id"] == plan.P_PLUS_TASK_ID and cell["prompt_arm"] == "B0"
    )
    negative_cell = next(
        cell
        for cell in plan_record["cells"]
        if cell["task_id"] == plan.N_MINUS_TASK_ID and cell["prompt_arm"] == "B0"
    )
    positive_options = _production_options(positive_cell, scratch / "p-plus")
    negative_options = _production_options(negative_cell, scratch / "n-minus")

    independent = _decide(positive_options, _frozen_pair(plan.P_PLUS_TASK_ID))
    frozen_names = [spec.key for spec in _frozen_pair(plan.P_PLUS_TASK_ID)]
    if independent["route"] != "delegate_parallel":
        problems.append(f"the P+ pair is {independent['route']}: {independent['reason_codes']}")
    if independent["admitted_children"] != frozen_names:
        problems.append(f"the P+ admitted children are {independent['admitted_children']}")
    if "DELEGATION_ADMITTED" not in independent["reason_codes"]:
        problems.append("the P+ pair is not admitted for the expected reason")
    if independent["max_wave_width"] != len(frozen_names):
        problems.append("the P+ wave width is not the pair")
    if independent["policy_version"] != contract.BUDGET["router_policy"]:
        problems.append("the router policy version moved")

    shared = _decide(negative_options, _shared_write_pair(plan.N_MINUS_TASK_ID))
    if shared["route"] != "direct" or "SHARED_WRITE_RISK" not in shared["reason_codes"]:
        problems.append(f"the N- shared write is not refused for SHARED_WRITE_RISK: {shared}")
    if shared["admitted_children"]:
        problems.append("the N- shared write admitted a child")

    dag = _decide(negative_options, _frozen_dag_pair(plan.N_MINUS_TASK_ID))
    if dag["route"] != "direct" or "DEPENDENCY_NOT_SUPPORTED" not in dag["reason_codes"]:
        problems.append(f"the frozen N- DAG is not refused for its declared dependency: {dag}")

    boundary: dict[str, Any] = {}
    for shape, tasks, expected in (
        (
            "shared_write",
            (
                _spec("a", "First half.", ("pipeline/parsing.py",)),
                _spec("b", "Second half.", ("pipeline/parsing.py", "pipeline/store.py")),
            ),
            "SHARED_WRITE_RISK",
        ),
        (
            "outside_grant",
            (
                _spec("a", "First half.", ("pipeline/parsing.py",)),
                _spec("b", "Second half.", ("test_public.py",)),
            ),
            "WRITE_SCOPE_NOT_GRANTED",
        ),
        (
            "path_escape",
            (
                _spec("a", "First half.", ("pipeline/parsing.py",)),
                _spec("b", "Second half.", ("../outside.py",)),
            ),
            "INVALID_PLAN",
        ),
        (
            "research_write",
            (
                _spec("a", "First half.", ("pipeline/parsing.py",), profile_id=contract.RESEARCH),
                _spec("b", "Second half.", ("pipeline/store.py",), profile_id=contract.RESEARCH),
            ),
            "WRITE_SCOPE_NOT_GRANTED",
        ),
    ):
        decision = _decide(positive_options, tasks)
        boundary[shape] = decision
        if decision["route"] != "direct" or expected not in decision["reason_codes"]:
            problems.append(f"the boundary shape {shape} is {decision}")

    ok = not problems
    return _check(
        "C05_router_admits_the_independent_pair_and_refuses_the_shared_write",
        ok,
        "; ".join(problems)
        or (
            f"the P+ pair {frozen_names} is admitted as a "
            f"{independent['max_wave_width']}-wide parallel wave under "
            f"{independent['policy_version']}; the N- shared write is refused "
            f"SHARED_WRITE_RISK and its frozen DAG DEPENDENCY_NOT_SUPPORTED; the four boundary "
            "shapes are unchanged"
        ),
        independent=independent,
        shared_write=shared,
        frozen_dag=dag,
        boundary=boundary,
        production_grant={
            "p_plus": {
                key: list(value) for key, value in positive_options.child_write_grants.items()
            },
            "n_minus": {
                key: list(value) for key, value in negative_options.child_write_grants.items()
            },
        },
    )


# -- C06: the real dispatch entry ---------------------------------------------------------------


def _synthetic_runner() -> executor.CellRunner:
    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

    return run


def dispatch_report(
    plan_record: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    """The production dispatch, one offline run over all four cells."""

    record = c1_checks._offline_signed(
        plan_record,
        scratch=scratch,
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
        cost_stop_usd=plan.COST_STOP_CANDIDATE,
        per_cell_cost_stop_usd=plan.PER_CELL_COST_STOP_CANDIDATE,
    )
    authority_path = scratch / "offline-authorization.json"
    authority_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    evidence = scratch / "dispatch"
    with executor.claim_root_override(scratch / "claims"):
        report = asyncio.run(
            executor.dispatch(
                plan.PLAN_ID,
                runs_root=evidence / "runs",
                evidence_root=evidence,
                authority_path=authority_path,
                cell_runner=_synthetic_runner(),
                repo_root=REPO_ROOT,
                mode=executor.MODE_OFFLINE_PROOF,
                clock=lambda: "2026-11-26T00:00:00Z",
            )
        )
    cells = [
        {
            "cell_id": cell.get("cell_id"),
            "position": position,
            "verdict": cell.get("verdict"),
            "execution_integrity": cell.get("execution_integrity"),
            "goal_correctness": cell.get("goal_correctness"),
            "public_test_status": cell.get("public_test_status"),
            "oracle_passed": cell.get("oracle_passed"),
            "stops": cell.get("stops"),
            "failed_checks": [
                item["check"] for item in cell.get("checks") or [] if not item.get("ok")
            ],
            "check_count": len(cell.get("checks") or []),
        }
        for position, cell in enumerate(report.get("cells") or [], start=1)
    ]
    matrix_report = _read_json(evidence / "matrix-report.json") or {}
    return {
        "report": {
            "plan_id": report.get("plan_id"),
            "scope": report.get("scope"),
            "plan_digest": report.get("plan_digest"),
            "mode": report.get("mode"),
            "complete": report.get("complete"),
            "cells_planned": report.get("cells_planned"),
            "cells_started": report.get("cells_started"),
            "cells_passed": report.get("cells_passed"),
            "stop_reason": report.get("stop_reason"),
            "aggregate_includes_sample": report.get("aggregate_includes_sample"),
            "token_stop": report.get("token_stop"),
            "cost_stop_usd": report.get("cost_stop_usd"),
            "per_cell_cost_stop_usd": report.get("per_cell_cost_stop_usd"),
        },
        "cells": cells,
        "matrix_report": {
            "plan_digest": matrix_report.get("plan_digest"),
            "token_stop": matrix_report.get("token_stop"),
            "cells": len(matrix_report.get("cells") or []),
        },
        "evidence_exists": {
            "matrix_report": (evidence / "matrix-report.json").is_file(),
            "ledger": (evidence / "ledger.jsonl").is_file(),
            "attempts": all(
                (evidence / "cells" / f"c{order:02d}" / "attempt.json").is_file()
                for order in range(1, 5)
            ),
            "analyses": all(
                (evidence / "cells" / f"c{order:02d}" / "analysis.json").is_file()
                for order in range(1, 5)
            ),
        },
    }


def _check_dispatch_entry(
    plan_record: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    try:
        report = dispatch_report(
            plan_record,
            scratch=scratch,
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        )
    except Exception as error:  # noqa: BLE001 - a raised entry is a failed check
        return _check(
            "C06_real_dispatch_entry_runs_four_offline_cells",
            False,
            f"{type(error).__name__}: {error}"[:300],
        )
    body = report["report"]
    cells = report["cells"]
    ok = (
        body["plan_id"] == plan.PLAN_ID
        and body["scope"] == plan.SCOPE
        and body["plan_digest"] == plan_record["digest"]
        and body["mode"] == executor.MODE_OFFLINE_PROOF
        and body["complete"] is True
        and body["cells_planned"] == 4
        and body["cells_started"] == 4
        and body["cells_passed"] == 4
        and body["stop_reason"] == executor.STOP_NONE
        and body["aggregate_includes_sample"] is False
        and int(body["token_stop"]) == plan.NOMINAL_TOKEN_CAPACITY
        and float(body["cost_stop_usd"]) == plan.COST_STOP_CANDIDATE
        and float(body["per_cell_cost_stop_usd"]) == plan.PER_CELL_COST_STOP_CANDIDATE
        and len(cells) == 4
        and all(cell["verdict"] == "PASS" for cell in cells)
        and all(cell["execution_integrity"] == "PASS" for cell in cells)
        and all(cell["goal_correctness"] is True for cell in cells)
        and all(not cell["stops"] and not cell["failed_checks"] for cell in cells)
        and report["matrix_report"]["plan_digest"] == plan_record["digest"]
        and int(report["matrix_report"]["token_stop"] or 0) == plan.NOMINAL_TOKEN_CAPACITY
        and report["matrix_report"]["cells"] == 4
        and all(report["evidence_exists"].values())
    )
    return _check(
        "C06_real_dispatch_entry_runs_four_offline_cells",
        ok,
        f"dispatch({plan.PLAN_ID!r}) resolved the plan, verified the signed record, claimed in a "
        f"throwaway root, ran four synthetic cells and wrote the audit + matrix report (verdicts "
        f"{[cell['verdict'] for cell in cells]}, token_stop {body['token_stop']}, cost_stop "
        f"{body['cost_stop_usd']})",
        **report,
    )


# -- C07: production admission -----------------------------------------------------------------


def _check_preflight(plan_record: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    try:
        report = executor.run_admission_preflight(
            plan_record, repo_root=REPO_ROOT, scratch_root=scratch
        )
    except Exception as error:  # noqa: BLE001 - a raised preflight is a failed check
        return _check(
            "C07_production_admission_is_ready_for_all_four_cells",
            False,
            f"{type(error).__name__}: {error}"[:300],
        )
    cells = report.get("cells") or []
    ok = (
        report.get("status") == "READY"
        and not report.get("blocking_failures")
        and not report.get("contract_blocking_failures")
        and len(cells) == 4
        and all(cell.get("worker_profile") == contract.CODING for cell in cells)
        and all(cell.get("worker_exposure") == "card" for cell in cells)
        and all(cell.get("oracle_mode") == "EXTERNAL_SANDBOX_ORACLE" for cell in cells)
        and all(cell.get("fixture_matches_frozen_seed") is True for cell in cells)
    )
    return _check(
        "C07_production_admission_is_ready_for_all_four_cells",
        ok,
        f"the pre-claim gate accepts all four C2 cells ({report.get('status')}); the credential "
        f"(present={bool(report.get('credential_present'))}) is reported apart from the contract",
        status=report.get("status"),
        blocking_failures=list(report.get("blocking_failures") or []),
        contract_blocking_failures=list(report.get("contract_blocking_failures") or []),
        credential_present=bool(report.get("credential_present")),
        cells=[
            {
                "cell_id": cell.get("cell_id"),
                "status": cell.get("status"),
                "worker_profile": cell.get("worker_profile"),
                "worker_exposure": cell.get("worker_exposure"),
                "oracle_mode": cell.get("oracle_mode"),
                "fixture_matches_frozen_seed": cell.get("fixture_matches_frozen_seed"),
                "checks": cell.get("checks"),
            }
            for cell in cells
        ],
    )


# -- C08: the older identities -----------------------------------------------------------------


def _frozen_artefacts() -> dict[str, Any]:
    stage = REPO_ROOT / "experiments" / "stage_c1c"
    plan_file = _read_json(stage / "plan.json") or {}
    authorization = _read_json(stage / "authorization.json") or {}
    executor_file = _read_json(stage / "executor.json") or {}
    offline = _read_json(stage / "offline.json") or {}
    return {
        "plan_digest": plan_file.get("digest"),
        "plan_digest_recomputes": plan_file.get("digest") == c1c.plan_record()["digest"],
        "authorization_plan_digest": authorization.get("plan_digest"),
        "authorization_status": authorization.get("status"),
        "executor_version": executor_file.get("executor_version"),
        "executor_digest": executor_file.get("executor_digest"),
        "offline_plan_digest": offline.get("plan_digest"),
        "offline_executor_digest": offline.get("executor_digest"),
        "offline_status": offline.get("status"),
    }


def _check_history() -> dict[str, Any]:
    problems: list[str] = []
    protocol = authority.protocol_digest(contract.protocol_document())
    if protocol != BPLUS_PROTOCOL_DIGEST:
        problems.append(f"the B+ protocol digest moved to {protocol}")

    refused: list[str] = []
    for plan_id in (contract.CANARY_PLAN_ID_V1, contract.CANARY_PLAN_ID_V2):
        try:
            contract.scope_plan(plan_id)
        except ValueError:
            refused.append(plan_id)
            continue
        problems.append(f"{plan_id} can be rebuilt into a dispatchable plan again")

    frozen = _frozen_artefacts()
    for label, actual, expected in (
        ("C1 plan", contract.scope_plan(c1.PLAN_ID)["digest"], C1_PLAN_DIGEST),
        ("C1-B plan", contract.scope_plan(c1b.PLAN_ID)["digest"], C1B_PLAN_DIGEST),
        ("C1-C plan", contract.scope_plan(c1c.PLAN_ID)["digest"], C1C_PLAN_DIGEST),
        (
            "B+ canary",
            contract.scope_plan(contract.SCOPE_CANARY)["digest"],
            BPLUS_CANARY_PLAN_DIGEST,
        ),
        (
            "B+ formal",
            contract.scope_plan(contract.SCOPE_FORMAL)["digest"],
            BPLUS_FORMAL_PLAN_DIGEST,
        ),
        ("C1-C plan.json", frozen["plan_digest"], C1C_PLAN_DIGEST),
        ("C1-C authorization", frozen["authorization_plan_digest"], C1C_PLAN_DIGEST),
        ("C1-C offline", frozen["offline_plan_digest"], C1C_PLAN_DIGEST),
    ):
        if actual != expected:
            problems.append(f"the {label} identity moved to {actual}")
    if frozen["plan_digest_recomputes"] is not True:
        problems.append("the C1-C plan digest no longer recomputes")
    if frozen["authorization_status"] != "PENDING_OWNER_APPROVAL":
        problems.append("the C1-C authorization is no longer the pending record it was")
    if frozen["executor_version"] != C1C_EXECUTOR_VERSION:
        problems.append("the C1-C executor version was rewritten")
    if frozen["executor_digest"] != C1C_EXECUTOR_DIGEST:
        problems.append("the C1-C executor digest was rewritten")
    if frozen["offline_executor_digest"] != C1C_EXECUTOR_DIGEST:
        problems.append("the C1-C offline report's executor identity moved")
    if frozen["offline_status"] != "STAGE_C1C_HEADROOM_CONTROL_READY_FOR_LIVE_AUTHORIZATION":
        problems.append("the C1-C offline report was regenerated")

    ok = not problems
    return _check(
        "C08_older_plan_and_stage_identities_are_preserved",
        ok,
        "; ".join(problems)
        or (
            f"B+ canary/formal, C1, C1-B and C1-C plans still digest to their recorded values; the "
            f"protocol is still {BPLUS_PROTOCOL_DIGEST[:19]}…; both superseded canary ids remain "
            f"refused; the C1-C artefacts are still the ones its own stage wrote"
        ),
        protocol_digest=protocol,
        refused=refused,
        frozen_artefacts=frozen,
    )


# -- C09: claims and custody -------------------------------------------------------------------


def _claims_snapshot() -> dict[str, Any]:
    root, is_default = executor.claim_root()
    out: dict[str, Any] = {"root": str(root), "root_is_default": is_default, "claims": {}}
    for label, digest in CONSUMED_CLAIMS:
        path = executor.claim_path(digest)
        if path.is_file():
            payload = path.read_bytes()
            out["claims"][label] = {
                "path": str(path),
                "sha256": _digest_bytes(payload),
                "plan_digest_in_file": (_read_json(path) or {}).get("plan_digest"),
            }
        else:
            out["claims"][label] = {"path": str(path), "present": False}
    return out


def _evidence_index_report() -> dict[str, Any]:
    index = C1C_EVIDENCE_ROOT / "evidence-index.json"
    if not index.is_file():
        return {"mounted": False, "root": str(C1C_EVIDENCE_ROOT), "ok": True}
    payload = _read_json(index) or {}
    files = payload.get("files") or {}
    recomputed = _digest_bytes(json.dumps(files, sort_keys=True).encode("utf-8"))
    return {
        "mounted": True,
        "root": str(C1C_EVIDENCE_ROOT),
        "files": len(files) if isinstance(files, Mapping) else None,
        "index_digest": recomputed,
        "declared_in_file": payload.get("index_digest"),
        "plan_digest": payload.get("plan_digest"),
        "ok": (
            recomputed == C1C_EVIDENCE_INDEX_DIGEST
            and payload.get("index_digest") == C1C_EVIDENCE_INDEX_DIGEST
            and payload.get("plan_digest") == C1C_PLAN_DIGEST
            and isinstance(files, Mapping)
            and len(files) == C1C_EVIDENCE_FILES
        ),
    }


def _check_claims(
    plan_record: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    problems: list[str] = []
    states = {label: bool(item.get("sha256")) for label, item in after["claims"].items()}
    present = [label for label, ok in states.items() if ok]
    if len(present) not in (0, len(CONSUMED_CLAIMS)):
        problems.append(f"{len(present)} of {len(CONSUMED_CLAIMS)} consumed claims are present")
    if before.get("claims") != after.get("claims"):
        problems.append("a consumed claim changed during this preparation")
    if executor.claim_path(plan_record["digest"]).exists():
        problems.append("a claim exists for the C2 plan")
    if not after["root_is_default"]:
        problems.append("the production claim root is not in force")

    index = _evidence_index_report()
    if not index["ok"]:
        problems.append("the C1-C evidence index does not match its recorded digest")

    ok = not problems
    detail = (
        f"{len(present)}/{len(CONSUMED_CLAIMS)} consumed claims are byte-identical before and "
        f"after this preparation, no claim exists for {plan_record['digest'][:19]}…, "
    ) + (
        f"and the C1-C evidence index re-hashes to {C1C_EVIDENCE_INDEX_DIGEST[:19]}… "
        f"({index.get('files')} files)"
        if index.get("mounted")
        else "the C1-C evidence tree is not mounted here, so only the repository's frozen "
        "artefacts were compared"
    )
    return _check(
        "C09_historical_claims_are_intact_and_no_c2_claim_exists",
        ok,
        "; ".join(problems) or detail,
        claims=states,
        claim_path=str(executor.claim_path(plan_record["digest"])),
        claim_root=after["root"],
        evidence_index=index,
    )


# -- C10: the pending authorization -------------------------------------------------------------


def _check_authorization(
    plan_record: Mapping[str, Any],
    pending: Mapping[str, Any],
    *,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    problems: list[str] = []
    verdict = authority.verify(
        pending,
        plan=plan_record,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    if verdict["ready"] is not False:
        problems.append("the pending record verifies as an authorization")
    if pending.get("status") != "PENDING_OWNER_APPROVAL":
        problems.append("the pending record is not pending")
    if pending.get("authorizes_live_execution") is not False:
        problems.append("the pending record claims to authorize live execution")
    if (
        pending.get("cost_stop_usd") is not None
        or pending.get("per_cell_cost_stop_usd") is not None
    ):
        problems.append("a cost stop is signed in a pending record")
    if int(pending.get("token_stop") or 0) != plan.NOMINAL_TOKEN_CAPACITY:
        problems.append("the pending token stop is not the four-cell capacity")
    if pending.get("budget_digest") != plan_record["budget_digest"]:
        problems.append("the pending record does not carry the C2 budget digest")
    if pending.get("executor_digest") != identity["executor_digest"]:
        problems.append("the pending record does not carry the current executor digest")
    if list(pending.get("cell_ids") or []) != [cell["cell_id"] for cell in plan_record["cells"]]:
        problems.append("the pending record does not authorize exactly these four cells")
    if pending.get("attempt_limit") != 1:
        problems.append("the pending record does not declare one attempt")
    if (pending.get("proposed_envelope") or {}).get("approved") is not False:
        problems.append("the proposed envelope is presented as approved")
    for field in (
        "approved_by",
        "approved_at",
        "owner_statement",
        "approval_provenance",
        "reviewed_head",
        "execution_head",
    ):
        if pending.get(field) is not None:
            problems.append(f"{field} is filled in a pending record")

    ok = not problems
    return _check(
        "C10_pending_authorization_authorizes_nothing",
        ok,
        "; ".join(problems)
        or (
            f"status {pending.get('status')}, authorizes_live_execution false, both cost ceilings "
            f"null, token stop {pending.get('token_stop')} over four cells and one attempt; the "
            f"verifier refuses it for {len(verdict['problems'])} stated reasons"
        ),
        blocked_by=list(verdict["problems"]),
        envelope_candidate=dict(plan_record.get("envelope_candidate") or {}),
    )


# -- C11: the plan's own invariants -------------------------------------------------------------


def _check_plan(plan_record: Mapping[str, Any]) -> dict[str, Any]:
    problems = plan.validate(plan_record)
    return _check(
        "C11_plan_contract_invariants",
        not problems,
        "the frozen design invariants hold on the record itself",
        problems=problems,
    )


# -- C12: the reporting sources -----------------------------------------------------------------


def _check_reporting(plan_record: Mapping[str, Any]) -> dict[str, Any]:
    from experiments.stage_benchmark import reconstruct

    from atlasos.delegation.models import DelegationBatch, DelegationTotals

    contract_of_reporting = plan_record.get("reporting") or {}
    problems: list[str] = []

    totals_fields = set(DelegationTotals.model_fields)
    batch_fields = set(DelegationBatch.model_fields)
    cell_fields = {field.name for field in dataclasses.fields(executor.CellRun)}
    levels = {name for name, _ in reconstruct.LEVELS}

    for field in (
        "cache_hit_tokens",
        "cache_miss_tokens",
        "total_prompt_tokens",
        "completion_tokens",
    ):
        if field not in totals_fields:
            problems.append(f"DelegationTotals has no {field!r} field")
    for field in ("result_chars", "result_truncated", "max_observed_concurrency", "totals"):
        if field not in batch_fields:
            problems.append(f"DelegationBatch has no {field!r} field")
    if "oracle" not in cell_fields:
        problems.append("CellRun no longer carries the oracle verdict")
    if "L6_goal_correctness" not in levels:
        problems.append("the analyzer no longer has L6_goal_correctness, so the exclusion is stale")
    if reconstruct.CHILD_CACHE_VISIBILITY != "UNKNOWN":
        problems.append(
            "the analyzer's child cache field is no longer the constant this stage excludes"
        )

    final = contract_of_reporting.get("final_correctness") or {}
    if "L6_goal_correctness" not in str(final.get("not", "")):
        problems.append("the reporting contract does not exclude the analyzer's L6 field")
    if "oracle" not in str(final.get("source", "")):
        problems.append("the reporting contract does not name the audit's oracle verdict")
    for key in (
        "static_acceptance",
        "public_test",
        "independent_oracle",
        "main_completion",
        "execution_integrity",
    ):
        if key not in (final.get("reported_separately") or []):
            problems.append(f"the reporting contract does not report {key!r} separately")

    cache = contract_of_reporting.get("cache") or {}
    expected_children = [
        "delegation.totals.cache_hit_tokens",
        "delegation.totals.cache_miss_tokens",
    ]
    if sorted(cache.get("children") or []) != expected_children:
        problems.append("the child cache source is not the delegation totals split")
    if "child_cache_visibility" not in str(cache.get("not", "")):
        problems.append("the reporting contract does not exclude child_cache_visibility")

    handoff = contract_of_reporting.get("handoff") or {}
    for key in ("preview_delivered", "full_reference_read", "shared_file_consumption"):
        if not handoff.get(key):
            problems.append(f"the handoff reporting has no {key!r} entry")

    decision = contract_of_reporting.get("delegation_decision") or {}
    if "not_a_target" not in decision:
        problems.append(
            "the reporting contract does not state that delegation count is not a target"
        )

    ok = not problems
    return _check(
        "C12_reporting_sources_are_the_runtime_fields_not_the_analyzer",
        ok,
        "; ".join(problems)
        or (
            "final correctness is the cell audit's oracle verdict (CellRun.oracle), never the "
            "analyzer's L6_goal_correctness; the child cache split is "
            "DelegationTotals.cache_hit/miss_tokens, not child_cache_visibility; handoff preview, "
            "full reference read and shared-file consumption are three separate entries"
        ),
        analyzer={
            "analyzer_version": reconstruct.ANALYZER_VERSION,
            "levels": sorted(levels),
            "child_cache_visibility": reconstruct.CHILD_CACHE_VISIBILITY,
        },
        runtime_fields={
            "delegation_totals": sorted(totals_fields),
            "delegation_batch": sorted(batch_fields),
            "cell_run": sorted(cell_fields),
        },
    )


# -- assembly ----------------------------------------------------------------------------------


def authorization_record(
    plan_record: Mapping[str, Any], *, protocol_digest: str, identity: Mapping[str, Any]
) -> dict[str, Any]:
    record = authority.pending_record(
        plan_record, protocol_digest_value=protocol_digest, executor=identity
    )
    record["stage"] = plan.STAGE_ID
    record["proposed_envelope"] = {
        **dict(plan_record["envelope_candidate"]),
        "note": "a candidate for the Owner's decision; the signed fields stay null until signed",
    }
    record["intervention"] = dict(plan_record["intervention"])
    record["review_surface"] = {
        "plan_id": plan.PLAN_ID,
        "decision_axis": plan.DECISION_AXIS,
        "cells": [
            {
                "cell_id": cell["cell_id"],
                "task_id": cell["task_id"],
                "prompt_arm": cell["prompt_arm"],
                "goal_digest": cell["goal_digest"],
                "main_prompt_version": cell["main_prompt_version"],
                "main_prompt_digest": cell["main_prompt_digest"],
                "child_write_grants": {
                    key: list(value) for key, value in cell["child_write_grants"].items()
                },
            }
            for cell in plan_record["cells"]
        ],
        "prompts": {
            arm: {"version": plan.PROMPT_VERSIONS[arm], "digest": plan.PROMPT_DIGESTS[arm]}
            for arm in plan.PROMPT_ARMS
        },
        "worker_contract_digest": plan.worker_contract()["rendered_prompt_digest"],
        "effective_budget_digest": plan_record["budget_digest"],
        "budget_change": dict(plan_record["budget_change"]),
        "cost_stop_candidate": {
            "cost_stop_usd": plan.COST_STOP_CANDIDATE,
            "per_cell_cost_stop_usd": plan.PER_CELL_COST_STOP_CANDIDATE,
            "approved": False,
        },
        "reporting_contract": dict(plan.REPORTING_CONTRACT),
        "single_variable_ablation": True,
        "statistical_claim": False,
    }
    record["notes"] = (
        "Stage C2. This record authorizes nothing. It covers four cells and one attempt each: two "
        "tasks x two Main decision prompts (B0 byte-identical to Stage C1-B, B1 the coding-aware "
        "revision). The goal carries no delegation appendix, the task, grant, worker contract, "
        "router, oracle, turns and delegation limits are the frozen ones, and the shared token "
        "budget is 750000 per cell. A live dispatch needs every null field filled, both cost "
        "fields finite and positive, and an external provenance artefact whose bytes still hash "
        "to the recorded value."
    )
    return record


def run_checks(
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
    plan_record: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> list[dict[str, Any]]:
    scratch.mkdir(parents=True, exist_ok=True)
    before = _claims_snapshot()
    checks = [
        _check_suitability(plan_record),
        _check_entry_resolution(plan_record),
        _check_prompt_only(plan_record),
        _check_cells_match_tasks(plan_record, scratch / "c03"),
        _check_production_options(plan_record, scratch / "c04"),
        _check_router(plan_record, scratch / "c05"),
        _check_dispatch_entry(
            plan_record,
            scratch=scratch / "c06",
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        ),
        _check_preflight(plan_record, scratch / "c07"),
        _check_history(),
        _check_claims(plan_record, before, _claims_snapshot()),
        _check_authorization(
            plan_record,
            pending,
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        ),
        _check_plan(plan_record),
        _check_reporting(plan_record),
    ]
    return checks


def build(scratch: Path | None = None, *, write: bool = True) -> dict[str, Any]:
    head = executor.git_head(REPO_ROOT)
    identity = executor.executor_identity(REPO_ROOT)
    protocol_digest = authority.protocol_digest(contract.protocol_document())
    plan_record = plan.plan_record()
    pending = authorization_record(plan_record, protocol_digest=protocol_digest, identity=identity)

    parent = Path(scratch) if scratch is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=parent, prefix="stage-c2-run-"))
    checks = run_checks(
        scratch=base,
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
        plan_record=plan_record,
        pending=pending,
    )
    report = {
        "stage": plan.STAGE_ID,
        "design_version": plan.DESIGN_VERSION,
        "plan_id": plan.PLAN_ID,
        "scope": plan.SCOPE,
        "plan_digest": plan_record["digest"],
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "bplus_protocol_unchanged": protocol_digest == BPLUS_PROTOCOL_DIGEST,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "c1c_executor_digest": C1C_EXECUTOR_DIGEST,
        "budget_digest": plan_record["budget_digest"],
        "frozen_budget_digest": FROZEN_BUDGET_DIGEST,
        "nominal_token_capacity": plan_record["nominal_token_capacity"],
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "prompts": {
            arm: {
                "version": plan.PROMPT_VERSIONS[arm],
                "digest": plan.PROMPT_DIGESTS[arm],
                "chars": len(plan.PROMPT_TEXTS[arm]),
            }
            for arm in plan.PROMPT_ARMS
        },
        "prompt_changes": plan_record["prompt_changes"],
        "intervention_digest": plan.intervention_digest(),
        "budget_change": dict(plan_record["budget_change"]),
        "selection": plan_record["suitability"]["selection"],
        "cells": [
            {
                "cell_id": cell["cell_id"],
                "order": cell["order"],
                "task_id": cell["task_id"],
                "prompt_arm": cell["prompt_arm"],
                "goal_digest": cell["goal_digest"],
                "main_prompt_version": cell["main_prompt_version"],
                "main_prompt_digest": cell["main_prompt_digest"],
                "child_write_grants": {
                    key: list(value) for key, value in cell["child_write_grants"].items()
                },
            }
            for cell in plan_record["cells"]
        ],
        "built_from_head": head,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for check in checks if check["ok"]),
            "failed": [check["check_id"] for check in checks if not check["ok"]],
        },
        "status": (
            "STAGE_C2_NATURAL_DELEGATION_READY_FOR_LIVE_AUTHORIZATION"
            if all(check["ok"] for check in checks)
            else "STAGE_C2_NATURAL_DELEGATION_NOT_READY"
        ),
        "live_execution_authorized": False,
        "real_model_calls": 0,
        "real_sandboxes": 0,
        "real_cells": 0,
        "claims_consumed": 0,
    }
    if write:
        contract.write_document(plan_record, STAGE_DIR / "plan.json")
        contract.write_document(pending, STAGE_DIR / "authorization.json")
        contract.write_document(
            {
                "stage": plan.STAGE_ID,
                "executor_version": identity["executor_version"],
                "executor_digest": identity["executor_digest"],
                "files": identity["files"],
                "built_from_head": head,
                "supersedes": {
                    "executor_version": C1C_EXECUTOR_VERSION,
                    "executor_digest": C1C_EXECUTOR_DIGEST,
                    "scope": (
                        "Stage B+, C1, C1-B and C1-C plans only; their evidence is not rebuilt"
                    ),
                },
                "note": (
                    "the shared executor moved because contract.py gained the C2 plan entry and "
                    "the identity now covers experiments/stage_c2/plan.py and prompts.py, whose "
                    "bytes decide the cells this stage runs"
                ),
            },
            STAGE_DIR / "executor.json",
        )
        contract.write_document(report, STAGE_DIR / "offline.json")
    return report


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Stage C2 targeted verification")
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    report = build(arguments.scratch)
    if not arguments.quiet:
        for check in report["checks"]:
            flag = "PASS" if check["ok"] else "FAIL"
            print(f"{flag} {check['check_id']}: {check['detail'][:150]}")
        print(json.dumps(report["summary"], indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
