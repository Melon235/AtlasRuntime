"""Stage F — the two cell runners the goal runtime does not cover, and their audit.

``A1``/``A3`` need nothing here: they are the ordinary main-driven cell. ``A0`` (native single
agent) and ``A2`` (static DAG) each call their real entry with the cell's frozen
configuration, normalise the result into Unified Cell Evidence, and are audited by that
do not have. No Main field is invented for either.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import executor
from experiments.stage_f import normalize
from experiments.stage_f.tasks import TASKS_BY_ID


def _cell_root(context: Mapping[str, Any], cell: Mapping[str, Any]) -> Path:
    return Path(context["evidence_root"]) / "cells" / f"c{int(cell['order']):02d}"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _benchmark_task(cell: Mapping[str, Any], *, multi: bool) -> Any:
    from experiments.evaluation.closure2 import catalog as closure2

    task = TASKS_BY_ID[str(cell["task_id"])]
    return closure2.build_task(task, topology="multi" if multi else "single")


# -- A0 ----------------------------------------------------------------------------------------


def native_cell_runner() -> executor.CellRunner:
    """A0: one native agent, one guarded cell budget, per-request metering."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation.closure1b import native_baseline
        from experiments.evaluation.runner import RunnerSettings

        task = TASKS_BY_ID[str(cell["task_id"])]
        root = _cell_root(context, cell)
        fixture_dir = Path(executor.prepare_fixture(task, root / "fixture")["path"])
        settings = RunnerSettings(
            benchmark_id=str(cell["plan_id"]),
            runs_root=root / "native-runs",
            max_turns=int(cell.get("max_turns") or 40),
            token_budget=int(cell["token_budget"]),
            budget_mode="guarded",
            max_tool_calls=200,
            model_override=str((cell.get("budget") or {}).get("model") or "deepseek-chat"),
        )
        limit = int(cell["token_budget"])
        result = await native_baseline.run_native_sample(
            _benchmark_task(cell, multi=False),
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
            runs_root=root / "native-runs",
            budget_limit_tokens=limit,
        )
        raw = (
            json.loads(json.dumps(result.to_dict(), default=str))
            if hasattr(result, "to_dict")
            else {}
        )
        oracle = raw.get("oracle") or {}
        export = raw.get("export") or {}
        evidence = normalize.from_native_result(cell, raw, oracle=oracle, export=export)
        problems = normalize.validate_evidence(evidence)
        evidence["adapter_problems"] = problems
        _write_json(root / "evidence.json", evidence)
        return executor.CellRun(
            session=evidence,
            run_dir=Path(raw.get("run_dir") or root),
            fixture_root=fixture_dir,
            oracle=oracle or None,
            fixture={"matches_frozen_seed": True},
            export=export or None,
        )

    return run


# -- A2 ----------------------------------------------------------------------------------------


def static_dag_cell_runner() -> executor.CellRunner:
    """A2: the frozen human decomposition, run by the AnyCode scheduler on one shared budget."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation import runner as eval_runner

        task = TASKS_BY_ID[str(cell["task_id"])]
        root = _cell_root(context, cell)
        fixture_dir = Path(executor.prepare_fixture(task, root / "fixture")["path"])
        result = await eval_runner.run_sample(
            _benchmark_task(cell, multi=True),
            settings=eval_runner.RunnerSettings(
                benchmark_id=str(cell["plan_id"]),
                runs_root=root / "dag-runs",
                max_turns=int(cell.get("worker_max_turns") or 20),
                token_budget=int(cell["token_budget"]),
                budget_mode="guarded",
                ownership_mode="off",
                execution_integrity_mode="record",
                oracle_mode="sandbox",
            ),
        )
        raw = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        oracle = raw.get("oracle") or {}
        export = raw.get("export") or {}
        evidence = normalize.from_dag_result(cell, raw, oracle=oracle, export=export)
        problems = normalize.validate_evidence(evidence)
        evidence["adapter_problems"] = problems
        _write_json(root / "evidence.json", evidence)
        return executor.CellRun(
            session=evidence,
            run_dir=Path(raw.get("run_dir") or root),
            fixture_root=fixture_dir,
            oracle=oracle or None,
            fixture={"matches_frozen_seed": True},
            export=export or None,
        )

    return run


# -- audit -------------------------------------------------------------------------------------


def audit_stage_f_cell(
    *,
    cell: Mapping[str, Any],
    run: executor.CellRun,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """The Stage F branch of ``executor.audit_cell``.

    Same outcome shape the dispatch loop consumes, read from Unified Cell Evidence. A schema problem
    is a ``MODE_VIOLATION`` stop: a record that cannot be read must not be scored as a run.
    """

    evidence = run.session or {}
    problems = normalize.validate_evidence(evidence)
    correctness = evidence.get("correctness") or {}
    usage = evidence.get("usage") or {}
    budget = usage.get("budget") or {}
    oracle = correctness.get("independent_oracle") or {}
    checks = [
        {"check": "evidence_schema", "ok": not problems, "detail": "; ".join(problems)[:400]},
        {
            "check": "identity_matches_plan",
            "ok": evidence.get("cell_id") == cell["cell_id"],
            "detail": f"cell id {evidence.get('cell_id')!r}",
        },
        {
            "check": "attribution_matches_arm",
            "ok": evidence.get("usage_attribution") == cell.get("usage_attribution"),
            "detail": f"attribution {evidence.get('usage_attribution')!r}",
        },
        {
            "check": "independent_oracle_ran",
            "ok": oracle.get("status") in {"PASS", "FAIL"},
            "detail": f"oracle {oracle.get('status')!r}",
        },
    ]
    if run.error:
        checks.append({"check": "runner_returned", "ok": False, "detail": run.error[:300]})
    failed = [item["check"] for item in checks if not item["ok"]]
    stops = [executor.STOP_MODE_VIOLATION] if "evidence_schema" in failed else []

    system_total = usage.get("system_total_tokens")
    outcome = {
        "cell_id": cell["cell_id"],
        "arm_id": cell["arm_id"],
        "task_id": cell["task_id"],
        "profile_id": cell.get("worker_profiles") or [],
        "exposure": cell.get("worker_exposure"),
        "status": "failed"
        if failed or correctness.get("agent_success") is not True
        else "succeeded",
        "failure_kind": correctness.get("failure_kind"),
        "agent_success": correctness.get("agent_success"),
        "public_test_status": correctness.get("public_test"),
        "public_test_runs": 0,
        "public_test_problems": [],
        "static_acceptance": correctness.get("static_acceptance"),
        "oracle_passed": oracle.get("passed"),
        "oracle_status": oracle.get("status"),
        "goal_correctness": bool(oracle.get("passed") is True),
        "execution_integrity": correctness.get("execution_integrity"),
        "executor_verdict": "PASS"
        if not failed and oracle.get("passed") is True
        else "MEASURED_FAILURE",
        "checks": checks,
        "stops": stops,
        "usage": {
            "main_cumulative_input_tokens": (usage.get("main") or {}).get("prompt_tokens")
            if isinstance(usage.get("main"), Mapping)
            else 0,
            "main_completion_tokens": (usage.get("main") or {}).get("completion_tokens")
            if isinstance(usage.get("main"), Mapping)
            else 0,
            "child_total_prompt_tokens": (usage.get("children") or {}).get("prompt_tokens")
            if isinstance(usage.get("children"), Mapping)
            else 0,
            "child_completion_tokens": (usage.get("children") or {}).get("completion_tokens")
            if isinstance(usage.get("children"), Mapping)
            else 0,
            "system_total_tokens": system_total if isinstance(system_total, int) else 0,
        },
        "budget_stop_reason": budget.get("stop_reason"),
    }
    return outcome


__all__ = ["audit_stage_f_cell", "native_cell_runner", "static_dag_cell_runner"]
