"""Stage F — four-arm compatibility smoke on the formal P1 task.

The F2-A proof ran the four arms on the fixture that existed at that commit. F2-B replaced P1 with
the formal connector migration, so the same four entries are exercised once more on the *new* task:
the real native runner, the real direct-only goal runtime, the real static DAG and the real dynamic
delegation path, each through ``executor.dispatch`` with the deterministic provider injected.

Nothing here is a synthetic ``CellRun``: every arm goes through its production runner, produces a
raw result, exports a workspace, is scored by an independent oracle in its own sandbox, is
normalised into Unified Cell Evidence, audited, written to the ledger and summarised in a matrix
report. The provider is the only substitute, and the report says so.

This is a compatibility confirmation, not the F2-A proof: it does not add cells to the 96-cell
matrix, it consumes no production claim, and it never contacts a provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import arms, normalize, proof, scripted, tasks
from experiments.stage_f import plan as f_plan

REPO = Path(__file__).resolve().parents[2]
SMOKE_PLAN_ID = "stage-f-p1-compat-smoke"
SMOKE_SCOPE = "F_P1_COMPAT_SMOKE"
TASK_ID = "f-p1-connector-migration"
ARM_SEQUENCE: tuple[str, ...] = ("A0", "A1", "A2", "A3")


def _unique(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000}-{os.getpid()}"


def cells_for(
    arm_sequence: tuple[str, ...] = ARM_SEQUENCE,
    *,
    plan_id: str = SMOKE_PLAN_ID,
    scope: str = SMOKE_SCOPE,
) -> list[dict[str, Any]]:
    return [
        arms.cell_record(
            task_id=TASK_ID,
            arm_id=arm_id,
            repetition=1,
            sequence="Q1",
            position=index,
            batch_id=plan_id,
            plan_id=plan_id,
            scope=scope,
        )
        for index, arm_id in enumerate(arm_sequence, start=1)
    ]


def plan_for(
    cells: list[dict[str, Any]],
    *,
    plan_id: str = SMOKE_PLAN_ID,
    scope: str = SMOKE_SCOPE,
    master_id: str = "stage-f-p1-compat-smoke",
) -> dict[str, Any]:
    return {
        "stage": "F",
        "design_version": f_plan.DESIGN_VERSION,
        "master_id": master_id,
        "plan_id": plan_id,
        "scope": scope,
        "batch": 0,
        "repetition": 1,
        "repetitions": 1,
        "tasks": [TASK_ID],
        "arms": [str(cell["arm_id"]) for cell in cells],
        "cells_count": len(cells),
        "cells": cells,
        "digest": f_plan.plan_digest(cells),
        "claim_namespace": f"stage-f-claim/{plan_id}",
        "runs_subdir": f"runs/{plan_id}",
        "evidence_subdir": f"evidence/{plan_id}",
        "is_benchmark_sample": False,
        "attempt_limit": 1,
        "budget": arms.budget(),
        "budget_digest": arms.budget_digest(),
        "nominal_token_capacity": len(cells) * int(arms.CELL_BUDGET["token_budget"]),
        "cost_stop_usd": 4.0,
        "per_cell_cost_stop_usd": 1.0,
        "token_stop": len(cells) * int(arms.CELL_BUDGET["token_budget"]),
        "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
        "retry": "FORBIDDEN",
        "is_proof": True,
    }


def _cells() -> list[dict[str, Any]]:
    return cells_for()


def _plan(cells: list[dict[str, Any]]) -> dict[str, Any]:
    return plan_for(cells)


# -- scripts ------------------------------------------------------------------------------------


def _independent_pair(task: Any) -> list[dict[str, Any]]:
    return [dict(worker) for worker in task.workers if not worker["depends_on"]][:2]


def _integration_node(task: Any) -> dict[str, Any]:
    dependents = [dict(worker) for worker in task.workers if worker["depends_on"]]
    if len(dependents) != 1:
        raise ValueError(f"{task.task_id} has {len(dependents)} dependent nodes, expected one sink")
    return dependents[0]


def delegation_proposal(task: Any) -> dict[str, Any]:
    """The Main's real ``delegate_tasks`` input, built from this task's own decomposition."""

    pair = _independent_pair(task)
    if len(pair) != 2:
        raise ValueError(f"{task.task_id} does not declare two independent implementation workers")
    return {
        "tasks": [
            {
                "key": f"{worker['task_id']}-unit",
                "objective": str(worker["assignment"]),
                "background": (
                    "The project is at /workspace/project and `docs/INTERFACE.md` is frozen. "
                    f"Edit only {', '.join(f'`{path}`' for path in worker['owned_paths'])}; the "
                    "public suite is `test_public.py`."
                ),
                "profile_id": "coding_worker",
                "write_paths": list(worker["owned_paths"]),
            }
            for worker in pair
        ],
        "reason": (
            "two independent connectors with disjoint write scopes; the shared batching, "
            "checkpoint, registry, pipeline and report surface stays in the Main"
        ),
    }


def main_delegating_script(task: Any) -> list[scripted.ScriptStep]:
    integration = _integration_node(task)
    return [
        ("delegate_tasks", delegation_proposal(task)),
        *proof._writes(task, tuple(str(path) for path in integration["owned_paths"])),
        *proof._checks(),
        ("run_public_tests", {"reason": "verify the integrated workspace against the suite"}),
    ]


def _run_id_for(cell: Mapping[str, Any]) -> str:
    """The run id ``live_cell_runner`` gives this cell: its own short order key."""

    return f"bplus-c{int(cell['order']):02d}"


def _combined_run(
    task: Any, cells: Sequence[Mapping[str, Any]] | None = None
) -> tuple[scripted.ScriptedRun, dict[str, str]]:
    """One scripted provider for the whole dispatch, and the run-id prefix that selects each arm.

    The smoke no longer builds a runner per arm: ``dispatch`` is given a ``RunnerConfig`` with this
    provider factory and the four cells are routed by ``cell_runner_for`` exactly as production
    routes them. A1 and A3 carry the same task text and differ only in their tool surface, so their
    scripts are selected by the trusted run id — the same provider-boundary identity a real adapter
    receives — instead of by the prompt.
    """

    scripts: dict[str, list[scripted.ScriptStep]] = {
        "a0-main": proof.single_agent_script(task),
    }
    markers: dict[str, str] = {"a0-main": "You are the only worker on this task"}
    run_markers: dict[str, str] = {}
    for node_id, steps in proof.dag_scripts(task).items():
        scripts[f"a2-{node_id}"] = steps
        markers[f"a2-{node_id}"] = proof.node_markers(task)[node_id]
    scripts["a1-main"] = proof.main_direct_script(task)
    markers["a1-main"] = task.prompt[:60]
    scripts["a3-main"] = main_delegating_script(task)
    markers["a3-main"] = task.prompt[:60]
    for spec in delegation_proposal(task)["tasks"]:
        key = str(spec["key"])
        # The child owns every path it declared; writing only the first one would deliver a
        # half-migrated connector and score the arm on the script rather than on the topology.
        scripts[f"a3-{key}"] = [
            *proof._writes(task, tuple(str(path) for path in spec["write_paths"])),
            *proof._checks(),
        ]
        # The child's run id ends with ``:r1:<key>``, so this probe is more specific than the
        # Main's and wins for the child while the Main keeps ``bplus-c04``.
        run_markers[f":r1:{key}"] = f"a3-{key}"
    # The run id is the cell's own order key, so the map is built from the cells that will run
    # rather than assumed from a four-cell layout: a two-cell plan gives A1 ``bplus-c01``.
    for cell in cells or ():
        arm_id = str(cell["arm_id"])
        if arm_id in {"A1", "A3"}:
            run_markers[_run_id_for(cell)] = f"{arm_id.lower()}-main"
    run = scripted.ScriptedRun(
        scripts=scripts,
        markers=markers,
        run_markers=run_markers,
        final_text="stage-f scripted response",
    )
    run.requests.clear()
    return run, {
        "A0": "a0-",
        "A1": "a1-",
        "A2": "a2-",
        "A3": "a3-",
    }


def arm_requests(run: scripted.ScriptedRun, prefix: str) -> list[dict[str, Any]]:
    """The request rows the deterministic provider answered for one arm, by recorded script name."""

    return [row for row in run.requests if str(row.get("script") or "").startswith(prefix)]


# -- dispatch -----------------------------------------------------------------------------------


def _authority_file(plan_record: Mapping[str, Any], scratch: Path) -> Path:
    from experiments.stage_c1 import checks as c1_checks

    identity = executor.executor_identity(REPO)
    record = c1_checks._offline_signed(
        plan_record,
        scratch=scratch / "sign",
        head=executor.git_head(REPO),
        identity=identity,
        protocol_digest=authority.protocol_digest(contract.protocol_document()),
        cost_stop_usd=float(plan_record["cost_stop_usd"]),
        per_cell_cost_stop_usd=float(plan_record["per_cell_cost_stop_usd"]),
    )
    path = scratch / "smoke-authority.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


async def run(*, scratch: Path | None = None) -> dict[str, Any]:
    base = Path(scratch or tempfile.mkdtemp(prefix="stage-f-smoke-"))
    await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
    task = tasks.task_by_id(TASK_ID)
    cells = _cells()
    plan_record = _plan(cells)
    run, prefixes = _combined_run(task, cells)
    provider_name = _unique("stagef-smoke")
    # The provider is the only injection. No ``cell_runner`` is passed, so every cell reaches its
    # runner through the production selection in ``executor.cell_runner_for``.
    runner_config = executor.RunnerConfig(llm_factory=scripted.factory_for(run, provider_name))

    started = time.perf_counter()
    with executor.claim_root_override(base / "claims"):
        report = await executor.dispatch(
            SMOKE_PLAN_ID,
            runs_root=base / "runs",
            evidence_root=base / "evidence",
            authority_path=_authority_file(plan_record, base),
            repo_root=REPO,
            mode=executor.MODE_OFFLINE_PROOF,
            plan=plan_record,
            runner_config=runner_config,
        )
    duration = round(time.perf_counter() - started, 3)

    outcomes = {str(cell["cell_id"]): cell for cell in report.get("cells") or []}
    arms_report: list[dict[str, Any]] = []
    problems: list[str] = []
    for cell in cells:
        arm_id = str(cell["arm_id"])
        outcome = outcomes.get(str(cell["cell_id"]))
        if outcome is None:
            problems.append(f"{arm_id}: the dispatcher produced no outcome")
            continue
        evidence = (
            proof._read_json(  # noqa: SLF001 - the same reader the F2-A proof uses
                proof._cell_dir(base / "evidence", cell["order"]) / "evidence.json"
            )
            or {}
        )
        rows = arm_requests(run, prefixes[arm_id])
        arm = proof.arm_report(
            arm_id=arm_id,
            cell=cell,
            outcome=outcome,
            evidence_root=base / "evidence",
            runs_root=base / "runs",
            scripted_run=run,
            requests=rows,
        )
        checks = proof.proof_checks(
            arm_id=arm_id,
            cell=cell,
            report=outcome,
            evidence=evidence,
            scripted_run=run,
            requests=rows,
        )
        # The common integrity gate is new since F2-A: every arm must have produced one, and it
        # must be PASS for the cell to count as delivered.
        integrity = evidence.get("integrity") or {}
        checks.append(
            {
                "check": "common_integrity_passed",
                "ok": integrity.get("verdict") == normalize.PASS,
                "detail": (
                    f"verdict={integrity.get('verdict')!r} failed={integrity.get('failed')} "
                    f"unknown={integrity.get('unknown')}"
                ),
            }
        )
        correctness = evidence.get("correctness") or {}
        # The pre-registered definition: oracle PASS and normal completion and integrity PASS. The
        # public suite is a separate metric and is deliberately not part of delivery.
        expected_delivered = (
            correctness.get("agent_success") is True
            and (correctness.get("independent_oracle") or {}).get("status") == normalize.PASS
            and correctness.get("execution_integrity") == normalize.PASS
        )
        checks.append(
            {
                # Delivered success is a conjunction of three independent verdicts, not a copy of
                # the audit verdict: a cell may pass the audit while its agent did not finish
                # normally, and that is a measured outcome rather than a delivery.
                "check": "delivered_success_consistent",
                "ok": bool(correctness.get("delivered_success")) is expected_delivered,
                "detail": (
                    f"delivered={correctness.get('delivered_success')} "
                    f"expected={expected_delivered} verdict={outcome.get('verdict')!r} "
                    f"public={correctness.get('public_test')!r}"
                ),
            }
        )
        # What the *production* selection returns for this cell, recorded beside the outcome. No
        # ``cell_runner`` was injected into the dispatch, so this is the path the cell took.
        resolved = executor.cell_runner_for(cell)
        checks.append(
            {
                "check": "routed_by_production_selection",
                "ok": getattr(resolved, "execution_mode", None)
                in {str(cell["execution_mode"]), "main_driven"},
                "detail": (
                    f"declared={cell['execution_mode']!r} "
                    f"resolved={getattr(resolved, 'execution_mode', 'legacy_live')!r} "
                    f"stage_f={bool(getattr(resolved, 'is_stage_f_runner', False))}"
                ),
            }
        )
        failed = [item["check"] for item in checks if not item["ok"]]
        problems.extend(f"{arm_id}: {name}" for name in failed)
        arm["routing"] = {
            "declared_execution_mode": cell["execution_mode"],
            "resolved_runner": getattr(resolved, "execution_mode", "legacy_live"),
            "is_stage_f_runner": bool(getattr(resolved, "is_stage_f_runner", False)),
            "cell_runner_injected": False,
        }
        arm["checks"] = checks
        arm["integrity"] = integrity
        arm["requests"] = {
            "count": len(rows),
            "per_agent": sorted(
                {str(row.get("trusted_task_id") or "unattributed") for row in rows}
            ),
        }
        arms_report.append(arm)

    counts = {
        "scripted_requests": len(run.requests),
        "external_model_requests": 0,
        "production_claims_consumed": 0,
    }
    identity = executor.executor_identity(REPO)
    return {
        "stage": "F",
        "kind": "p1_four_arm_compatibility_smoke",
        "generated_at": executor.now(),
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "repo_head": executor.git_head(REPO),
        "task_id": TASK_ID,
        "plan": {
            "plan_id": SMOKE_PLAN_ID,
            "scope": SMOKE_SCOPE,
            "plan_digest": plan_record["digest"],
            "cells": [cell["cell_id"] for cell in cells],
            "mode": executor.MODE_OFFLINE_PROOF,
            "claim_root": "isolated offline claim (no production claim consumed)",
        },
        "provider": "deterministic scripted provider (no model call)",
        "routing": {
            "cell_runner_injected": False,
            "provider_seam": "executor.RunnerConfig.llm_factory",
            "selection": "executor.cell_runner_for (declared execution_mode)",
            "note": (
                "the four cells were dispatched with no cell_runner, so each one was routed by the "
                "production selection into its mode's runner"
            ),
        },
        "dispatch": {
            "complete": report.get("complete"),
            "cells_planned": report.get("cells_planned"),
            "cells_started": report.get("cells_started"),
            "cells_passed": report.get("cells_passed"),
            "stop_reason": report.get("stop_reason"),
            "usage_completeness": report.get("usage_completeness"),
            "total_tokens": report.get("total_tokens"),
            "total_cost_usd": report.get("total_cost_usd"),
            "success_counts": report.get("success_counts"),
            "success_definition": report.get("success_definition"),
            "ledger": [
                {
                    "cell_id": item.get("cell_id"),
                    "verdict": item.get("verdict"),
                    "cost_usd": item.get("cost_usd"),
                    "stops": item.get("stops"),
                }
                for item in report.get("cells") or []
            ],
        },
        "arms": arms_report,
        "counts": counts,
        "problems": problems,
        "ok": not problems and report.get("cells_passed") == len(cells),
        "status": (
            "STAGE_F_P1_FOUR_ARM_SMOKE_PASS"
            if not problems and report.get("cells_passed") == len(cells)
            else "STAGE_F_P1_FOUR_ARM_SMOKE_FAIL"
        ),
        "duration_s": duration,
        "scratch_root": str(base),
        "skill": "compatibility confirmation only; no cell here enters the 96-cell matrix",
    }


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    output = Path(args[0]) if args else Path(__file__).resolve().parent / "p1-compat-smoke.json"
    scratch = Path(args[1]) if len(args) > 1 else None
    payload = asyncio.run(run(scratch=scratch))
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": payload["ok"],
                "status": payload["status"],
                "cells_passed": payload["dispatch"]["cells_passed"],
                "problems": payload["problems"][:8],
                "output": str(output),
            }
        )
    )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
