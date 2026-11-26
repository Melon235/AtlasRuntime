"""Stage F — the four-arm real runner proof.

The goal is one thing: make each of the four execution arms produce a *real* run — real dispatch,
real entry point, real sandbox, real tool execution, real export, real independent oracle, real
audit, real ledger — with the provider replaced by a deterministic script and the substitution
recorded everywhere it matters.

What is not used, by construction:

* no pre-built ``CellRun``: every cell goes through ``executor.dispatch`` →
  ``executor.cell_runner_for`` → the arm's own entry;
* no hand-built normaliser input: the adapters read the producer's own serialisation
  (``EvalRunResult.as_dict()``, ``session.json``, ``result.json``);
* no mocked audit or settlement: ``audit_cell`` computes usage and cost with the executor's
  functions from the run's own counters.

Run with::

    python -m experiments.stage_f.proof --all              # four arms
    python -m experiments.stage_f.proof --arm A0           # one arm
    python -m experiments.stage_f.proof --negatives        # controlled failure cases

Raw material is written outside the repository (``%TEMP%/atlas-stage-f-proof/<stamp>``) and the
sanitised report to ``experiments/stage_f/runner-proof.json``. Nothing here calls a model, spends a
production claim or signs anything: the claim is an isolated offline claim under a scratch root.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import arms, normalize, scripted, tasks
from experiments.stage_f import plan as f_plan
from experiments.stage_f import runners as f_runners

REPO_ROOT = Path(__file__).resolve().parents[2]

PROOF_PLAN_ID = "stage-f-runner-proof-v1"
PROOF_SCOPE = "F_RUNNER_PROOF"
#: Execution order is the Owner's: native, static DAG, direct-only, dynamic.
ARM_SEQUENCE: tuple[str, ...] = ("A0", "A2", "A1", "A3")
#: One task for all four arms: the same goal makes the four architectures comparable, and P1 is the
#: only Stage F task with two genuinely disjoint implementations plus a real integration step.
DEFAULT_TASK = "f-p1-connector-migration"
REPORT_PATH = REPO_ROOT / "experiments" / "stage_f" / "runner-proof.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _windows_long_paths() -> None:
    """Let the child ``git`` processes stage a fixture below MAX_PATH on this host.

    The frozen fixture stager runs real ``git`` commands, and a staged repository under a run
    directory reaches ~260 characters on Windows. ``core.longpaths`` is supplied through git's
    environment configuration for this process only: no repository, global or system config is
    changed, and no tracked file is affected.
    """

    if os.name != "nt":
        return
    if os.environ.get("GIT_CONFIG_COUNT"):
        return
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "core.longpaths"
    os.environ["GIT_CONFIG_VALUE_0"] = "true"


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", "utf-8")
    return path


def _read_json(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# -- the temporary plan ------------------------------------------------------------------------


def proof_cells(task_id: str) -> list[dict[str, Any]]:
    """The four cells, in execution order, each built by the frozen cell constructor."""

    return [
        arms.cell_record(
            task_id=task_id,
            arm_id=arm_id,
            repetition=1,
            sequence="Q1",
            position=index,
            batch_id=PROOF_PLAN_ID,
            plan_id=PROOF_PLAN_ID,
            scope=PROOF_SCOPE,
        )
        for index, arm_id in enumerate(ARM_SEQUENCE, start=1)
    ]


def proof_plan(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """A plan record that is a real plan to the executor, and a proof to everyone else."""

    return {
        "stage": "F",
        "design_version": f_plan.DESIGN_VERSION,
        "master_id": "stage-f-runner-proof",
        "plan_id": PROOF_PLAN_ID,
        "scope": PROOF_SCOPE,
        "batch": 0,
        "repetition": 1,
        "repetitions": 1,
        "tasks": sorted({str(cell["task_id"]) for cell in cells}),
        "arms": [str(cell["arm_id"]) for cell in cells],
        "cells_count": len(cells),
        "cells": cells,
        "digest": f_plan.plan_digest(cells),
        "claim_namespace": f"stage-f-claim/{PROOF_PLAN_ID}",
        "runs_subdir": f"runs/{PROOF_PLAN_ID}",
        "evidence_subdir": f"evidence/{PROOF_PLAN_ID}",
        "is_benchmark_sample": False,
        "attempt_limit": 1,
        "budget": arms.budget(),
        "budget_digest": arms.budget_digest(),
        "nominal_token_capacity": len(cells) * int(arms.CELL_BUDGET["token_budget"]),
        "cost_stop_usd": 1.0,
        "per_cell_cost_stop_usd": 0.5,
        "token_stop": len(cells) * int(arms.CELL_BUDGET["token_budget"]),
        "execution_contract": contract.EXECUTION_CONTRACT_DIGEST,
        "retry": "FORBIDDEN",
        "is_proof": True,
    }


# -- the scripts -------------------------------------------------------------------------------


def _writes(task: Any, paths: tuple[str, ...]) -> list[scripted.ScriptStep]:
    return [("file_write", {"path": path, "content": task.reference[path]}) for path in paths]


def _checks() -> list[scripted.ScriptStep]:
    """One read/list/grep/compile round trip after the writes, through the real tool executor."""

    return [
        ("list_files", {"path": "."}),
        ("bash", {"command": "python3 -m compileall -q . && echo COMPILE_OK"}),
    ]


def single_agent_script(task: Any) -> list[scripted.ScriptStep]:
    """A0: one agent writes every deliverable of the task itself."""

    return [*_writes(task, tuple(sorted(task.reference))), *_checks()]


def dag_scripts(task: Any) -> dict[str, list[scripted.ScriptStep]]:
    """A2: one script per DAG node, keyed by a marker unique to that node's assignment."""

    scripts: dict[str, list[scripted.ScriptStep]] = {}
    for worker in task.workers:
        node_id = str(worker["task_id"])
        scripts[node_id] = [
            *_writes(task, tuple(str(path) for path in worker["owned_paths"])),
            *_checks(),
        ]
    return scripts


def node_markers(task: Any) -> dict[str, str]:
    """A marker per node: the first line of its own assignment, which the shared header repeats."""

    return {
        str(worker["task_id"]): str(worker["assignment"]).splitlines()[0] for worker in task.workers
    }


def main_direct_script(task: Any) -> list[scripted.ScriptStep]:
    """A1: the Main does the whole task itself, then runs the public suite."""

    return [
        *_writes(task, tuple(sorted(task.reference))),
        *_checks(),
        ("run_public_tests", {"reason": "verify the delivered modules against the public suite"}),
    ]


def delegation_proposal(task: Any) -> dict[str, Any]:
    """The Main's real ``delegate_tasks`` input: two independent, granted, writable subtasks.

    Both are coding workers; their write paths are disjoint and inside the Host grant the cell
    carries, and neither declares a dependency because one call is one parallel wave.
    """

    tasks_by_key = {str(worker["task_id"]): worker for worker in task.workers}
    csv = tasks_by_key["csv"]
    jsonl = tasks_by_key["jsonl"]
    return {
        "tasks": [
            {
                "key": "csv-source",
                "objective": str(csv["assignment"]),
                "background": (
                    "The project is at /workspace/project and `docs/INTERFACE.md` is frozen. "
                    "Edit only `sources/csv_source.py`; the public suite is `test_public.py`."
                ),
                "profile_id": "coding_worker",
                "write_paths": list(csv["owned_paths"]),
            },
            {
                "key": "jsonl-source",
                "objective": str(jsonl["assignment"]),
                "background": (
                    "The project is at /workspace/project and `docs/INTERFACE.md` is frozen. "
                    "Edit only `sources/jsonl_source.py`; the public suite is `test_public.py`."
                ),
                "profile_id": "coding_worker",
                "write_paths": list(jsonl["owned_paths"]),
            },
        ],
        "reason": (
            "two independent connector implementations with disjoint write scopes; the shared "
            "merge and registry stay in the Main"
        ),
    }


def main_delegating_script(task: Any) -> list[scripted.ScriptStep]:
    """A3: propose two children, integrate their scope yourself, then run the public suite."""

    integration = next(worker for worker in task.workers if str(worker["task_id"]) == "integrate")
    return [
        ("delegate_tasks", delegation_proposal(task)),
        *_writes(task, tuple(str(path) for path in integration["owned_paths"])),
        *_checks(),
        (
            "run_public_tests",
            {"reason": "verify the integrated workspace against the public suite"},
        ),
    ]


# -- provider registration ---------------------------------------------------------------------


def _unique(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000}-{os.getpid()}"


def _provider_for(arm_id: str, task: Any, *, budget_only: bool = False) -> scripted.ScriptedRun:
    """One scripted provider run, scripted for this arm.

    ``budget_only`` is the guard negative: a script that always has another step, so the only way
    the loop ends is the shared budget refusing the next request.
    """

    if budget_only:
        run = scripted.ScriptedRun(
            scripts={"main": [("bash", {"command": "echo step"})] * 64},
            markers={"main": "ingest layer"},
            default_marker="main",
            model_name="scripted-stage-f-budget",
        )
    elif arm_id == "A0":
        run = scripted.ScriptedRun(
            scripts={"main": single_agent_script(task)},
            markers={"main": "You are the only worker on this task"},
            default_marker="main",
        )
    elif arm_id == "A2":
        run = scripted.ScriptedRun(
            scripts=dag_scripts(task),
            markers=node_markers(task),
            final_text="node implemented and compiled",
        )
    elif arm_id == "A1":
        run = scripted.ScriptedRun(
            scripts={"main": main_direct_script(task)},
            markers={"main": task.prompt[:60]},
            final_text="implemented all modules, public suite run",
        )
    else:
        proposal = delegation_proposal(task)
        scripts: dict[str, list[scripted.ScriptStep]] = {
            "main": main_delegating_script(task),
        }
        markers: dict[str, str] = {"main": task.prompt[:60]}
        for spec in proposal["tasks"]:
            root = str(spec["write_paths"][0])
            scripts[root] = [*_writes(task, (root,)), *_checks()]
            markers[root] = str(spec["objective"]).splitlines()[0]
        run = scripted.ScriptedRun(
            scripts=scripts,
            markers=markers,
            final_text="delegated two connectors, integrated and verified",
        )
    run.requests.clear()
    return run


# -- the composed cell runner ------------------------------------------------------------------


def proof_runner(arm_id: str, task: Any, run: scripted.ScriptedRun) -> executor.CellRunner:
    """The arm's real runner with the deterministic provider injected.

    ``executor.cell_runner_for`` is consulted first and its verdict has to agree with the arm the
    proof is building for: that is what proves the routing function still sends this execution mode
    to this entry point. Only the provider is supplied by the proof.
    """

    provider_name = f"stagef-{arm_id.lower()}-{_unique('p')}"
    factory = scripted.factory_for(run, provider_name)
    goal_runner = f_runners.main_driven_cell_runner(
        provider_name=provider_name, provider_factory=factory, scripted_run=run
    )
    injected = {
        "native": f_runners.native_cell_runner(provider_factory=factory, scripted_run=run),
        "atlas_static_dag": f_runners.static_dag_cell_runner(
            provider_factory=factory, scripted_run=run
        ),
        "atlas_direct": goal_runner,
        "atlas_dynamic": goal_runner,
    }

    async def run_cell(cell: dict[str, Any], context: Any) -> executor.CellRun:
        routed = executor.cell_runner_for(cell, fallback=goal_runner)
        mode = str(cell.get("execution_mode"))
        if mode in executor.MAIN_DRIVEN_MODES:
            if routed is not goal_runner:
                raise RuntimeError(f"{mode}: cell_runner_for did not return the goal runner")
        elif routed is goal_runner:
            raise RuntimeError(f"{mode}: cell_runner_for routed a non-goal arm to the goal runner")
        return await injected[mode](cell, context)

    run_cell.__name__ = f"proof_runner_{arm_id}"
    run_cell.provider_name = provider_name  # type: ignore[attr-defined]
    return run_cell


# -- the dispatch ------------------------------------------------------------------------------


def _signed_authority(plan: dict[str, Any], scratch: Path, head: str) -> Path:
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    pending = authority.pending_record(plan, protocol_digest_value=protocol, executor=identity)
    statement = scratch / "proof-owner-statement.json"
    body = {
        "statement": "offline runner proof, not an approval of any live run",
        "scope": PROOF_SCOPE,
    }
    statement.parent.mkdir(parents=True, exist_ok=True)
    statement.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    provenance = {
        "kind": "external_file",
        "path": str(statement),
        "sha256": normalize.sha256_bytes(statement.read_bytes()),
        "content_digest": authority.digest_of(body),
        "body": body,
    }
    signed = authority.sign(
        pending,
        approved_by="offline-runner-proof",
        approved_at=_now(),
        owner_statement="offline control-flow proof, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=float(plan["cost_stop_usd"]),
        per_cell_cost_stop_usd=float(plan["per_cell_cost_stop_usd"]),
        provenance=provenance,
    )
    path = scratch / "proof-authority.json"
    _write_json(path, signed)
    return path


async def run_dispatch(
    *, scratch: Path, plan: dict[str, Any], cells: list[dict[str, Any]], task: Any
) -> tuple[dict[str, Any], dict[str, scripted.ScriptedRun]]:
    """Dispatch the proof plan once, through the production entry point."""

    head = executor.git_head(REPO_ROOT)
    authority_path = _signed_authority(plan, scratch, head)
    runs: dict[str, scripted.ScriptedRun] = {}
    runners: dict[str, executor.CellRunner] = {}
    for cell in cells:
        arm_id = str(cell["arm_id"])
        scripted_run = _provider_for(arm_id, task)
        runs[arm_id] = scripted_run
        runners[arm_id] = proof_runner(arm_id, task, scripted_run)

    async def composed(cell: dict[str, Any], context: Any) -> executor.CellRun:
        return await runners[str(cell["arm_id"])](cell, context)

    composed.is_live = False  # type: ignore[attr-defined]
    with executor.claim_root_override(scratch / "claims"):
        report = await executor.dispatch(
            PROOF_PLAN_ID,
            runs_root=scratch / "runs",
            evidence_root=scratch / "evidence",
            authority_path=authority_path,
            cell_runner=composed,
            repo_root=REPO_ROOT,
            mode=executor.MODE_OFFLINE_PROOF,
            head_override=head,
            plan=plan,
        )
    return report, runs


# -- per-arm reporting -------------------------------------------------------------------------


def _cell_dir(evidence_root: Path, order: int) -> Path:
    return evidence_root / "cells" / f"c{int(order):02d}"


def arm_report(
    *,
    arm_id: str,
    cell: dict[str, Any],
    outcome: dict[str, Any],
    evidence_root: Path,
    runs_root: Path,
    scripted_run: scripted.ScriptedRun,
) -> dict[str, Any]:
    """One arm's evidence, read from the files the run actually produced."""

    root = _cell_dir(evidence_root, cell["order"])
    evidence = _read_json(root / "evidence.json") or {}
    analysis = _read_json(root / "analysis.json") or {}
    raw_dir = _raw_result_dir(root, evidence)
    raw = _read_json(raw_dir["file"]) if raw_dir["file"] else None
    resources = evidence.get("resources") or {}
    correctness = evidence.get("correctness") or {}
    oracle = correctness.get("independent_oracle") or {}
    return {
        "arm_id": arm_id,
        "cell_id": cell["cell_id"],
        "task_id": cell["task_id"],
        "execution_mode": cell["execution_mode"],
        "execution_entry": _ENTRY_POINTS[arm_id],
        "effective_config": {
            "max_turns": cell.get("max_turns"),
            "worker_max_turns": cell.get("worker_max_turns"),
            "integration_max_turns": cell.get("integration_max_turns"),
            "token_budget": cell.get("token_budget"),
            "delegation": cell.get("delegation"),
            "worker_profiles": cell.get("worker_profiles"),
            "worker_exposure": cell.get("worker_exposure"),
            "provider": "scripted (deterministic, no model call)",
            "budget_mode": "guarded",
        },
        "provider": scripted_run.as_dict(),
        "container": {
            "agent": _agent_container(resources, raw),
            "oracle": _oracle_container(analysis, raw),
            "created_and_destroyed": _containers(resources, raw),
        },
        "raw_result": raw_dir,
        "workspace_export": {
            "path": resources.get("artifact_export"),
            "patch_digest": raw.get("patch_digest") if isinstance(raw, dict) else None,
            "analysis_export": analysis.get("export"),
        },
        "oracle": {
            **oracle,
            "record": analysis.get("oracle"),
            "sandbox_id": _oracle_sandbox_id(analysis, raw),
        },
        "normalized": {
            "usage": evidence.get("usage"),
            "context": evidence.get("context"),
            "correctness": correctness,
            "delegation": evidence.get("delegation"),
            "handoff": evidence.get("handoff"),
            "workers": evidence.get("workers"),
            "adapter_problems": evidence.get("adapter_problems"),
        },
        "audit": {
            "verdict": outcome.get("verdict"),
            "checks": outcome.get("checks"),
            "stops": outcome.get("stops"),
            "agent_success": outcome.get("agent_success"),
            "public_test_status": outcome.get("public_test_status"),
            "public_test_runs": outcome.get("public_test_runs"),
            "execution_integrity": outcome.get("execution_integrity"),
            "delivered_success": outcome.get("delivered_success"),
            "oracle_status": outcome.get("oracle_status"),
        },
        "settlement": {
            "usage_fields": outcome.get("usage"),
            "delta_cost_usd": outcome.get("delta_cost_usd"),
            "cumulative_cost_usd": outcome.get("cost_usd"),
            "basis": "frozen DeepSeek Flash rates applied to the scripted provider's own counters",
        },
        "duration_s": outcome.get("duration_s"),
    }


_ENTRY_POINTS = {
    "A0": (
        "experiments.stage_f.runners.native_cell_runner -> "
        "experiments.evaluation.closure1b.native_baseline.run_native_sample "
        "(AnyCode native loop, NeutralEngine, six sandbox tools)"
    ),
    "A2": (
        "experiments.stage_f.runners.static_dag_cell_runner -> "
        "experiments.evaluation.runner.run_sample (AnyCode DAG scheduler, Atlas context plane)"
    ),
    "A1": (
        "experiments.stage_f.runners.main_driven_cell_runner -> atlasos.app.main_driven.run_goal "
        "(delegation compiled out of the tool surface)"
    ),
    "A3": (
        "experiments.stage_f.runners.main_driven_cell_runner -> atlasos.app.main_driven.run_goal "
        "(delegation enabled, DelegateTasksTool -> DelegationService)"
    ),
}


def _agent_container(resources: dict[str, Any], raw: Any) -> dict[str, Any] | None:
    """The agent sandbox, from the normalised block or from the raw result that recorded it."""

    sandbox = resources.get("sandbox")
    if isinstance(sandbox, dict) and sandbox:
        return sandbox
    environment = (raw or {}).get("environment") if isinstance(raw, dict) else None
    recorded = (environment or {}).get("sandbox")
    if isinstance(recorded, dict) and recorded:
        return recorded
    return None


def _oracle_sandbox_id(analysis: dict[str, Any], raw: Any) -> str | None:
    record = analysis.get("oracle") or {}
    details = record.get("details") or {}
    hidden = details.get("hidden_tests") or {}
    if hidden.get("sandbox_id"):
        return str(hidden["sandbox_id"])
    sandbox = analysis.get("oracle_sandbox") or {}
    if isinstance(sandbox, dict) and sandbox.get("sandbox_id"):
        return str(sandbox["sandbox_id"])
    environment = (raw or {}).get("environment") if isinstance(raw, dict) else None
    return ((environment or {}).get("oracle") or {}).get("sandbox_id")


def _oracle_container(analysis: dict[str, Any], raw: Any) -> dict[str, Any] | None:
    sandbox_id = _oracle_sandbox_id(analysis, raw)
    if not sandbox_id:
        return None
    record = analysis.get("oracle") or {}
    return {
        "sandbox_id": sandbox_id,
        "mode": record.get("execution_mode"),
        "cleanup_failure": record.get("cleanup_failure"),
    }


def _containers(resources: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Whether the agent container was created and destroyed, from whichever record holds it.

    The three arms record this differently: the goal runtime writes ``created``/``destroyed`` on its
    sandbox block, the native arm on ``metrics``, and the static DAG only on ``safety``. Reading one
    place and calling the rest "not created" would understate what actually ran.
    """

    sandbox = _agent_container(resources, raw) or {}
    metrics = (raw or {}).get("metrics") if isinstance(raw, dict) else None
    safety = (raw or {}).get("safety") if isinstance(raw, dict) else None
    created = sandbox.get("created", (metrics or {}).get("sandbox_created"))
    destroyed = sandbox.get(
        "destroyed",
        (metrics or {}).get("sandbox_destroyed", (safety or {}).get("sandbox_destroyed")),
    )
    return {
        "sandbox_id": sandbox.get("sandbox_id"),
        "created": created,
        "destroyed": destroyed,
        "provider": sandbox.get("provider"),
        "network": sandbox.get("network") or ((raw or {}).get("environment") or {}).get("network"),
        "image": sandbox.get("image")
        or ((raw or {}).get("environment") or {}).get("sandbox_image"),
    }


def _raw_result_dir(cell_root: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    """Locate the raw result the cell's own producer wrote, and say which file it is.

    The producer names it: ``result.json`` for the native and static-DAG entries, ``session.json``
    for the goal runtime. The search stays inside the cell's directory, so a file left by another
    cell can never be reported as this one's raw evidence.
    """

    provenance = evidence.get("provenance") or {}
    source = str(provenance.get("source") or "")
    raw_name = "native" in source or "evaluation runner" in source
    filename = "result.json" if raw_name else "session.json"
    candidates = sorted(cell_root.rglob(filename), key=lambda item: item.stat().st_mtime)
    if not candidates:
        return {"file": None, "basename": filename, "found": False}
    latest = candidates[-1]
    return {
        "file": str(latest),
        "basename": filename,
        "found": True,
        "bytes": latest.stat().st_size,
        "digest": "sha256:" + normalize.sha256_bytes(latest.read_bytes()),
    }


# -- verification ------------------------------------------------------------------------------


def _production_caps(cell: dict[str, Any]) -> dict[str, Any]:
    """The values the production object is built with for this cell, read from the builder itself.

    ``production_goal_options`` is the same function the live runner and the pre-claim admission
    call, so reading it here proves the cell's declared caps reach the object that executes — rather
    than restating what the cell says.
    """

    base = executor.RunnerConfig()
    options = executor.production_goal_options(
        cell,
        evidence_root=Path("."),
        fixture_dir=Path("."),
        # The same resolver the live runner applies before it builds the options: without it the
        # process default (20) would be read instead of the cell's cap.
        config=executor.cell_runner_config(cell, base),
        run_id="proof-cap-check",
    )
    return {
        "max_turns": int(options.max_turns),
        "worker_max_turns": int(options.worker_max_turns or 0),
        "token_budget": int(options.token_budget or 0),
        "delegation_enabled": bool(options.delegation.enabled),
        "worker_profiles": list(options.worker_profiles or ()),
        "worker_exposure": options.worker_exposure,
        "oracle_mode": getattr(options.acceptance, "oracle_mode", None),
    }


def proof_checks(
    *,
    arm_id: str,
    cell: dict[str, Any],
    report: dict[str, Any],
    evidence: dict[str, Any],
    scripted_run: scripted.ScriptedRun,
) -> list[dict[str, Any]]:
    """What this arm had to demonstrate, checked against its own evidence."""

    correctness = evidence.get("correctness") or {}
    oracle = correctness.get("independent_oracle") or {}
    context = evidence.get("context") or {}
    usage = evidence.get("usage") or {}
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    caps = (evidence.get("resources") or {}).get("applied_turn_caps") or {}
    check("cell_passed_audit", report.get("verdict") == "PASS", f"verdict={report.get('verdict')}")
    check(
        "independent_oracle_passed",
        oracle.get("status") == "PASS" and oracle.get("passed") is True,
        f"oracle={oracle.get('status')!r}",
    )
    check(
        "workspace_exported",
        bool((evidence.get("resources") or {}).get("artifact_export")),
        f"path={((evidence.get('resources') or {}).get('artifact_export'))!r}",
    )
    blocks = [usage.get(name) for name in ("main", "children", "primary_agent")]
    measured = [
        block
        for block in blocks
        if isinstance(block, dict)
        and isinstance(block.get("prompt_tokens"), int)
        and isinstance(block.get("completion_tokens"), int)
    ]
    check(
        "usage_complete",
        bool(measured) and isinstance(usage.get("system_total_tokens"), int),
        f"blocks={len(measured)} system_total={usage.get('system_total_tokens')}",
    )
    check(
        "provider_answered",
        bool(scripted_run.requests),
        f"requests={len(scripted_run.requests)}",
    )

    if arm_id == "A0":
        check(
            "native_turn_cap_is_40",
            caps.get("primary_agent") == 40,
            f"applied={caps}",
        )
        check(
            "primary_agent_context_recorded",
            isinstance(context.get("primary_agent_peak_context"), int),
            f"peak={context.get('primary_agent_peak_context')}",
        )
        check(
            "main_is_not_applicable",
            context.get("main_peak_context") == normalize.NOT_APPLICABLE,
            f"main_peak={context.get('main_peak_context')!r}",
        )
        check(
            "guarded_budget_ran",
            (usage.get("budget") or {}).get("enforcement") == "pre_admission_guard",
            f"enforcement={(usage.get('budget') or {}).get('enforcement')!r}",
        )
        check(
            "per_request_usage_recorded",
            len(context.get("per_request_input_tokens") or []) >= 5,
            f"requests={len(context.get('per_request_input_tokens') or [])}",
        )
    if arm_id == "A2":
        workers = evidence.get("workers") or []
        check("three_dag_nodes", len(workers) == 3, f"nodes={len(workers)}")
        check(
            "two_independent_nodes",
            len([node for node in workers if not node.get("depends_on")]) == 2,
            "csv and jsonl declare no dependency",
        )
        check(
            "integration_node_after_both",
            all(node.get("is_integration_node") for node in workers if node.get("depends_on")),
            "the sink node is the one with dependencies",
        )
        concurrency = (evidence.get("resources") or {}).get("concurrency") or {}
        check(
            "independent_pair_overlapped",
            bool(concurrency.get("overlapping_pairs")),
            f"pairs={concurrency.get('overlapping_pairs')}",
        )
        check(
            "integration_context_recorded",
            isinstance(evidence.get("context", {}).get("integration_node_context"), int),
            f"peak={evidence.get('context', {}).get('integration_node_context')}",
        )
        check(
            "implementation_nodes_20_and_integration_40",
            [value for key, value in caps.items() if key != "integrate"] == [20, 20]
            and caps.get("integrate") == 40,
            f"applied={caps}",
        )
        check(
            "main_metrics_not_applicable",
            evidence.get("context", {}).get("main_peak_context") == normalize.NOT_APPLICABLE
            and evidence.get("delegation") == normalize.NOT_APPLICABLE,
            "no Main exists in a static DAG",
        )
        check(
            "shared_budget_guarded",
            (usage.get("budget") or {}).get("limit_tokens") == 1_000_000,
            f"limit={(usage.get('budget') or {}).get('limit_tokens')}",
        )
    if arm_id in {"A1", "A3"}:
        caps_production = _production_caps(cell)
        check(
            "main_turn_cap_is_40",
            caps_production["max_turns"] == 40,
            f"max_turns={caps_production['max_turns']}",
        )
        check(
            "shared_budget_is_one_million",
            caps_production["token_budget"] == 1_000_000,
            f"token_budget={caps_production['token_budget']}",
        )
        check(
            "delegation_switch_is_the_arms",
            caps_production["delegation_enabled"] is (arm_id == "A3"),
            f"enabled={caps_production['delegation_enabled']}",
        )
        check(
            "external_oracle_contract",
            "EXTERNAL" in str(caps_production["oracle_mode"]),
            f"oracle_mode={caps_production['oracle_mode']!r}",
        )
    if arm_id == "A1":
        check(
            "delegation_tools_absent",
            _delegate_tools_absent(evidence),
            "neither delegate_tasks nor delegate.read appears in the recorded tool surface",
        )
        check(
            "public_test_ran",
            (report.get("public_test_runs") or 0) >= 1,
            f"runs={report.get('public_test_runs')}",
        )
        check(
            "execution_integrity_recorded",
            correctness.get("execution_integrity") in {"PASS", "UNKNOWN"},
            f"integrity={correctness.get('execution_integrity')!r}",
        )
    if arm_id == "A3":
        delegation = evidence.get("delegation") or {}
        batches = delegation.get("batches") or []
        children = [child for batch in batches for child in batch.get("children") or []]
        check(
            "proposal_recorded",
            delegation.get("natural_proposal") is True,
            f"proposals={delegation.get('proposal_count')}",
        )
        check(
            "two_children_admitted",
            sum(1 for child in children if child.get("status") == "completed") == 2,
            f"children={[child.get('key') for child in children]}",
        )
        check(
            "children_overlapped",
            max((batch.get("max_observed_concurrency") or 0) for batch in batches) >= 2
            if batches
            else False,
            f"max_concurrency={[batch.get('max_observed_concurrency') for batch in batches]}",
        )
        check(
            "system_max_context_from_all_agents",
            len(context.get("per_agent_peak_context_tokens") or {}) >= 2,
            f"agents={list(context.get('per_agent_peak_context_tokens') or {})}",
        )
        check(
            "child_usage_attributed",
            isinstance((usage.get("children") or {}).get("prompt_tokens"), int)
            and (usage.get("children") or {}).get("prompt_tokens") > 0,
            f"child_prompt={(usage.get('children') or {}).get('prompt_tokens')}",
        )
    return checks


def _delegate_tools_absent(evidence: dict[str, Any]) -> bool:
    tool_surface = evidence.get("resources", {}).get("tool_surface")
    if not isinstance(tool_surface, list):
        return False
    return not ({"delegate_tasks", "delegate.read"} & set(tool_surface))


# -- negatives ---------------------------------------------------------------------------------


async def run_negatives(*, scratch: Path, task: Any) -> list[dict[str, Any]]:
    """Controlled failure cases, each driven through a real entry point."""

    from experiments.stage_b_plus import cost as cost_module

    import atlasos.app.main_driven as main_driven

    cases: list[dict[str, Any]] = []
    prices = dict(cost_module.stage_b_estimate.pricing())
    plan = proof_plan(proof_cells(DEFAULT_TASK))

    # 1. the budget guard refuses, and the provider never receives the next request.
    run = _provider_for("A1", task, budget_only=True)
    cell = next(item for item in proof_cells(DEFAULT_TASK) if item["arm_id"] == "A1")
    root = scratch / "negatives" / "budget"
    provider_name = f"stagef-neg-{_unique('b')}"
    factory = scripted.factory_for(run, provider_name)
    fixture = Path(executor.prepare_fixture(task, root / "fixture")["path"])
    options = executor.production_goal_options(
        cell,
        evidence_root=root / "evidence",
        fixture_dir=fixture,
        config=executor.RunnerConfig(llm_provider=provider_name, llm_factory=factory),
        run_id="neg-budget",
    )
    # 600 tokens is far below one request's prompt: the first admission must be refused. The options
    # record is frozen, so the injected values are a copy, never a mutation of the built record.
    options = dataclasses.replace(options, token_budget=600, max_turns=8)
    session: dict[str, Any] | None = None
    error: str | None = None
    try:
        session = await main_driven.run_goal(options)
    except BaseException as exception:  # noqa: BLE001 - the refusal is the evidence
        error = f"{type(exception).__name__}: {exception}"
    budget = (session or {}).get("budget") or {}
    sent = len(run.requests)
    cases.append(
        {
            "case": "budget_guard_refuses_and_sends_nothing",
            "ok": bool(budget.get("admission_rejections"))
            and sent <= int(budget.get("requests_started") or 0),
            "detail": {
                "admission_rejections": budget.get("admission_rejections"),
                "requests_started": budget.get("requests_started"),
                "provider_requests": sent,
                "token_budget": 600,
                "error": error,
                "session_status": (session or {}).get("status"),
            },
        }
    )

    # 2. a real run whose artifact does not satisfy the hidden oracle.
    wrong = scripted.ScriptedRun(
        scripts={
            "main": [
                (
                    "file_write",
                    {"path": "sources/csv_source.py", "content": "def read(raw):\n    return []\n"},
                ),
                *_checks(),
            ]
        },
        markers={"main": task.prompt[:60]},
        final_text="implemented",
    )
    variant = dict(cell)
    variant["arm_id"] = "A1"
    variant["cell_id"] = "f.proof.neg.oracle.1"
    variant["order"] = 98
    run_oracle = await _one_off(
        cell=variant,
        task=task,
        run=wrong,
        root=scratch / "negatives" / "oracle-fail",
        prices=prices,
    )
    cases.append(
        {
            "case": "agent_completed_but_oracle_failed",
            "ok": run_oracle["audit"].get("oracle_status") == "FAIL"
            and run_oracle["audit"].get("verdict") == "MEASURED_FAILURE"
            and run_oracle["audit"].get("delivered_success") is False,
            "detail": {
                "oracle": run_oracle["audit"].get("oracle_status"),
                "verdict": run_oracle["audit"].get("verdict"),
                "agent_success": run_oracle["audit"].get("agent_success"),
                "delivered_success": run_oracle["audit"].get("delivered_success"),
            },
        }
    )

    # 3. the agent does not complete, yet the artifact it left behind is correct.
    truncated = scripted.ScriptedRun(
        scripts={"main": _writes(task, tuple(sorted(task.reference)))},
        markers={"main": task.prompt[:60]},
        final_text="",
    )
    variant_truncated = dict(cell)
    variant_truncated["cell_id"] = "f.proof.neg.truncated.1"
    variant_truncated["order"] = 97
    variant_truncated["max_turns"] = 4  # four writes, then no turn left to finish on
    run_truncated = await _one_off(
        cell=variant_truncated,
        task=task,
        run=truncated,
        root=scratch / "negatives" / "truncated",
        prices=prices,
    )
    cases.append(
        {
            "case": "agent_did_not_complete_but_artifact_correct",
            "ok": run_truncated["audit"].get("oracle_status") == "PASS"
            and run_truncated["audit"].get("agent_success") is False
            and run_truncated["audit"].get("delivered_success") is False,
            "detail": {
                "oracle": run_truncated["audit"].get("oracle_status"),
                "agent_success": run_truncated["audit"].get("agent_success"),
                "delivered_success": run_truncated["audit"].get("delivered_success"),
            },
        }
    )

    # 4. an oracle environment failure is a stop, not a measured failure.
    stop_evidence = dict(
        normalize.from_native_result(
            cell,
            {
                "metrics": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 10,
                },
                "environment": {},
                "safety": {},
                "failure_class": "TASK_SUCCESS",
                "agent_success": True,
            },
            oracle={"status": "ERROR", "passed": False, "environment_failure": True},
            export={"workspace_path": None},
        )
    )
    outcome = f_runners.audit_stage_f_cell(
        cell=cell,
        run=executor.CellRun(
            session=stop_evidence, run_dir=None, fixture_root=None, oracle=None, export=None
        ),
        plan=plan,
        usage_before=None,
        cost_before=None,
        prices=prices,
    )
    cases.append(
        {
            "case": "oracle_environment_failure_stops",
            "ok": outcome["verdict"] == "STOPPED"
            and executor.STOP_ORACLE_ENVIRONMENT in outcome["stops"],
            "detail": {"stops": outcome["stops"], "verdict": outcome["verdict"]},
        }
    )

    # 5. missing counters and a missing export must not become zero or PASS.
    missing = normalize.from_native_result(
        cell,
        {
            "metrics": {"prompt_tokens": None, "completion_tokens": None},
            "environment": {},
            "safety": {},
            "agent_success": True,
            "failure_class": "TASK_SUCCESS",
        },
        oracle={"status": "PASS", "passed": True},
        export={"workspace_path": None},
    )
    outcome_missing = f_runners.audit_stage_f_cell(
        cell=cell,
        run=executor.CellRun(
            session=missing, run_dir=None, fixture_root=None, oracle=None, export=None
        ),
        plan=plan,
        usage_before=None,
        cost_before=None,
        prices=prices,
    )
    cases.append(
        {
            "case": "unknown_or_missing_evidence_is_not_zero_or_pass",
            "ok": outcome_missing["verdict"] == "STOPPED"
            and executor.STOP_UNKNOWN_USAGE in outcome_missing["stops"]
            and executor.STOP_EXPORT in outcome_missing["stops"]
            and all(value is None for value in outcome_missing["usage"].values())
            and outcome_missing["cost_usd"] is None,
            "detail": {
                "stops": outcome_missing["stops"],
                "usage": outcome_missing["usage"],
                "cost_usd": outcome_missing["cost_usd"],
            },
        }
    )
    return cases


async def _one_off(
    *,
    cell: dict[str, Any],
    task: Any,
    run: scripted.ScriptedRun,
    root: Path,
    prices: dict[str, Any],
) -> dict[str, Any]:
    """Run one ad-hoc cell through the A1 entry and audit it, without a plan dispatch."""

    provider_name = f"stagef-neg-{_unique('n')}"
    runner = f_runners.main_driven_cell_runner(
        provider_name=provider_name,
        provider_factory=scripted.factory_for(run, provider_name),
        scripted_run=run,
    )
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    context = {
        "evidence_root": root / "evidence",
        "runs_root": root / "runs",
        "plan": {},
        "authority": {},
        "head": "negatives",
        "prices": {},
    }
    await asyncio.to_thread(
        (root / "evidence" / "cells" / f"c{int(cell['order']):02d}").mkdir,
        parents=True,
        exist_ok=True,
    )
    result = await runner(cell, context)
    outcome = executor.audit_cell(
        cell=cell,
        run=result,
        plan={"plan_id": cell["plan_id"], "scope": cell["scope"]},
        usage_before=None,
        cost_before=None,
        prices=prices,
    )
    return {
        "audit": outcome,
        "evidence": result.session,
    }


# -- the report --------------------------------------------------------------------------------


def sanitize(report: dict[str, Any]) -> dict[str, Any]:
    """Replace host paths with stable labels, keeping every measured fact."""

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    for key in ("scratch_root", "runs_root", "evidence_root"):
        value = report.get(key)
        if value:
            text = text.replace(str(value), f"<{key}>")
    return json.loads(text)


async def run_proof(
    *,
    arm: str | None = None,
    task_id: str = DEFAULT_TASK,
    out_root: Path | None = None,
    negatives: bool = False,
) -> dict[str, Any]:
    _windows_long_paths()
    task = tasks.task_by_id(task_id)
    # A short root: the fixture stager nests a git repository several levels below the run
    # directory, and the arm's own run path adds a sample id and a run id below that.
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    scratch = Path(out_root or Path(tempfile.gettempdir()) / "sf-proof" / stamp)
    await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
    cells = proof_cells(task_id)
    selected = [arm] if arm else list(ARM_SEQUENCE)
    dispatch_cells = [cell for cell in cells if cell["arm_id"] in selected]
    runnable = proof_plan(dispatch_cells)

    started = time.time()
    report, runs = await run_dispatch(
        scratch=scratch, plan=runnable, cells=dispatch_cells, task=task
    )
    evidence_root = scratch / "evidence"
    runs_root = scratch / "runs"

    per_arm: list[dict[str, Any]] = []
    for cell, outcome in zip(dispatch_cells, report.get("cells") or [], strict=False):
        evidence = _read_json(_cell_dir(evidence_root, cell["order"]) / "evidence.json") or {}
        entry = arm_report(
            arm_id=str(cell["arm_id"]),
            cell=cell,
            outcome=outcome,
            evidence_root=evidence_root,
            runs_root=runs_root,
            scripted_run=runs[str(cell["arm_id"])],
        )
        entry["checks"] = proof_checks(
            arm_id=str(cell["arm_id"]),
            cell=cell,
            report=outcome,
            evidence=evidence,
            scripted_run=runs[str(cell["arm_id"])],
        )
        entry["passed"] = all(item["ok"] for item in entry["checks"])
        per_arm.append(entry)

    result: dict[str, Any] = {
        "stage": "F",
        "kind": "four_arm_real_runner_proof",
        "generated_at": _now(),
        "repo_head": executor.git_head(REPO_ROOT),
        "plan": {
            "plan_id": runnable["plan_id"],
            "plan_digest": runnable["digest"],
            "scope": runnable["scope"],
            "cells": [cell["cell_id"] for cell in dispatch_cells],
            "mode": executor.MODE_OFFLINE_PROOF,
            "claim_root": "isolated offline claim (no production claim consumed)",
        },
        "model_calls": 0,
        "production_claims_consumed": 0,
        "scripted_provider": True,
        "scratch_root": str(scratch),
        "runs_root": str(runs_root),
        "evidence_root": str(evidence_root),
        "dispatch": {
            "stop_reason": report.get("stop_reason"),
            "complete": report.get("complete"),
            "cells_started": report.get("cells_started"),
            "cells_passed": report.get("cells_passed"),
            "usage_completeness": report.get("usage_completeness"),
            "total_tokens": report.get("total_tokens"),
            "total_cost_usd": report.get("total_cost_usd"),
            "observed_cost_floor_usd": report.get("observed_cost_floor_usd"),
            "ledger": _ledger_rows(evidence_root),
        },
        "arms": per_arm,
        "duration_s": round(time.time() - started, 3),
    }
    if negatives:
        result["negatives"] = await run_negatives(scratch=scratch, task=task)
    result["all_arms_passed"] = bool(per_arm) and all(item["passed"] for item in per_arm)
    # The stage sub-status names the *four-arm* proof: a partial run reports what it ran and never
    # the stage verdict.
    complete_four = len(per_arm) == len(ARM_SEQUENCE)
    result["status"] = (
        "STAGE_F_RUNNER_PROOF_PASS"
        if complete_four and result["all_arms_passed"]
        else "RUNNER_PROOF_INCOMPLETE"
    )
    _write_json(scratch / "proof-report.raw.json", result)
    # Only a complete four-arm run publishes the tracked report: a partial run must not overwrite
    # the record of a complete one.
    if len(per_arm) == len(ARM_SEQUENCE):
        _write_json(REPORT_PATH, sanitize(result))
    return result


def _ledger_rows(evidence_root: Path) -> list[dict[str, Any]]:
    path = evidence_root / "ledger.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage F four-arm real runner proof")
    parser.add_argument("--arm", choices=ARM_SEQUENCE, help="run one arm only")
    parser.add_argument("--all", action="store_true", help="run every arm in order")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--out", default=None, help="scratch root (default: system temp)")
    parser.add_argument("--negatives", action="store_true", help="also run the failure cases")
    args = parser.parse_args(argv)
    if not args.all and not args.arm:
        parser.error("choose --all or --arm")
    result = asyncio.run(
        run_proof(
            arm=args.arm,
            task_id=args.task,
            out_root=Path(args.out) if args.out else None,
            negatives=args.negatives,
        )
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "arms": {
                    item["arm_id"]: "PASS" if item["passed"] else "FAIL" for item in result["arms"]
                },
                "dispatch": {
                    key: result["dispatch"][key]
                    for key in ("stop_reason", "complete", "cells_passed", "total_cost_usd")
                },
                "report": str(REPORT_PATH),
            },
            indent=2,
        )
    )
    return 0 if result["all_arms_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
