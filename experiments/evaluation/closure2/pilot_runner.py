"""Closure-2B-0: the thin orchestration layer over the frozen Closure-2A benchmark.

What this module is:

* an **arm adapter** — six frozen arms mapped onto the two engines that already exist
  (``experiments.evaluation.runner.run_sample`` for Atlas, and
  ``experiments.evaluation.closure1b.native_baseline.run_native_sample`` for native AnyCode);
* a **contract checker** — every frozen identity (preregistration digest, task digests, router
  digest, budget table, sandbox contract, arm/config wiring) is re-derived and compared before
  anything is planned or executed;
* a **manifest generator** — the 18 planned cells (3 tasks x 6 arms) with their resolved identity,
  written to ``experiments/results/closure2/pilot-dry-run.json``;
* a **result normaliser** — one flat record per run, with missing values left missing.

What this module is not: an agent loop, a scheduler, a worker, a sandbox, an oracle, or a new
Context Plane. It calls the existing ones and records what happened.

Execution modes are two distinct entry points, never one entry point with a flag that an environment
variable can flip:

``build_dry_run_manifest()``
    Pure planning. No sandbox, no provider, no API key, no approval requirement. It reads YAML and
    frozen Python data and nothing else.

``run_live_pilot()``
    Real execution. The *first* statement is ``require_execution_approval()``; only after the frozen
    identity and the owner approval agree does an executor get called. Without an explicit
    ``live_intent=True`` it refuses before that too.

The default is always the dry run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from experiments.evaluation.runner import RunnerSettings

from experiments.evaluation.closure2.catalog import (
    BENCHMARK_ID,
    DATASET_VERSION,
    TASKS,
    Task,
    build_task,
    task_digests,
)
from experiments.evaluation.closure2.preflight import (
    C2A_FROZEN_TASK_DIGESTS,
    ROUTING_POLICY_DIGEST,
    router_audit,
)
from experiments.evaluation.closure2.protocol import (
    ARMS,
    BUDGET,
    C2A_R1_PREREGISTRATION_DIGEST,
    LEGACY_CONTEXT_CONFIG,
    PROTOCOL_REVISION,
    ROUTING_POLICY_VERSION,
    SHARED_PREFIX_CONFIG,
    approval_gate,
    arm_config_digest,
    config_semantic_digest,
    layout_ablation_delta,
    preregistration_digest,
    require_execution_approval,
    resolve_repo_path,
)
from experiments.evaluation.models import BenchmarkTask, effective_public_task_digest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DRY_RUN_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "pilot-dry-run.json"
DEFAULT_RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "closure2"

#: The native engine's own configuration. It carries the same sandbox block as the Atlas configs and
#: the same provider model; ``check_frozen_contract`` proves that instead of assuming it.
NATIVE_CONFIG = "experiments/configs/eval-benchmark.yaml"

#: The six frozen arms, in registration order.
ARM_ORDER: tuple[str, ...] = ("N0", "N1", "A0", "A1-L", "A1-P", "A2-P")

#: The frozen task order.
TASK_ORDER: tuple[str, ...] = (
    "closure2-pipeline-migration",
    "closure2-plugin-migration",
    "closure2-state-machine-refactor",
)

#: Shared resource contract, restated so a change in ``protocol.BUDGET`` fails a test instead of
#: silently re-defining what the pilot runs. ``image`` is the resolved sandbox image; the frozen
#: budget table calls the same value ``sandbox_image``.
FROZEN_RESOURCE_CONTRACT: dict[str, str] = {
    "image": "atlas-uat-python:3.12",
    "cpu": "1",
    "memory": "1Gi",
    "network": "none",
    "workspace_root": "/workspace/project",
}
FROZEN_MAX_TOKENS_PER_REQUEST = 8000
FROZEN_TEMPERATURE = 0.0

#: Budget enforcement semantics. Recorded per arm; never presented as equivalent.
BUDGET_SEMANTICS = {
    "atlas": "guarded_before_send",
    "native": "post_hoc_after_run",
}

#: Proposed, **not approved**, live pilot selection (docs/closure-2b-pilot-runner-design.md §6).
PROPOSED_PILOT_CELLS: tuple[tuple[str, str], ...] = (
    ("closure2-pipeline-migration", "N0"),
    ("closure2-pipeline-migration", "N1"),
    ("closure2-pipeline-migration", "A0"),
    ("closure2-pipeline-migration", "A1-L"),
    ("closure2-pipeline-migration", "A1-P"),
    ("closure2-pipeline-migration", "A2-P"),
    ("closure2-plugin-migration", "A1-P"),
    ("closure2-state-machine-refactor", "A2-P"),
)
PILOT_REPETITIONS = 1


class PilotContractError(RuntimeError):
    """A frozen identity or a runner contract does not match. Never downgraded, never retried."""


class LiveExecutionBlocked(RuntimeError):
    """A live run was requested without the approval or the explicit intent it requires."""


# --------------------------------------------------------------------------------------- the arms


@dataclass(frozen=True)
class ArmSpec:
    """One frozen arm, read from ``protocol.ARMS`` rather than re-typed here."""

    arm_id: str
    engine: str
    runner: str
    topology: str
    config: str | None
    prompt_layout: str
    requested_route: str

    @property
    def budget_semantics(self) -> str:
        return BUDGET_SEMANTICS[self.runner]

    @property
    def is_atlas(self) -> bool:
        return self.runner == "atlas"


def _arm_spec(arm_id: str) -> ArmSpec:
    entry = ARMS[arm_id]
    return ArmSpec(
        arm_id=arm_id,
        engine=str(entry["engine"]),
        runner=str(entry["runner"]),
        topology=str(entry["topology"]),
        config=None if entry["config"] is None else str(entry["config"]),
        prompt_layout=str(entry["prompt_layout"]),
        requested_route=str(entry["route"]),
    )


ARM_SPECS: dict[str, ArmSpec] = {arm: _arm_spec(arm) for arm in ARMS}


@dataclass(frozen=True)
class Cell:
    """One (task, arm) pair. Building the task and the settings is deterministic."""

    task_id: str
    arm_id: str

    @property
    def spec(self) -> ArmSpec:
        try:
            return ARM_SPECS[self.arm_id]
        except KeyError:
            raise PilotContractError(
                f"unknown arm_id {self.arm_id!r}: not in the frozen six-arm matrix"
            ) from None

    @property
    def cell_id(self) -> str:
        return f"{self.task_id}::{self.arm_id}"

    def source_task(self) -> Task:
        for task in TASKS:
            if task.task_id == self.task_id:
                return task
        raise PilotContractError(f"unknown task_id {self.task_id!r}: not in the frozen catalog")

    def benchmark_task(self) -> BenchmarkTask:
        return build_task(self.source_task(), topology=self.spec.topology)

    def config_path(self) -> Path:
        return resolve_repo_path(self.spec.config or NATIVE_CONFIG)

    def config_relative(self) -> str:
        return self.spec.config or NATIVE_CONFIG


def frozen_budget(topology: str) -> dict[str, Any]:
    """The frozen resource table for one topology, with the invariants restated as assertions."""

    if topology == "single":
        block = BUDGET["single"]
        expected = {
            "max_turns": 36,
            "token_budget": 260_000,
            "max_tool_calls": 90,
            "timeout_seconds": 1800,
        }
        kind = "single"
    elif topology == "multi":
        block = BUDGET["multi"]
        expected = {
            "max_turns_per_worker": 12,
            "workers": 3,
            "token_budget": 260_000,
            "max_tool_calls": 90,
            "timeout_seconds": 1800,
        }
        kind = "multi"
    else:
        raise PilotContractError(f"unknown topology {topology!r}")
    for key, value in expected.items():
        if int(block[key]) != value:
            raise PilotContractError(
                f"frozen budget drifted: {kind}.{key}={block[key]!r}, registered {value}"
            )
    return {**{key: block[key] for key in expected}, "topology": kind, "shared": BUDGET["shared"]}


def effective_model() -> str:
    """The model every arm must actually use.

    Atlas resolves ``settings.model_override or config.provider.model``; the native engine resolves
    ``settings.model_override or "deepseek-chat"`` and never reads the YAML. Passing the override
    explicitly is what makes "both arms used the same model" a fact instead of an inference.
    """

    return _config_model(LEGACY_CONTEXT_CONFIG)


def _config_model(relative: str) -> str:
    import yaml

    path = resolve_repo_path(relative)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = payload.get("provider", {}).get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model:
        raise PilotContractError(f"{relative} does not declare provider.model")
    return model


def settings_for(cell: Cell, *, runs_root: Path, model: str | None = None) -> RunnerSettings:
    """Build the frozen ``RunnerSettings`` for one cell.

    The defaults of ``RunnerSettings`` (14 turns, 120k tokens, 40 tools, 900 s, 4096 tokens) are all
    *different* from the frozen contract, so every value that matters is passed explicitly. That is
    the whole reason this function exists instead of ``RunnerSettings(...)`` at the call site.
    """

    from experiments.evaluation.runner import (
        CONTEXT_PLANE_TASK_SCOPED,
        CONTEXT_PLANE_UNSPECIFIED,
        RunnerSettings,
    )

    budget = frozen_budget(cell.spec.topology)
    per_worker_turns = int(
        budget["max_turns"] if cell.spec.topology == "single" else budget["max_turns_per_worker"]
    )
    atlas = cell.spec.is_atlas
    return RunnerSettings(
        benchmark_id=BENCHMARK_ID,
        dataset_version=DATASET_VERSION,
        scorer_version="oracle-1",
        config_path=cell.config_path(),
        runs_root=runs_root,
        timeout_seconds=int(budget["timeout_seconds"]),
        temperature=FROZEN_TEMPERATURE,
        max_turns=per_worker_turns,
        max_tokens=FROZEN_MAX_TOKENS_PER_REQUEST,
        token_budget=int(budget["token_budget"]),
        # The fairness difference is recorded, not removed: Atlas admits requests against a shared
        # run budget before sending, the native arm only gets a post-hoc verdict.
        budget_mode="guarded" if atlas else "legacy",
        max_tool_calls=int(budget["max_tool_calls"]),
        # Inert for the native engine; ``False`` so the settings never claim an Atlas mechanism the
        # engine does not have.
        progressive_tools=atlas,
        model_override=model or effective_model(),
        handoff_mode="legacy",
        compaction_mode="off",
        tool_error_mode="corrected",
        tool_result_mode="off",
        worker_contract_mode="legacy",
        ownership_mode="off",
        execution_integrity_mode="off",
        verification_mode="off",
        # One isolated sandboxed oracle for every arm (Stage R1-2H).
        oracle_mode="sandbox",
        delegation_route_mode=cell.spec.requested_route,
        delegation_policy_version=ROUTING_POLICY_VERSION,
        context_plane_expectation=(
            CONTEXT_PLANE_TASK_SCOPED if atlas else CONTEXT_PLANE_UNSPECIFIED
        ),
        prompt_layout=cell.spec.prompt_layout,
        request_evidence=atlas,
        capture_evidence=True,
    )


# --------------------------------------------------------------------------- frozen contract check


def _sandbox_of(relative: str) -> dict[str, Any]:
    from atlasos.config import load_config

    config = load_config(resolve_repo_path(relative))
    return {
        "image": config.sandbox.image,
        "cpu": config.sandbox.cpu,
        "memory": config.sandbox.memory,
        "network": config.sandbox.network,
        "workspace_root": config.sandbox.workspace_root,
        "provider": config.sandbox.provider,
        "model": config.provider.model,
    }


def check_frozen_contract() -> list[str]:
    """Every identity the pilot depends on, re-derived. An empty list means "safe to plan".

    Nothing here is a formality: each item has been wrong at least once in this project's history
    (a checkout-dependent digest, a file-name-only arm digest, an approval that did not name the
    frozen definition, a runner default that disagreed with the registered budget).
    """

    problems: list[str] = []
    if preregistration_digest() != C2A_R1_PREREGISTRATION_DIGEST:
        problems.append(
            "preregistration digest is "
            f"{preregistration_digest()}, frozen {C2A_R1_PREREGISTRATION_DIGEST}"
        )
    from experiments.evaluation.stage_r0_routing import routing_policy

    if routing_policy(ROUTING_POLICY_VERSION).digest() != ROUTING_POLICY_DIGEST:
        problems.append("router policy digest does not match the frozen value")
    if tuple(ARMS) != ARM_ORDER:
        problems.append(f"arm matrix is {tuple(ARMS)}, frozen {ARM_ORDER}")
    if tuple(task.task_id for task in TASKS) != TASK_ORDER:
        problems.append(f"task order is {[t.task_id for t in TASKS]}, frozen {list(TASK_ORDER)}")
    for task in TASKS:
        live = task_digests(task)
        for key, expected in C2A_FROZEN_TASK_DIGESTS[task.task_id].items():
            if live.get(key) != expected:
                problems.append(f"{task.task_id}:{key} differs from the C2-A table")
    for topology in ("single", "multi"):
        try:
            frozen_budget(topology)
        except PilotContractError as error:
            problems.append(str(error))
    shared = BUDGET["shared"]
    if shared["max_tokens_per_request"] != FROZEN_MAX_TOKENS_PER_REQUEST:
        problems.append("frozen max_tokens_per_request drifted")
    if float(shared["temperature"]) != FROZEN_TEMPERATURE:
        problems.append("frozen temperature drifted")
    if str(shared["sandbox_image"]) != FROZEN_RESOURCE_CONTRACT["image"]:
        problems.append(f"frozen sandbox_image={shared['sandbox_image']!r}")
    for key in ("cpu", "memory", "network", "workspace_root"):
        if str(shared[key]) != FROZEN_RESOURCE_CONTRACT[key]:
            problems.append(
                f"frozen shared.{key}={shared[key]!r}, registered {FROZEN_RESOURCE_CONTRACT[key]}"
            )

    # The two configs an arm can load must be the frozen ones and must differ only in the layout.
    ablation = layout_ablation_delta()
    if ablation["only_layout_differs"] is not True:
        problems.append(
            f"A1-L/A1-P configs differ in {ablation['differing_keys']}, not only prompt_layout"
        )
    for relative in (LEGACY_CONTEXT_CONFIG, SHARED_PREFIX_CONFIG, NATIVE_CONFIG):
        try:
            sandbox = _sandbox_of(relative)
        except Exception as error:  # noqa: BLE001 - any load failure is a contract failure
            problems.append(f"{relative} could not be loaded: {error}")
            continue
        for key, expected in FROZEN_RESOURCE_CONTRACT.items():
            if str(sandbox[key]) != str(expected):
                problems.append(f"{relative}: sandbox.{key}={sandbox[key]!r}, frozen {expected}")
        if sandbox["model"] != _config_model(LEGACY_CONTEXT_CONFIG):
            problems.append(
                f"{relative}: provider.model={sandbox['model']!r} differs from the Atlas config"
            )
    digests = {arm: arm_config_digest(arm) for arm in ARM_ORDER}
    if len(set(digests.values())) != len(digests):
        problems.append("two arms share one config digest")
    return problems


def require_frozen_contract() -> None:
    problems = check_frozen_contract()
    if problems:
        raise PilotContractError("frozen contract violated: " + "; ".join(problems))


# ------------------------------------------------------------------------------------- the cells


def plan_cells() -> tuple[Cell, ...]:
    """The 18 planned cells: three frozen tasks x six frozen arms. A plan, not 18 executions."""

    return tuple(Cell(task_id, arm_id) for task_id in TASK_ORDER for arm_id in ARM_ORDER)


def pilot_cells() -> tuple[Cell, ...]:
    """The proposed, unapproved live pilot selection (§9 of the stage brief)."""

    return tuple(Cell(task_id, arm_id) for task_id, arm_id in PROPOSED_PILOT_CELLS)


def route_expectation(cell: Cell) -> dict[str, Any]:
    """What the frozen router is expected to decide for this cell — computed, not remembered.

    Only ``A2-P`` runs the router. Every other arm records ``not_applicable``: a route expectation
    that no code will produce would be an invented result.
    """

    if cell.spec.requested_route != "selective":
        return {
            "applicable": False,
            "reason": f"arm route is {cell.spec.requested_route!r}; the router does not run",
            "route": None,
            "reason_codes": [],
            "blocked_gates": [],
            "executed_worker_count": len(cell.benchmark_task().workers),
            "executed_topology": cell.spec.topology,
        }
    audit = router_audit(cell.source_task())
    direct = audit["route"] == "direct"
    return {
        "applicable": True,
        "policy_version": audit["policy_version"],
        "policy_digest": audit["policy_digest"],
        "route": audit["route"],
        "reason_codes": list(audit["reason_codes"]),
        "blocked_gates": list(audit["blocked_gates"]),
        "executed_worker_count": 1 if direct else len(cell.benchmark_task().workers),
        "executed_topology": "single_worker_direct" if direct else cell.spec.topology,
    }


def cell_descriptor(cell: Cell, *, runs_root: Path, model: str | None = None) -> dict[str, Any]:
    """Everything one cell's identity depends on, with no host path and no execution."""

    settings = settings_for(cell, runs_root=runs_root, model=model)
    task = cell.benchmark_task()
    expectation = route_expectation(cell)
    budget = frozen_budget(cell.spec.topology)
    return {
        "cell_id": cell.cell_id,
        "benchmark_id": BENCHMARK_ID,
        "task_id": cell.task_id,
        "arm_id": cell.arm_id,
        "engine": cell.spec.engine,
        "runner": cell.spec.runner,
        "topology": cell.spec.topology,
        "prompt_layout": cell.spec.prompt_layout,
        "requested_route": cell.spec.requested_route,
        "resolved_route_expectation": expectation["route"],
        "route_expectation": expectation,
        "candidate_dag": [
            {"task_id": step.task_id, "depends_on": list(step.depends_on)} for step in task.workers
        ],
        "candidate_worker_count": len(task.workers),
        "model": settings.model_override,
        "config": cell.config_relative(),
        "config_semantic_digest": config_semantic_digest(cell.config_relative()),
        "arm_config_digest": arm_config_digest(cell.arm_id),
        "effective_public_task_digest": effective_public_task_digest(task),
        "settings_digest": settings.digest(),
        "preregistration_digest": preregistration_digest(),
        "budget": {
            "topology": budget["topology"],
            "max_turns": budget.get("max_turns"),
            "max_turns_per_worker": budget.get("max_turns_per_worker"),
            "workers": budget.get("workers"),
            "token_budget": budget["token_budget"],
            "max_tool_calls": budget["max_tool_calls"],
            "timeout_seconds": budget["timeout_seconds"],
            "max_tokens_per_request": FROZEN_MAX_TOKENS_PER_REQUEST,
            "temperature": FROZEN_TEMPERATURE,
            "sandbox": dict(FROZEN_RESOURCE_CONTRACT),
        },
        "budget_semantics": cell.spec.budget_semantics,
        "oracle_mode": settings.oracle_mode,
        "handoff_mode": settings.handoff_mode,
        "tool_error_mode": settings.tool_error_mode,
    }


# -------------------------------------------------------------------------------------- dry run


def build_dry_run_manifest(*, runs_root: Path | None = None) -> dict[str, Any]:
    """The plan for all 18 cells plus the identity it was planned against.

    This function must stay side-effect free: no sandbox provider, no provider adapter, no API key
    and no network. It reads YAML and frozen Python data only.
    """

    root = (runs_root or DEFAULT_RUNS_ROOT).resolve()
    gate = approval_gate()
    problems = check_frozen_contract()
    cells = [cell_descriptor(cell, runs_root=root) for cell in plan_cells()]
    for descriptor in cells:
        descriptor["approval_status"] = gate["status"]
        descriptor["execution_allowed"] = False
    return {
        "mode": "DRY_RUN",
        "stage": "closure-2b-0",
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": DATASET_VERSION,
        "protocol_revision": PROTOCOL_REVISION,
        "preregistration_digest": preregistration_digest(),
        "frozen_preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
        "router_policy": {
            "version": ROUTING_POLICY_VERSION,
            "digest": ROUTING_POLICY_DIGEST,
        },
        "generated_at": _now(),
        "model_calls": 0,
        "sandbox_runs": 0,
        "contains_live_outcomes": False,
        "route_expectation_is_prediction": True,
        "approval": {
            "status": gate["status"],
            "execution_ready": gate["execution_ready"],
            "problems": gate["problems"],
        },
        "execution_ready": gate["execution_ready"],
        "contract_problems": problems,
        "contract_ok": not problems,
        "cell_count": len(cells),
        "cells": cells,
        "proposed_live_pilot": {
            "approved": False,
            "repetitions": PILOT_REPETITIONS,
            "cells": [cell.cell_id for cell in pilot_cells()],
            "note": (
                "proposal only: the owner must approve this selection and its cost ceiling before "
                "any live cell runs"
            ),
        },
    }


def write_dry_run(path: Path, *, runs_root: Path | None = None) -> dict[str, Any]:
    manifest = build_dry_run_manifest(runs_root=runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return manifest


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------------- live execution

#: ``(cell, settings, task, runs_root, repetition_id) -> EvalRunResult``
Executor = Callable[..., Awaitable[Any]]


async def _default_executor(
    cell: Cell,
    *,
    settings: Any,
    task: BenchmarkTask,
    runs_root: Path,
    repetition_id: int,
) -> Any:
    """Dispatch to the engine the arm names. Never falls back to the other one."""

    if cell.spec.runner == "native":
        from experiments.evaluation.closure1b.native_baseline import run_native_sample

        return await run_native_sample(
            task,
            settings=settings,
            repetition_id=repetition_id,
            runs_root=runs_root,
            config_path=settings.config_path,
        )
    if cell.spec.runner == "atlas":
        from experiments.evaluation.runner import run_sample

        return await run_sample(
            task,
            settings=settings,
            repetition_id=repetition_id,
            config_path=settings.config_path,
        )
    raise PilotContractError(f"arm {cell.arm_id} names unknown runner {cell.spec.runner!r}")


async def run_live_pilot(
    cells: tuple[Cell, ...],
    *,
    runs_root: Path,
    repetition_id: int = 0,
    executor: Executor | None = None,
    live_intent: bool = False,
) -> dict[str, Any]:
    """Execute the given cells for real.

    Order is the contract:

    1. ``live_intent`` must be explicit — no environment variable, no default, no inference.
    2. ``require_execution_approval()`` — raises before *any* executor exists, so before a sandbox
       is provisioned or a request is sent.
    3. The frozen contract is re-checked; a drift aborts the whole pilot instead of running part.
    4. Only then does each cell reach its engine.

    A cell that raises is recorded as ``EXECUTION_ERROR`` and the pilot continues: one broken cell
    is never rewritten as a success and never silently retried on another engine.
    """

    if not live_intent:
        raise LiveExecutionBlocked(
            "LIVE_INTENT_NOT_CONFIRMED: pass --live --confirm-live; no environment variable can "
            "enable a live run"
        )
    gate = require_execution_approval()
    require_frozen_contract()

    runner = executor or _default_executor
    model = effective_model()
    records: list[dict[str, Any]] = []
    for cell in cells:
        settings = settings_for(cell, runs_root=runs_root, model=model)
        task = cell.benchmark_task()
        try:
            result = await runner(
                cell,
                settings=settings,
                task=task,
                runs_root=runs_root,
                repetition_id=repetition_id,
            )
        except Exception as error:  # noqa: BLE001 - a failed cell is data, not a crash
            records.append(_execution_error_record(cell, error))
            continue
        records.append(normalize_result(cell, result, settings=settings))
    return {
        "mode": "LIVE",
        "stage": "closure-2b-0",
        "benchmark_id": BENCHMARK_ID,
        "protocol_revision": PROTOCOL_REVISION,
        "preregistration_digest": preregistration_digest(),
        "router_policy": {"version": ROUTING_POLICY_VERSION, "digest": ROUTING_POLICY_DIGEST},
        "generated_at": _now(),
        "repetition_id": repetition_id,
        "approval_status": gate["status"],
        "approved_digest": gate["approved_digest"],
        "cell_count": len(records),
        # Unknown stays unknown: a pilot with one unmeasured cell does not have a total.
        "model_calls": _total_or_none([record.get("model_calls") for record in records]),
        "sandbox_runs": sum(1 for record in records if record.get("sandbox_created") is True),
        "cells": records,
    }


def _total_or_none(values: list[Any]) -> int | None:
    """Sum only when every term is known. One unknown term makes the total unknown."""

    if not values or any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _execution_error_record(cell: Cell, error: BaseException) -> dict[str, Any]:
    return {
        "cell_id": cell.cell_id,
        "task_id": cell.task_id,
        "arm_id": cell.arm_id,
        "status": "EXECUTION_ERROR",
        "failure_class": "HARNESS_ERROR",
        "failure_reason": f"{type(error).__name__}: {error}",
        "oracle_passed": None,
        "agent_success": None,
        "task_success": None,
        "model_calls": None,
        "provider_prompt_tokens": None,
        "provider_completion_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "estimated_cost_usd": None,
        "budget_exceeded": None,
        "budget_rejected_requests": None,
    }


# ----------------------------------------------------------------------------------- normalising


def _attestation(metrics: Any) -> dict[str, Any]:
    extra = getattr(metrics, "extra", {}) or {}
    budget = extra.get("budget")
    routing = extra.get("routing")
    evidence = extra.get("request_evidence")
    cache_complete = extra.get("cache_metrics_complete")
    if isinstance(evidence, dict) and "requests_observed" in evidence:
        model_calls: Any = evidence["requests_observed"]
        model_calls_source = "request_evidence.requests_observed"
    elif getattr(metrics, "model_turns", None):
        model_calls = metrics.model_turns
        model_calls_source = "metrics.model_turns"
    else:
        model_calls = None
        model_calls_source = "not_exposed_by_runner"
    return {
        "budget": budget if isinstance(budget, dict) else None,
        "routing": routing if isinstance(routing, dict) else None,
        "cache_metrics_complete": cache_complete if isinstance(cache_complete, bool) else None,
        "cache_split_status": extra.get("cache_split_status"),
        "model_calls": model_calls,
        "model_calls_source": model_calls_source,
    }


def normalize_result(cell: Cell, result: Any, *, settings: Any | None = None) -> dict[str, Any]:
    """Map one ``EvalRunResult`` onto the flat evidence schema.

    Rules that this function exists to enforce:

    * a missing number stays ``None``; it is never written as ``0``;
    * an unknown cache split stays unknown, it is not a 0 % hit rate;
    * ``oracle_passed`` (the deliverable) is separate from ``agent_success`` (the agent's own claim)
      and from ``task_success`` (both, under the run's failure class);
    * "the budget was exceeded" is separate from "the oracle failed";
    * ``A2-P`` on a refused DAG runs one worker, and the record says so.
    """

    metrics = result.metrics
    attestation = _attestation(metrics)
    environment = getattr(result, "environment", {}) or {}
    native_budget = environment.get("budget_check")
    routing = attestation["routing"] or {}
    decision = routing.get("decision")
    resolved_route = decision.get("route") if isinstance(decision, dict) else None
    executed_workers = routing.get("executed_worker_count")
    if executed_workers is None:
        selection = getattr(metrics, "worker_selection", None) or []
        executed_workers = len(selection) or None
    direct_downgrade = bool(routing) and int(routing.get("executed_worker_count") or 0) == 1
    # Atlas counts refusals before send; native has no guard at all, so "rejected requests" is not
    # applicable there and must stay ``None`` rather than be reported as an observed zero.
    budget_rejected = None
    guarded = attestation["budget"]
    if isinstance(guarded, dict):
        budget_rejected = guarded.get("admission_rejections")
    budget_exceeded = None
    if isinstance(guarded, dict):
        budget_exceeded = bool(budget_rejected)
    elif isinstance(native_budget, dict):
        budget_exceeded = bool(native_budget.get("exceeded"))
    # The runner's own verdict wins: a run can cross the threshold without a pre-send refusal.
    if result.failure_class == "BUDGET_EXCEEDED":
        budget_exceeded = True

    oracle_passed = bool(result.oracle.passed)
    return {
        "cell_id": cell.cell_id,
        "status": "COMPLETED",
        "benchmark_id": BENCHMARK_ID,
        "protocol_revision": PROTOCOL_REVISION,
        "preregistration_digest": preregistration_digest(),
        "repair_revision": "closure-2b-0",
        "task_id": cell.task_id,
        "arm_id": cell.arm_id,
        "repetition_id": int(result.identity.repetition_id),
        "run_id": result.identity.run_id,
        "run_dir": result.run_dir,
        "task_digest": task_digests(cell.source_task())["seed_digest"],
        "effective_public_task_digest": effective_public_task_digest(cell.benchmark_task()),
        "arm_config_digest": arm_config_digest(cell.arm_id),
        "settings_digest": settings.digest() if settings is not None else None,
        "git_commit": result.identity.git_commit,
        "git_dirty": result.identity.git_dirty,
        "engine": cell.spec.engine,
        "topology": cell.spec.topology,
        "resolved_route": resolved_route,
        "executed_worker_count": executed_workers,
        "multi_worker_execution": (
            None
            if executed_workers is None
            else bool(executed_workers and int(executed_workers) > 1)
        ),
        "single_worker_downgrade": direct_downgrade,
        "model": result.identity.model,
        "sandbox_image": environment.get("sandbox_image"),
        "budget_semantics": cell.spec.budget_semantics,
        "agent_success": bool(result.agent_success),
        "oracle_passed": oracle_passed,
        "task_success": bool(result.success),
        "failure_class": result.failure_class,
        "failure_reason": result.failure_reason,
        "provider_prompt_tokens": metrics.prompt_tokens,
        "provider_completion_tokens": metrics.completion_tokens,
        "cache_hit_tokens": metrics.cache_hit_tokens,
        "cache_miss_tokens": metrics.cache_miss_tokens,
        "cache_usage_complete": attestation["cache_metrics_complete"],
        "cache_split_status": attestation["cache_split_status"],
        "provider_usage_complete": attestation["cache_metrics_complete"],
        "provider_usage_source": (
            "metrics.extra.cache_metrics_complete"
            if attestation["cache_metrics_complete"] is not None
            else "not_exposed_by_runner"
        ),
        "estimated_cost_usd": metrics.estimated_cost_usd,
        "model_calls": attestation["model_calls"],
        "model_calls_source": attestation["model_calls_source"],
        "tool_calls": metrics.tool_calls,
        "budget_exceeded": budget_exceeded,
        "budget_rejected_requests": budget_rejected,
        "budget_record": attestation["budget"] or native_budget,
        "agent_latency_ms": environment.get("agent_latency_ms"),
        "end_to_end_latency_ms": metrics.latency_ms,
        "parallel_overlap_evidence": None,
        "parallel_overlap_source": (
            "not_exposed_by_runner: worker lifecycle timings live in run_dir/stage_e_evidence.json "
            "and are computed in Closure-2B-1"
        ),
        "deliverable_passed_but_budget_failed": bool(oracle_passed and budget_exceeded),
        "workspace_isolated": environment.get("workspace_isolation"),
        "sandbox_created": metrics.sandbox_created,
        "sandbox_destroyed": metrics.sandbox_destroyed,
        "oracle_environment_failure": bool(result.oracle.environment_failure),
        "oracle_failure_kind": result.oracle.failure_kind,
        "raw_result_path": str(Path(result.run_dir) / "result.json"),
        "raw_evidence_preserved": True,
    }


# ------------------------------------------------------------------------------------------- cli


def _parse_cells(raw: str) -> tuple[Cell, ...]:
    cells: list[Cell] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        task_id, separator, arm_id = token.partition("::")
        if not separator or not task_id or not arm_id:
            raise PilotContractError(f"cell {token!r} is not <task_id>::<arm_id>")
        if task_id not in TASK_ORDER:
            raise PilotContractError(f"unknown task_id {task_id!r}")
        if arm_id not in ARM_SPECS:
            raise PilotContractError(f"unknown arm_id {arm_id!r}")
        cells.append(Cell(task_id, arm_id))
    if not cells:
        raise PilotContractError("no cells requested")
    return tuple(cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="closure-2b pilot runner: dry-run planning and gated live execution"
    )
    parser.add_argument("--dry-run", action="store_true", help="plan only (the default)")
    parser.add_argument("--out", default=str(DEFAULT_DRY_RUN_OUT))
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    parser.add_argument("--live", action="store_true", help="request a real run")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="second, explicit confirmation required together with --live",
    )
    parser.add_argument("--cells", help="comma separated <task_id>::<arm_id>, default the proposal")
    parser.add_argument("--repetition", type=int, default=0)
    args = parser.parse_args(argv)

    runs_root = Path(args.runs_root)

    if not args.live:
        manifest = write_dry_run(Path(args.out), runs_root=runs_root)
        print(
            json.dumps(
                {
                    "mode": manifest["mode"],
                    "cells": manifest["cell_count"],
                    "contract_ok": manifest["contract_ok"],
                    "contract_problems": manifest["contract_problems"],
                    "execution_ready": manifest["execution_ready"],
                    "approval": manifest["approval"]["status"],
                    "model_calls": manifest["model_calls"],
                    "sandbox_runs": manifest["sandbox_runs"],
                    "out": str(Path(args.out)),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if manifest["contract_ok"] else 1

    if not args.confirm_live:
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "reason": "LIVE_INTENT_NOT_CONFIRMED",
                    "detail": "--live requires --confirm-live; no environment variable enables it",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    try:
        cells = _parse_cells(args.cells) if args.cells else pilot_cells()
        summary = asyncio.run(
            run_live_pilot(
                cells,
                runs_root=runs_root,
                repetition_id=args.repetition,
                live_intent=True,
            )
        )
    except (LiveExecutionBlocked, PilotContractError, PermissionError) as error:
        print(
            json.dumps(
                {"status": "REFUSED", "reason": str(error)},
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    out = Path(args.out).with_name("pilot-live-summary.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"mode": "LIVE", "cells": summary["cell_count"], "out": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
