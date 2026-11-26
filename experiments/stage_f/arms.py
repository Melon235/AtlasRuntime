"""Stage F — the four execution arms, their real configuration, and the unified cell record.

One arm is one *execution architecture*, not a worker capability:

* ``A0`` native AnyCode single agent with the neutral sandbox tool adapter and, for this evaluation,
  the same guarded one-million-token cell budget.
* ``A1`` AtlasRuntime main-driven runtime with delegation compiled out of the tool surface.
* ``A2`` AtlasRuntime static DAG: a human-authored decomposition run by the AnyCode scheduler.
* ``A3`` AtlasRuntime main-driven runtime with selective delegation enabled.

The cell record is the single input every runner and every adapter consumes; it is also what the
plan digest covers. Nothing here executes anything.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from experiments.stage_b_plus import contract
from experiments.stage_c2 import prompts as c2_prompts
from experiments.stage_f import tasks as catalog

ADAPTER_VERSION = "stage-f-cell-adapter/1.0"

ExecutionMode = Literal["native", "atlas_direct", "atlas_static_dag", "atlas_dynamic"]
UsageAttribution = Literal["single_agent", "main_child", "dag_all_children"]

ARM_ORDER: tuple[str, ...] = ("A0", "A1", "A2", "A3")

EXECUTION_MODES: dict[str, ExecutionMode] = {
    "A0": "native",
    "A1": "atlas_direct",
    "A2": "atlas_static_dag",
    "A3": "atlas_dynamic",
}

USAGE_ATTRIBUTION: dict[str, UsageAttribution] = {
    "A0": "single_agent",
    "A1": "main_child",
    "A2": "dag_all_children",
    "A3": "main_child",
}

#: The balanced sequences. Each batch gives every sequence exactly two tasks.
QUATERNIONS: dict[str, tuple[str, ...]] = {
    "Q1": ("A0", "A1", "A3", "A2"),
    "Q2": ("A1", "A2", "A0", "A3"),
    "Q3": ("A2", "A3", "A1", "A0"),
    "Q4": ("A3", "A0", "A2", "A1"),
}

#: Stage F's own effective budget. The frozen Stage B+ ``contract.BUDGET`` is not edited: every cell
#: carries this block, and ``contract.budget_for(cell)`` merges it over the frozen defaults.
CELL_BUDGET: dict[str, Any] = {
    **{key: value for key, value in contract.BUDGET.items() if key != "token_budget"},
    "token_budget": 1_000_000,
    "main_max_turns": 40,
    "worker_max_turns": 20,
    "static_integration_max_turns": 40,
    "matrix_concurrency": 1,
    "max_concurrent_children": 2,
    "max_children_per_call": 2,
    "max_delegation_rounds": 3,
    "public_test_max_calls": 4,
}

BATCH_COUNT = 3
REPETITIONS = 3
CELLS_PER_BATCH = len(catalog.TASK_IDS) * len(ARM_ORDER)

#: Candidate stops. None of these is approved.
COST_STOP_PER_CELL = 1.00
COST_STOP_PER_BATCH = 32.00
COST_STOP_TOTAL = 96.00
TOKEN_STOP_PER_BATCH = CELLS_PER_BATCH * 1_000_000
TOKEN_STOP_TOTAL = BATCH_COUNT * TOKEN_STOP_PER_BATCH


def budget() -> dict[str, Any]:
    return dict(CELL_BUDGET)


def budget_digest() -> str:
    from experiments.stage_b_plus import authority

    return authority.digest_of(budget())


@dataclass(frozen=True)
class ArmSpec:
    """What one arm actually runs. ``attribution`` is not cosmetic: the analysis layer refuses a
    Main-scoped metric unless it says ``main_child``."""

    arm_id: str
    name: str
    execution_mode: ExecutionMode
    usage_attribution: UsageAttribution
    delegation_enabled: bool
    delegation_tools_visible: bool
    worker_profiles: tuple[str, ...]
    worker_exposure: str
    main_prompt: bool
    notes: str


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        arm_id="A0",
        name="AnyCode native single agent with common sandbox and budget controls",
        execution_mode="native",
        usage_attribution="single_agent",
        delegation_enabled=False,
        delegation_tools_visible=False,
        worker_profiles=(),
        worker_exposure="native",
        main_prompt=False,
        notes=(
            "AnyCode 0.9.0 native loop and scheduler with the neutral six-tool "
            "OpenSandbox adapter; "
            "no context plane, no router, no ownership policy, no action gate. Guarded one-million-"
            "token cell budget and per-request metering are added by the evaluation adapter only."
        ),
    ),
    ArmSpec(
        arm_id="A1",
        name="AtlasRuntime direct-only main-driven runtime",
        execution_mode="atlas_direct",
        usage_attribution="main_child",
        delegation_enabled=False,
        delegation_tools_visible=False,
        worker_profiles=(contract.CODING,),
        worker_exposure="card",
        main_prompt=True,
        notes=(
            "the full goal runtime with `DelegationConfig(enabled=False)`: neither "
            "`delegate_tasks` "
            "nor `delegate.read` exists in the tool surface."
        ),
    ),
    ArmSpec(
        arm_id="A2",
        name="AtlasRuntime static DAG",
        execution_mode="atlas_static_dag",
        usage_attribution="dag_all_children",
        delegation_enabled=False,
        delegation_tools_visible=False,
        worker_profiles=(contract.CODING,),
        worker_exposure="card",
        main_prompt=False,
        notes=(
            "a human-authored decomposition frozen into the plan and executed by the AnyCode "
            "scheduler; one shared budget and one shared workspace; no Main exists."
        ),
    ),
    ArmSpec(
        arm_id="A3",
        name="AtlasRuntime dynamic selective delegation",
        execution_mode="atlas_dynamic",
        usage_attribution="main_child",
        delegation_enabled=True,
        delegation_tools_visible=True,
        worker_profiles=(contract.RESEARCH, contract.CODING),
        worker_exposure="card",
        main_prompt=True,
        notes=(
            "the same goal runtime with delegation enabled, router m.1.0, both worker profiles "
            "disclosed through the capability card."
        ),
    ),
)

ARMS_BY_ID: dict[str, ArmSpec] = {arm.arm_id: arm for arm in ARMS}


def arm_spec(arm_id: str) -> ArmSpec:
    if arm_id not in ARMS_BY_ID:
        raise KeyError(f"unknown Stage F arm {arm_id!r}")
    return ARMS_BY_ID[arm_id]


def main_prompt() -> dict[str, str]:
    """A1 and A3 share these bytes verbatim; the only difference between them is the tool switch."""

    return {"version": c2_prompts.V30_VERSION, "text": c2_prompts.V30_TEXT}


def main_prompt_digest() -> str:
    return c2_prompts.V30_DIGEST


def worker_contract() -> dict[str, Any]:
    from experiments.stage_c1b import plan as c1b

    return c1b.worker_contract()


def delegation_config(enabled: bool) -> Any:
    from atlasos.delegation.config import DelegationConfig

    return DelegationConfig(
        enabled=enabled,
        max_parallel_children=int(CELL_BUDGET["max_concurrent_children"]),
        max_children_per_call=int(CELL_BUDGET["max_children_per_call"]),
        max_delegation_rounds=int(CELL_BUDGET["max_delegation_rounds"]),
        child_timeout_seconds=180.0,
        child_max_turns=int(CELL_BUDGET["worker_max_turns"]),
        result_preview_chars=int(CELL_BUDGET["result_preview_chars"]),
    )


def runner_config() -> Any:
    """The one ``RunnerConfig`` every main-driven Stage F cell uses."""

    from experiments.stage_b_plus import executor

    return executor.RunnerConfig(main_max_turns=int(CELL_BUDGET["main_max_turns"]))


def child_grants(task_id: str) -> dict[str, tuple[str, ...]]:
    """The task's own write scope, normalised once. Same function the router's matcher uses."""

    from atlasos.ownership.paths import normalize_declared_path

    task = catalog.task_by_id(task_id)
    root = "/workspace/project"
    return {
        contract.CODING: tuple(
            normalize_declared_path(path, workspace_root=root) for path in task.allowed_paths
        )
    }


def grant_digest(task_id: str) -> str:
    from experiments.stage_b_plus import authority

    return authority.digest_of({key: list(value) for key, value in child_grants(task_id).items()})


def static_dag(task_id: str) -> list[dict[str, Any]]:
    """The human decomposition, with everything a plan check and a reader need.

    The assignment text is part of the identity, not a comment: A2 executes exactly this
    decomposition, so a reworded assignment is a different experiment even when the write sets and
    the dependencies are unchanged.
    """

    task = catalog.task_by_id(task_id)
    workers = {str(worker["task_id"]): dict(worker) for worker in task.workers}
    depended_on = {str(dep) for worker in task.workers for dep in worker["depends_on"]}
    integration_cap = int(CELL_BUDGET["static_integration_max_turns"])
    implementation_cap = int(CELL_BUDGET["worker_max_turns"])
    nodes: list[dict[str, Any]] = []
    for node_id, paths, depends_on in catalog.STATIC_DAG[task_id]:
        worker = workers.get(str(node_id), {})
        sink = str(node_id) not in depended_on
        nodes.append(
            {
                "task_id": node_id,
                "objective": worker.get("objective"),
                "assignment": worker.get("assignment"),
                "owned_paths": list(paths),
                "depends_on": list(depends_on),
                # The cap the scheduler is actually given (`_benchmark_task`): the integration sink
                # carries the integration cap, every implementation node the worker cap.
                "max_turns": integration_cap if sink else implementation_cap,
            }
        )
    return nodes


def static_dag_digest(task_id: str) -> str:
    from experiments.stage_b_plus import authority

    return authority.digest_of(static_dag(task_id))


def goal_of(task_id: str) -> str:
    return catalog.task_by_id(task_id).prompt


def goal_digest(task_id: str) -> str:
    return "sha256:" + hashlib.sha256(goal_of(task_id).encode("utf-8")).hexdigest()


def cell_record(
    *,
    task_id: str,
    arm_id: str,
    repetition: int,
    sequence: str,
    position: int,
    batch_id: str,
    plan_id: str,
    scope: str,
) -> dict[str, Any]:
    """One cell, complete enough that nothing has to be looked up at execution time."""

    spec = arm_spec(arm_id)
    task = catalog.task_by_id(task_id)
    grants = child_grants(task_id)
    record: dict[str, Any] = {
        "cell_id": f"f.{task_id}.{arm_id}.r{repetition}",
        "plan_id": plan_id,
        "scope": scope,
        "batch_id": batch_id,
        "task_id": task_id,
        "family": catalog.family_of(task_id),
        "arm_id": arm_id,
        "arm_name": spec.name,
        "execution_mode": spec.execution_mode,
        "usage_attribution": spec.usage_attribution,
        "repetition": repetition,
        "sequence": sequence,
        "order": position,
        "is_benchmark_sample": False,
        "attempt_limit": 1,
        "adapter_version": ADAPTER_VERSION,
        "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
        "goal_id": f"{task_id}::public-requirement",
        "goal": task.prompt,
        "goal_digest": goal_digest(task_id),
        "worker_profiles": list(spec.worker_profiles),
        "worker_exposure": spec.worker_exposure,
        "worker_contract_digest": worker_contract()["rendered_prompt_digest"],
        "delegation": {
            "enabled": spec.delegation_enabled,
            "tools_visible": spec.delegation_tools_visible,
            "limits": {
                "max_parallel_children": int(CELL_BUDGET["max_concurrent_children"]),
                "max_children_per_call": int(CELL_BUDGET["max_children_per_call"]),
                "max_delegation_rounds": int(CELL_BUDGET["max_delegation_rounds"]),
                "child_max_turns": int(CELL_BUDGET["worker_max_turns"]),
                "result_preview_chars": int(CELL_BUDGET["result_preview_chars"]),
            },
        },
        "child_write_grants": {key: list(value) for key, value in grants.items()},
        "grant_digest": grant_digest(task_id),
        "budget": budget(),
        "budget_digest": budget_digest(),
        "token_budget": int(CELL_BUDGET["token_budget"]),
        "max_turns": int(CELL_BUDGET["main_max_turns"]),
        "worker_max_turns": int(CELL_BUDGET["worker_max_turns"]),
        "public_tests": sorted(task.public_tests),
        "deliverables": list(task.deliverables),
        "allowed_paths": list(task.allowed_paths),
    }
    if spec.main_prompt:
        record["main_prompt"] = main_prompt()
        record["main_prompt_version"] = main_prompt()["version"]
        record["main_prompt_digest"] = main_prompt_digest()
    if spec.execution_mode == "atlas_static_dag":
        record["static_dag"] = static_dag(task_id)
        record["static_dag_digest"] = static_dag_digest(task_id)
        record["integration_max_turns"] = int(CELL_BUDGET["static_integration_max_turns"])
    return record


def arm_summary() -> list[dict[str, Any]]:
    """The frozen per-arm configuration, for the manifest and the report."""

    return [
        {
            **asdict(arm),
            "usage_attribution": arm.usage_attribution,
            "delegation_tools_visible": arm.delegation_tools_visible,
        }
        for arm in ARMS
    ]


def validate_cell(cell: Mapping[str, Any]) -> list[str]:
    """Every invariant a cell must satisfy before it can enter a batch plan."""

    problems: list[str] = []
    arm = ARMS_BY_ID.get(str(cell.get("arm_id")))
    if arm is None:
        return [f"unknown arm {cell.get('arm_id')!r}"]
    if cell.get("execution_mode") != arm.execution_mode:
        problems.append(f"{cell.get('cell_id')}: execution mode is not the arm's")
    if cell.get("usage_attribution") != arm.usage_attribution:
        problems.append(f"{cell.get('cell_id')}: attribution is not the arm's")
    if cell.get("is_benchmark_sample") is not False:
        problems.append(f"{cell.get('cell_id')}: marked as a benchmark sample")
    if cell.get("attempt_limit") != 1:
        problems.append(f"{cell.get('cell_id')}: attempt limit is not 1")
    task_id = str(cell.get("task_id"))
    if task_id not in catalog.TASKS_BY_ID:
        problems.append(f"{cell.get('cell_id')}: unknown task")
        return problems
    if cell.get("goal") != goal_of(task_id) or cell.get("goal_digest") != goal_digest(task_id):
        problems.append(f"{cell.get('cell_id')}: goal is not the published requirement")
    if int(cell.get("token_budget") or 0) != int(CELL_BUDGET["token_budget"]):
        problems.append(f"{cell.get('cell_id')}: token budget is not one million")
    if contract.token_budget_for(cell) != int(CELL_BUDGET["token_budget"]):
        problems.append(f"{cell.get('cell_id')}: the resolver does not read the cell budget")
    if int(contract.BUDGET["token_budget"]) != 500_000:
        problems.append("the frozen Stage B+ budget moved")
    if list(cell.get("worker_profiles") or []) != list(arm.worker_profiles):
        problems.append(f"{cell.get('cell_id')}: worker profiles are not the arm's")
    if cell.get("worker_exposure") != arm.worker_exposure:
        problems.append(f"{cell.get('cell_id')}: worker exposure is not the arm's")
    if cell.get("delegation", {}).get("enabled") is not arm.delegation_enabled:
        problems.append(f"{cell.get('cell_id')}: delegation switch is not the arm's")
    has_prompt = "main_prompt" in cell
    if has_prompt is not arm.main_prompt:
        problems.append(f"{cell.get('cell_id')}: prompt presence does not match the arm")
    if has_prompt:
        if cell["main_prompt"].get("text") != main_prompt()["text"]:
            problems.append(f"{cell.get('cell_id')}: prompt bytes are not the frozen bytes")
        if cell.get("main_prompt_digest") != main_prompt_digest():
            problems.append(f"{cell.get('cell_id')}: prompt digest is not the frozen digest")
    if arm.execution_mode == "atlas_static_dag":
        nodes = cell.get("static_dag") or []
        if not nodes:
            problems.append(f"{cell.get('cell_id')}: a static-DAG cell has no decomposition")
        else:
            ids = {node["task_id"] for node in nodes}
            for node in nodes:
                if set(node["depends_on"]) - ids:
                    problems.append(f"{cell.get('cell_id')}: node depends on an unknown node")
            for index, left in enumerate(nodes):
                for right in nodes[index + 1 :]:
                    if set(left["depends_on"]) & {right["task_id"]} or set(right["depends_on"]) & {
                        left["task_id"]
                    }:
                        continue
                    overlap = set(left["owned_paths"]) & set(right["owned_paths"])
                    if overlap:
                        problems.append(
                            f"{cell.get('cell_id')}: concurrent nodes share {sorted(overlap)}"
                        )
    else:
        if "static_dag" in cell:
            problems.append(f"{cell.get('cell_id')}: a non-DAG arm carries a decomposition")
    return problems


__all__ = [
    "ADAPTER_VERSION",
    "ARM_ORDER",
    "ARMS",
    "ARMS_BY_ID",
    "ArmSpec",
    "BATCH_COUNT",
    "CELLS_PER_BATCH",
    "CELL_BUDGET",
    "COST_STOP_PER_BATCH",
    "COST_STOP_PER_CELL",
    "COST_STOP_TOTAL",
    "EXECUTION_MODES",
    "QUATERNIONS",
    "REPETITIONS",
    "TOKEN_STOP_PER_BATCH",
    "TOKEN_STOP_TOTAL",
    "USAGE_ATTRIBUTION",
    "arm_spec",
    "arm_summary",
    "budget",
    "budget_digest",
    "cell_record",
    "child_grants",
    "delegation_config",
    "goal_digest",
    "goal_of",
    "grant_digest",
    "main_prompt",
    "main_prompt_digest",
    "runner_config",
    "static_dag",
    "static_dag_digest",
    "validate_cell",
    "worker_contract",
]
