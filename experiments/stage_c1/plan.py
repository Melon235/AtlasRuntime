"""Stage C1 frozen positive control: one cell, one attempt, one user-goal appendix.

What this stage is
------------------
Stage B+ V3 ran six real cells and every one of them ended ``NO_PROPOSAL``: the Main judged P1's
scale and coupling itself and chose direct execution. Stage C1 does not build more runtime. It runs
one cell on a task whose two implementation halves are genuinely disjoint (Closure-2 P2, the plugin
migration) and appends one instruction to the *user goal* asking the Main to delegate the
independent parts.

The intervention is deliberately single and narrow:

* the Main **system prompt** is the unchanged ``main-v2.1-rc1-bplus`` text — its digest is the one
  Stage B+ recorded, and :func:`validate` recomputes it;
* the task requirement is the unchanged P2 prompt, byte for byte, as a prefix of the goal;
* the only added bytes are :data:`ACTIVATION_INSTRUCTION`, appended after the requirement;
* no worker assignment, DAG, writable-scope list, reference implementation or hidden test is given
  to the model. :func:`validate` checks the frozen catalog for every one of those strings.

Why P2 and not P1
-----------------
P1's two disjoint workers are ``parse``/``normalise`` and ``aggregate``/``store``; P2's are the CSV
plugin and the JSON plugin. P2 is chosen because the two workstreams differ in *input format*, the
interface they implement (``plugins/base.py``) is frozen and needs no edit, the registry/loader
integration is left to the Main, and B0/B1 will run the same task so the variable is controlled.
P2 remains a mechanism diagnostic; it is not a claim about a large real project.

Identity
--------
The plan digest is ``matrix.plan_digest`` over one cell, exactly as Stage B+ computes it, so a
reader can recompute the identity with the existing function. The goal text and its digest are
*inside* the cell, so the identity moves if either the P2 requirement or the appendix changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from experiments.evaluation.closure2 import catalog as closure2
from experiments.evaluation.models import text_digest
from experiments.stage_b_plus import authority as authority_module
from experiments.stage_b_plus import contract
from experiments.stage_benchmark import matrix

DESIGN_VERSION = "c1-1.1.0"
STAGE_ID = "C1"

PLAN_ID = "stage-c1-coding-delegation-positive-control-v1"
SCOPE = "C1_POSITIVE_CONTROL_ONLY"

TASK_ID = "closure2-plugin-migration"
ARM_ID = "C3"
REPETITION = 1
ATTEMPT_LIMIT = 1

GOAL_ID = "c1-p2-requirement-plus-coding-delegation-appendix-v1"

#: The only activation intervention, one string per paragraph. Appended to the user goal, never to
#: the system prompt, and never carrying a decomposition, a path list, a worker assignment or a
#: hidden test.
_ACTIVATION_PARAGRAPHS: tuple[str, ...] = (
    "For this task, use coding workers to implement independently executable parts of the "
    "migration.",
    "Identify at least two independent implementation workstreams when the published interfaces "
    "and ownership boundaries permit it, and attempt to delegate them together before implementing "
    "those workstreams yourself.",
    "Choose the decomposition, writable scopes, worker inputs, and deliverables yourself using the "
    "available runtime capabilities.",
    "Respect all admission, ownership, and safety constraints. If no safe delegation is possible "
    "or the runtime rejects the proposal, explain the actual reason and continue appropriately.",
    "You remain responsible for integration, public testing, and final task correctness.",
)

ACTIVATION_INSTRUCTION = "\n\n".join(_ACTIVATION_PARAGRAPHS)

#: One blank line between the frozen requirement and the appendix. Stated as a constant so the
#: composition is checkable rather than implied by a literal.
GOAL_SEPARATOR = "\n\n"

#: The workspace root every tool, ownership check and child sees inside a project sandbox (AnyCode's
#: M5 contract). The offline checks compare it with
#: ``atlasos.app.main_driven.SANDBOX_WORKSPACE_ROOT`` rather than trust this copy.
WORKSPACE_ROOT = "/workspace/project"

#: One grant, two representations. The frozen task scope is a glob; the router's matcher accepts a
#: normalised file or a directory declaration. The repository already owns the normaliser for the
#: second form (the ownership manifest applies it), so the C1 cell binds the normalised form instead
#: of declaring a second matcher.
GRANT_NORMALIZER = "atlasos.ownership.paths.normalize_declared_path"


def _normalize(grants: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    from atlasos.ownership.paths import normalize_declared_path

    return {
        profile_id: tuple(
            normalize_declared_path(path, workspace_root=WORKSPACE_ROOT) for path in paths
        )
        for profile_id, paths in grants.items()
    }


def raw_grant() -> dict[str, tuple[str, ...]]:
    """The arm's grant as the frozen B+ contract states it: the task's own write scope."""

    return dict(contract.ARMS_BY_ID[ARM_ID].grant_for(TASK_ID))


def effective_grant() -> dict[str, tuple[str, ...]]:
    """The same scope, in the representation the runtime's matcher accepts."""

    return _normalize(raw_grant())


def grant_covers(target: str) -> bool:
    """Whether the effective grant admits ``target``, by the runtime's own predicate.

    ``path_is_within`` plus ``normalize_workspace_path`` is exactly what the router applies to a
    model declaration and what the ownership manifest applies to a write, so this answers "would the
    runtime let a child write here" rather than "does a string look similar".
    """

    from atlasos.ownership.models import path_is_within
    from atlasos.ownership.paths import normalize_workspace_path

    normalized = normalize_workspace_path(target, workspace_root=WORKSPACE_ROOT)
    granted = effective_grant()[contract.CODING]
    return any(path_is_within(declared, normalized) for declared in granted)


def grant_record() -> dict[str, Any]:
    """Both representations, the normaliser that connects them, and what did *not* change."""

    raw = raw_grant()
    effective = effective_grant()
    forbidden = list(task().forbidden_paths)
    return {
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
        # Stated so a reader never has to infer it: no scope was added, removed or merged.
        "scope_changed": False,
        "profile_changed": False,
        "matches_frozen_task_scope": set(raw.get(contract.CODING, ()))
        == set(contract.TASK_WRITE_SCOPE[TASK_ID]),
        # The three layers, kept apart: what the runtime denies, what only the task text forbids,
        # and what only the oracle can catch. ``plugins/base.py`` is inside the granted directory,
        # so the runtime would accept a write to it; it is protected by the task instruction and by
        # the oracle's reference check, not by ownership.
        "denied_by_runtime": [path for path in forbidden if not grant_covers(path)],
        "forbidden_inside_grant": [path for path in forbidden if grant_covers(path)],
        "forbidden_inside_grant_protection": (
            "task instruction plus independent oracle, not ownership"
        ),
    }


#: Candidate envelope. Not an approval: every cost field of the authority record stays null until
#: the Owner signs it.
ENVELOPE_CANDIDATE: dict[str, Any] = {
    "cost_stop_usd": 0.15,
    "per_cell_cost_stop_usd": 0.15,
    "token_stop": int(contract.BUDGET["token_budget"]),
    "approved": False,
    "basis": (
        "one cell; covers the B+ S1 delegated estimate per cell (about 0.083 USD at the frozen "
        "price) with margin, and is the same per-cell ceiling Stage B+ proposed for its canary"
    ),
}

#: What this stage promises *not* to change. Checked by :func:`validate` where it is mechanical.
DOES_NOT_MODIFY: tuple[str, ...] = (
    "experiments/stage_b_plus/contract.py::CANARY_TASK_IDS",
    "experiments/stage_b_plus/contract.py::EXECUTION_CONTRACT",
    "experiments/stage_b_plus/plan-canary.json",
    "experiments/stage_b_plus/plan-formal.json",
    "experiments/stage_b_plus/protocol.json",
    "experiments/evaluation/closure2/*",
)


def task() -> closure2.Task:
    return contract.TASKS_BY_ID[TASK_ID]


def goal_text() -> str:
    """The frozen P2 requirement followed by the frozen appendix. No other bytes."""

    return contract.GOAL_OF[TASK_ID] + GOAL_SEPARATOR + ACTIVATION_INSTRUCTION


def goal_digest() -> str:
    return "sha256:" + text_digest(goal_text())


def activation_digest() -> str:
    return authority_module.digest_of({"id": GOAL_ID, "text": ACTIVATION_INSTRUCTION})


def main_prompt_spec() -> Any:
    """The Stage B+ prompt object, not a copy of it."""

    return contract.main_prompt_spec()


def prompt_digest() -> str:
    return "sha256:" + text_digest(contract.prompt_text("p3-bplus"))


def cells() -> list[dict[str, Any]]:
    """The single cell, with the goal bound into it.

    ``matrix.build_plan`` gives the cell id/order/repetition the rest of the stack expects; the
    annotation follows the Stage B+ canary convention (own prefix, own scope, own plan id, not a
    benchmark sample, execution contract stamped in).
    """

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
                "cell_id": f"c1.{cell['cell_id']}",
                "scope": SCOPE,
                "plan_id": PLAN_ID,
                "is_benchmark_sample": False,
                "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
                # The intervention and the goal are part of the cell, so they are part of the plan
                # digest: renaming the plan cannot produce a different goal.
                "goal_id": GOAL_ID,
                "goal": goal_text(),
                "goal_digest": goal_digest(),
                "intervention_digest": activation_digest(),
                "main_prompt_version": contract.BPLUS_VERSION,
                "main_prompt_digest": prompt_digest(),
                "worker_profile": arm.profile_id,
                "worker_exposure": arm.exposure,
                "attempt_limit": ATTEMPT_LIMIT,
                # The Host grant the run executes with, in the form the runtime's matcher accepts,
                # plus the frozen original and the normaliser that connects them. Both are in the
                # cell, so renaming or re-scoping the plan cannot produce a different grant.
                "child_write_grants": effective_grant(),
                "grant_normalization": grant_record(),
            }
        )
    return cells_out


def intervention_record() -> dict[str, Any]:
    """The intervention, stated once for the report and for the authorization's review surface."""

    return {
        "id": GOAL_ID,
        "kind": "user_goal_appendix",
        "text": ACTIVATION_INSTRUCTION,
        "digest": activation_digest(),
        "goal_digest": goal_digest(),
        "goal_chars": len(goal_text()),
        "requirement_chars": len(contract.GOAL_OF[TASK_ID]),
        "appendix_chars": len(ACTIVATION_INSTRUCTION),
        "separator": repr(GOAL_SEPARATOR),
        "main_prompt_modified": False,
        "main_prompt_version": contract.BPLUS_VERSION,
        "main_prompt_digest": prompt_digest(),
        "worker_assignment_supplied": False,
        "dag_supplied": False,
        "writable_scopes_supplied": False,
        "reference_implementation_supplied": False,
        "hidden_tests_supplied": False,
    }


def plan_record() -> dict[str, Any]:
    """The plan object ``executor.dispatch`` consumes. Digest = f(cells) only."""

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
        "claim_namespace": f"c1-claim/{PLAN_ID}",
        "runs_subdir": f"runs/stage-c1/{PLAN_ID}",
        "evidence_subdir": f"evidence/stage-c1/{PLAN_ID}",
        "is_benchmark_sample": False,
        "is_diagnostic": True,
        "attempt_limit": ATTEMPT_LIMIT,
        "nominal_token_capacity": len(plan_cells) * int(contract.BUDGET["token_budget"]),
        "execution_contract": dict(contract.EXECUTION_CONTRACT),
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "intervention": intervention_record(),
        "goal_id": GOAL_ID,
        "goal_digest": goal_digest(),
        "write_grant": grant_record(),
        "main_prompt": {
            "version": contract.BPLUS_VERSION,
            "digest": prompt_digest(),
            "unchanged_from_bplus": True,
            "literal_p3_digest": contract.P3_DIGEST,
        },
        "task": {
            "task_id": TASK_ID,
            "family": task().family,
            "write_scope": list(task().allowed_paths),
            "deliverables": list(task().deliverables),
            "forbidden_paths": list(task().forbidden_paths),
            "public_tests": sorted(task().public_tests),
            "frozen_workers": [
                {"task_id": worker["task_id"], "owned_paths": list(worker["owned_paths"])}
                for worker in task().workers
            ],
        },
        "budget": dict(contract.BUDGET),
        "budget_digest": authority_module.digest_of(dict(contract.BUDGET)),
        "envelope_candidate": dict(ENVELOPE_CANDIDATE),
        "does_not_modify": list(DOES_NOT_MODIFY),
        # An approval for another plan never carries over: the Owner signs a digest.
        "authorization_migrates": False,
    }


def _leaked_strings() -> tuple[str, ...]:
    """Every frozen string that would hand the model the decomposition or the answer."""

    frozen = task()
    leaked: list[str] = ["[YOUR ASSIGNMENT"]
    for worker in frozen.workers:
        leaked.append(str(worker["assignment"]))
        leaked.append(str(worker["objective"]))
    leaked.extend(sorted(frozen.reference))  # reference implementation file names
    leaked.extend(sorted(frozen.oracle_tests))  # hidden test file names
    return tuple(item for item in leaked if item)


def validate(plan: Mapping[str, Any] | None = None) -> list[str]:
    """Every invariant this design promises, checked on the record itself."""

    record = dict(plan or plan_record())
    problems: list[str] = []
    frozen = task()

    if record.get("plan_id") != PLAN_ID or record.get("scope") != SCOPE:
        problems.append("the plan is not the C1 positive control identity")
    if record.get("stage") != STAGE_ID:
        problems.append("the plan does not declare its own stage")
    if record.get("is_benchmark_sample") is not False:
        problems.append("the C1 cell would be counted as a benchmark sample")

    plan_cells = list(record.get("cells") or [])
    if len(plan_cells) != 1:
        problems.append(f"the plan has {len(plan_cells)} cells; the design is exactly one")
    if not plan_cells:
        return problems
    cell = plan_cells[0]
    if record.get("digest") != matrix.plan_digest(plan_cells):
        problems.append("the plan digest does not recompute from its cells")
    if cell.get("task_id") != TASK_ID or cell.get("arm_id") != ARM_ID:
        problems.append("the cell is not P2 on arm C3")
    if cell.get("scope") != SCOPE or cell.get("plan_id") != PLAN_ID:
        problems.append("the cell does not carry the plan's own identity")
    if cell.get("execution_contract") != contract.EXECUTION_CONTRACT_DIGEST:
        problems.append("the cell is not bound to the current execution contract")
    if cell.get("is_benchmark_sample") is not False:
        problems.append("a C1 cell is marked as a benchmark sample")
    if cell.get("attempt_limit") != ATTEMPT_LIMIT:
        problems.append("the cell does not declare one attempt")

    arm = contract.ARMS_BY_ID[ARM_ID]
    if arm.profile_id != contract.CODING or arm.exposure != "card":
        problems.append("arm C3 is no longer coding_worker with the full card")
    if cell.get("worker_profile") != arm.profile_id or cell.get("worker_exposure") != arm.exposure:
        problems.append("the cell disagrees with the arm about profile or exposure")

    # the Host grant: two representations of one frozen scope, nothing added or removed
    expected_raw = raw_grant()
    expected_effective = effective_grant()
    bound = {
        str(profile_id): tuple(str(path) for path in paths)
        for profile_id, paths in (cell.get("child_write_grants") or {}).items()
    }
    if bound != expected_effective:
        problems.append("the cell's Host grant is not the frozen effective grant")
    if set(expected_effective) != {contract.CODING}:
        problems.append("the effective grant is not for the coding profile alone")
    normalization = cell.get("grant_normalization") or {}
    as_lists = {profile_id: list(paths) for profile_id, paths in expected_raw.items()}
    if normalization.get("raw") != as_lists:
        problems.append("the cell does not record the frozen original grant")
    if normalization.get("normalizer") != GRANT_NORMALIZER:
        problems.append("the cell names a normaliser the repository does not own")
    if normalization.get("workspace_root") != WORKSPACE_ROOT:
        problems.append("the grant was normalised against another workspace root")
    if normalization.get("scope_changed") is not False:
        problems.append("the normalisation claims to change the scope")
    if normalization.get("matches_frozen_task_scope") is not True:
        problems.append("the original grant is not the frozen task write scope")
    if record.get("write_grant") != normalization:
        problems.append("the plan and the cell disagree about the Host grant")
    missing = [path for path in frozen.deliverables if not grant_covers(path)]
    if missing:
        problems.append(f"the effective grant does not admit every deliverable: {missing}")
    if "test_public.py" in (normalization.get("forbidden_inside_grant") or []):
        problems.append("the effective grant admits the public suite")
    if not normalization.get("denied_by_runtime"):
        problems.append("the effective grant denies nothing, which cannot be the task scope")

    # the goal: the frozen requirement, one separator, the frozen appendix, nothing else
    goal = str(cell.get("goal") or "")
    if not goal.startswith(contract.GOAL_OF[TASK_ID] + GOAL_SEPARATOR):
        problems.append("the goal does not begin with the frozen P2 requirement")
    if goal != goal_text():
        problems.append("the executed goal is not the frozen composition")
    if goal.count(ACTIVATION_INSTRUCTION) != 1:
        problems.append("the activation instruction does not occur exactly once in the goal")
    if cell.get("goal_digest") != goal_digest():
        problems.append("the cell's goal digest does not match the frozen goal")
    if record.get("goal_digest") != cell.get("goal_digest"):
        problems.append("the plan and the cell disagree about the goal digest")
    if cell.get("intervention_digest") != activation_digest():
        problems.append("the cell's intervention digest does not match the frozen appendix")

    for leaked in _leaked_strings():
        if leaked and leaked in goal:
            problems.append(f"the goal carries a frozen assignment/answer string: {leaked[:60]!r}")
    lowered = goal.lower()
    for phrase in contract.FORBIDDEN_GOAL_PHRASES:
        if phrase in lowered:
            problems.append(f"the goal says {phrase!r}, which the B+ contract forbids")

    # the prompt: the Stage B+ bytes, unchanged
    document = contract.protocol_document()
    declared = (document.get("prompts") or {}).get("p3-bplus") or {}
    if cell.get("main_prompt_version") != contract.BPLUS_VERSION:
        problems.append("the cell names a different Main prompt version")
    if cell.get("main_prompt_digest") != declared.get("digest"):
        problems.append("the cell's prompt digest is not the Stage B+ prompt digest")
    if prompt_digest() != declared.get("digest"):
        problems.append("the live prompt text no longer hashes to the Stage B+ digest")
    literal_digest = ((document.get("prompts") or {}).get("p3-literal") or {}).get("digest")
    if declared.get("digest") == literal_digest:
        problems.append("the B+ prompt is not a distinct object from P3")

    # the budget and the capacity
    if record.get("budget") != dict(contract.BUDGET):
        problems.append("the plan does not run on the frozen Stage B+ budget")
    if record.get("budget_digest") != authority_module.digest_of(dict(contract.BUDGET)):
        problems.append("the budget digest does not recompute")
    expected_capacity = len(plan_cells) * int(contract.BUDGET["token_budget"])
    if record.get("nominal_token_capacity") != expected_capacity:
        problems.append("the nominal token capacity is not one cell's budget")
    envelope = record.get("envelope_candidate") or {}
    if envelope.get("token_stop") != expected_capacity:
        problems.append("the candidate token stop is not the nominal capacity")
    for field in ("cost_stop_usd", "per_cell_cost_stop_usd"):
        value = envelope.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            problems.append(f"the candidate {field} is not finite and positive")
    if envelope.get("approved") is not False:
        problems.append("the stage proposes a cost ceiling as if it were approved")
    if float(envelope.get("per_cell_cost_stop_usd") or 0) > float(
        envelope.get("cost_stop_usd") or 0
    ):
        problems.append("the per-cell stop is larger than the cumulative stop")

    # the task facts this design depends on
    if list(record.get("tasks") or []) != [TASK_ID] or list(record.get("arms") or []) != [ARM_ID]:
        problems.append("the plan's task/arm lists are not the single P2/C3 cell")
    if record.get("authorization_migrates") is not False:
        problems.append("the plan claims an existing authorization migrates to it")
    if frozen.family != "P2":
        problems.append("the closure-2 P2 task changed family")
    if "plugins/base.py" not in frozen.seed:
        problems.append("the frozen plugin interface is not part of the seed")
    if "plugins/base.py" not in frozen.forbidden_paths:
        problems.append("the frozen plugin interface is no longer protected from edits")
    return problems


__all__ = [
    "ACTIVATION_INSTRUCTION",
    "ARM_ID",
    "ATTEMPT_LIMIT",
    "DESIGN_VERSION",
    "DOES_NOT_MODIFY",
    "ENVELOPE_CANDIDATE",
    "GOAL_ID",
    "GOAL_SEPARATOR",
    "GRANT_NORMALIZER",
    "PLAN_ID",
    "REPETITION",
    "SCOPE",
    "STAGE_ID",
    "TASK_ID",
    "WORKSPACE_ROOT",
    "activation_digest",
    "cells",
    "effective_grant",
    "goal_digest",
    "goal_text",
    "grant_covers",
    "grant_record",
    "intervention_record",
    "main_prompt_spec",
    "plan_record",
    "prompt_digest",
    "task",
    "validate",
]
