"""Per-cell execution orchestration and its offline-only CLI.

The live path is deliberately narrow: it needs preflight READY, byte-level identity verification,
an explicit cell-scoped authorization and two live flags. A failed cell keeps its marker and its
evidence. Nothing in this module retries a provider call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .. import protocol
from .. import run_stage_b as stage_runner
from ..tasks import BY_ID
from . import admission, collector, provision
from .models import (
    CellRequest,
    CellResult,
    ExecutionBlocked,
)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def _exception_kind(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}".lower()
    if isinstance(error, (KeyError, TypeError, AttributeError)):
        return "aggregation_failure"
    if "budget" in text or "runbudgetexhausted" in text:
        return "budget_exhaustion"
    if "sandbox" in text and (
        "start" in text or "create" in text or "health" in text or "lost" in text
    ):
        return "sandbox_creation"
    if "sandbox" in text and ("close" in text or "cleanup" in text or "destroy" in text):
        return "sandbox_cleanup"
    if "collect" in text or "evidence" in text:
        return "collector_failure"
    return "provider_exception"


def _terminal_kind(session: dict[str, Any], collection: dict[str, Any] | None) -> str:
    """Name the outcome without turning an absent signal into a success."""

    if not isinstance(session, dict) or not session:
        return "provider_exception"
    main = session.get("main") or {}
    stop = str(main.get("stop_reason") or "")
    budget = session.get("budget") or {}
    if stop == "max_turns":
        return "main_max_turns"
    if stop == "budget_exceeded" or budget.get("rejection_code") == "budget_exhausted":
        return "budget_exhaustion"
    if collection is not None and not collection.get("ok"):
        return "collector_failure"
    analysis = (collection or {}).get("analysis", {}).get("record", {})
    verification = analysis.get("verification") or {}
    propagation = verification.get("public_test_statuses") or []
    if propagation and propagation[-1] == "FAIL":
        return "public_tests_failed"
    if verification.get("oracle") == "FAIL":
        return "oracle_failed"
    delegation = analysis.get("delegation") or {}
    admission = delegation.get("admission") or {}
    proposed = int(admission.get("proposals") or delegation.get("proposals") or 0)
    if proposed and not (admission.get("admitted") or 0) and (admission.get("rejected") or 0):
        return "router_rejected"
    admitted = int(delegation.get("children_admitted") or 0)
    succeeded = int(delegation.get("children_succeeded") or 0)
    if admitted and succeeded < admitted:
        return "child_failure"
    if session.get("status") != "succeeded":
        return "provider_exception"
    if proposed and not (analysis.get("handoff") or {}).get("delivered"):
        return "handoff_missing"
    return "none"


async def execute_cell(request: CellRequest) -> CellResult:
    """Run one cell once. Provider calls happen only after the claim is durable."""

    document = dict(request.protocol_document or protocol.read_protocol())
    source = admission.source_identity()
    preflight = admission.preflight(
        evidence_root=request.evidence_root,
        cell=request.cell,
        live=request.live,
        protocol_document=document,
        require_offline_report=request.live,
    )
    if preflight["status"] != "READY":
        raise ExecutionBlocked("preflight blocked: " + "; ".join(preflight["problems"]))
    runtime = stage_runner.source_identity()
    identity_problems = provision.verify_identity(
        request.cell,
        document=document,
        runtime_digest=runtime["tree_digest"],
        plan_digest=preflight["plan_digest"],
    )
    if identity_problems:
        raise ExecutionBlocked("cell identity drift: " + "; ".join(identity_problems))

    gate: dict[str, Any] = {"ready": not request.live, "digest": "OFFLINE", "problems": []}
    if request.live:
        gate = admission.authorization_gate(
            request.authorization,
            cell=request.cell,
            preflight_result=preflight,
            live=True,
        )
        if not gate["ready"]:
            raise ExecutionBlocked("not authorized: " + "; ".join(gate["problems"]))

    cell_root = admission.cell_dir(request.evidence_root, request.cell)
    attempt = admission.claim_attempt(
        cell_root,
        cell=request.cell,
        preflight_digest=preflight["digest"],
        authorization_digest=gate["digest"],
        source_digest=source["digest"],
    )
    admission.mark_attempt(cell_root, "RUNNING")
    bundle = admission.evidence_dir(cell_root)
    run_root = request.runs_root / request.cell.cell_id / f"r{request.cell.repetition}"
    fixture_root = run_root / "fixture"
    runs_dir = run_root / "runs"
    result_path = cell_root / "result.json"
    collection: dict[str, Any] | None = None
    session: dict[str, Any] = {}
    options: Any = None
    failure = "none"
    detail = ""
    gaps: list[str] = []

    try:
        try:
            if request.cell.in_matrix:
                fixture = provision.materialize_fixture(fixture_root, BY_ID[request.cell.task_id])
            else:
                fixture = provision.materialize_calibration_fixture(fixture_root)
            options = provision.build_options(
                cell=request.cell,
                fixture_repo=fixture,
                runs_root=runs_dir,
                run_id=f"stage-b-{request.cell.cell_id}",
                document=document,
                provider_factory=request.provider_factory,
                sandbox_factory=request.sandbox_factory,
                offline=not request.live,
            )
            runner = request.run_fn or _run_goal()
            session = await runner(options)
        except BaseException as error:  # one cell, one attempt: no retry on any exception
            failure = _exception_kind(error)
            detail = f"{type(error).__name__}: {error}"
            session = session or {}

        run_dir = runs_dir / options.run_id if options is not None else runs_dir
        try:
            exported = (session.get("artifacts") or {}).get("workspace_path")
            oracle = None
            if (
                request.cell.task_id in BY_ID
                and exported
                and await asyncio.to_thread(os.path.isdir, exported)
            ):
                oracle = provision.host_oracle(Path(exported), request.cell.task_id)
            collection = collector.collect(
                run_dir=run_dir,
                bundle=bundle,
                cell=request.cell,
                attempt_path=attempt.path,
                source_digest=source["digest"],
                host_oracle=oracle,
                document=document,
            )
            gaps = list(collection["evidence_gaps"])
            if not collection["ok"]:
                gaps.extend(f"missing: {name}" for name in collection["missing"])
        except BaseException as error:
            failure = "collector_failure" if failure == "none" else failure
            detail = detail or f"{type(error).__name__}: {error}"

        if failure == "none":
            failure = _terminal_kind(session, collection)
        state = "COLLECTED" if failure == "none" else "FAILED"
        admission.mark_attempt(cell_root, state)
    except BaseException as error:
        failure = "aggregation_failure"
        detail = detail or f"{type(error).__name__}: {error}"

    result = CellResult(
        cell=request.cell,
        ok=failure == "none",
        status="COMPLETED" if failure == "none" else "FAILED",
        failure_kind=failure,
        detail=detail,
        evidence_dir=bundle if bundle.is_dir() else None,
        analysis_path=bundle / collector.ANALYSIS
        if (bundle / collector.ANALYSIS).is_file()
        else None,
        gaps=tuple(gaps),
        digests={"runner_source": source["digest"], "preflight": preflight["digest"]},
    )
    _write(result_path, result.to_dict())
    return result


def _run_goal():  # pragma: no cover - the one true live composition
    from atlasos.app.main_driven import run_goal

    return run_goal


def recover(cell_id: str, *, evidence_root: Path, protocol_document: Any = None) -> dict[str, Any]:
    """CLI-facing read-only recovery. It never reaches a provider or a sandbox."""

    document = dict(protocol_document or protocol.read_protocol())
    cell = provision.cell_from_plan(document, cell_id)
    cell_root = admission.cell_dir(evidence_root, cell)
    bundle = admission.evidence_dir(cell_root)
    result = collector.recover_evidence(
        bundle, cell=cell, source_digest=admission.source_identity()["digest"]
    )
    _write(cell_root / "recovery.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage B per-cell execution harness (offline safe)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument(
        "--offline", action="store_true", help="run the fake-provider suite; no model"
    )
    group.add_argument("--freeze", action="store_true")
    group.add_argument("--execute-cell", metavar="CELL_ID")
    group.add_argument("--recover-evidence", metavar="CELL_ID")
    parser.add_argument("--runs-root", type=Path, default=Path.cwd() / ".stage-b-runs")
    parser.add_argument("--evidence-root", type=Path, default=Path.cwd() / ".stage-b-evidence")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--authorization", type=Path)
    args = parser.parse_args(argv)

    if args.offline:
        from .offline import run_all

        report = run_all(args.runs_root / "offline")
        path = args.runs_root / "offline" / "offline.json"
        _write(path, report)
        _write(admission.OFFLINE_PATH, report)
        print(json.dumps({"status": "READY" if report["passed"] else "BLOCKED", "path": str(path)}))
        return 0 if report["passed"] else 2

    if args.freeze:
        report = (
            admission.read_json(admission.OFFLINE_PATH) if admission.OFFLINE_PATH.is_file() else {}
        )
        if not report.get("passed"):
            print(json.dumps({"status": "BLOCKED", "reason": "OFFLINE_SUITE_REQUIRED"}))
            return 2
        check = admission.preflight(
            evidence_root=args.evidence_root, offline_report=report, require_offline_report=False
        )
        if check["status"] != "READY":
            print(json.dumps(check))
            return 2
        frozen = admission.write_freeze(offline_report=report, preflight_result=check)
        print(
            json.dumps(
                {
                    "status": "FROZEN",
                    "path": str(admission.FREEZE_PATH),
                    "source_digest": frozen["source"]["digest"],
                }
            )
        )
        return 0

    if args.execute_cell:
        if not (args.live and args.confirm_live and args.authorization):
            print(
                json.dumps(
                    {"status": "BLOCKED", "reason": "LIVE_INTENT_AND_AUTHORIZATION_REQUIRED"}
                )
            )
            return 3
        authorization = json.loads(args.authorization.read_text(encoding="utf-8"))
        document = protocol.read_protocol()
        cell = provision.cell_from_plan(document, args.execute_cell)
        request = CellRequest(
            cell=cell,
            runs_root=args.runs_root,
            evidence_root=args.evidence_root,
            live=True,
            authorization=authorization,
            authorization_digest=admission.canonical_digest(authorization),
            protocol_document=document,
        )
        import asyncio

        result = asyncio.run(execute_cell(request))
        _write(args.evidence_root / "last-result.json", result.to_dict())
        print(json.dumps(result.to_dict()))
        return 0 if result.ok else 1

    if args.recover_evidence:
        result = recover(args.recover_evidence, evidence_root=args.evidence_root)
        print(json.dumps({"status": result["status"], "ok": result["ok"]}))
        return 0 if result["ok"] else 2

    report = admission.read_json(admission.OFFLINE_PATH) if admission.OFFLINE_PATH.is_file() else {}
    result = admission.preflight(
        evidence_root=args.evidence_root,
        live=bool(args.live),
        offline_report=report,
        require_offline_report=bool(args.live),
    )
    admission.write_preflight(result)
    print(json.dumps(result))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
