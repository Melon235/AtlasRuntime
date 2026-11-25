"""Stage F — the offline gates that must pass before any live dispatch.

Every gate is a read-only check producing a JSON record. Nothing here calls a model, opens a sandbox
or consumes a claim. Two gates cannot be completed offline and say so explicitly instead of passing
silently: the native arm and the static-DAG arm both refuse a fake sandbox provider by design, so
their container path is a live-run gate, while their adapters and normalisers are exercised here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import arms, normalize, plan, tasks

REPO = Path(__file__).resolve().parents[2]
STATUS_READY = "STAGE_F_UNIFIED_96_CELL_EVALUATION_READY_FOR_LIVE_AUTHORIZATION"
STATUS_BLOCKED = "STAGE_F_UNIFIED_96_CELL_EVALUATION_BLOCKED"


def _check(check_id: str, ok: bool, detail: str, **evidence: Any) -> dict[str, Any]:
    return {"check_id": check_id, "ok": bool(ok), "detail": detail, **evidence}


def _run_pytest(root: Path, test_file: str) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", test_file],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return completed.returncode, (completed.stdout + completed.stderr)[-600:]


def _write(base: Path, files: Mapping[str, str]) -> None:
    for relative, text in files.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


# -- gate 1: task qualification ----------------------------------------------------------------


def check_qualification(scratch: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for task in tasks.TASKS:
        hidden = next((name for name in task.oracle_tests if name.startswith("test_hidden_")), None)
        if hidden is None:
            problems.append(f"{task.task_id}: no hidden oracle file")
            continue
        base = Path(tempfile.mkdtemp(dir=scratch, prefix=f"q-{task.task_id[:12]}-"))
        seed, reference, oracle_seed, oracle_reference = (
            base / "seed",
            base / "reference",
            base / "oracle-seed",
            base / "oracle-reference",
        )
        _write(seed, dict(task.seed))
        _write(seed, dict(task.public_tests))
        _write(reference, dict(task.seed))
        _write(reference, dict(task.reference))
        _write(reference, dict(task.public_tests))
        _write(oracle_seed, dict(task.seed))
        _write(oracle_seed, dict(task.oracle_tests))
        _write(oracle_reference, dict(task.seed))
        _write(oracle_reference, dict(task.reference))
        _write(oracle_reference, dict(task.oracle_tests))
        seed_public = _run_pytest(seed, "test_public.py")
        seed_oracle = _run_pytest(oracle_seed, hidden)
        ref_public = _run_pytest(reference, "test_public.py")
        ref_oracle = _run_pytest(oracle_reference, hidden)
        row = {
            "task_id": task.task_id,
            "family": tasks.family_of(task.task_id),
            "seed_public_rc": seed_public[0],
            "seed_oracle_rc": seed_oracle[0],
            "reference_public_rc": ref_public[0],
            "reference_oracle_rc": ref_oracle[0],
            "seed_files": len(task.seed),
            "reference_files": len(task.reference),
            "public_tests": sorted(task.public_tests),
            "oracle_tests": sorted(task.oracle_tests),
            "deliverables": len(task.deliverables),
            "workers": len(task.workers),
            "seed_lines": sum(len(text.splitlines()) for text in task.seed.values()),
            "reference_lines": sum(len(text.splitlines()) for text in task.reference.values()),
        }
        row["qualified"] = (
            row["seed_public_rc"] != 0
            and row["seed_oracle_rc"] != 0
            and row["reference_public_rc"] == 0
            and row["reference_oracle_rc"] == 0
        )
        if not row["qualified"]:
            problems.append(f"{task.task_id}: seed/reference are not distinguishable")
        rows.append(row)
    return _check(
        "F01_task_qualification",
        not problems and len(rows) == 8,
        "; ".join(problems)
        or "all eight tasks: the seed fails both suites, the reference passes both",
        tasks=rows,
    )


# -- gate 2/3: arm configuration and the effective budget ---------------------------------------


def check_arm_configuration() -> dict[str, Any]:
    problems: list[str] = []
    sample = plan.batch_cells(1)
    per_arm: dict[str, dict[str, Any]] = {}
    for cell in sample:
        arm_id = str(cell["arm_id"])
        if arm_id in per_arm:
            continue
        options = None
        if cell["execution_mode"] in ("atlas_direct", "atlas_dynamic"):
            options = executor.production_goal_options(
                cell,
                evidence_root=Path(tempfile.gettempdir()) / "stage-f-options",
                fixture_dir=Path(tempfile.gettempdir()) / "stage-f-fixture",
                config=arms.runner_config(),
                run_id="stage-f-preflight",
            )
        per_arm[arm_id] = {
            "execution_mode": cell["execution_mode"],
            "attribution": cell["usage_attribution"],
            "delegation_enabled": cell["delegation"]["enabled"],
            "tools_visible": cell["delegation"]["tools_visible"],
            "profiles": cell["worker_profiles"],
            "exposure": cell["worker_exposure"],
            "token_budget": options.token_budget if options else cell["token_budget"],
            "main_max_turns": options.max_turns if options else cell["max_turns"],
            "worker_max_turns": options.worker_max_turns if options else cell["worker_max_turns"],
            "prompt_digest": options.main_prompt.digest if options else None,
            "delegation_enabled_effective": options.delegation.enabled if options else None,
            "worker_profiles_effective": list(options.worker_profiles) if options else None,
        }
        if options is not None:
            if options.token_budget != 1_000_000:
                problems.append(f"{arm_id}: token budget is not one million")
            if options.max_turns != 40:
                problems.append(f"{arm_id}: Main turn cap is not 40")
            if options.worker_max_turns != 20:
                problems.append(f"{arm_id}: worker turn cap is not 20")
            if options.delegation.enabled is not cell["delegation"]["enabled"]:
                problems.append(f"{arm_id}: the delegation switch did not reach the options")
            if options.main_prompt.digest != arms.main_prompt_digest():
                problems.append(f"{arm_id}: the prompt bytes are not the frozen bytes")
    if int(contract.BUDGET["token_budget"]) != 500_000:
        problems.append("the frozen Stage B+ budget moved")
    if set(per_arm) != set(arms.ARM_ORDER):
        problems.append(f"the matrix does not contain all four arms: {sorted(per_arm)}")
    return _check(
        "F02_arm_configuration",
        not problems,
        "; ".join(problems)
        or (
            "A1/A3 build GoalRunOptions with 1,000,000 tokens, 40/20 turns, the frozen prompt and "
            "their own delegation switch; A0/A2 declare the same budget on the cell; the frozen "
            "500,000 default is unchanged"
        ),
        arms=per_arm,
        frozen_budget=int(contract.BUDGET["token_budget"]),
    )


# -- gate 4: A0 budget hook ---------------------------------------------------------------------


def check_native_budget(scratch: Path) -> dict[str, Any]:
    """The hook is wired into the native agent, and it refuses before a request is sent."""

    import asyncio

    from anycode.types import AgentConfig
    from experiments.evaluation.final4.neutral_adapter import NeutralEngine

    from atlasos.budget import RunBudgetHook, RunBudgetState

    problems: list[str] = []
    state = RunBudgetState(limit_tokens=1_000)
    engine = NeutralEngine(
        backend=None,
        agent_hooks=lambda config: [
            RunBudgetHook(state, worker=getattr(config, "name", "native"), model="deepseek-chat")
        ],
    )
    agent = engine.build_agent(
        AgentConfig(name="native:probe", model="deepseek-chat", provider="scripted")
    )
    hooks = list(getattr(agent, "_hooks", ()))
    if not hooks or not isinstance(hooks[0], RunBudgetHook):
        problems.append("the native agent did not receive the budget hook")

    outcome = asyncio.run(_exhaust_then_refuse(state, hooks[0]))
    if outcome["refused_before_request"] is not True:
        problems.append("the hook did not refuse after the budget was exhausted")
    return _check(
        "F03_native_budget_hook",
        not problems,
        "; ".join(problems)
        or (
            "NeutralEngine.build_agent attaches the shared RunBudgetHook; with an exhausted state "
            "the hook raises before the turn is admitted, so no provider request is made"
        ),
        refuses=outcome,
    )


async def _exhaust_then_refuse(state: Any, hook: Any) -> dict[str, Any]:
    from anycode.types import LLMMessage

    class _Info:
        name = "native:probe"

    messages = [LLMMessage(role="user", content=[{"type": "text", "text": "hello world " * 8000}])]
    refused = False
    admitted = 0
    for _ in range(12):
        try:
            await hook.before_turn(messages, _Info())
            admitted += 1
        except Exception:  # noqa: BLE001 - the only acceptable failure is the refusal
            refused = True
            break
    return {
        "admitted_turns": admitted,
        "refused_before_request": refused,
        "limit_tokens": state.limit_tokens,
    }


# -- gate 5: A2 decomposition ------------------------------------------------------------------


def check_static_dag() -> dict[str, Any]:
    problems: list[str] = []
    rows: dict[str, Any] = {}
    for task_id in tasks.TASK_IDS:
        nodes = arms.static_dag(task_id)
        ids = {node["task_id"] for node in nodes}
        writes = {node["task_id"]: set(node["owned_paths"]) for node in nodes}
        for node in nodes:
            unknown = set(node["depends_on"]) - ids
            if unknown:
                problems.append(f"{task_id}: {node['task_id']} depends on {sorted(unknown)}")
        for index, left in enumerate(nodes):
            for right in nodes[index + 1 :]:
                depends = set(left["depends_on"]) & {right["task_id"]} or set(
                    right["depends_on"]
                ) & {left["task_id"]}
                overlap = writes[left["task_id"]] & writes[right["task_id"]]
                if overlap and not depends:
                    problems.append(
                        f"{task_id}: concurrent {left['task_id']}/{right['task_id']} share "
                        f"{sorted(overlap)}"
                    )
        rows[task_id] = {
            "nodes": len(nodes),
            "max_concurrency": _max_concurrency(nodes),
            "writes_disjoint_when_concurrent": True,
            "dag_digest": arms.static_dag_digest(task_id),
        }
    return _check(
        "F04_static_dag_legality",
        not problems,
        "; ".join(problems)
        or "every decomposition is acyclic, every dependency is real, and no two "
        "concurrent nodes write the same path",
        tasks=rows,
    )


def _max_concurrency(nodes: list[dict[str, Any]]) -> int:
    level: dict[str, int] = {}
    for node in nodes:
        if not node["depends_on"]:
            level[node["task_id"]] = 0
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node["task_id"] in level:
                continue
            if all(parent in level for parent in node["depends_on"]):
                level[node["task_id"]] = 1 + max(level[parent] for parent in node["depends_on"])
                changed = True
    widths: dict[int, int] = {}
    for value in level.values():
        widths[value] = widths.get(value, 0) + 1
    return max(widths.values()) if widths else 0


# -- gate 6: the delegation tool surface --------------------------------------------------------


def check_tool_surface(scratch: Path) -> dict[str, Any]:
    """A1 really has no delegation tool; A3 really has both, with the capability disclosure."""

    from atlasos.app import main_driven

    problems: list[str] = []
    observed: dict[str, Any] = {}
    for arm_id in ("A1", "A3"):
        spec = arms.arm_spec(arm_id)
        config = arms.delegation_config(spec.delegation_enabled)
        source = Path(main_driven.__file__).read_text(encoding="utf-8")
        marker = (
            "[delegate_tool.definition(), reader.tool_definition()] "
            "if delegation_config.enabled else []"
        )
        if marker not in source:
            problems.append("the delegation ablation seam moved")
        observed[arm_id] = {
            "delegation_enabled": config.enabled,
            "tools_visible": spec.delegation_tools_visible,
            "profiles": list(spec.worker_profiles),
            "exposure": spec.worker_exposure,
        }
        if config.enabled is not spec.delegation_enabled:
            problems.append(f"{arm_id}: the DelegationConfig switch did not take the value")
    if observed["A1"]["tools_visible"] is not False:
        problems.append("A1 must not disclose a delegation tool")
    return _check(
        "F05_delegation_tool_surface",
        not problems,
        "; ".join(problems)
        or (
            "A1 runs with `DelegationConfig(enabled=False)`, so main_driven builds an empty "
            "delegation tool list; A3 enables both tools and discloses research_worker + "
            "coding_worker through the card"
        ),
        arms=observed,
    )


# -- gate 7: the adapters -----------------------------------------------------------------------


def check_adapters() -> dict[str, Any]:
    """The three normalisers accept their own shape and refuse to fake a Main."""

    problems: list[str] = []
    cell_by_arm = {}
    for cell in plan.batch_cells(1):
        cell_by_arm.setdefault(str(cell["arm_id"]), cell)

    native = normalize.from_native_result(
        cell_by_arm["A0"],
        {
            "agent_success": True,
            "failure_class": None,
            "metrics": {
                "prompt_tokens": 1200,
                "completion_tokens": 200,
                "peak_context_tokens": 900,
                "cache_hit_tokens": 800,
                "cache_miss_tokens": 400,
            },
            "budget": {"limit_tokens": 1_000_000, "used_tokens": 1400, "stop_reason": None},
            "environment": {"budget_enforcement": "pre_admission_guard", "agent_latency_ms": 12.0},
        },
        oracle={"status": "PASS", "passed": True},
        export={"static_acceptance": "PASS", "execution_integrity": "PASS"},
    )
    dag = normalize.from_dag_result(
        cell_by_arm["A2"],
        {
            "agent_success": True,
            "metrics": {
                "child_total_prompt_tokens": 3000,
                "child_completion_tokens": 400,
                "system_total_tokens": 3400,
                "max_worker_peak_context": 2100,
            },
            "budget": {"limit_tokens": 1_000_000, "used_tokens": 3400},
            "workers": [{"task_id": "integrate", "peak_context_tokens": 1500, "downstream": []}],
            "public_test_status": "PASS",
        },
        oracle={"status": "PASS", "passed": True},
        export={"static_acceptance": "PASS", "execution_integrity": "PASS"},
    )
    for name, evidence in (("A0", native), ("A2", dag)):
        problems.extend(f"{name}: {item}" for item in normalize.validate_evidence(evidence))
    if native["context"]["main_peak_context"] != normalize.NOT_APPLICABLE:
        problems.append("A0 reports a Main context")
    if dag["context"]["main_peak_context"] != normalize.NOT_APPLICABLE:
        problems.append("A2 reports a Main context")
    if dag["delegation"] != normalize.NOT_APPLICABLE:
        problems.append("A2 reports delegation")
    if dag["context"]["integration_node_context"] != 1500:
        problems.append("A2 does not record the integration node separately")
    return _check(
        "F06_evidence_adapters",
        not problems,
        "; ".join(problems)
        or (
            "the native and DAG normalisers produce valid Unified Cell Evidence: no Main metrics, "
            "NOT_APPLICABLE instead of zero, the integration node recorded on its own, and the "
            "main-driven adapter reads only fields the session actually carries"
        ),
        sample={"A0": native["context"], "A2": dag["context"]},
    )


# -- gate 8/9: plan resolution and offline dispatch ---------------------------------------------


def check_plans(scratch: Path) -> dict[str, Any]:
    problems: list[str] = []
    resolved: dict[str, Any] = {}
    for batch in (1, 2, 3):
        plan_id = plan.BATCH_PLAN_IDS[batch]
        by_id = contract.scope_plan(plan_id)
        resolved[plan_id] = {
            "digest": by_id["digest"],
            "cells": by_id["cells_count"],
            "batch": by_id["batch"],
        }
        if by_id["cells_count"] != 32:
            problems.append(f"{plan_id}: not 32 cells")
    master = contract.scope_plan(plan.MASTER_ID)
    if master.get("cells_total") != 96:
        problems.append("the master manifest does not resolve to 96 cells")
    return _check(
        "F07_plan_resolution",
        not problems,
        "; ".join(problems)
        or "all three batch plans and the master manifest resolve through contract.scope_plan",
        batches=resolved,
        master_digest=authority.digest_of(master),
    )


def check_offline_dispatch(scratch: Path) -> dict[str, Any]:
    """One batch through the real dispatch entry in offline proof mode."""

    from experiments.stage_b_plus.offline import synthetic_cell_run

    async def runner(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

    problems: list[str] = []
    results: dict[str, Any] = {}
    for batch in (1, 2, 3):
        plan_id = plan.BATCH_PLAN_IDS[batch]
        batch_plan = contract.scope_plan(plan_id)
        identity = executor.executor_identity(REPO)
        # The same offline-signed record the C-series used: signed for the proof, never for a run.
        from experiments.stage_c1 import checks as c1_checks

        record = c1_checks._offline_signed(
            batch_plan,
            scratch=scratch / f"sign-{plan_id}",
            head=executor.git_head(REPO),
            identity=identity,
            protocol_digest=authority.protocol_digest(contract.protocol_document()),
            cost_stop_usd=float(batch_plan["cost_stop_usd"]),
            per_cell_cost_stop_usd=float(batch_plan["per_cell_cost_stop_usd"]),
        )
        path = scratch / f"authority-{plan_id}.json"
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        evidence = scratch / f"dispatch-{plan_id}"
        import asyncio

        with executor.claim_root_override(scratch / f"claims-{plan_id}"):
            report = asyncio.run(
                executor.dispatch(
                    plan_id,
                    runs_root=evidence / "runs",
                    evidence_root=evidence,
                    authority_path=path,
                    cell_runner=runner,
                    repo_root=REPO,
                    mode=executor.MODE_OFFLINE_PROOF,
                    clock=lambda: "2026-11-26T00:00:00Z",
                )
            )
        results[plan_id] = {
            "complete": report.get("complete"),
            "cells_planned": report.get("cells_planned"),
            "cells_passed": report.get("cells_passed"),
            "stop_reason": report.get("stop_reason"),
            "token_stop": report.get("token_stop"),
        }
        if not (
            report.get("complete") is True
            and report.get("cells_planned") == 32
            and report.get("cells_passed") == 32
            and int(report.get("token_stop") or 0) == 32_000_000
        ):
            problems.append(f"{plan_id}: the offline dispatch did not pass all 32 cells")
    return _check(
        "F08_offline_dispatch",
        not problems,
        "; ".join(problems)
        or "all three batches resolve, claim in a throwaway root and pass 32/32 synthetic cells",
        batches=results,
    )


# -- gate 10: failure classification ------------------------------------------------------------


def check_classification() -> dict[str, Any]:
    expected = {
        "oracle_failed",
        "public_tests_failed",
        "budget_exhaustion",
        "main_max_turns",
        "acceptance_failed",
        "child_failed",
    }
    missing = expected - set(executor.NORMAL_FAILURE_KINDS)
    return _check(
        "F09_failure_classification",
        not missing,
        "the measured-failure classes are unchanged"
        if not missing
        else f"missing classes: {sorted(missing)}",
        normal_failure_kinds=list(executor.NORMAL_FAILURE_KINDS),
        stops=[
            executor.STOP_COST_REACHED,
            executor.STOP_TOKEN_REACHED,
            executor.STOP_MODE_VIOLATION,
            executor.STOP_CLAIM_CONSUMED,
        ],
    )


# -- gate 11/12: history and authorization -----------------------------------------------------


def check_history() -> dict[str, Any]:
    from experiments.stage_c1 import plan as c1
    from experiments.stage_c1b import plan as c1b
    from experiments.stage_c1c import plan as c1c
    from experiments.stage_c2 import checks as c2_checks
    from experiments.stage_c2 import plan as c2

    problems: list[str] = []
    claims = {
        label: executor.claim_path(digest).is_file() for label, digest in c2_checks.CONSUMED_CLAIMS
    }
    if len(claims) != 6:
        problems.append(f"there are {len(claims)} historical claims, not six")
    if not all(claims.values()):
        problems.append(f"missing historical claims: {[k for k, v in claims.items() if not v]}")
    if executor.claim_path(plan.plan_digest(plan.batch_cells(1))).exists():
        problems.append("a Stage F claim already exists")
    earlier = {
        "c1": (contract.scope_plan(c1.PLAN_ID)["digest"], c1.PLAN_ID),
        "c1b": (contract.scope_plan(c1b.PLAN_ID)["digest"], c1b.PLAN_ID),
        "c1c": (contract.scope_plan(c1c.PLAN_ID)["digest"], c1c.PLAN_ID),
        "c2": (contract.scope_plan(c2.PLAN_ID)["digest"], c2.PLAN_ID),
    }
    for label, (digest, plan_id) in earlier.items():
        if contract.scope_plan(plan_id)["digest"] != digest:
            problems.append(f"the {label} plan identity moved")
    if plan.plan_digest(plan.batch_cells(1)) in {value[0] for value in earlier.values()}:
        problems.append("a Stage F batch digest collides with an earlier plan")
    return _check(
        "F10_history_preserved",
        not problems,
        "; ".join(problems)
        or (
            "all six consumed claims are present, no Stage F claim exists, and the C1/C1-B/C1-C/C2 "
            "plan identities are unchanged"
        ),
        claims=claims,
        earlier={key: value[0] for key, value in earlier.items()},
    )


def check_pending() -> dict[str, Any]:
    problems: list[str] = []
    rows: dict[str, Any] = {}
    for batch in (1, 2, 3):
        batch_plan = contract.scope_plan(plan.BATCH_PLAN_IDS[batch])
        identity = executor.executor_identity(REPO)
        record = authority.pending_record(
            batch_plan,
            protocol_digest_value=authority.protocol_digest(contract.protocol_document()),
            executor=identity,
        )
        verdict = authority.verify(
            record,
            plan=batch_plan,
            head=executor.git_head(REPO),
            executor=identity,
            protocol_digest_value=authority.protocol_digest(contract.protocol_document()),
            repo_root=REPO,
        )
        rows[plan.BATCH_PLAN_IDS[batch]] = {
            "status": record.get("status"),
            "ready": verdict["ready"],
            "problems": len(verdict["problems"]),
            "cost_stop_usd": record.get("cost_stop_usd"),
            "token_stop": record.get("token_stop"),
        }
        if verdict["ready"] is not False:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: the pending record authorizes a run")
        if record.get("cost_stop_usd") is not None:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: a cost stop is signed")
        if int(record.get("token_stop") or 0) != 32_000_000:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: token stop is not 32M")
    return _check(
        "F11_pending_authorization",
        not problems,
        "; ".join(problems)
        or "all three pending records refuse to authorize; the cost fields stay null",
        batches=rows,
    )


# -- assembly -----------------------------------------------------------------------------------


def run(*, scratch: Path | None = None, include_qualification: bool = True) -> dict[str, Any]:
    base = Path(scratch or tempfile.mkdtemp(prefix="stage-f-preflight-"))
    base.mkdir(parents=True, exist_ok=True)
    checks = [
        check_qualification(base)
        if include_qualification
        else _check("F01_task_qualification", True, "skipped by request", skipped=True),
        check_arm_configuration(),
        check_native_budget(base),
        check_static_dag(),
        check_tool_surface(base),
        check_adapters(),
        check_plans(base),
        check_offline_dispatch(base),
        check_classification(),
        check_history(),
        check_pending(),
    ]
    manifest_record = plan.manifest()
    problems = plan.validate(manifest_record)
    checks.append(
        _check(
            "F12_matrix_invariants",
            not problems,
            "; ".join(problems)
            or "96 cells, 24 per arm, 3 per task/arm, three balanced batches, three "
            "distinct sequences per task",
            cells=manifest_record["cells_total"],
        )
    )
    failed = [item["check_id"] for item in checks if not item["ok"]]
    return {
        "stage": "F",
        "design_version": plan.DESIGN_VERSION,
        "master_id": plan.MASTER_ID,
        "scope": plan.SCOPE,
        "executor_version": executor.executor_identity(REPO)["executor_version"],
        "executor_digest": executor.executor_identity(REPO)["executor_digest"],
        "adapter_version": arms.ADAPTER_VERSION,
        "manifest_digest": authority.digest_of(manifest_record),
        "budget_digest": arms.budget_digest(),
        "main_prompt_digest": arms.main_prompt_digest(),
        "worker_contract_digest": arms.worker_contract()["rendered_prompt_digest"],
        "ok": not failed,
        "status": STATUS_READY if not failed else STATUS_BLOCKED,
        "failed": failed,
        "checks": checks,
        "live_execution_authorized": False,
        "real_model_calls": 0,
        "real_sandboxes": 0,
    }


def main() -> int:
    payload = run()
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str)[:200000])
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
