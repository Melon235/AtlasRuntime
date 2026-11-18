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
    Real execution, behind **two** independent authorizations: the benchmark definition
    (``require_execution_approval()``) and this specific pilot's scope
    (``require_pilot_authorization()``). The requested cells and repetition must equal the
    authorized ones exactly, so an approval of "the benchmark" can never be stretched into "any
    cells I like". Without an explicit ``live_intent=True`` it refuses before that too.

The default is always the dry run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
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
from experiments.evaluation.closure2.pilot_ledger import (
    ExecutionLease,
    PilotLedger,
    PilotLedgerError,
    default_ledger_path,
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
from experiments.evaluation.models import (
    BenchmarkTask,
    canonical_digest,
    effective_public_task_digest,
)

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

#: B-0 execution-adapter fingerprint of the native engine's own config. The native arm never reads
#: the Atlas YAML, so its config is an execution dependency in its own right: pricing, tools,
#: trajectory and provider blocks all change what the arm measures or what it costs. This is **not**
#: a Closure-2A preregistration identity — it constrains the adapter, and changing it means the
#: adapter needs a new review, not that the benchmark moved.
FROZEN_NATIVE_CONFIG_DIGEST = "6cb2d1d91badaa235efa4427e5a53bdfc17610931e7b4c13e524406c2a863d14"

#: Bumped whenever the live-summary / dry-run schema changes, so an old artifact cannot be read with
#: new semantics.
EVIDENCE_SCHEMA_VERSION = "closure-2b-0-r1"


class PilotContractError(RuntimeError):
    """A frozen identity or a runner contract does not match. Never downgraded, never retried."""


class LiveExecutionBlocked(RuntimeError):
    """A live run was requested without the approval or the explicit intent it requires."""


class PilotNotAuthorized(LiveExecutionBlocked):
    """The requested execution scope is not the one the owner approved."""


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


# ---------------------------------------------------------------- the authorized execution scope


@dataclass(frozen=True)
class PilotPlan:
    """The execution scope one pilot approval covers. Immutable, digestible, approval-independent.

    Two different questions, two different objects:

    * this plan answers *which cells, which repetitions, how many executions, under which cost
      ceiling*;
    * ``PILOT_APPROVAL`` answers *whether the owner approved this plan digest*.

    Moving the approval from pending to approved therefore cannot change the plan identity, and
    changing a cell, a repetition or the ceiling cannot silently keep the old approval valid.
    """

    plan_id: str
    benchmark_id: str
    preregistration_digest: str
    runner_revision: str
    cells: tuple[str, ...]
    repetitions: tuple[int, ...]
    max_cell_executions: int
    estimated_cost_ceiling_usd: float | None

    def payload(self) -> dict[str, Any]:
        """Every field that defines the scope. No timestamp, no path, no random id."""

        return {
            "plan_id": self.plan_id,
            "benchmark_id": self.benchmark_id,
            "preregistration_digest": self.preregistration_digest,
            "runner_revision": self.runner_revision,
            "cells": list(self.cells),
            "repetitions": list(self.repetitions),
            "max_cell_executions": self.max_cell_executions,
            "estimated_cost_ceiling_usd": self.estimated_cost_ceiling_usd,
        }

    def digest(self) -> str:
        return canonical_digest(self.payload())

    @property
    def planned_executions(self) -> int:
        return len(self.cells) * len(self.repetitions)


#: The one pilot this stage proposes. Eight cells, one repetition, and the owner-approved estimated
#: cost ceiling of USD 2.00. The ceiling is part of the plan digest, so approving that digest
#: approves the number; ``cost_policy()`` still calls it advisory, never a hard stop.
PILOT_PLAN = PilotPlan(
    plan_id="closure2-pilot-1",
    benchmark_id=BENCHMARK_ID,
    preregistration_digest=C2A_R1_PREREGISTRATION_DIGEST,
    runner_revision="closure-2b-0-r1",
    cells=tuple(f"{task_id}::{arm_id}" for task_id, arm_id in PROPOSED_PILOT_CELLS),
    repetitions=tuple(range(PILOT_REPETITIONS)),
    max_cell_executions=len(PROPOSED_PILOT_CELLS),
    estimated_cost_ceiling_usd=2.00,
)

#: Mutable approval record for the *scope*, separate from ``protocol.APPROVAL_RECORD`` which
#: approves the *definition*. Deliberately not a parameter of ``run_live_pilot``: a caller must not
#: be able to hand in its own approval.
#:
#: The one authorized execution has happened (attempt ``f862981c…``, 2026-11-18), so the status is
#: no longer ``APPROVED``. The record keeps who approved what, against which digest, from which
#: commit — and adds the attempt that spent it. This matters beyond bookkeeping: the claim lives in
#: per-user state (``%LOCALAPPDATA%``), so on a *different* machine the ledger would be empty while
#: this record, if still ``APPROVED``, would authorize a second paid run. A consumed status refuses
#: before any claim is taken. Another execution needs a new owner authorization, not a status edit.
PILOT_APPROVAL: dict[str, Any] = {
    "status": "CONSUMED_BY_EXECUTION",
    "approved_plan_digest": "0f61f84ad195fbfb2e27f4c80f7906530066f7128da84595b0293dfd0b2e32bb",
    "approved_by": "repository_owner",
    # The scope commit: it already exists, so approving it is not a self-reference.
    "approval_commit": "de2acbc4fad395e29364aa6900e388afb492f962",
    "approved_at": "2026-11-18T08:43:30+00:00",
    "consumed_at": "2026-11-18T08:44:11.500026+00:00",
    "consumed_attempt_id": "f862981c20a24fdf881884b9fbd891cc",
}


PILOT_PLAN_ID = "closure2-pilot-1"
ROUTING_PAIR_PLAN_ID = "closure2-routing-pair-1"

#: Closure-2C-1: one task, two arms, one variable. ``A1-P`` runs with ``route=off`` and executes
#: the two-worker DAG; ``A2-P`` runs the frozen router and is expected to resolve ``direct``, i.e.
#: one worker. Task, fixture, oracle, model, layout, budget and sandbox are shared by construction:
#: only the recorded routing policy may change the execution path. The shared prefix does **not**
#: make rendered prompts byte-identical — direct hands one worker the union assignment while multi
#: gives each worker its own region, so prompts differ even though the pre-route digest is equal.
ROUTING_PAIR_CELLS: tuple[str, ...] = (
    "closure2-state-machine-refactor::A1-P",
    "closure2-state-machine-refactor::A2-P",
)

#: The owner authorized a USD 0.10 *estimated* cost warning for this run. It is the advisory
#: between-cell threshold, not a provider-side cap: a cell already started can overspend it, and the
#: check only stops the next dispatch. The ceiling is part of the digest, so approving the digest is
#: what approved the number.
ROUTING_PAIR_PLAN = PilotPlan(
    plan_id=ROUTING_PAIR_PLAN_ID,
    benchmark_id=BENCHMARK_ID,
    preregistration_digest=C2A_R1_PREREGISTRATION_DIGEST,
    runner_revision="closure-2c1-r1",
    cells=ROUTING_PAIR_CELLS,
    repetitions=(0,),
    max_cell_executions=2,
    estimated_cost_ceiling_usd=0.10,
)

#: Owner authorization for the one routing execution. It names the scope commit that already
#: exists, so approving it is not a self-reference, and it names the plan digest that carries the
#: USD 0.10 estimate — approving either one separately is not possible.
#:
#: The authorized execution happened on 2026-11-18 (attempt ``f51f374a…``), so the status is no
#: longer ``APPROVED``. Who approved what, against which digest, from which commit stays recorded,
#: and the attempt that spent it is added. A further routing run needs a new owner authorization,
#: not a status edit.
ROUTING_PAIR_APPROVAL: dict[str, Any] = {
    "status": "CONSUMED_BY_EXECUTION",
    "approved_plan_digest": "41551523488b6a4ae8e02fa9e195a1728fe061935beac856621c05c2457a6b99",
    "approved_by": "repository_owner",
    "approval_commit": "609da3d921492c9ce06a668e1821427a8848466e",
    "approved_at": "2026-11-18T11:17:54+00:00",
    "consumed_at": "2026-11-18T11:19:06.504706+00:00",
    "consumed_attempt_id": "f51f374ac0274d66837e5df76c67b48e",
}


def pilot_scope(plan_id: str = PILOT_PLAN_ID) -> tuple[PilotPlan, dict[str, Any]]:
    """The fixed (plan, approval) pair a plan id names.

    A closed set of two plans, dispatched by id: no registration hook, no caller-supplied scope,
    no way to reach a third plan from the CLI. The globals are read **at call time** on purpose:
    tests substitute ``PILOT_PLAN`` / ``PILOT_APPROVAL`` per test and must stay effective.
    """

    if plan_id == ROUTING_PAIR_PLAN_ID:
        return ROUTING_PAIR_PLAN, ROUTING_PAIR_APPROVAL
    if plan_id == PILOT_PLAN_ID:
        return PILOT_PLAN, PILOT_APPROVAL
    raise PilotContractError(
        f"unknown plan_id {plan_id!r}: known plans are {PILOT_PLAN_ID!r} and "
        f"{ROUTING_PAIR_PLAN_ID!r}"
    )


def plan_scope_ids() -> tuple[str, ...]:
    """The plan ids the CLI may name. Frozen order, so ``--help`` is deterministic."""

    return (PILOT_PLAN_ID, ROUTING_PAIR_PLAN_ID)


def routing_pair_cells() -> tuple[Cell, ...]:
    """The Closure-2C-1 pair as ``Cell`` objects, in the plan's frozen order."""

    cells: list[Cell] = []
    for cell_id in ROUTING_PAIR_CELLS:
        task_id, _, arm_id = cell_id.partition("::")
        cells.append(Cell(task_id, arm_id))
    return tuple(cells)


def plan_cells_for(plan_id: str = PILOT_PLAN_ID) -> tuple[Cell, ...]:
    """The cells a plan id authorizes. Deliberately not derived from ``plan.cells``.

    ``pilot_cells()`` is the stage's proposal constant and tests substitute ``PILOT_PLAN`` without
    redefining it; deriving the live selection from the (substitutable) plan would silently change
    which cells the CLI asks for.
    """

    if plan_id == ROUTING_PAIR_PLAN_ID:
        return routing_pair_cells()
    if plan_id == PILOT_PLAN_ID:
        return pilot_cells()
    raise PilotContractError(f"unknown plan_id {plan_id!r}")


#: One summary per plan: the pilot keeps the filename its 2026-11-18 artifact already has, and a
#: routing run can never overwrite it even when both are pointed at the same ``--out`` directory.
LIVE_SUMMARY_FILENAMES: dict[str, str] = {
    PILOT_PLAN_ID: "pilot-live-summary.json",
    ROUTING_PAIR_PLAN_ID: "routing-pair-live-summary.json",
}

#: Run roots are plan-scoped for the same reason: two plans never share a results tree.
DEFAULT_RUNS_ROOTS: dict[str, Path] = {
    PILOT_PLAN_ID: DEFAULT_RUNS_ROOT,
    ROUTING_PAIR_PLAN_ID: REPO_ROOT / "experiments" / "runs" / "closure2-routing-pair",
}


def live_summary_path(out: Path, plan_id: str = PILOT_PLAN_ID) -> Path:
    """Where a live summary for ``plan_id`` is written, given the ``--out`` path."""

    try:
        name = LIVE_SUMMARY_FILENAMES[plan_id]
    except KeyError:
        raise PilotContractError(f"unknown plan_id {plan_id!r}") from None
    return out.with_name(name)


def default_runs_root(plan_id: str = PILOT_PLAN_ID) -> Path:
    """The plan-scoped default run root."""

    try:
        return DEFAULT_RUNS_ROOTS[plan_id]
    except KeyError:
        raise PilotContractError(f"unknown plan_id {plan_id!r}") from None


def _finite_number(value: Any) -> float | None:
    """A usable finite number, or ``None``.

    ``float("nan") <= 0`` is false and ``float("inf") <= 0`` is false, so a plain comparison lets
    NaN and infinity through every "is this a sane ceiling" check. Booleans are rejected even though
    ``True`` is an ``int``, because ``True`` as a dollar amount is a bug, not a value.
    """

    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _finite_cost(value: Any) -> float | None:
    """A usable cost: finite and non-negative."""

    number = _finite_number(value)
    if number is None or number < 0:
        return None
    return number


def cost_policy(plan: PilotPlan | None = None) -> dict[str, Any]:
    """What the cost ceiling actually guarantees — which is nothing hard, and that is recorded.

    Neither engine has a pre-send dollar guard: Atlas guards tokens before send, native only after
    the run. A cell that is already executing can therefore overspend before the next dispatch is
    stopped. ``hard_enforcement`` is therefore always ``False`` and the reason code says why.
    """

    target = plan or PILOT_PLAN
    raw_ceiling = target.estimated_cost_ceiling_usd
    if raw_ceiling is None:
        return {
            "valid": False,
            "reason": "COST_CEILING_NOT_APPROVED",
            "ceiling_usd": None,
            "enforcement": "ADVISORY_POST_CELL",
            "hard_enforcement": False,
            "guarantee": "COST_CEILING_NOT_HARD_ENFORCED",
        }
    ceiling = _finite_number(raw_ceiling)
    if ceiling is None:
        return {
            "valid": False,
            "reason": "COST_CEILING_INVALID",
            "ceiling_usd": None,
            "ceiling_raw": repr(raw_ceiling),
            "enforcement": "ADVISORY_POST_CELL",
            "hard_enforcement": False,
            "guarantee": "COST_CEILING_NOT_HARD_ENFORCED",
        }
    if ceiling <= 0:
        return {
            "valid": False,
            "reason": "COST_CEILING_NOT_POSITIVE",
            "ceiling_usd": ceiling,
            "enforcement": "ADVISORY_POST_CELL",
            "hard_enforcement": False,
            "guarantee": "COST_CEILING_NOT_HARD_ENFORCED",
        }
    return {
        "valid": True,
        "reason": "COST_CEILING_APPROVED",
        "ceiling_usd": ceiling,
        "enforcement": "ADVISORY_POST_CELL",
        "hard_enforcement": False,
        "guarantee": "COST_CEILING_NOT_HARD_ENFORCED",
        "scope": (
            "checked between cells from the runner's estimated cost; a cell already executing can "
            "exceed the ceiling before the next dispatch is stopped"
        ),
    }


def pilot_authorization_gate(
    *,
    cells: tuple[Cell, ...],
    repetition_id: int,
    plan: PilotPlan | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Layer B: is *this* execution the one the owner authorized?

    The requested sequence must equal the authorized sequence exactly. A subset, an extra cell, a
    different order or an unapproved repetition is refused; a partial retry would need its own
    authorization record, which this stage does not define.
    """

    target = plan or PILOT_PLAN
    active = PILOT_APPROVAL if record is None else record
    requested = tuple(cell.cell_id for cell in cells)
    digest = target.digest()
    approved_digest = active.get("approved_plan_digest")
    problems: list[str] = []
    if active.get("status") != "APPROVED":
        problems.append(f"pilot approval status is {active.get('status')!r}, not 'APPROVED'")
    if approved_digest != digest:
        problems.append(f"pilot plan digest {digest!r} is not the approved {approved_digest!r}")
    if not active.get("approved_by"):
        problems.append("pilot approval has no approver")
    if not active.get("approval_commit"):
        problems.append("pilot approval has no approval commit")
    if not active.get("approved_at"):
        problems.append("pilot approval has no approval timestamp")
    if target.preregistration_digest != C2A_R1_PREREGISTRATION_DIGEST:
        problems.append("pilot plan is pinned to a different preregistration digest")
    if preregistration_digest() != target.preregistration_digest:
        problems.append(
            "the live preregistration digest differs from the one the pilot plan was pinned to"
        )
    if len(set(requested)) != len(requested):
        problems.append("the requested list contains the same cell twice")
    if requested != target.cells:
        missing = [cell for cell in target.cells if cell not in requested]
        extra = [cell for cell in requested if cell not in target.cells]
        problems.append(
            "requested cells are not the authorized cells: "
            f"missing={missing}, extra={extra}, order_matches={requested == target.cells}"
        )
    if repetition_id not in target.repetitions:
        problems.append(
            f"repetition {repetition_id} is not authorized; approved {list(target.repetitions)}"
        )
    if len(requested) > target.max_cell_executions:
        problems.append(
            f"{len(requested)} executions requested, {target.max_cell_executions} authorized"
        )
    policy = cost_policy(target)
    if not policy["valid"]:
        problems.append(f"cost policy is not usable: {policy['reason']}")
    return {
        "execution_ready": not problems,
        "problems": problems,
        "plan_id": target.plan_id,
        "plan_digest": digest,
        "approved_plan_digest": approved_digest,
        "approved_cells": list(target.cells),
        "requested_cells": list(requested),
        "approved_repetitions": list(target.repetitions),
        "requested_repetition": repetition_id,
        "max_cell_executions": target.max_cell_executions,
        "approval_status": active.get("status"),
        "cost_policy": policy,
    }


def require_pilot_authorization(
    *,
    cells: tuple[Cell, ...],
    repetition_id: int,
    plan: PilotPlan | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Raise unless the requested scope is the approved scope. Called by the live entry point.

    ``plan``/``record`` default to the pilot scope, which keeps every existing caller unchanged; the
    live entry point passes the pair selected by ``pilot_scope``.
    """

    gate = pilot_authorization_gate(
        cells=cells, repetition_id=repetition_id, plan=plan, record=record
    )
    if not gate["execution_ready"]:
        raise PilotNotAuthorized("PILOT_NOT_AUTHORIZED: " + "; ".join(gate["problems"]))
    return gate


def _ledger_factory() -> PilotLedger:
    """The production ledger. One per-user file, shared by every checkout on this machine.

    Deliberately not derived from ``--runs-root``, and deliberately not a CLI option: a caller who
    can point the ledger somewhere else can hand itself a fresh, empty ledger and reuse an approval.
    Tests substitute this factory; the CLI never passes a path.
    """

    return PilotLedger(default_ledger_path())


def _sum_or_none(values: list[Any]) -> float | None:
    """Sum only when every term is known and finite. One bad term makes the total unknown."""

    if not values:
        return None
    numbers = [_finite_cost(value) for value in values]
    if any(number is None for number in numbers):
        return None
    try:
        total = math.fsum(numbers)  # type: ignore[arg-type]
    except OverflowError:
        return None
    return total if math.isfinite(total) else None


def _cost_dispatch_state(records: list[dict[str, Any]], plan: PilotPlan) -> dict[str, Any]:
    """May the next cell be dispatched?

    Fail-closed on every unknown: an unset ceiling, a non-finite ceiling, a cell whose cost is
    missing or invalid, and a sum that overflows. ``spent == ceiling`` stops too — the next cell
    would cross the line by definition.
    """

    raw_ceiling = plan.estimated_cost_ceiling_usd
    if raw_ceiling is None:
        return {
            "can_dispatch": False,
            "reason": "COST_CEILING_NOT_APPROVED",
            "spent_usd": None,
            "ceiling_usd": None,
        }
    ceiling = _finite_number(raw_ceiling)
    if ceiling is None or ceiling <= 0:
        return {
            "can_dispatch": False,
            "reason": "COST_CEILING_INVALID",
            "spent_usd": None,
            "ceiling_usd": None,
        }
    costs: list[float] = []
    for record in records:
        # The record already classified its own cost: unknown and invalid are different problems and
        # must not collapse into one "missing" bucket.
        if record.get("estimated_cost_usd_state") == "INVALID":
            return {
                "can_dispatch": False,
                "reason": "COST_INVALID",
                "spent_usd": None,
                "ceiling_usd": ceiling,
            }
        raw_cost = record.get("estimated_cost_usd")
        if raw_cost is None:
            return {
                "can_dispatch": False,
                "reason": "COST_UNKNOWN",
                "spent_usd": None,
                "ceiling_usd": ceiling,
            }
        value = _finite_cost(raw_cost)
        if value is None:
            return {
                "can_dispatch": False,
                "reason": "COST_INVALID",
                "spent_usd": None,
                "ceiling_usd": ceiling,
            }
        costs.append(value)
    try:
        spent = math.fsum(costs)
    except OverflowError:
        return {
            "can_dispatch": False,
            "reason": "COST_INVALID",
            "spent_usd": None,
            "ceiling_usd": ceiling,
        }
    if not math.isfinite(spent):
        return {
            "can_dispatch": False,
            "reason": "COST_INVALID",
            "spent_usd": None,
            "ceiling_usd": ceiling,
        }
    if spent > ceiling:
        return {
            "can_dispatch": False,
            "reason": "COST_CEILING_EXCEEDED",
            "spent_usd": spent,
            "ceiling_usd": ceiling,
        }
    if spent >= ceiling:
        return {
            "can_dispatch": False,
            "reason": "COST_CEILING_REACHED",
            "spent_usd": spent,
            "ceiling_usd": ceiling,
        }
    return {
        "can_dispatch": True,
        "reason": "WITHIN_CEILING",
        "spent_usd": spent,
        "ceiling_usd": ceiling,
    }


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


def native_config_digest(*, root: Path | None = None) -> str:
    """The native config's semantics, through the same canonicalisation as every other config."""

    return config_semantic_digest(NATIVE_CONFIG, root=root)


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

    # R1-04: the native config is an execution dependency of its own. Field checks above cannot
    # notice a change to pricing, tools or trajectory, and pricing feeds the cost estimate.
    try:
        live_native = native_config_digest()
    except Exception as error:  # noqa: BLE001 - any load failure is a contract failure
        problems.append(f"{NATIVE_CONFIG} could not be hashed: {error}")
    else:
        if live_native != FROZEN_NATIVE_CONFIG_DIGEST:
            problems.append(
                f"{NATIVE_CONFIG} semantics are {live_native}, frozen {FROZEN_NATIVE_CONFIG_DIGEST}"
            )
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
    pilot = pilot_authorization_gate(cells=pilot_cells(), repetition_id=0)
    return {
        "mode": "DRY_RUN",
        "stage": "closure-2b-0",
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": DATASET_VERSION,
        "protocol_revision": PROTOCOL_REVISION,
        "preregistration_digest": preregistration_digest(),
        "frozen_preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
        "native_config_digest": native_config_digest(),
        "frozen_native_config_digest": FROZEN_NATIVE_CONFIG_DIGEST,
        "router_policy": {
            "version": ROUTING_POLICY_VERSION,
            "digest": ROUTING_POLICY_DIGEST,
        },
        "generated_at": _now(),
        "model_calls": 0,
        "contains_live_outcomes": False,
        "route_expectation_is_prediction": True,
        "approval": {
            "status": gate["status"],
            "execution_ready": gate["execution_ready"],
            "problems": gate["problems"],
        },
        "pilot_plan": {
            "plan_id": PILOT_PLAN.plan_id,
            "plan_digest": pilot["plan_digest"],
            "approved_plan_digest": pilot["approved_plan_digest"],
            "approval_status": pilot["approval_status"],
            "execution_ready": pilot["execution_ready"],
            "problems": pilot["problems"],
            "cells": list(PILOT_PLAN.cells),
            "repetitions": list(PILOT_PLAN.repetitions),
            "max_cell_executions": PILOT_PLAN.max_cell_executions,
            "estimated_cost_ceiling_usd": PILOT_PLAN.estimated_cost_ceiling_usd,
            "cost_policy": pilot["cost_policy"],
        },
        "execution_ready": gate["execution_ready"],
        "pilot_execution_ready": pilot["execution_ready"],
        "contract_problems": problems,
        "contract_ok": not problems,
        "cell_count": len(cells),
        "cells": cells,
        "sandbox_count_scope": "agent_sandbox_only",
        "agent_sandbox_runs_confirmed_min": 0,
        "agent_sandbox_runs_confirmed_not_created": 0,
        "agent_sandbox_runs_unknown": 0,
        "agent_sandbox_runs_total": 0,
        "agent_sandbox_runs_complete": True,
        "oracle_sandbox_runs": None,
        "oracle_sandbox_source": "not_exposed_by_runner: the oracle sandbox is created separately",
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


def _mark(
    lease: ExecutionLease,
    cell_id: str,
    repetition_id: int,
    status: str,
    *,
    result_path: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Write one cell state and let a failure propagate.

    Returning the error as a string would let the caller keep going, which is exactly the defect
    this function used to have: a cell whose ``RUNNING`` row could not be written still reached the
    engine. Durability of the state transition is a precondition for the side effect, not a note
    taken afterwards, so a failure stops the dispatch instead of being recorded next to it.
    """

    if status == "RUNNING":
        lease.mark_running(cell_id, repetition_id)
    elif status == "COMPLETED":
        lease.mark_completed(cell_id, repetition_id, result_path=result_path)
    elif status == "FAILED":
        lease.mark_failed(
            cell_id,
            repetition_id,
            failure_reason=failure_reason or "unspecified",
            result_path=result_path,
        )
    else:
        lease.mark(cell_id, repetition_id, status)


def _ledger_integrity_record(
    cell: Cell,
    repetition_id: int,
    attempted_status: str,
    error: PilotLedgerError,
    *,
    executed: bool,
) -> dict[str, Any]:
    """What failed, in which phase, and what is still true about the execution.

    ``executed`` is the difference between "the cell never started" and "the cell ran and its
    outcome exists but cannot be confirmed in the ledger". Both stop the pilot; only one of them has
    an agent result to preserve.
    """

    return {
        "status": "BLOCKED_LEDGER_POST_EXECUTION" if executed else "BLOCKED_LEDGER_PRE_EXECUTION",
        "phase": "POST_EXECUTION" if executed else "PRE_EXECUTION",
        "cell_id": cell.cell_id,
        "repetition_id": repetition_id,
        "attempted_status": attempted_status,
        "error_code": getattr(error, "reason_code", type(error).__name__),
        "error_class": type(error).__name__,
        "message": str(error),
        "executed_before_failure": executed,
        "claim_status": "CONSUMED",
        "auto_retry": False,
    }


def _git_commit() -> str | None:
    """Best-effort commit for the ledger's audit column. A failure is not a reason to abort."""

    import subprocess

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


async def run_live_pilot(
    cells: tuple[Cell, ...],
    *,
    runs_root: Path,
    repetition_id: int = 0,
    executor: Executor | None = None,
    live_intent: bool = False,
    plan_id: str = PILOT_PLAN_ID,
) -> dict[str, Any]:
    """Execute the given cells for real.

    Order is the contract:

    1. ``live_intent`` must be explicit — no environment variable, no default, no inference.
    2. ``require_execution_approval()`` — raises before *any* executor exists.
    3. ``require_pilot_authorization()`` — the scope must be the approved scope of ``plan_id``.
    4. The frozen contract is re-checked; a drift aborts the whole pilot instead of running part.
    5. The plan is claimed in the durable ledger. One claim per plan digest, taken in a single
       transaction, before any provider request or sandbox provisioning. A plan that was already
       claimed is refused even if the previous attempt crashed.
    6. Only then does each cell reach its engine; each state change is persisted.

    A cell that raises is recorded as ``EXECUTION_ERROR`` and the pilot continues unless the cost
    state becomes unknowable: one broken cell is never rewritten as a success and never silently
    retried on another engine.
    """

    if not live_intent:
        raise LiveExecutionBlocked(
            "LIVE_INTENT_NOT_CONFIRMED: pass --live --confirm-live; no environment variable can "
            "enable a live run"
        )
    plan, approval_record = pilot_scope(plan_id)
    gate = require_execution_approval()
    pilot_gate = require_pilot_authorization(
        cells=cells, repetition_id=repetition_id, plan=plan, record=approval_record
    )
    require_frozen_contract()

    # The lease is taken after every approval and before any provider or sandbox work. If the plan
    # was ever claimed, this raises and nothing downstream runs — a crashed process does not give
    # its claim back. The claim is keyed by *this* plan's digest, so the pilot's spent claim can
    # neither block nor authorize the routing pair.
    lease = _ledger_factory().claim_plan(
        plan_digest=pilot_gate["plan_digest"],
        plan_id=pilot_gate["plan_id"],
        cells=[(cell.cell_id, repetition_id) for cell in cells],
        runner_revision=plan.runner_revision,
        git_commit=_git_commit(),
    )

    runner = executor or _default_executor
    model = effective_model()
    records: list[dict[str, Any]] = []
    ledger_write_failures: list[str] = []
    ledger_integrity: dict[str, Any] | None = None
    integrity_status = "COMPLETE"
    stopped_reason: str | None = None
    not_started: list[str] = []
    for index, cell in enumerate(cells):
        if index:
            # The ceiling is advisory and evaluated between cells: a cell already running can
            # overspend before this check sees it. Unknown, invalid or exhausted cost stops too.
            state = _cost_dispatch_state(records, plan)
            if not state["can_dispatch"]:
                stopped_reason = state["reason"]
                not_started = [pending.cell_id for pending in cells[index:]]
                break
        settings = settings_for(cell, runs_root=runs_root, model=model)
        task = cell.benchmark_task()

        # Phase A — durability before the side effect. If the RUNNING row cannot be written, the
        # cell never starts: no provider request, no sandbox, no tool call. The plan claim stays
        # CONSUMED, so this is a stop, not a retry.
        try:
            _mark(lease, cell.cell_id, repetition_id, "RUNNING")
        except PilotLedgerError as error:
            integrity_status = "BLOCKED_LEDGER_PRE_EXECUTION"
            ledger_integrity = _ledger_integrity_record(
                cell, repetition_id, "RUNNING", error, executed=False
            )
            ledger_write_failures.append(str(error))
            stopped_reason = error.reason_code
            not_started = [pending.cell_id for pending in cells[index:]]
            break

        try:
            result = await runner(
                cell,
                settings=settings,
                task=task,
                runs_root=runs_root,
                repetition_id=repetition_id,
            )
        except Exception as error:  # noqa: BLE001 - a failed cell is data, not a crash
            record = _execution_error_record(cell, error, repetition_id=repetition_id)
            records.append(record)
            # Phase B — the agent already failed. A ledger failure here is recorded next to the
            # real outcome, never converted into one and never allowed to continue dispatching.
            try:
                _mark(
                    lease,
                    cell.cell_id,
                    repetition_id,
                    "FAILED",
                    failure_reason=record["failure_reason"],
                )
            except PilotLedgerError as ledger_error:
                integrity_status = "BLOCKED_LEDGER_POST_EXECUTION"
                ledger_integrity = _ledger_integrity_record(
                    cell, repetition_id, "FAILED", ledger_error, executed=True
                )
                ledger_write_failures.append(str(ledger_error))
                stopped_reason = ledger_error.reason_code
                not_started = [pending.cell_id for pending in cells[index + 1 :]]
                break
            continue
        record = normalize_result(cell, result, settings=settings)
        records.append(record)
        try:
            _mark(
                lease,
                cell.cell_id,
                repetition_id,
                "COMPLETED",
                result_path=record.get("raw_result_path"),
            )
        except PilotLedgerError as ledger_error:
            # The result is real and stays in ``records``; the ledger simply cannot confirm it, so
            # the pilot must not continue and must not be reported as complete.
            integrity_status = "BLOCKED_LEDGER_POST_EXECUTION"
            ledger_integrity = _ledger_integrity_record(
                cell, repetition_id, "COMPLETED", ledger_error, executed=True
            )
            ledger_write_failures.append(str(ledger_error))
            stopped_reason = ledger_error.reason_code
            not_started = [pending.cell_id for pending in cells[index + 1 :]]
            break
    # The last cell never gets a "next dispatch" check, so its own overrun must be recorded here.
    final_cost_state = _cost_dispatch_state(records, plan)
    statuses = [record.get("sandbox_creation_status") for record in records]
    confirmed = sum(1 for status in statuses if status == "SANDBOX_CREATED_CONFIRMED")
    confirmed_absent = sum(1 for status in statuses if status == "SANDBOX_NOT_CREATED_CONFIRMED")
    unknown = len(statuses) - confirmed - confirmed_absent
    return {
        "mode": "LIVE",
        "stage": "closure-2b-0",
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "protocol_revision": PROTOCOL_REVISION,
        "preregistration_digest": preregistration_digest(),
        "router_policy": {"version": ROUTING_POLICY_VERSION, "digest": ROUTING_POLICY_DIGEST},
        "generated_at": _now(),
        "repetition_id": repetition_id,
        "approval_status": gate["status"],
        "approved_digest": gate["approved_digest"],
        "pilot_plan_id": pilot_gate["plan_id"],
        "pilot_plan_digest": pilot_gate["plan_digest"],
        "authorized_cells": pilot_gate["approved_cells"],
        "authorized_repetitions": pilot_gate["approved_repetitions"],
        "max_cell_executions": pilot_gate["max_cell_executions"],
        "cost_policy": pilot_gate["cost_policy"],
        "cost_ceiling_usd": pilot_gate["cost_policy"]["ceiling_usd"],
        "cost_ceiling_enforcement": pilot_gate["cost_policy"]["enforcement"],
        "cost_ceiling_guarantee": pilot_gate["cost_policy"]["guarantee"],
        "cell_count": len(records),
        # Unknown stays unknown: a pilot with one unmeasured cell does not have a total.
        "model_calls": _total_or_none([record.get("model_calls") for record in records]),
        "estimated_cost_usd_total": _sum_or_none(
            [record.get("estimated_cost_usd") for record in records]
        ),
        # Nothing in this runner reports a billed amount; an estimate is not an observation.
        "observed_cost_usd_total": None,
        "observed_cost_source": "not_exposed_by_runner",
        "stopped_reason": stopped_reason,
        "not_started_cells": not_started,
        # Evaluated after the last cell too: nothing checks "the next dispatch" past the end.
        "final_cost_state": final_cost_state,
        "cost_ceiling_state": final_cost_state["reason"],
        "cost_ceiling_breached": final_cost_state["reason"]
        in {"COST_CEILING_REACHED", "COST_CEILING_EXCEEDED"},
        "execution_claim": {
            "claimed": True,
            "claim_status": "CONSUMED",
            "plan_digest": pilot_gate["plan_digest"],
            "attempt_id": lease.attempt_id,
            "ledger_path": str(lease.path),
            "ledger_scope": "local_user_state",
        },
        # A ledger write failure is not a slow path: it means the durable record no longer matches
        # what happened, so the pilot cannot claim completeness even when the cells ran fine.
        "execution_integrity_status": integrity_status,
        "execution_integrity_ok": integrity_status == "COMPLETE",
        "ledger_integrity": ledger_integrity,
        "ledger_write_failures": ledger_write_failures,
        # Agent sandboxes only: the oracle has its own, which the runner does not count here.
        "sandbox_count_scope": "agent_sandbox_only",
        "agent_sandbox_runs_confirmed_min": confirmed,
        "agent_sandbox_runs_confirmed_not_created": confirmed_absent,
        "agent_sandbox_runs_unknown": unknown,
        "agent_sandbox_runs_total": None if unknown else confirmed,
        "agent_sandbox_runs_complete": unknown == 0,
        "oracle_sandbox_runs": None,
        "oracle_sandbox_source": "not_exposed_by_runner: the oracle sandbox is created separately",
        "cells": records,
    }


def _total_or_none(values: list[Any]) -> int | None:
    """Sum only when every term is known. One unknown term makes the total unknown."""

    if not values or any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _execution_error_record(
    cell: Cell, error: BaseException, *, repetition_id: int = 0
) -> dict[str, Any]:
    """A failed cell, recorded without inventing a lifecycle it cannot observe.

    The exception carries no sandbox event stream, so the creation state is ``UNKNOWN`` — never
    ``False``. A cell that created a sandbox and then raised would otherwise be counted as zero.
    """

    return {
        "cell_id": cell.cell_id,
        "task_id": cell.task_id,
        "arm_id": cell.arm_id,
        "repetition_id": int(repetition_id),
        "status": "EXECUTION_ERROR",
        "failure_class": "HARNESS_ERROR",
        "failure_reason": f"{type(error).__name__}: {error}",
        "oracle_passed": None,
        "agent_success": None,
        "task_success": None,
        "model_calls": None,
        "model_calls_source": "not_observed: the cell raised before producing a result",
        "provider_prompt_tokens": None,
        "provider_completion_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "estimated_cost_usd": None,
        "estimated_cost_usd_state": "UNKNOWN",
        "budget_exceeded": None,
        "budget_final_verdict": "UNKNOWN",
        "budget_verdict_basis": "no_result",
        "budget_admission_rejected": None,
        "budget_rejected_requests": None,
        # No lifecycle record exists for a cell that raised before returning.
        "sandbox_created": None,
        "sandbox_destroyed": None,
        "sandbox_creation_status": "SANDBOX_CREATION_UNKNOWN",
        "sandbox_count_scope": "agent_sandbox_only",
        "sandbox_evidence_source": "no lifecycle record: the executor raised before returning",
        "oracle_sandbox_counted": False,
        "raw_evidence_preserved": True,
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
    guarded = attestation["budget"]
    budget_admission_rejected = (
        guarded.get("admission_rejections") if isinstance(guarded, dict) else None
    )
    budget_observed_limit_exceeded = None
    if isinstance(native_budget, dict) and "exceeded" in native_budget:
        budget_observed_limit_exceeded = bool(native_budget["exceeded"])
    # The runner's own verdict outranks anything inferred from a counter, and zero pre-send refusals
    # are not proof that nothing overshot: the last admitted request can still cross the threshold.
    if result.failure_class == "BUDGET_EXCEEDED":
        budget_final_verdict, budget_verdict_basis = "EXCEEDED", "runner_failure_class"
    elif budget_admission_rejected is not None and int(budget_admission_rejected) > 0:
        budget_final_verdict, budget_verdict_basis = "EXCEEDED", "pre_send_rejections"
    elif budget_observed_limit_exceeded is True:
        budget_final_verdict, budget_verdict_basis = "EXCEEDED", "post_hoc_check"
    elif budget_observed_limit_exceeded is False:
        budget_final_verdict, budget_verdict_basis = "NOT_EXCEEDED", "post_hoc_check"
    else:
        budget_final_verdict, budget_verdict_basis = "UNKNOWN", "no_negative_verdict_from_runner"
    budget_exceeded = {"EXCEEDED": True, "NOT_EXCEEDED": False}.get(budget_final_verdict)

    oracle_passed = bool(result.oracle.passed)
    # Three-state composite: an oracle failure makes "passed but over budget" impossible, an unknown
    # budget leaves it unknown rather than optimistically false.
    if not oracle_passed:
        deliverable_over_budget: bool | None = False
    elif budget_exceeded is None:
        deliverable_over_budget = None
    else:
        deliverable_over_budget = bool(budget_exceeded)

    prompt_tokens = metrics.prompt_tokens
    completion_tokens = metrics.completion_tokens
    if prompt_tokens is None and completion_tokens is None:
        usage_observed: bool | None = False
    elif prompt_tokens is None or completion_tokens is None:
        usage_observed = None
    else:
        usage_observed = True

    created = metrics.sandbox_created
    if created is True:
        sandbox_status = "SANDBOX_CREATED_CONFIRMED"
    elif created is False:
        sandbox_status = "SANDBOX_NOT_CREATED_CONFIRMED"
    else:
        sandbox_status = "SANDBOX_CREATION_UNKNOWN"

    # A NaN or infinite cost must never enter the numbers: it would poison every later comparison.
    raw_cost = metrics.estimated_cost_usd
    if raw_cost is None:
        cell_cost, cell_cost_state = None, "UNKNOWN"
    else:
        cell_cost = _finite_cost(raw_cost)
        cell_cost_state = "COMPLETE" if cell_cost is not None else "INVALID"
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
        "provider_prompt_tokens": prompt_tokens,
        "provider_completion_tokens": completion_tokens,
        "cache_hit_tokens": metrics.cache_hit_tokens,
        "cache_miss_tokens": metrics.cache_miss_tokens,
        "cache_usage_complete": attestation["cache_metrics_complete"],
        "cache_split_status": attestation["cache_split_status"],
        # Two different questions. The cache split can be complete while the token totals are not
        # attested, and neither runner attests that *every* request reported usage, so token-usage
        # completeness stays unknown instead of being copied from the cache flag.
        "provider_token_usage_observed": usage_observed,
        "provider_usage_complete": None,
        "provider_usage_source": (
            "not_exposed_by_runner: usage is aggregated across requests but completeness is not "
            "attested"
        ),
        "estimated_cost_usd": cell_cost,
        "estimated_cost_usd_state": cell_cost_state,
        "model_calls": attestation["model_calls"],
        "model_calls_source": attestation["model_calls_source"],
        "model_calls_semantics": (
            "requests_observed"
            if attestation["model_calls_source"].startswith("request_evidence")
            else "completed_model_turns"
            if attestation["model_calls_source"] == "metrics.model_turns"
            else None
        ),
        "tool_calls": metrics.tool_calls,
        "budget_exceeded": budget_exceeded,
        "budget_final_verdict": budget_final_verdict,
        "budget_verdict_basis": budget_verdict_basis,
        "budget_admission_rejected": budget_admission_rejected,
        "budget_observed_limit_exceeded": budget_observed_limit_exceeded,
        "budget_rejected_requests": budget_admission_rejected,
        "budget_record": attestation["budget"] or native_budget,
        "agent_latency_ms": environment.get("agent_latency_ms"),
        "end_to_end_latency_ms": metrics.latency_ms,
        "parallel_overlap_evidence": None,
        "parallel_overlap_source": (
            "not_exposed_by_runner: worker lifecycle timings live in run_dir/stage_e_evidence.json "
            "and are computed in Closure-2B-1"
        ),
        "deliverable_passed_but_budget_failed": deliverable_over_budget,
        "workspace_isolated": environment.get("workspace_isolation"),
        "sandbox_created": metrics.sandbox_created,
        "sandbox_destroyed": metrics.sandbox_destroyed,
        "sandbox_creation_status": sandbox_status,
        # An agent sandbox is not an oracle sandbox: the oracle runs in its own, which this runner
        # does not expose. Counting the agent side must never be read as the whole lifecycle.
        "sandbox_count_scope": "agent_sandbox_only",
        "oracle_sandbox_counted": False,
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
    parser.add_argument(
        "--plan",
        default=PILOT_PLAN_ID,
        choices=plan_scope_ids(),
        help=(
            "which fixed plan a run selects; only the two registered plans exist. The dry-run "
            "manifest always describes the frozen 18-cell contract, never the selected plan"
        ),
    )
    parser.add_argument(
        "--runs-root",
        default=None,
        help="run tree; defaults to the tree owned by the selected plan",
    )
    parser.add_argument("--live", action="store_true", help="request a real run")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="second, explicit confirmation required together with --live",
    )
    parser.add_argument("--cells", help="comma separated <task_id>::<arm_id>, default the plan's")
    parser.add_argument("--repetition", type=int, default=0)
    args = parser.parse_args(argv)

    runs_root = Path(args.runs_root) if args.runs_root else default_runs_root(args.plan)

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
                    "pilot_plan_digest": manifest["pilot_plan"]["plan_digest"],
                    "pilot_approval": manifest["pilot_plan"]["approval_status"],
                    "pilot_execution_ready": manifest["pilot_execution_ready"],
                    "model_calls": manifest["model_calls"],
                    "agent_sandbox_runs_total": manifest["agent_sandbox_runs_total"],
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
        cells = _parse_cells(args.cells) if args.cells else plan_cells_for(args.plan)
        summary = asyncio.run(
            run_live_pilot(
                cells,
                runs_root=runs_root,
                repetition_id=args.repetition,
                live_intent=True,
                plan_id=args.plan,
            )
        )
    except (
        LiveExecutionBlocked,
        PilotContractError,
        PermissionError,
        PilotLedgerError,
    ) as error:
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    # A ledger refusal keeps its own code so it is not read as a harness failure.
                    "reason": getattr(error, "reason_code", type(error).__name__),
                    "detail": str(error),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    out = live_summary_path(Path(args.out), args.plan)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as error:
        # Do not pretend the evidence was saved: say so and fail.
        print(
            json.dumps(
                {
                    "status": "SUMMARY_NOT_SAVED",
                    "detail": f"{type(error).__name__}: {error}",
                    "execution_integrity_status": summary["execution_integrity_status"],
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 5
    print(
        json.dumps(
            {
                "mode": "LIVE",
                "cells": summary["cell_count"],
                "execution_claim": summary["execution_claim"],
                "execution_integrity_status": summary["execution_integrity_status"],
                "cost_ceiling_state": summary["cost_ceiling_state"],
                "out": str(out),
            },
            indent=2,
        )
    )
    # A broken durable record is not a successful pilot, even when every cell ran.
    return 0 if summary["execution_integrity_status"] == "COMPLETE" else 4


if __name__ == "__main__":
    raise SystemExit(main())
