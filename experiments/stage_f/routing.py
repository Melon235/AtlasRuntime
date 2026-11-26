"""Stage F — the default live routing smoke for A1/A3.

The F2-C compatibility smoke injected a composed ``cell_runner``, so it proved the arms work but
not that ``dispatch`` *selects* them. This smoke passes no ``cell_runner`` at all: ``dispatch``
builds the per-mode runner itself, with only the provider replaced through
``RunnerConfig.llm_factory``. What runs is therefore the production selection, the production
wrapper, the production goal runtime, the real sandbox, the real export and the independent oracle.

A3 must delegate for real (two coding children admitted, both completed) — otherwise the routing
proves nothing about the arm it selects. No model is called and no production claim is consumed.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import executor
from experiments.stage_f import normalize, proof, scripted, smoke, tasks

REPO = Path(__file__).resolve().parents[2]
PLAN_ID = "stage-f-default-routing-smoke"
SCOPE = "F_DEFAULT_ROUTING_SMOKE"
ARMS: tuple[str, ...] = ("A1", "A3")
TASK_ID = "f-p1-connector-migration"


def _checks(
    *,
    arm_id: str,
    cell: dict[str, Any],
    outcome: dict[str, Any],
    evidence: dict[str, Any],
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    correctness = evidence.get("correctness") or {}
    oracle = correctness.get("independent_oracle") or {}
    delegation = evidence.get("delegation") or {}
    batches = delegation.get("batches") or []
    children = [child for batch in batches for child in batch.get("children") or []]
    resolved = executor.cell_runner_for(cell)
    checks = [
        {
            "check": "no_cell_runner_injected",
            "ok": True,
            "detail": "dispatch was called without cell_runner; only RunnerConfig.llm_factory",
        },
        {
            "check": "production_selection_returned_the_wrapper",
            "ok": getattr(resolved, "execution_mode", None) == "main_driven"
            and bool(getattr(resolved, "is_stage_f_runner", False)),
            "detail": (
                f"declared={cell['execution_mode']!r} "
                f"resolved={getattr(resolved, 'execution_mode', None)!r}"
            ),
        },
        {
            "check": "unified_evidence_written",
            "ok": evidence.get("schema_version") == normalize.SCHEMA_VERSION,
            "detail": f"schema={evidence.get('schema_version')!r}",
        },
        {
            "check": "audited_by_the_common_schema",
            "ok": any("Unified Cell Evidence" in str(note) for note in outcome.get("notes") or []),
            "detail": f"verdict={outcome.get('verdict')!r}",
        },
        {
            "check": "session_record_preserved",
            "ok": bool((evidence.get("provenance") or {}).get("source_digest")),
            "detail": f"source={(evidence.get('provenance') or {}).get('source')!r}",
        },
        {
            "check": "independent_oracle_passed",
            "ok": oracle.get("status") == "PASS" and oracle.get("passed") is True,
            "detail": f"oracle={oracle.get('status')!r}",
        },
        {
            "check": "integrity_passed",
            "ok": (evidence.get("integrity") or {}).get("verdict") == normalize.PASS,
            "detail": f"integrity={(evidence.get('integrity') or {}).get('verdict')!r}",
        },
        {
            "check": "delivered_success",
            "ok": bool(correctness.get("delivered_success")) is True,
            "detail": (
                f"delivered={correctness.get('delivered_success')} "
                f"agent={correctness.get('agent_success')} "
                f"public={correctness.get('public_test')!r}"
            ),
        },
        {
            "check": "provider_answered",
            "ok": bool(requests),
            "detail": f"requests={len(requests)}",
        },
        {
            # Every agent must have consumed its *own* script. A shared run id once let a child
            # consume the Main's remaining steps, which ends the Main early and still looks like a
            # completed run.
            "check": "each_agent_used_its_own_script",
            "ok": {str(row.get("script")) for row in requests}
            == {
                "A1": {"a1-main"},
                "A3": {"a3-main", "a3-csv-unit", "a3-jsonl-unit"},
            }[arm_id],
            "detail": f"scripts={sorted({str(row.get('script')) for row in requests})}",
        },
        {
            "check": "cost_is_the_shared_formula",
            "ok": isinstance((outcome.get("cost_basis") or {}).get("cache_split"), str),
            "detail": f"basis={outcome.get('cost_basis')}",
        },
    ]
    if arm_id == "A1":
        checks.append(
            {
                "check": "no_delegation",
                "ok": not children and int(delegation.get("proposal_count") or 0) == 0,
                "detail": f"proposals={delegation.get('proposal_count')} children={len(children)}",
            }
        )
    else:
        checks.extend(
            [
                {
                    "check": "proposal_recorded",
                    "ok": delegation.get("natural_proposal") is True,
                    "detail": f"proposals={delegation.get('proposal_count')}",
                },
                {
                    "check": "two_children_completed",
                    "ok": sum(1 for child in children if child.get("status") == "completed") == 2,
                    "detail": f"children={[child.get('key') for child in children]}",
                },
                {
                    "check": "children_overlapped",
                    "ok": max((batch.get("max_observed_concurrency") or 0) for batch in batches)
                    >= 2
                    if batches
                    else False,
                    "detail": f"concurrency={[b.get('max_observed_concurrency') for b in batches]}",
                },
            ]
        )
    return checks


async def run(*, scratch: Path | None = None) -> dict[str, Any]:
    base = Path(scratch or tempfile.mkdtemp(prefix="stage-f-routing-"))
    await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
    task = tasks.task_by_id(TASK_ID)
    cells = smoke.cells_for(ARMS, plan_id=PLAN_ID, scope=SCOPE)
    plan_record = smoke.plan_for(
        cells, plan_id=PLAN_ID, scope=SCOPE, master_id="stage-f-default-routing-smoke"
    )
    run_state, prefixes = smoke._combined_run(task, cells)  # noqa: SLF001 - the smoke's builder
    provider_name = smoke._unique("stagef-routing")  # noqa: SLF001
    runner_config = executor.RunnerConfig(
        llm_factory=scripted.factory_for(run_state, provider_name)
    )

    started = time.perf_counter()
    with executor.claim_root_override(base / "claims"):
        report = await executor.dispatch(
            PLAN_ID,
            runs_root=base / "runs",
            evidence_root=base / "evidence",
            authority_path=smoke._authority_file(plan_record, base),  # noqa: SLF001
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
            proof._read_json(  # noqa: SLF001 - the same reader the compatibility smoke uses
                proof._cell_dir(base / "evidence", cell["order"]) / "evidence.json"  # noqa: SLF001
            )
            or {}
        )
        requests = smoke.arm_requests(run_state, prefixes[arm_id])
        checks = _checks(
            arm_id=arm_id,
            cell=cell,
            outcome=outcome,
            evidence=evidence,
            requests=requests,
        )
        failed = [item["check"] for item in checks if not item["ok"]]
        problems.extend(f"{arm_id}: {name}" for name in failed)
        arms_report.append(
            {
                "arm_id": arm_id,
                "cell_id": cell["cell_id"],
                "declared_execution_mode": cell["execution_mode"],
                "routing": {
                    "resolved_runner": getattr(
                        executor.cell_runner_for(cell), "execution_mode", "legacy_live"
                    ),
                    "cell_runner_injected": False,
                },
                "audit": {
                    "verdict": outcome.get("verdict"),
                    "notes": outcome.get("notes"),
                    "public_test_status": outcome.get("public_test_status"),
                    "delivered_success": outcome.get("delivered_success"),
                    "execution_integrity": outcome.get("execution_integrity"),
                    "cost_basis": outcome.get("cost_basis"),
                    "delta_cost_usd": outcome.get("delta_cost_usd"),
                },
                "evidence": {
                    "schema_version": evidence.get("schema_version"),
                    "correctness": evidence.get("correctness"),
                    "integrity": evidence.get("integrity"),
                    "delegation": evidence.get("delegation"),
                    "context": evidence.get("context"),
                    "usage": evidence.get("usage"),
                    "resources": evidence.get("resources"),
                },
                "requests": {
                    "count": len(requests),
                    "per_agent": sorted(
                        {str(row.get("trusted_task_id") or "unattributed") for row in requests}
                    ),
                },
                "checks": checks,
            }
        )

    identity = executor.executor_identity(REPO)
    counts = {
        "scripted_requests": len(run_state.requests),
        "external_model_requests": 0,
        "production_claims_consumed": 0,
    }
    ok = not problems and report.get("cells_passed") == len(cells)
    return {
        "stage": "F",
        "kind": "default_live_routing_smoke",
        "generated_at": executor.now(),
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "repo_head": executor.git_head(REPO),
        "task_id": TASK_ID,
        "plan": {
            "plan_id": PLAN_ID,
            "scope": SCOPE,
            "plan_digest": plan_record["digest"],
            "cells": [cell["cell_id"] for cell in cells],
            "mode": executor.MODE_OFFLINE_PROOF,
            "claim_root": "isolated offline claim (no production claim consumed)",
        },
        "provider": {
            "kind": "deterministic scripted provider (no model call)",
            "run": run_state.as_dict(),
        },
        "routing": {
            "cell_runner_injected": False,
            "provider_seam": "executor.RunnerConfig.llm_factory",
            "selection": "executor.cell_runner_for (declared execution_mode)",
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
        },
        "arms": arms_report,
        "counts": counts,
        "problems": problems,
        "ok": ok,
        "status": "STAGE_F_DEFAULT_ROUTING_SMOKE_PASS"
        if ok
        else "STAGE_F_DEFAULT_ROUTING_SMOKE_FAIL",
        "duration_s": duration,
        "scratch_root": str(base),
    }


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    output = (
        Path(args[0]) if args else Path(__file__).resolve().parent / "default-routing-smoke.json"
    )
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
