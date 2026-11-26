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
from collections.abc import Mapping
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


def _cells() -> list[dict[str, Any]]:
    return [
        arms.cell_record(
            task_id=TASK_ID,
            arm_id=arm_id,
            repetition=1,
            sequence="Q1",
            position=index,
            batch_id=SMOKE_PLAN_ID,
            plan_id=SMOKE_PLAN_ID,
            scope=SMOKE_SCOPE,
        )
        for index, arm_id in enumerate(ARM_SEQUENCE, start=1)
    ]


def _plan(cells: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "stage": "F",
        "design_version": f_plan.DESIGN_VERSION,
        "master_id": "stage-f-p1-compat-smoke",
        "plan_id": SMOKE_PLAN_ID,
        "scope": SMOKE_SCOPE,
        "batch": 0,
        "repetition": 1,
        "repetitions": 1,
        "tasks": [TASK_ID],
        "arms": list(ARM_SEQUENCE),
        "cells_count": len(cells),
        "cells": cells,
        "digest": f_plan.plan_digest(cells),
        "claim_namespace": f"stage-f-claim/{SMOKE_PLAN_ID}",
        "runs_subdir": f"runs/{SMOKE_PLAN_ID}",
        "evidence_subdir": f"evidence/{SMOKE_PLAN_ID}",
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


def _provider_for(arm_id: str, task: Any) -> scripted.ScriptedRun:
    if arm_id == "A0":
        run = scripted.ScriptedRun(
            scripts={"main": proof.single_agent_script(task)},
            markers={"main": "You are the only worker on this task"},
            default_marker="main",
        )
    elif arm_id == "A2":
        run = scripted.ScriptedRun(
            scripts=proof.dag_scripts(task),
            markers=proof.node_markers(task),
            final_text="node implemented and compiled",
        )
    elif arm_id == "A1":
        run = scripted.ScriptedRun(
            scripts={"main": proof.main_direct_script(task)},
            markers={"main": task.prompt[:60]},
            final_text="implemented all modules, public suite run",
        )
    else:
        proposal = delegation_proposal(task)
        scripts: dict[str, list[scripted.ScriptStep]] = {"main": main_delegating_script(task)}
        markers: dict[str, str] = {"main": task.prompt[:60]}
        for spec in proposal["tasks"]:
            root = str(spec["write_paths"][0])
            # The child owns every path it declared; writing only the first one would deliver a
            # half-migrated connector and score the arm on the script rather than on the topology.
            scripts[root] = [
                *proof._writes(task, tuple(str(path) for path in spec["write_paths"])),
                *proof._checks(),
            ]
            markers[root] = str(spec["objective"]).splitlines()[0]
        run = scripted.ScriptedRun(
            scripts=scripts,
            markers=markers,
            final_text="delegated two connectors, integrated and verified",
        )
    run.requests.clear()
    return run


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
    runs = {str(cell["arm_id"]): _provider_for(str(cell["arm_id"]), task) for cell in cells}
    runners = {arm_id: proof.proof_runner(arm_id, task, runs[arm_id]) for arm_id in ARM_SEQUENCE}

    async def composed(cell: dict[str, Any], context: Any) -> executor.CellRun:
        return await runners[str(cell["arm_id"])](cell, context)

    composed.is_live = False  # type: ignore[attr-defined]
    started = time.perf_counter()
    with executor.claim_root_override(base / "claims"):
        report = await executor.dispatch(
            SMOKE_PLAN_ID,
            runs_root=base / "runs",
            evidence_root=base / "evidence",
            authority_path=_authority_file(plan_record, base),
            cell_runner=composed,
            repo_root=REPO,
            mode=executor.MODE_OFFLINE_PROOF,
            plan=plan_record,
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
        arm = proof.arm_report(
            arm_id=arm_id,
            cell=cell,
            outcome=outcome,
            evidence_root=base / "evidence",
            runs_root=base / "runs",
            scripted_run=runs[arm_id],
        )
        checks = proof.proof_checks(
            arm_id=arm_id, cell=cell, report=outcome, evidence=evidence, scripted_run=runs[arm_id]
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
        expected_delivered = (
            correctness.get("agent_success") is True
            and (correctness.get("independent_oracle") or {}).get("status") == normalize.PASS
            and correctness.get("execution_integrity") == normalize.PASS
            and correctness.get("public_test") != normalize.FAIL
        )
        checks.append(
            {
                # Delivered success is a conjunction of four independent verdicts, not a copy of
                # the audit verdict: a cell may pass the audit while its agent did not finish
                # normally, and that is a measured outcome rather than a delivery.
                "check": "delivered_success_consistent",
                "ok": bool(correctness.get("delivered_success")) is expected_delivered,
                "detail": (
                    f"delivered={correctness.get('delivered_success')} "
                    f"expected={expected_delivered} verdict={outcome.get('verdict')!r}"
                ),
            }
        )
        failed = [item["check"] for item in checks if not item["ok"]]
        problems.extend(f"{arm_id}: {name}" for name in failed)
        arm["checks"] = checks
        arm["integrity"] = integrity
        arm["requests"] = {
            "count": len(runs[arm_id].requests),
            "per_agent": sorted(
                {str(row.get("trusted_task_id") or "unattributed") for row in runs[arm_id].requests}
            ),
        }
        arms_report.append(arm)

    counts = {
        "scripted_requests": sum(len(run.requests) for run in runs.values()),
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
        "dispatch": {
            "complete": report.get("complete"),
            "cells_planned": report.get("cells_planned"),
            "cells_started": report.get("cells_started"),
            "cells_passed": report.get("cells_passed"),
            "stop_reason": report.get("stop_reason"),
            "usage_completeness": report.get("usage_completeness"),
            "total_tokens": report.get("total_tokens"),
            "total_cost_usd": report.get("total_cost_usd"),
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
