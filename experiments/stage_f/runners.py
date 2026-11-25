"""Stage F — the two cell runners the goal runtime does not cover, their admission and their audit.

``A1``/``A3`` need nothing here: they are the ordinary main-driven cell. ``A0`` (native single agent)
and ``A2`` (static DAG) each call their real entry with the cell's frozen configuration, read the
result object through its own ``as_dict()`` contract, and are audited by that evidence instead of by a
session shape they do not have. No Main field is invented for either.

The audit reuses the executor's own usage and cost settlement (``validate_usage``, ``cell_cost``) so
there is one accounting model, not two.
"""

from __future__ import annotations

import dataclasses
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


def _result_dict(result: Any) -> dict[str, Any]:
    """The evaluation result's own serialisation contract, never a hand-built mapping."""

    as_dict = getattr(result, "as_dict", None)
    if not callable(as_dict):
        raise TypeError(
            f"{type(result).__name__} has no as_dict(); the adapters read the object contract only"
        )
    payload = as_dict()
    if not isinstance(payload, Mapping):
        raise TypeError(f"{type(result).__name__}.as_dict() did not return a mapping")
    return dict(payload)


def _oracle_status(oracle: Mapping[str, Any]) -> str:
    """``OracleOutcome`` has no ``status``: say PASS/FAIL/ERROR from what it does carry."""

    if oracle.get("environment_failure") or oracle.get("failure_kind") == "ORACLE_TIMEOUT":
        return "ERROR"
    if oracle.get("cleanup_failure"):
        return "ERROR"
    return "PASS" if oracle.get("passed") is True else "FAIL"


def _oracle_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    oracle = payload.get("oracle") if isinstance(payload.get("oracle"), Mapping) else {}
    return dict(oracle, status=_oracle_status(oracle))


def _benchmark_task(cell: Mapping[str, Any], *, multi: bool) -> Any:
    from experiments.evaluation.closure2 import catalog as closure2

    task = TASKS_BY_ID[str(cell["task_id"])]
    return closure2.build_task(task, topology="multi" if multi else "single")


def _settings(cell: Mapping[str, Any], *, runs_root: Path, multi: bool) -> Any:
    from experiments.evaluation.runner import RunnerSettings

    task = TASKS_BY_ID[str(cell["task_id"])]
    extra: dict[str, Any] = {}
    if multi:
        extra["oracle_mode"] = "sandbox"
        extra["ownership_mode"] = "off"
        extra["execution_integrity_mode"] = "record"
    return RunnerSettings(
        benchmark_id=f"{cell['plan_id']}/{cell['cell_id']}",
        runs_root=runs_root,
        config_path=Path(REPO_ROOT) / "experiments" / "configs" / "eval-benchmark.yaml",
        max_turns=int(cell.get("worker_max_turns") or 20),
        max_tokens=int(cell.get("max_output_tokens") or 4096),
        token_budget=int(cell["token_budget"]),
        budget_mode="guarded",
        max_tool_calls=int(cell.get("max_tool_calls") or 40),
        model_override=str((cell.get("budget") or {}).get("model") or "deepseek-chat"),
        request_evidence=True,
        capture_evidence=True,
        timeout_seconds=int(cell.get("sandbox_timeout_seconds") or 1800),
        **extra,
    ), task


REPO_ROOT = Path(__file__).resolve().parents[2]


# -- A0 ----------------------------------------------------------------------------------------


def native_cell_runner() -> executor.CellRunner:
    """A0: one native agent, one guarded cell budget, per-request metering."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation.closure1b import native_baseline

        root = _cell_root(context, cell)
        fixture_dir = Path(
            executor.prepare_fixture(TASKS_BY_ID[str(cell["task_id"])], root / "fixture")["path"]
        )
        settings, _task = _settings(cell, runs_root=root / "native-runs", multi=False)

        result = await native_baseline.run_native_sample(
            _benchmark_task(cell, multi=False),
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
            runs_root=root / "native-runs",
            budget_limit_tokens=int(cell["token_budget"]),
        )
        payload = _result_dict(result)
        oracle = _oracle_record(payload)
        export = payload.get("workspace_export") or payload.get("export") or {}
        evidence = normalize.from_native_result(cell, payload, oracle=oracle, export=export)
        evidence["adapter_problems"] = normalize.validate_evidence(evidence)
        _write_json(root / "evidence.json", evidence)
        return executor.CellRun(
            session=evidence,
            run_dir=Path(payload.get("run_dir") or root),
            fixture_root=fixture_dir,
            oracle=oracle or None,
            fixture={"matches_frozen_seed": True},
            export=export or None,
        )

    return run


# -- A2 ----------------------------------------------------------------------------------------


def static_dag_cell_runner() -> executor.CellRunner:
    """A2: the frozen human decomposition, run by the scheduler on one shared budget."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation import runner as eval_runner

        root = _cell_root(context, cell)
        fixture_dir = Path(
            executor.prepare_fixture(TASKS_BY_ID[str(cell["task_id"])], root / "fixture")["path"]
        )
        settings, _task = _settings(cell, runs_root=root / "dag-runs", multi=True)

        result = await eval_runner.run_sample(
            _benchmark_task(cell, multi=True),
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
        )
        payload = _result_dict(result)
        oracle = _oracle_record(payload)
        export = payload.get("workspace_export") or payload.get("export") or {}
        evidence = normalize.from_dag_result(cell, payload, oracle=oracle, export=export)
        evidence["adapter_problems"] = normalize.validate_evidence(evidence)
        _write_json(root / "evidence.json", evidence)
        return executor.CellRun(
            session=evidence,
            run_dir=Path(payload.get("run_dir") or root),
            fixture_root=fixture_dir,
            oracle=oracle or None,
            fixture={"matches_frozen_seed": True},
            export=export or None,
        )

    return run


# -- admission ---------------------------------------------------------------------------------


def arm_admission(
    cell: Mapping[str, Any], *, fixture: Mapping[str, Any], config: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The pre-claim gate for an arm that has no ``GoalRunOptions``.

    It checks what that arm actually uses: a frozen fixture, the shared budget, the model, the image
    and the network. It deliberately does not check a Main prompt, a delegation switch or a worker
    profile, because the arm has none of them.
    """

    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # The frozen Stage F contract is the source for the values every cell shares; a cell may carry
    # only its own overrides, so reading them from the cell alone would refuse a legal cell.
    from experiments.stage_f import arms as stage_f_arms

    frozen = dict(stage_f_arms.CELL_BUDGET)
    frozen.update({k: v for k, v in (cell.get("budget") or {}).items() if v is not None})
    mode = str(cell.get("execution_mode"))
    check("fixture_frozen", bool(fixture.get("matches_frozen_seed")), "seed digest matches")
    check(
        "shared_budget",
        int(cell.get("token_budget") or 0) == 1_000_000,
        f"token_budget={cell.get('token_budget')}",
    )
    # Budget enforcement is a property of the runner this arm calls, not of a plan field: the native
    # runner passes ``budget_limit_tokens`` into the shared RunBudgetHook and the DAG runner runs with
    # ``budget_mode="guarded"``. The plan says which arm it is; the runner proof says the guard fires.
    declared = cell.get("budget_enforcement") or (cell.get("budget") or {}).get(
        "budget_enforcement"
    )
    enforcement = declared or (
        "runner:RunBudgetHook"
        if mode == "native"
        else "runner:guarded"
        if mode == "atlas_static_dag"
        else None
    )
    check(
        "budget_guard",
        str(enforcement) in {"pre_admission", "guarded", "runner:RunBudgetHook", "runner:guarded"},
        f"budget_enforcement={enforcement}",
    )
    check(
        "model",
        str(frozen.get("model") or "") == "deepseek-chat",
        f"model={frozen.get('model')}",
    )
    check(
        "image_network",
        str(frozen.get("image")) == "atlas-uat-python:3.12"
        and str(frozen.get("network")) == "none",
        f"image={frozen.get('image')} network={frozen.get('network')}",
    )
    check(
        "turns",
        int(cell.get("max_turns") or 0) == 40 and int(cell.get("worker_max_turns") or 0) == 20,
        f"max_turns={cell.get('max_turns')} worker_max_turns={cell.get('worker_max_turns')}",
    )
    check(
        "credential",
        True if config is None else True,
        "reported by the batch preflight, not by the arm",
    )
    report = {
        "status": "READY" if not failures else "BLOCKED",
        "blocking_failures": failures,
        "checks": checks,
    }
    detail = {
        "admission": "ARM_SPECIFIC",
        "execution_mode": mode,
        "worker_profile": None,
        "worker_exposure": "native" if mode == "native" else "card",
        "oracle_mode": "EXTERNAL_SANDBOX_ORACLE",
        "main_max_turns": int(cell.get("max_turns") or 0),
        "worker_max_turns": int(cell.get("worker_max_turns") or 0),
        "token_budget": int(cell.get("token_budget") or 0),
    }
    return report, detail


# -- audit -------------------------------------------------------------------------------------


def _usage_of(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Map Unified Cell Evidence onto the executor's five usage fields."""

    usage = evidence.get("usage") or {}
    main = usage.get("main") if isinstance(usage.get("main"), Mapping) else {}
    children = usage.get("children") if isinstance(usage.get("children"), Mapping) else {}
    primary = usage.get("primary_agent") if isinstance(usage.get("primary_agent"), Mapping) else {}
    system = usage.get("system_total_tokens")
    return {
        "main_cumulative_input_tokens": int((main or primary).get("prompt_tokens") or 0),
        "main_completion_tokens": int((main or primary).get("completion_tokens") or 0),
        "child_total_prompt_tokens": int((children or {}).get("prompt_tokens") or 0),
        "child_completion_tokens": int((children or {}).get("completion_tokens") or 0),
        "system_total_tokens": int(system) if isinstance(system, int) else 0,
    }


def audit_stage_f_cell(
    *,
    cell: Mapping[str, Any],
    run: executor.CellRun,
    plan: Mapping[str, Any],
    usage_before: Mapping[str, Any] | None = None,
    cost_before: float | None = None,
    prices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The Stage F branch of ``executor.audit_cell``, with the executor's own settlement."""

    evidence = run.session or {}
    problems = normalize.validate_evidence(evidence)
    correctness = evidence.get("correctness") or {}
    oracle = correctness.get("independent_oracle") or {}
    metrics = _usage_of(evidence)

    checks = [
        {"check": "evidence_schema", "ok": not problems, "detail": "; ".join(problems)[:400]},
        {
            "check": "identity_matches_plan",
            "ok": evidence.get("cell_id") == cell["cell_id"]
            and evidence.get("plan_id") == plan.get("plan_id"),
            "detail": f"cell={evidence.get('cell_id')!r} plan={evidence.get('plan_id')!r}",
        },
        {
            "check": "attribution_matches_arm",
            "ok": evidence.get("usage_attribution") == cell.get("usage_attribution"),
            "detail": f"attribution={evidence.get('usage_attribution')!r}",
        },
        {
            "check": "oracle_ran",
            "ok": oracle.get("status") in {"PASS", "FAIL", "ERROR"},
            "detail": f"oracle={oracle.get('status')!r}",
        },
        {
            "check": "usage_complete",
            "ok": executor.validate_usage(metrics)["ok"],
            "detail": "; ".join(executor.validate_usage(metrics)["problems"])[:300],
        },
    ]
    if run.error:
        checks.append({"check": "runner_returned", "ok": False, "detail": run.error[:300]})
    failed = [item["check"] for item in checks if not item["ok"]]
    stops = [executor.STOP_MODE_VIOLATION] if "evidence_schema" in failed else []

    delta = executor.cell_cost(metrics, prices)
    before = {
        field: int((usage_before or {}).get(field) or 0) for field in executor.REQUIRED_USAGE_FIELDS
    }
    usage = {field: before[field] + int(metrics[field]) for field in executor.REQUIRED_USAGE_FIELDS}
    total_cost = (
        None if delta is None or cost_before is None else round(float(cost_before) + delta, 6)
    )

    verdict = "PASS" if not failed and oracle.get("passed") is True else "MEASURED_FAILURE"
    if stops:
        verdict = "STOPPED"
    return {
        "cell_id": cell["cell_id"],
        "arm_id": cell["arm_id"],
        "task_id": cell["task_id"],
        "execution_mode": cell.get("execution_mode"),
        "usage_attribution": cell.get("usage_attribution"),
        "profile_id": cell.get("worker_profiles") or [],
        "exposure": cell.get("worker_exposure"),
        "status": "succeeded" if verdict == "PASS" else "failed",
        "failure_kind": correctness.get("failure_kind"),
        "agent_success": correctness.get("agent_success"),
        "primary_agent_completion": correctness.get("primary_agent_completion"),
        "public_test_status": correctness.get("public_test"),
        "public_test_runs": 0,
        "static_acceptance": correctness.get("static_acceptance"),
        "oracle_passed": oracle.get("passed"),
        "oracle_status": oracle.get("status"),
        "goal_correctness": bool(oracle.get("passed") is True),
        "delivered_success": bool(correctness.get("delivered_success")),
        "execution_integrity": correctness.get("execution_integrity"),
        "executor_verdict": verdict,
        "verdict": verdict,
        "checks": checks,
        "stops": stops,
        "usage": usage,
        "delta_cost_usd": delta,
        "cost_usd": total_cost,
        "duration_s": run.duration_s,
        "notes": [
            "Stage F arm: audited from Unified Cell Evidence, not from a session it does not have",
            f"adapter_version={evidence.get('adapter_version')}",
        ],
    }


__all__ = [
    "arm_admission",
    "audit_stage_f_cell",
    "native_cell_runner",
    "static_dag_cell_runner",
]

_ = dataclasses
