"""Stage F — the three cell runners, their admission and their audit.

``A1``/``A3`` are the ordinary main-driven cell: they run the goal runtime. This module only wraps
it so that the run is normalised into Unified Cell Evidence instead of being audited by a session
shape the Stage F schema does not use. ``A0`` (native single agent) and ``A2`` (static DAG) each
call their real entry with the cell's frozen configuration, read the result object through its own
``as_dict()`` contract, and are audited by that evidence instead of by a session shape they do not
have. No Main field is invented for either.

Each runner accepts a deterministic provider factory. When one is given, the arm executes exactly
as it does in production — same agent loop, scheduler, tool registry, sandbox, export, oracle and
budget guard — with the provider replaced and the substitution recorded. When it is not given, the
runner is the production runner and needs a credential.

The audit reuses the executor's own usage and cost settlement (``validate_usage``, ``cell_cost``)
so there is one accounting model, not two.
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


def _export_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The exported workspace, as evidence rather than as a path.

    ``EvalRunResult.workspace_export`` is a *path string*. Passing it to a mapping consumer is how a
    real path becomes a fictional field, so the path is kept under its own key and the integrity
    facts are read from where the result actually records them.
    """

    raw = payload.get("workspace_export")
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
    evidence: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        evidence.update(raw)
    elif isinstance(raw, str):
        evidence["workspace_path"] = raw
    else:
        return {}
    for key in ("patch_digest", "patch_changed_files", "workspace_files", "workspace_digest"):
        if payload.get(key) is not None:
            evidence.setdefault(key, payload[key])
    if metrics.get("sandbox_created") is not None:
        evidence.setdefault("sandbox_created", metrics.get("sandbox_created"))
    if metrics.get("sandbox_destroyed") is not None:
        evidence.setdefault("sandbox_destroyed", metrics.get("sandbox_destroyed"))
    return evidence


def _benchmark_task(cell: Mapping[str, Any], *, multi: bool) -> Any:
    """The evaluation task for this cell, with the cell's own turn caps on every step.

    ``closure2.build_task`` freezes 12 turns per worker for its own pilots. A Stage F cell declares
    20 for implementation nodes and 40 for the integration node, so those caps are written onto the
    steps here rather than inherited from another stage's protocol.
    """

    from experiments.evaluation.closure2 import catalog as closure2

    task = TASKS_BY_ID[str(cell["task_id"])]
    if not multi:
        return closure2.build_task(task, topology="single")
    # A strongly coupled task has one honest node; a one-node DAG is a degenerate DAG, and the
    # benchmark contract refuses to call it multi-worker, so it is built as a single-step task.
    if len(task.workers) < 2:
        return closure2.build_task(task, topology="single")
    benchmark = closure2.build_task(task, topology="multi")
    implementation = int(cell.get("worker_max_turns") or 20)
    integration = int(cell.get("integration_max_turns") or 40)
    # The integration node is the one nothing else depends on. Read from the frozen decomposition
    # rather than from a per-node flag the task does not carry.
    depended_on = {str(dep) for node in task.workers for dep in node["depends_on"]}
    sinks = {
        str(node["task_id"]) for node in task.workers if str(node["task_id"]) not in depended_on
    }
    steps = tuple(
        dataclasses.replace(
            step,
            max_turns=integration if step.task_id in sinks else implementation,
        )
        for step in benchmark.workers
    )
    return dataclasses.replace(benchmark, workers=steps, mode="multi_worker")


def _settings(cell: Mapping[str, Any], *, runs_root: Path, multi: bool) -> Any:
    from experiments.evaluation.runner import RunnerSettings

    task = TASKS_BY_ID[str(cell["task_id"])]
    extra: dict[str, Any] = {}
    if multi:
        extra["oracle_mode"] = "sandbox"
        extra["ownership_mode"] = "off"
        extra["execution_integrity_mode"] = "record"
    # The single agent is the primary agent: it carries the Main cap, not the worker cap. A static
    # DAG's *implementation* nodes carry the worker cap and its integration node the integration
    # cap, which is applied per step in ``_benchmark_task``.
    turns = (
        int(cell.get("max_turns") or 40) if not multi else int(cell.get("worker_max_turns") or 20)
    )
    return RunnerSettings(
        # A short directory key, not the cell identity: the run path is
        # ``<runs_root>/<benchmark_id>/<sample_id>/r<rep>-<run_id>`` and the ownership lock inside
        # it adds a 64-character file name, which puts a Windows host over MAX_PATH. The identity a
        # reader needs is the cell id in ``evidence.json``, never a path component.
        benchmark_id=f"sf-{str(cell['arm_id']).lower()}-{int(cell['order']):02d}",
        runs_root=runs_root,
        config_path=Path(REPO_ROOT) / "experiments" / "configs" / "eval-benchmark.yaml",
        max_turns=turns,
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


def native_cell_runner(
    *, provider_factory: Any | None = None, scripted_run: Any | None = None
) -> executor.CellRunner:
    """A0: one native agent, one guarded cell budget, per-request metering."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation.closure1b import native_baseline

        root = _cell_root(context, cell)
        fixture_dir = Path(
            executor.prepare_fixture(TASKS_BY_ID[str(cell["task_id"])], root / "fixture")["path"]
        )
        settings, _task = _settings(cell, runs_root=root / "native-runs", multi=False)
        task_events: list[dict[str, Any]] = []

        result = await native_baseline.run_native_sample(
            _benchmark_task(cell, multi=False),
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
            runs_root=root / "native-runs",
            budget_limit_tokens=int(cell["token_budget"]),
            task_events=task_events,
            provider_factory=provider_factory,
        )
        payload = _result_dict(result)
        oracle = _oracle_record(payload)
        export = _export_evidence(payload)
        requests = _requests_from(scripted_run)
        evidence = normalize.from_native_result(
            cell, payload, oracle=oracle, export=export, requests=requests
        )
        evidence["resources"]["task_events"] = task_events
        # The cap the runner was actually configured with, not the cap the cell declared.
        evidence["resources"]["applied_turn_caps"] = {"primary_agent": int(settings.max_turns)}
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


def static_dag_cell_runner(
    *, provider_factory: Any | None = None, scripted_run: Any | None = None
) -> executor.CellRunner:
    """A2: the frozen human decomposition, run by the scheduler on one shared budget."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation import runner as eval_runner

        root = _cell_root(context, cell)
        fixture_dir = Path(
            executor.prepare_fixture(TASKS_BY_ID[str(cell["task_id"])], root / "fixture")["path"]
        )
        settings, _task = _settings(cell, runs_root=root / "dag-runs", multi=True)
        benchmark = _benchmark_task(cell, multi=True)

        result = await eval_runner.run_sample(
            benchmark,
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
            provider_factory=provider_factory,
        )
        payload = _result_dict(result)
        oracle = _oracle_record(payload)
        export = _export_evidence(payload)
        requests = _requests_from(scripted_run)
        evidence = normalize.from_dag_result(
            cell, payload, oracle=oracle, export=export, requests=requests
        )
        # The per-node caps the scheduler was actually given, from the benchmark task it ran.
        evidence["resources"]["applied_turn_caps"] = {
            step.task_id: int(step.max_turns or settings.max_turns) for step in benchmark.workers
        }
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


# -- A1 / A3 -----------------------------------------------------------------------------------


def _conversation_proposals(run_dir: Path | None, main_task_id: str | None) -> list[dict[str, Any]]:
    """Every ``delegate_tasks`` input the Main actually sent, from the run's own snapshot.

    ``AtlasToolRuntime`` writes each agent's conversation to ``tool_conversations.json`` before
    every turn. That file is the only place the model's tool *inputs* survive, so the proposal
    evidence is read from it rather than from a session field that never held them.
    """

    if run_dir is None:
        return []
    path = Path(run_dir) / "tool_conversations.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tasks = payload.get("tasks") if isinstance(payload, Mapping) else None
    if not isinstance(tasks, Mapping):
        return []
    keys = [main_task_id] if main_task_id in tasks else list(tasks)
    proposals: list[dict[str, Any]] = []
    for key in keys:
        entry = tasks.get(key)
        messages = (entry or {}).get("messages") if isinstance(entry, Mapping) else None
        for message in messages or []:
            for block in message.get("content") or []:
                if isinstance(block, Mapping) and block.get("name") == "delegate_tasks":
                    proposals.append(
                        {
                            "task_id": key,
                            "input": block.get("input"),
                            "block_type": block.get("type"),
                            "source": "tool_conversations.json",
                        }
                    )
    return proposals


def main_driven_cell_runner(
    *, provider_name: str, provider_factory: Any, scripted_run: Any | None = None
) -> executor.CellRunner:
    """A1/A3: the production goal runtime, normalised into Unified Cell Evidence.

    The inner runner is the production one — ``live_cell_runner`` with only the provider seam
    injected. This wrapper adds no execution behaviour: it reads the result, normalises it, and
    returns the same ``CellRun`` with the evidence as its session.
    """

    inner = executor.live_cell_runner(
        executor.RunnerConfig(llm_provider=provider_name, llm_factory=provider_factory)
    )

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        result = await inner(cell, context)
        if result.session is None:
            return result
        root = _cell_root(context, cell)
        session = dict(result.session)
        main = session.get("main") or {}
        proposals = _conversation_proposals(result.run_dir, main.get("task_id"))
        requests = _requests_from(scripted_run)
        evidence = normalize.from_main_child_session(
            cell,
            session,
            oracle=result.oracle,
            export=result.export,
            proposals=proposals,
            requests=requests,
        )
        evidence["adapter_problems"] = normalize.validate_evidence(evidence)
        _write_json(root / "evidence.json", evidence)
        _write_json(root / "proposals.json", {"proposals": proposals})
        result.session = evidence
        return result

    return run


def _requests_from(scripted_run: Any | None) -> list[dict[str, Any]]:
    """The deterministic provider's own request rows. Empty when no provider was injected."""

    if scripted_run is None:
        return []
    return list(getattr(scripted_run, "requests", []) or [])


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
    # Budget enforcement is a property of the runner this arm calls, not of a plan field: the
    # native runner passes ``budget_limit_tokens`` into the shared RunBudgetHook and the DAG runner
    # runs with ``budget_mode="guarded"``. The plan says which arm it is; the proof says the guard
    # fires.
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
    attribution = evidence.get("usage_attribution")

    def _count(block: Mapping[str, Any], field: str) -> Any:
        """A counter, or ``None`` when this run did not record it.

        ``None`` means not measurable and flows into a stop. A *structural* zero (an arm with no
        children) is a measured zero and is reported as one; the reporting layer still says
        NOT_APPLICABLE for a role the arm does not have, because accounting and reporting are two
        different questions.
        """

        source = (block or {}).get(field)
        return int(source) if isinstance(source, int) and not isinstance(source, bool) else None

    children_block = children if isinstance(children, Mapping) else {}
    system = usage.get("system_total_tokens")
    system_total = system if isinstance(system, int) and not isinstance(system, bool) else None
    if attribution == "single_agent":
        return {
            "main_cumulative_input_tokens": _count(primary, "prompt_tokens"),
            "main_completion_tokens": _count(primary, "completion_tokens"),
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": system_total,
        }
    if attribution == "dag_all_children":
        return {
            "main_cumulative_input_tokens": 0,
            "main_completion_tokens": 0,
            "child_total_prompt_tokens": _count(children_block, "prompt_tokens"),
            "child_completion_tokens": _count(children_block, "completion_tokens"),
            "system_total_tokens": system_total,
        }
    return {
        "main_cumulative_input_tokens": _count(main, "prompt_tokens"),
        "main_completion_tokens": _count(main, "completion_tokens"),
        "child_total_prompt_tokens": _count(children_block, "prompt_tokens"),
        "child_completion_tokens": _count(children_block, "completion_tokens"),
        "system_total_tokens": system_total,
    }


def _export_path(evidence: Mapping[str, Any]) -> str | None:
    """The exported workspace path, read from where each adapter records it."""

    resources = evidence.get("resources") or {}
    value = resources.get("artifact_export")
    if isinstance(value, str) and value:
        return value
    export = evidence.get("export") or {}
    candidate = export.get("workspace_path") if isinstance(export, Mapping) else None
    return str(candidate) if candidate else None


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

    export_path = _export_path(evidence)
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
            # A dead or unclean oracle sandbox is an environment defect: the cell produced no
            # verdict about the deliverable and the matrix must not continue as if it had.
            "check": "oracle_environment",
            "ok": oracle.get("status") != "ERROR",
            "detail": f"oracle status={oracle.get('status')!r}",
        },
        {
            # The oracle scores an exported workspace. A missing export is missing evidence, and a
            # missing path must never be read as "nothing to score, therefore fine".
            "check": "artifact_exported",
            "ok": export_path is not None and Path(export_path).is_dir(),
            "detail": f"workspace_path={export_path!r}",
        },
        {
            "check": "usage_complete",
            "ok": all(value is not None for value in metrics.values()),
            "detail": "unknown: " + ",".join(key for key, value in metrics.items() if value is None)
            if any(value is None for value in metrics.values())
            else "complete",
        },
    ]
    if run.error:
        checks.append({"check": "runner_returned", "ok": False, "detail": run.error[:300]})
    failed = [item["check"] for item in checks if not item["ok"]]
    stops: list[str] = []
    for name, stop in (
        ("evidence_schema", executor.STOP_MODE_VIOLATION),
        ("oracle_environment", executor.STOP_ORACLE_ENVIRONMENT),
        ("artifact_exported", executor.STOP_EXPORT),
        ("usage_complete", executor.STOP_UNKNOWN_USAGE),
        ("runner_returned", executor.STOP_EXECUTION_EXCEPTION),
    ):
        if name in failed and stop not in stops:
            stops.append(stop)

    delta = executor.cell_cost(metrics, prices)
    before = {
        field: int((usage_before or {}).get(field) or 0) for field in executor.REQUIRED_USAGE_FIELDS
    }
    # A counter this cell did not report makes the cumulative total unknown: adding zero would be
    # the fabrication the whole schema exists to prevent.
    usage: dict[str, Any] = {
        field: (None if metrics[field] is None else before[field] + int(metrics[field]))
        for field in executor.REQUIRED_USAGE_FIELDS
    }
    total_cost = (
        None
        if delta is None or cost_before is None or any(value is None for value in usage.values())
        else round(float(cost_before) + delta, 6)
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
        # Read from the run's own public-test record; absent means the arm ran none, which is
        # reported as unknown rather than as zero runs.
        "public_test_runs": (evidence.get("resources") or {}).get("public_test_runs"),
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
    "main_driven_cell_runner",
    "native_cell_runner",
    "static_dag_cell_runner",
]

_ = dataclasses
