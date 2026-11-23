"""Per-cell execution orchestration and its offline-only CLI.

The live path is deliberately narrow: it needs preflight READY, byte-level identity verification,
an explicit cell-scoped authorization and two live flags. A failed cell keeps its marker and its
evidence. Nothing in this module retries a provider call.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .. import protocol
from .. import run_stage_b as stage_runner
from ..tasks import BY_ID
from . import adapter, admission, collector, network, provision
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


def _views(
    session: dict[str, Any], collection: dict[str, Any] | None, host_oracle: Any
) -> dict[str, Any]:
    """Return the versioned view. Delegation is diagnostic here, never a verdict input."""

    analysis = (collection or {}).get("analysis") or {}
    record = analysis.get("record") or {}
    view = analysis.get("execution_view") or record.get("execution_view")
    if view is None:
        # A degraded analysis has no record; the raw session still carries the real failure.
        view = adapter.build_view(record or session, oracle=host_oracle, gaps=[])
    return view


def _integrity_kind(session: dict[str, Any]) -> str | None:
    """Stop reasons and sandbox lifecycle. Never reads the analyser, so it cannot be broken."""

    if not isinstance(session, dict) or not session:
        return "provider_exception"
    main = session.get("main") or {}
    stop = str(main.get("stop_reason") or "")
    budget = session.get("budget") or {}
    if stop == "max_turns":
        return "main_max_turns"
    if stop == "budget_exceeded" or budget.get("rejection_code") == "budget_exhausted":
        return "budget_exhaustion"
    if (session.get("execution_integrity") or {}).get("sandbox_failure"):
        return "sandbox_cleanup"
    sandbox = session.get("sandbox") or {}
    failure_text = session.get("failure") if isinstance(session.get("failure"), str) else ""
    if (
        session.get("status") == "failed"
        and sandbox.get("provider_is_real") is True
        and sandbox.get("created") is False
        and "sandbox" in failure_text.lower()
    ):
        return "sandbox_creation"
    return None


def _terminal_kind(
    session: dict[str, Any], collection: dict[str, Any] | None, *, host_oracle: Any = None
) -> str:
    """Execution integrity first, then goal correctness. Delegation never sets the outcome."""

    integrity = _integrity_kind(session)
    if integrity:
        return integrity
    if not isinstance(session, dict) or not session:
        return "provider_exception"
    if collection is not None and not collection.get("ok"):
        return "collector_failure"

    view = _views(session, collection, host_oracle)["goal_correctness"]
    if view["status"] == "FAIL":
        return "oracle_failed" if view["host_oracle"] == "FAIL" else "public_tests_failed"
    if view["status"] == "UNKNOWN":
        return "goal_unknown"
    if session.get("status") != "succeeded":
        return "provider_exception"
    return "none"


async def execute_cell(request: CellRequest) -> CellResult:
    """Run one cell once. Provider calls happen only after the claim is durable."""

    document = dict(request.protocol_document or protocol.read_protocol())
    # The sandbox host must bypass the Windows VPN proxy before any runtime client is built.
    network.apply_proxy_bypass()
    source = admission.source_identity()
    preflight = admission.preflight(
        evidence_root=request.evidence_root,
        cell=request.cell,
        live=request.live,
        protocol_document=document,
        require_offline_report=request.live,
        probe_sandbox=request.live,
        sandbox_probe=request.sandbox_probe,
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
    # The claim itself is durable; a failed state write must not abort the attempt.
    with contextlib.suppress(BaseException):
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
    secondary: list[str] = []
    oracle: dict[str, Any] | None = None
    view: dict[str, Any] = {}
    goal_view: dict[str, Any] = {}

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

        # An integrity failure is decided before collection: a crashed collector must not be able
        # to replace "the sandbox never started" with "the collector failed".
        integrity_failure = _integrity_kind(session) if failure == "none" else None
        if integrity_failure:
            failure = integrity_failure

        run_dir = runs_dir / options.run_id if options is not None else runs_dir
        try:
            exported = (session.get("artifacts") or {}).get("workspace_path")
            oracle = (
                dict(request.host_oracle) if (request.host_oracle and not request.live) else None
            )
            if request.live and exported and await asyncio.to_thread(os.path.isdir, exported):
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
            gaps.extend(
                f"not applicable: {name}" for name in collection.get("not_applicable") or ()
            )
        except BaseException as error:
            secondary.append(f"collector_failure: {type(error).__name__}: {error}")
            if failure == "none":
                failure = "collector_failure"
            detail = detail or f"{type(error).__name__}: {error}"

        try:
            view = _views(session, collection, oracle)
            goal_view = view["goal_correctness"]
            if failure == "none":
                failure = _terminal_kind(session, collection, host_oracle=oracle)
        except BaseException as error:
            secondary.append(f"aggregation_failure: {type(error).__name__}: {error}")
            if failure == "none":
                failure = "aggregation_failure"
            detail = detail or f"{type(error).__name__}: {error}"
    except BaseException as error:
        # A fault outside the guarded stages. The primary failure, if one already exists, wins.
        secondary.append(f"aggregation_failure: {type(error).__name__}: {error}")
        if failure == "none":
            failure = "aggregation_failure"
        detail = detail or f"{type(error).__name__}: {error}"
    finally:
        # The marker must reach a terminal state even when collection or analysis failed. A marker
        # write error is a diagnostic, never a replacement for the primary failure.
        state = "COLLECTED" if failure == "none" else "FAILED"
        try:
            admission.mark_attempt(cell_root, state)
        except BaseException as error:
            secondary.append(f"marker_update_failure: {type(error).__name__}: {error}")

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
        execution_integrity="OK"
        if failure in {"none", "goal_unknown", "oracle_failed", "public_tests_failed"}
        else "FAILED",
        goal_correctness=str(goal_view.get("status", "UNKNOWN")),
        delegation_diagnostics=str(
            (view.get("delegation_diagnostics") or {}).get("status", "NOT_APPLICABLE")
        ),
        oracle_agreement=str(goal_view.get("oracle_agreement", "UNKNOWN")),
        host_oracle_source=str(goal_view.get("authoritative_source", "UNKNOWN")),
        secondary_failures=tuple(secondary),
    )
    _write(result_path, result.to_dict())
    return result


def _run_goal():  # pragma: no cover - the one true live composition
    from atlasos.app.main_driven import run_goal

    return run_goal


def recover(
    cell_id: str,
    *,
    evidence_root: Path,
    protocol_document: Any = None,
    calibration: bool = False,
) -> dict[str, Any]:
    """CLI-facing read-only recovery. It never reaches a provider or a sandbox."""

    document = dict(protocol_document or protocol.read_protocol())
    cell = (
        provision.calibration_cell() if calibration else provision.cell_from_plan(document, cell_id)
    )
    if calibration:
        evidence_root = evidence_root / "calibration"
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
    group.add_argument("--execute-calibration", action="store_true")
    group.add_argument("--sandbox-smoke", action="store_true")
    group.add_argument("--recover-evidence", metavar="CELL_ID")
    group.add_argument("--recover-calibration", action="store_true")
    parser.add_argument("--runs-root", type=Path, default=Path.cwd() / ".stage-b-runs")
    parser.add_argument("--evidence-root", type=Path, default=Path.cwd() / ".stage-b-evidence")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument(
        "--sandbox-endpoint",
        metavar="HOST:PORT",
        help="set OPEN_SANDBOX_ENDPOINT for this process; the host is added to NO_PROXY",
    )
    parser.add_argument("--smoke-authorization", type=Path)
    args = parser.parse_args(argv)
    if args.sandbox_endpoint:
        os.environ["OPEN_SANDBOX_ENDPOINT"] = args.sandbox_endpoint
    network.apply_proxy_bypass()

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

    if args.sandbox_smoke:
        from .smoke import SmokeRequest, run_smoke

        if not args.smoke_authorization or not args.smoke_authorization.is_file():
            print(json.dumps({"status": "BLOCKED", "reason": "SMOKE_AUTHORIZATION_REQUIRED"}))
            return 3
        authorization = json.loads(args.smoke_authorization.read_text(encoding="utf-8"))
        smoke = asyncio.run(
            run_smoke(
                SmokeRequest(
                    evidence_dir=args.evidence_root / "sandbox-smoke",
                    authorization=authorization,
                    endpoint=args.sandbox_endpoint or os.environ.get("OPEN_SANDBOX_ENDPOINT"),
                )
            )
        )
        print(
            json.dumps(
                {
                    "status": smoke.get("status"),
                    "verdict": smoke.get("verdict"),
                    "model_requests": smoke.get("model_requests"),
                    "calibration_attempts": smoke.get("calibration_attempts"),
                    "checks": smoke.get("checks"),
                }
            )
        )
        return 0 if smoke.get("verdict") == "SANDBOX_SMOKE_PASS" else 1

    if args.execute_cell or args.execute_calibration:
        if not (args.live and args.confirm_live and args.authorization):
            print(
                json.dumps(
                    {"status": "BLOCKED", "reason": "LIVE_INTENT_AND_AUTHORIZATION_REQUIRED"}
                )
            )
            return 3
        authorization = json.loads(args.authorization.read_text(encoding="utf-8"))
        document = protocol.read_protocol()
        calibration = bool(args.execute_calibration)
        cell = (
            provision.calibration_cell()
            if calibration
            else provision.cell_from_plan(document, args.execute_cell)
        )
        evidence_root = args.evidence_root / "calibration" if calibration else args.evidence_root
        request = CellRequest(
            cell=cell,
            runs_root=args.runs_root,
            evidence_root=evidence_root,
            live=True,
            calibration=calibration,
            authorization=authorization,
            authorization_digest=admission.canonical_digest(authorization),
            protocol_document=document,
        )
        result = asyncio.run(execute_cell(request))
        _write(evidence_root / "last-result.json", result.to_dict())
        print(json.dumps(result.to_dict()))
        return 0 if result.ok else 1

    if args.recover_evidence or args.recover_calibration:
        result = recover(
            args.recover_evidence or provision.calibration_cell().cell_id,
            evidence_root=args.evidence_root,
            calibration=bool(args.recover_calibration),
        )
        print(json.dumps({"status": result["status"], "ok": result["ok"]}))
        return 0 if result["ok"] else 2

    report = admission.read_json(admission.OFFLINE_PATH) if admission.OFFLINE_PATH.is_file() else {}
    result = admission.preflight(
        evidence_root=args.evidence_root,
        live=bool(args.live),
        offline_report=report,
        require_offline_report=bool(args.live),
        # A real health probe through the runtime client. It creates no sandbox and calls no model.
        probe_sandbox=True,
    )
    admission.write_preflight(result)
    print(json.dumps(result))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
