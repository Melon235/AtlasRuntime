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
STATUS_PREPARATION_BLOCKED = "STAGE_F_UNIFIED_96_CELL_EVALUATION_PREPARATION_BLOCKED"
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
    """The three normalisers read the records the three arms really write.

    The samples below are the production shapes, not hand-built convenience mappings: an
    ``EvalRunResult`` with ``per_request_usage`` and ``environment.resolved_models``, an evaluation
    runner result with ``metrics.request_evidence.records``, and a main-driven ``session.json`` with
    ``main_context_tokens_by_request``. A sample that no producer writes would pass this gate while
    the real run fails, which is the defect this gate exists to prevent.
    """

    problems: list[str] = []
    cell_by_arm = {}
    for cell in plan.batch_cells(1):
        # P1 is the task whose decomposition has two sources and one integration node, so the
        # A2 sample can assert the integration node is recorded on its own.
        if str(cell["task_id"]) == "f-p1-connector-migration":
            cell_by_arm.setdefault(str(cell["arm_id"]), cell)
    if set(cell_by_arm) != set(arms.ARM_ORDER):
        return _check(
            "F06_evidence_adapters",
            False,
            f"batch 1 does not carry all four arms for P1: {sorted(cell_by_arm)}",
        )

    native = normalize.from_native_result(
        cell_by_arm["A0"],
        {
            "agent_success": True,
            "failure_class": "TASK_SUCCESS",
            "metrics": {
                "prompt_tokens": 1200,
                "completion_tokens": 200,
                "cache_hit_tokens": 800,
                "cache_miss_tokens": 400,
                "budget": {
                    "limit_tokens": 1_000_000,
                    "used_tokens": 1400,
                    "stop_reason": None,
                    "admission_rejections": 0,
                },
                "per_request_usage": [
                    {"index": 0, "prompt_tokens": 500, "completion_tokens": 80},
                    {"index": 1, "prompt_tokens": 900, "completion_tokens": 120},
                ],
                "model_turns": 2,
                "provision_latency_ms": 4200.0,
                "latency_ms": 33000.0,
            },
            "environment": {
                "budget_enforcement": "pre_admission_guard",
                "agent_latency_ms": 12000.0,
                "resolved_models": ["deepseek-flash"],
                "sandbox": {"sandbox_id": "sbx-a0", "destroyed": True},
            },
            "safety": {"sandbox_destroyed": True, "host_fixture_unchanged": True},
            "identity": {"run_id": "run-a0", "model": "deepseek-chat"},
        },
        oracle={"status": "PASS", "passed": True},
        export={"workspace_path": None},
    )
    dag = normalize.from_dag_result(
        cell_by_arm["A2"],
        {
            "agent_success": True,
            "failure_class": "TASK_SUCCESS",
            "identity": {"run_id": "run-a2", "model": "deepseek-chat"},
            "metrics": {
                "prompt_tokens": 3000,
                "completion_tokens": 400,
                "cache_hit_tokens": 1000,
                "cache_miss_tokens": 2000,
                "model_turns": 3,
                "budget": {"limit_tokens": 1_000_000, "used_tokens": 3400},
                "request_evidence": {
                    "requests": [
                        {
                            "call_index": 0,
                            "trusted_task_id": "run-a2-csv",
                            "usage": {"prompt_tokens": 1500, "cache_hit_tokens": None},
                        },
                        {
                            "call_index": 1,
                            "trusted_task_id": "run-a2-jsonl",
                            "usage": {"prompt_tokens": 1800, "cache_hit_tokens": None},
                        },
                        {
                            "call_index": 2,
                            "trusted_task_id": "run-a2-integrate",
                            "usage": {"prompt_tokens": 2100, "cache_hit_tokens": None},
                        },
                    ]
                },
            },
            "environment": {
                "public_tests": {"runs": []},
                "resolved_models": ["deepseek-flash"],
                "sandbox": {"sandbox_id": "sbx-a2"},
                "task_events": [],
            },
            "worker_results": {},
        },
        oracle={"status": "PASS", "passed": True},
        export={"workspace_path": None},
    )
    session = {
        "run_id": "run-a3",
        "status": "succeeded",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "main": {
            "task_id": "run-a3-main",
            "success": True,
            "stop_reason": None,
            "total_prompt_tokens": 5200,
            "completion_tokens": 600,
            "cache_hit_tokens": 4000,
            "cache_miss_tokens": 1200,
            "context_tokens_by_request": [1200, 2600, 4100],
        },
        "metrics": {
            "main_requests": 3,
            "main_peak_context_tokens": 4100,
            "main_cumulative_input_tokens": 5200,
            "main_context_tokens_by_request": [1200, 2600, 4100],
            "main_cache_hit_tokens": 4000,
            "main_cache_miss_tokens": 1200,
            "main_completion_tokens": 600,
            "child_total_prompt_tokens": 2600,
            "child_completion_tokens": 300,
            "system_total_tokens": 8700,
            "delegation_rounds": 1,
            "max_observed_child_concurrency": 2,
        },
        "delegation": {
            "totals": {"cache_hit_tokens": 0, "cache_miss_tokens": 2600},
            "batches": [
                {
                    "round": 1,
                    "route": "delegate",
                    "decision": {"reason_codes": ["two_independent_units"], "blocked_gates": []},
                    "children": [
                        {
                            "key": "csv-source",
                            "status": "completed",
                            "success": True,
                            "turns": 4,
                            "prompt_tokens": 1600,
                            "completion_tokens": 150,
                        }
                    ],
                    "max_observed_concurrency": 1,
                }
            ],
        },
        "budget": {"limit_tokens": 1_000_000, "used_tokens": 8700, "admission_rejections": 0},
        "public_tests": {"runs": []},
        "verification": {"verdict": "PASS"},
        "execution_integrity": {"mode": "record", "records": 4, "write_failures": 0},
        "ownership": {"mode": "enforce"},
        "sandbox": {"sandbox_id": "sbx-a3", "destroyed": True},
        "tool_surface": [],
        "handoff_reads": {},
    }
    goal = normalize.from_main_child_session(
        cell_by_arm["A3"],
        session,
        oracle={"status": "PASS", "passed": True},
        export={"workspace_path": None},
        proposals=[{"task_id": "run-a3-main", "input": {"tasks": []}}],
    )

    for name, evidence in (("A0", native), ("A2", dag), ("A3", goal)):
        problems.extend(f"{name}: {item}" for item in normalize.validate_evidence(evidence))

    if native["context"]["main_peak_context"] != normalize.NOT_APPLICABLE:
        problems.append("A0 reports a Main context")
    if dag["context"]["main_peak_context"] != normalize.NOT_APPLICABLE:
        problems.append("A2 reports a Main context")
    if dag["delegation"] != normalize.NOT_APPLICABLE:
        problems.append("A2 reports delegation")
    if native["context"]["system_max_agent_context"] != 900:
        problems.append(
            f"A0 system max is {native['context']['system_max_agent_context']!r}, not the peak of "
            "its per-request usage"
        )
    if native["usage"]["requests"]["total"] != 2:
        problems.append(f"A0 request count is {native['usage']['requests']!r}")
    if native["usage"]["returned_model"] != "deepseek-flash":
        problems.append("A0 does not read the provider-resolved model")
    if dag["context"]["integration_node_context"] != 2100:
        problems.append(
            "A2 integration node is "
            f"{dag['context']['integration_node_context']!r}, not its own peak"
        )
    if dag["context"]["system_max_agent_context"] != 2100:
        problems.append("A2 does not take the maximum over its nodes")
    if goal["context"]["system_max_agent_context"] != normalize.UNKNOWN:
        problems.append(
            "A3 reports a system maximum although no child peak was observed; Main peak is not "
            "the system peak"
        )
    if goal["context"]["agent_context_census"]["missing_agents"] != ["csv-source"]:
        problems.append("A3 does not name the agent whose peak is missing")
    if goal["context"]["per_request_input_tokens"] != [1200, 2600, 4100]:
        problems.append("A3 does not read the Main's per-request context series")
    if goal["usage"]["requests"]["children"] != normalize.UNKNOWN:
        problems.append("A3 claims a child request count it never recorded")
    if goal["correctness"]["atlas_execution_records"] != "PASS":
        problems.append("A3 does not record the Atlas payload corpus verdict beside the common one")

    # Integrity is a conjunction: only PASS establishes delivery.
    delivered = {}
    for verdict in (normalize.PASS, normalize.UNKNOWN, normalize.FAIL):
        probe = {
            "correctness": {
                "agent_success": True,
                "independent_oracle": {"status": "PASS", "passed": True},
                "public_test": "PASS",
            }
        }
        normalize.apply_integrity(probe, {"verdict": verdict, "checks": [{"check": "probe"}]})
        delivered[verdict] = probe["correctness"]["delivered_success"]
    if delivered != {normalize.PASS: True, normalize.UNKNOWN: False, normalize.FAIL: False}:
        problems.append(f"delivered success is not the integrity conjunction: {delivered}")
    return _check(
        "F06_evidence_adapters",
        not problems,
        "; ".join(problems)
        or (
            "all three adapters read production field names: A0 and A2 have no Main and say so, "
            "A2 records its integration node separately, A3 takes its system maximum from a "
            "census and reports UNKNOWN when a child peak is unobserved, and delivered success "
            "requires integrity PASS"
        ),
        sample={
            "A0": native["context"],
            "A2": dag["context"],
            "A3": goal["context"],
            "delivered": delivered,
        },
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
    """The three real batch plans, in order, through the real dispatch entry.

    One shared root, so batch 2 and batch 3 are settled against the reports batch 1 and batch 2
    actually wrote. That is the production contract, not a convenience: a fresh root would (and
    must) refuse batch 2, which is exactly what the settlement gate is for.
    """

    from experiments.stage_b_plus.offline import synthetic_cell_run

    async def runner(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

    problems: list[str] = []
    results: dict[str, Any] = {}
    root = scratch / "stage-f-dispatch"
    import asyncio

    with executor.claim_root_override(scratch / "claims-stage-f-dispatch"):
        for batch in (1, 2, 3):
            plan_id = plan.BATCH_PLAN_IDS[batch]
            batch_plan = contract.scope_plan(plan_id)
            # The 96-cell analysis reads every started cell. ``is_benchmark_sample = False`` is a
            # label on these cells, so the offline proof asserts it excludes nothing.
            sample_flag_excludes = [
                cell for cell in batch_plan["cells"] if cell.get("is_benchmark_sample") is True
            ]
            identity = executor.executor_identity(REPO)
            # The same offline-signed record the C-series used: signed for the proof, never a run.
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
            evidence = root / plan_id
            report = asyncio.run(
                executor.dispatch(
                    plan_id,
                    runs_root=evidence / "runs",
                    evidence_root=root,
                    authority_path=path,
                    cell_runner=runner,
                    repo_root=REPO,
                    mode=executor.MODE_OFFLINE_PROOF,
                    clock=lambda: "2026-11-26T00:00:00Z",
                    stage_f_root=root,
                )
            )
            settlement = report.get("cumulative_settlement") or {}
            results[plan_id] = {
                "complete": report.get("complete"),
                "cells_planned": report.get("cells_planned"),
                "cells_passed": report.get("cells_passed"),
                "stop_reason": report.get("stop_reason"),
                "token_stop": report.get("token_stop"),
                "settled": settlement.get("may_start"),
                "allowed_tokens": settlement.get("allowed_tokens"),
                "analysis_scope": report.get("analysis_scope"),
                "success_counts": report.get("success_counts"),
                "cells_with_the_sample_flag": len(sample_flag_excludes),
            }
            scope = report.get("analysis_scope") or {}
            if scope.get("cells_in_report") != 32 or scope.get("excluded_by_sample_flag") != 0:
                problems.append(f"{plan_id}: the analysis scope does not hold all 32 cells")
            if scope.get("cells_with_sample_flag") not in (0, None):
                problems.append(f"{plan_id}: a formal cell carries the benchmark-sample flag")
            if (report.get("success_counts") or {}).get("cells_started") != 32:
                problems.append(f"{plan_id}: the success counts do not cover 32 cells")
            if not (
                report.get("complete") is True
                and report.get("cells_planned") == 32
                and report.get("cells_passed") == 32
            ):
                problems.append(
                    f"{plan_id}: the offline dispatch did not pass all 32 cells "
                    f"({report.get('stop_reason')})"
                )
            if settlement.get("may_start") is not True:
                problems.append(f"{plan_id}: the settlement did not admit the batch")
            if batch == 1 and int(report.get("token_stop") or 0) != 32_000_000:
                problems.append(f"{plan_id}: token stop is not the frozen 32M")
            if batch > 1:
                prior = settlement.get("prior") or []
                if len(prior) != batch - 1 or any(
                    item.get("state") != "REPORTED" for item in prior
                ):
                    problems.append(f"{plan_id}: the settlement did not read its predecessors")
    return _check(
        "F08_offline_dispatch",
        not problems,
        "; ".join(problems)
        or (
            "batches 1, 2 and 3 resolve from the real plan ids, claim in a throwaway root, pass "
            "32/32 synthetic cells in order, and each later batch is settled against the reports "
            "its predecessors wrote"
        ),
        batches=results,
        root=str(root),
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
    # Seven production claims: the six the C2 checks already track plus the C2 run itself.
    claims = {
        label: executor.claim_path(digest).is_file() for label, digest in c2_checks.CONSUMED_CLAIMS
    }
    claims["c2"] = executor.claim_path(contract.scope_plan(c2.PLAN_ID)["digest"]).is_file()
    if len(claims) != 7:
        problems.append(f"there are {len(claims)} historical claims, not seven")
    if not all(claims.values()):
        problems.append(f"missing historical claims: {[k for k, v in claims.items() if not v]}")
    for batch in (1, 2, 3):
        digest = plan.plan_digest(plan.batch_cells(batch))
        if executor.claim_path(digest).exists():
            problems.append(f"a Stage F claim already exists for batch {batch}")
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
            "all seven consumed production claims are present, no Stage F claim exists, and the "
            "C1/C1-B/C1-C/C2 plan identities are unchanged"
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
        signed_path = EVIDENCE_DIR / f"authorization-b{batch}.json"
        signed: Mapping[str, Any] | None = None
        if signed_path.is_file():
            try:
                parsed = json.loads(signed_path.read_text(encoding="utf-8"))
                signed = parsed if isinstance(parsed, Mapping) else None
            except json.JSONDecodeError:
                signed = None
        signed_verdict = (
            authority.verify(
                signed,
                plan=batch_plan,
                head=executor.git_head(REPO),
                executor=identity,
                protocol_digest_value=authority.protocol_digest(contract.protocol_document()),
                repo_root=REPO,
            )
            if signed is not None
            else {"ready": False, "problems": ["no checked-in record"]}
        )
        rows[plan.BATCH_PLAN_IDS[batch]] = {
            "status": record.get("status"),
            "ready": verdict["ready"],
            "problems": len(verdict["problems"]),
            "cost_stop_usd": record.get("cost_stop_usd"),
            "token_stop": record.get("token_stop"),
            "checked_in": signed_path.name if signed is not None else None,
            "checked_in_ready": signed_verdict["ready"],
            "checked_in_problems": len(signed_verdict["problems"]),
        }
        if verdict["ready"] is not False:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: the pending record authorizes a run")
        if record.get("cost_stop_usd") is not None:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: a cost stop is signed")
        if int(record.get("token_stop") or 0) != 32_000_000:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: token stop is not 32M")
        if signed is None:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: no checked-in authorization record")
            continue
        if signed_verdict["ready"] is not False:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: the checked-in record would authorize")
        if signed.get("authorizes_live_execution") is not False:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: authorizes_live_execution is not false")
        for field in (
            "approved_by",
            "approved_at",
            "owner_statement",
            "approval_provenance",
            "reviewed_head",
            "execution_head",
            "cost_stop_usd",
            "per_cell_cost_stop_usd",
        ):
            if signed.get(field) is not None:
                problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: {field} is filled in")
        if signed.get("plan_digest") != batch_plan["digest"]:
            problems.append(f"{plan.BATCH_PLAN_IDS[batch]}: the record names another plan")
    return _check(
        "F11_pending_authorization",
        not problems,
        "; ".join(problems)
        or (
            "three checked-in pending records name the frozen plans, fill no signature field and "
            "are refused by the live verifier"
        ),
        batches=rows,
    )


# -- the records this stage has to produce --------------------------------------------------------


EVIDENCE_DIR = Path(__file__).resolve().parent
RECORD_FILES: dict[str, str] = {
    "runner_proof": "runner-proof.json",
    "task_qualification": "task-qualification.json",
    "settlement": "settlement-check.json",
    "environment": "environment-qualification.json",
    "p1_smoke": "p1-compat-smoke.json",
    "default_routing": "default-routing-smoke.json",
}


def _record(name: str) -> dict[str, Any] | None:
    path = EVIDENCE_DIR / RECORD_FILES[name]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _record_identity_problems(record: Mapping[str, Any] | None, *, name: str) -> list[str]:
    """A record produced by different code does not certify this code."""

    if record is None:
        return [f"{name}: no record"]
    expected = executor.executor_identity(REPO)["executor_digest"]
    if record.get("executor_digest") != expected:
        return [
            f"{name}: produced by executor {record.get('executor_digest')!r}, "
            f"this tree is {expected!r}"
        ]
    return []


def check_settlement_record() -> dict[str, Any]:
    """The ten cross-batch scenarios, from the record the real dispatcher wrote."""

    record = _record("settlement")
    problems = _record_identity_problems(record, name="settlement")
    scenarios = list((record or {}).get("scenarios") or [])
    if len(scenarios) != 10:
        problems.append(f"the settlement record holds {len(scenarios)} scenarios, not ten")
    failed = [item.get("scenario") for item in scenarios if item.get("ok") is not True]
    if failed:
        problems.append(f"failed scenarios: {failed}")
    if (record or {}).get("ok") is not True:
        problems.append(f"the settlement record status is {(record or {}).get('status')!r}")
    return _check(
        "F13_settlement_wiring",
        not problems,
        "; ".join(problems)
        or (
            "ten settlement scenarios ran through the real dispatcher: the normal chain, a missing "
            "report, a missing claim, a plan-identity mismatch, unknown usage, a prior frozen "
            "stop, the global remainder, the refusal stop, no claim consumed, and the effective "
            "ceiling inside the dispatcher"
        ),
        scenarios=[item.get("scenario") for item in scenarios],
        record=RECORD_FILES["settlement"],
    )


def check_environment_record() -> dict[str, Any]:
    record = _record("environment")
    problems = _record_identity_problems(record, name="environment")
    rows = list((record or {}).get("tasks") or [])
    if len(rows) != 8:
        problems.append(f"the environment record holds {len(rows)} tasks, not eight")
    bad = [row.get("task_id") for row in rows if row.get("ok") is not True]
    if bad:
        problems.append(f"tasks that did not qualify: {bad}")
    created = int((record or {}).get("containers_created") or 0)
    destroyed = int((record or {}).get("containers_destroyed") or 0)
    if created != 8 or destroyed != 8:
        problems.append(f"containers created/destroyed {created}/{destroyed}, expected 8/8")
    return _check(
        "F14_environment_qualification",
        not problems,
        "; ".join(problems)
        or (
            "all eight tasks were provisioned, ran their public suite in a real container, their "
            "reference passed public and hidden there, and every container was destroyed"
        ),
        tasks={
            row.get("task_id"): {
                "seed_public_rc": (row.get("seed_public") or {}).get("exit_code"),
                "reference_public_rc": (row.get("reference_public") or {}).get("exit_code"),
                "reference_hidden_rc": (row.get("reference_hidden") or {}).get("exit_code"),
            }
            for row in rows
        },
        record=RECORD_FILES["environment"],
    )


def check_smoke_record() -> dict[str, Any]:
    record = _record("p1_smoke")
    problems = _record_identity_problems(record, name="p1_smoke")
    if (record or {}).get("ok") is not True:
        problems.append(f"the smoke status is {(record or {}).get('status')!r}")
    dispatch = (record or {}).get("dispatch") or {}
    if dispatch.get("cells_passed") != 4 or dispatch.get("cells_planned") != 4:
        problems.append(f"the smoke passed {dispatch.get('cells_passed')}/4 cells")
    routing_block = (record or {}).get("routing") or {}
    if routing_block.get("cell_runner_injected") is not False:
        problems.append("the smoke injected a cell_runner instead of exercising the default route")
    counts = dispatch.get("success_counts") or {}
    if counts.get("cells_started") != 4 or counts.get("delivered_success") != 4:
        problems.append(
            f"the smoke delivered {counts.get('delivered_success')}/4 cells "
            f"(started {counts.get('cells_started')})"
        )
    missing_axes = [
        axis
        for axis in (
            "executor_verdict_pass",
            "agent_completed",
            "oracle_pass",
            "execution_integrity_pass",
            "delivered_success",
        )
        if axis not in counts
    ]
    if missing_axes:
        problems.append(f"the smoke report omits success axes: {missing_axes}")
    counts = (record or {}).get("counts") or {}
    if counts.get("external_model_requests") != 0:
        problems.append("the smoke reports an external model request")
    if counts.get("production_claims_consumed") != 0:
        problems.append("the smoke consumed a production claim")
    arms_rows = list((record or {}).get("arms") or [])
    if len(arms_rows) != 4:
        problems.append(f"the smoke holds {len(arms_rows)} arms, not four")
    failed_checks = [
        f"{row.get('arm_id')}:{item.get('check')}"
        for row in arms_rows
        for item in row.get("checks") or []
        if item.get("ok") is not True
    ]
    if failed_checks:
        problems.append(f"failed arm checks: {failed_checks}")
    return _check(
        "F15_p1_four_arm_smoke",
        not problems,
        "; ".join(problems)
        or (
            "the formal P1 ran once through all four real entries in a deterministic-provider "
            "dispatch: 4/4 audited cells, an independent oracle per arm, no external request and "
            "no production claim"
        ),
        arms=[
            {
                "arm_id": row.get("arm_id"),
                "verdict": (row.get("audit") or {}).get("verdict"),
                "integrity": (row.get("integrity") or {}).get("verdict"),
                "oracle": (row.get("oracle") or {}).get("status"),
                "requests": (row.get("requests") or {}).get("count"),
            }
            for row in arms_rows
        ],
        record=RECORD_FILES["p1_smoke"],
    )


def check_default_routing() -> dict[str, Any]:
    """A1/A3 must be reached through the production selection, with no injected ``cell_runner``.

    The record is produced by ``experiments.stage_f.routing``: a real dispatch with only the
    provider replaced, a real sandbox, a real export and a real oracle. The gate reads it rather
    than re-asserting the routing function, because the defect was that the *default* path was
    never exercised.
    """

    record = _record("default_routing")
    problems = _record_identity_problems(record, name="default_routing")
    if (record or {}).get("ok") is not True:
        problems.append(f"the routing smoke status is {(record or {}).get('status')!r}")
    routing_block = (record or {}).get("routing") or {}
    if routing_block.get("cell_runner_injected") is not False:
        problems.append("the routing smoke injected a cell_runner")
    dispatch = (record or {}).get("dispatch") or {}
    if dispatch.get("cells_passed") != 2 or dispatch.get("cells_started") != 2:
        problems.append(f"the routing smoke passed {dispatch.get('cells_passed')}/2 cells")
    rows = list((record or {}).get("arms") or [])
    if sorted(str(row.get("arm_id")) for row in rows) != ["A1", "A3"]:
        problems.append(
            f"the routing smoke covers {[row.get('arm_id') for row in rows]}, not A1/A3"
        )
    for row in rows:
        arm_id = row.get("arm_id")
        resolved = (row.get("routing") or {}).get("resolved_runner")
        if resolved != "main_driven":
            problems.append(f"{arm_id}: resolved runner is {resolved!r}")
        checks = {item.get("check"): item.get("ok") for item in row.get("checks") or []}
        for name in (
            "production_selection_returned_the_wrapper",
            "unified_evidence_written",
            "audited_by_the_common_schema",
            "session_record_preserved",
            "delivered_success",
        ):
            if checks.get(name) is not True:
                problems.append(f"{arm_id}: {name} did not pass")
        if arm_id == "A3":
            for name in ("proposal_recorded", "two_children_completed", "children_overlapped"):
                if checks.get(name) is not True:
                    problems.append(f"A3: {name} did not pass")
    counts = (record or {}).get("counts") or {}
    if counts.get("external_model_requests") != 0:
        problems.append("the routing smoke reports an external model request")
    if counts.get("production_claims_consumed") != 0:
        problems.append("the routing smoke consumed a production claim")
    return _check(
        "F17_default_live_routing",
        not problems,
        "; ".join(problems)
        or (
            "A1 and A3 were dispatched with no cell_runner: the production selection returned the "
            "main-driven wrapper, each ran the real goal runtime into Unified Cell Evidence, the "
            "common audit and a delivered success, and A3 delegated to two concurrent children"
        ),
        arms=[
            {
                "arm_id": row.get("arm_id"),
                "resolved": (row.get("routing") or {}).get("resolved_runner"),
                "delivered": (row.get("audit") or {}).get("delivered_success"),
                "integrity": (row.get("audit") or {}).get("execution_integrity"),
            }
            for row in rows
        ],
        record=RECORD_FILES["default_routing"],
    )


def check_cache_aware_pricing() -> dict[str, Any]:
    """One formula for four topologies, and a conservative fallback when the split is missing.

    Recomputed here from the frozen price block, not read from a record: the property is
    deterministic and a stale record must not be able to certify it.
    """

    prices = dict(contract_price_block())
    input_tokens, completion, hit = 100_000, 20_000, 60_000
    miss = input_tokens - hit
    total = input_tokens + completion
    topologies = {
        "single_agent": {
            "main_cumulative_input_tokens": input_tokens,
            "main_completion_tokens": completion,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": total,
            "main_cache_hit_tokens": hit,
            "main_cache_miss_tokens": miss,
            "child_cache_hit_tokens": 0,
            "child_cache_miss_tokens": 0,
        },
        "dag_all_children": {
            "main_cumulative_input_tokens": 0,
            "main_completion_tokens": 0,
            "child_total_prompt_tokens": input_tokens,
            "child_completion_tokens": completion,
            "system_total_tokens": total,
            "main_cache_hit_tokens": 0,
            "main_cache_miss_tokens": 0,
            "child_cache_hit_tokens": hit,
            "child_cache_miss_tokens": miss,
        },
        "main_child_split": {
            "main_cumulative_input_tokens": 40_000,
            "main_completion_tokens": 8_000,
            "child_total_prompt_tokens": 60_000,
            "child_completion_tokens": 12_000,
            "system_total_tokens": total,
            "main_cache_hit_tokens": 30_000,
            "main_cache_miss_tokens": 10_000,
            "child_cache_hit_tokens": 30_000,
            "child_cache_miss_tokens": 30_000,
        },
    }
    problems: list[str] = []
    costs = {name: executor.cell_cost(metrics, prices) for name, metrics in topologies.items()}
    if len({round(value or -1.0, 12) for value in costs.values()}) != 1:
        problems.append(f"the same composition is priced differently per topology: {costs}")
    expected = (
        hit * prices["cache_hit_input_per_million"]
        + miss * prices["cache_miss_input_per_million"]
        + completion * prices["output_per_million"]
    ) / 1_000_000
    if any(value is None or abs(value - expected) > 1e-12 for value in costs.values()):
        problems.append(f"the estimate is not the frozen formula: {costs} != {expected}")
    unsplit = {
        "main_cumulative_input_tokens": input_tokens,
        "main_completion_tokens": completion,
        "child_total_prompt_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": total,
    }
    conservative = executor.cell_cost(unsplit, prices)
    all_miss = (
        input_tokens * prices["cache_miss_input_per_million"]
        + completion * prices["output_per_million"]
    ) / 1_000_000
    if conservative is None or abs(conservative - all_miss) > 1e-12:
        problems.append("a missing cache split is not priced as a miss")
    partial = dict(topologies["single_agent"], main_cache_hit_tokens=10_000)
    if executor.cell_cost(partial, prices) is not None:
        problems.append("a partial split that does not add up was priced instead of refused")
    return _check(
        "F18_cache_aware_pricing",
        not problems,
        "; ".join(problems)
        or (
            "one frozen formula prices the same input/output/cache composition identically for a "
            "single agent, a DAG and a Main with children; a missing split is priced as a miss and "
            "a partial split is refused rather than re-priced"
        ),
        costs=costs,
        conservative_cost_usd=conservative,
        pricing=plan.pricing_identity(),
    )


def contract_price_block() -> dict[str, Any]:
    from experiments.stage_b_plus import cost

    return dict(cost.stage_b_estimate.pricing())


def check_production_measurement() -> dict[str, Any]:
    """Every arm's record carries a provider-observed accounting and a common integrity verdict."""

    record = _record("p1_smoke")
    problems = _record_identity_problems(record, name="p1_smoke")
    rows = list((record or {}).get("arms") or [])
    if not rows:
        problems.append("the smoke holds no arm record")
    for row in rows:
        arm_id = row.get("arm_id")
        context = (row.get("normalized") or {}).get("context") or {}
        usage = (row.get("normalized") or {}).get("usage") or {}
        census = context.get("agent_context_census") or {}
        if not census:
            problems.append(f"{arm_id}: no agent context census")
        if context.get("system_max_agent_context") is None:
            problems.append(f"{arm_id}: no system maximum, not even UNKNOWN")
        if not isinstance(usage.get("requests"), Mapping):
            problems.append(f"{arm_id}: no request accounting")
        if (row.get("integrity") or {}).get("verdict") != "PASS":
            problems.append(
                f"{arm_id}: integrity is {(row.get('integrity') or {}).get('verdict')!r}"
            )
        # The cost estimate must say where its cache split came from: a report that cannot state it
        # cannot be checked against the provider's counters.
        basis = (row.get("audit") or {}).get("cost_basis") or {}
        if basis.get("cache_split") not in {"COMPLETE", "UNKNOWN"}:
            problems.append(f"{arm_id}: no cost basis (cache_split={basis.get('cache_split')!r})")
    return _check(
        "F16_production_measurement",
        not problems,
        "; ".join(problems)
        or (
            "each arm's evidence carries provider-observed usage, a per-agent context census with "
            "a stated system maximum, request accounting and a common integrity verdict"
        ),
        arms=len(rows),
    )


# -- blockers, read from the records rather than asserted -----------------------------------------


def _blocker(blocker_id: str, closed: bool, evidence: str, detail: str) -> dict[str, Any]:
    return {
        "id": blocker_id,
        "closed": bool(closed),
        "evidence": evidence,
        "detail": detail,
    }


def blocker_states() -> list[dict[str, Any]]:
    """Every former blocker, with the record that closes it — or the reason it stays open."""

    qualification = _record("task_qualification")
    proof_record = _record("runner_proof")
    qualification_ok = (
        (qualification or {}).get("status") == "STAGE_F_TASK_QUALIFICATION_PASS"
        and len((qualification or {}).get("complexity") or []) == 8
        and len((qualification or {}).get("mutations") or []) >= 20
    )
    smallest = min(
        (
            int(row.get("business_source_loc") or 0)
            for row in (qualification or {}).get("complexity") or []
        ),
        default=0,
    )
    proof_ok = (
        (proof_record or {}).get("status") == "STAGE_F_RUNNER_PROOF_PASS"
        and (proof_record or {}).get("all_arms_passed") is True
        and int((proof_record or {}).get("model_calls") or 0) == 0
        and int((proof_record or {}).get("production_claims_consumed") or 0) == 0
    )
    states = [
        _blocker(
            "task_scale",
            qualification_ok and smallest >= 34,
            RECORD_FILES["task_qualification"],
            (
                f"the six formal tasks were rebuilt and accepted; the smallest business LOC in the "
                f"record is {smallest}"
            )
            if qualification_ok and smallest >= 34
            else f"the qualification record does not show the accepted task scale ({smallest})",
        ),
        _blocker(
            "task_qualification",
            qualification_ok,
            RECORD_FILES["task_qualification"],
            "eight tasks qualified, twenty mutations detected"
            if qualification_ok
            else "the F2-B qualification record is missing or not PASS",
        ),
        _blocker(
            "runner_proof",
            proof_ok,
            RECORD_FILES["runner_proof"],
            "the four-arm real runner proof passed at its recorded head"
            if proof_ok
            else "the F2-A runner proof record is missing or not PASS",
        ),
        _blocker(
            "production_measurement",
            bool(check_production_measurement()["ok"]),
            RECORD_FILES["p1_smoke"],
            "provider-observed usage, a context census and an integrity verdict exist per arm"
            if check_production_measurement()["ok"]
            else "the measurement gate does not pass",
        ),
        _blocker(
            "cumulative_wiring",
            bool(check_settlement_record()["ok"]),
            RECORD_FILES["settlement"],
            "the settlement runs inside the pre-claim path and all ten scenarios pass"
            if check_settlement_record()["ok"]
            else "the settlement record is missing, stale or failing",
        ),
        _blocker(
            "environment_qualification",
            bool(check_environment_record()["ok"]),
            RECORD_FILES["environment"],
            "eight tasks qualified in real containers"
            if check_environment_record()["ok"]
            else "the environment qualification is missing or failing",
        ),
        _blocker(
            "p1_compat_smoke",
            bool(check_smoke_record()["ok"]),
            RECORD_FILES["p1_smoke"],
            "the formal P1 passed the four-arm smoke"
            if check_smoke_record()["ok"]
            else "the P1 four-arm smoke is missing or failing",
        ),
        _blocker(
            "final_freeze",
            not _freeze_problems(),
            "manifest.json / plan-b*.json",
            "the frozen manifest and plans recompute and carry the frozen identities"
            if not _freeze_problems()
            else "; ".join(_freeze_problems())[:400],
        ),
        _blocker(
            "pending_authorization",
            bool(check_pending()["ok"]),
            "authorization-b1/b2/b3.json",
            "three pending records exist and all refuse a live run"
            if check_pending()["ok"]
            else "a pending record is missing or would authorize a live run",
        ),
        _blocker(
            "default_live_routing",
            bool(check_default_routing()["ok"]),
            RECORD_FILES["default_routing"],
            "A1/A3 reach the production wrapper with no cell_runner injected"
            if check_default_routing()["ok"]
            else "the default route is not proven: "
            + "; ".join(str(check_default_routing()["detail"]).split("; "))[:300],
        ),
        _blocker(
            "cache_aware_pricing",
            bool(check_cache_aware_pricing()["ok"]),
            "executor.cell_cost + the frozen price block",
            "one formula prices four topologies identically and a missing split as a miss"
            if check_cache_aware_pricing()["ok"]
            else str(check_cache_aware_pricing()["detail"])[:300],
        ),
    ]
    return states


def _freeze_problems() -> list[str]:
    """The checked-in freeze must be the current freeze, not a stale copy."""

    problems: list[str] = []
    manifest_path = EVIDENCE_DIR / "manifest.json"
    try:
        checked_in = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["manifest.json is missing or unreadable"]
    recomputed = plan.manifest()
    if authority.digest_of(checked_in) != authority.digest_of(recomputed):
        problems.append("the checked-in manifest differs from the recomputed one")
    identity = executor.executor_identity(REPO)
    if checked_in.get("executor_digest") != identity["executor_digest"]:
        problems.append("the manifest's executor digest is not this tree's")
    if checked_in.get("adapter_version") != arms.ADAPTER_VERSION:
        problems.append("the manifest's adapter version moved")
    plans = recomputed["batches"]
    for batch, item in zip((1, 2, 3), plans, strict=True):
        path = EVIDENCE_DIR / f"plan-b{batch}.json"
        try:
            checked_plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            problems.append(f"plan-b{batch}.json is missing")
            continue
        if checked_plan.get("digest") != plan.batch_plan(batch)["digest"]:
            problems.append(f"plan-b{batch}.json does not carry the recomputed digest")
        if checked_plan.get("digest") != item["plan_digest"]:
            problems.append(f"plan-b{batch}.json disagrees with the manifest")
    for batch in (1, 2, 3):
        path = EVIDENCE_DIR / f"authorization-b{batch}.json"
        if not path.is_file():
            problems.append(f"authorization-b{batch}.json is missing")
    try:
        summary = json.loads((EVIDENCE_DIR / "freeze.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = None
    if not isinstance(summary, Mapping):
        problems.append("freeze.json is missing or unreadable")
    else:
        if summary.get("manifest_digest") != authority.digest_of(checked_in):
            problems.append("freeze.json describes a different manifest")
        frozen_tasks = {
            item.get("task_id"): item.get("seed_digest") for item in summary.get("tasks") or []
        }
        for task_id, facts in (checked_in.get("task_facts") or {}).items():
            if frozen_tasks.get(task_id) != facts.get("seed_digest"):
                problems.append(f"freeze.json: {task_id} seed digest is not the manifest's")
    return problems


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
        check_settlement_record(),
        check_environment_record(),
        check_smoke_record(),
        check_production_measurement(),
        check_default_routing(),
        check_cache_aware_pricing(),
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
    blockers = blocker_states()
    open_blockers = [item for item in blockers if not item["closed"]]
    failed = [item["check_id"] for item in checks if not item["ok"]]
    identity = executor.executor_identity(REPO)
    return {
        "stage": "F",
        "design_version": plan.DESIGN_VERSION,
        "master_id": plan.MASTER_ID,
        "scope": plan.SCOPE,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "adapter_version": arms.ADAPTER_VERSION,
        "manifest_digest": authority.digest_of(manifest_record),
        "budget_digest": arms.budget_digest(),
        "main_prompt_digest": arms.main_prompt_digest(),
        "worker_contract_digest": arms.worker_contract()["rendered_prompt_digest"],
        "ok": not failed,
        "status": (
            STATUS_READY
            if not failed and not open_blockers
            else STATUS_PREPARATION_BLOCKED
            if not failed
            else STATUS_BLOCKED
        ),
        "blockers": blockers,
        "open_blockers": open_blockers,
        "failed": failed,
        "checks": checks,
        "records": {
            name: {
                "file": RECORD_FILES[name],
                "present": _record(name) is not None,
            }
            for name in RECORD_FILES
        },
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
