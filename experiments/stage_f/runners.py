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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.evaluation.fixtures import (
    changes_against_baseline,
    path_matches,
    workspace_hashes,
)
from experiments.stage_b_plus import authority, executor
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
    # A0 and A2 export a directory too; the Host identity of the scored directory is computed here,
    # the same way the goal path computes it for A1/A3, so the audit compares like with like.
    path = evidence.get("workspace_path")
    if isinstance(path, str) and path and Path(path).is_dir():
        for key, value in executor.workspace_identity(Path(path)).items():
            evidence.setdefault(key, value)
    return evidence


# -- common integrity ---------------------------------------------------------------------------
#
# The same eleven checks for every arm. They are the ones a reader needs to trust a cell:
# the identity that ran, the frozen fixture, the raw record, the accounting, the export, the write
# policy, host separation, what the oracle scored, that the oracle was a different sandbox, that
# both were cleaned up, and that a failure is classifiable. An architecture-specific mechanism
# (ownership enforcement, a delegation plane, whether a Main exists) may be NOT_APPLICABLE; these
# may not, and an unevaluable one is UNKNOWN — which is not PASS.


def _status(*, ok: bool | None, detail: str) -> dict[str, Any]:
    if ok is None:
        return {"status": normalize.UNKNOWN, "detail": detail}
    return {"status": normalize.PASS if ok else normalize.FAIL, "detail": detail}


def _oracle_sandbox_id(
    oracle: Mapping[str, Any], oracle_sandbox: Mapping[str, Any] | None
) -> str | None:
    """Where each producer records the oracle's sandbox identity.

    ``judge_export`` puts it at the top level; the evaluation runners put it under
    ``details.hidden_tests``; the goal runner also records the oracle sandbox block.
    """

    if oracle.get("sandbox_id"):
        return str(oracle["sandbox_id"])
    details = oracle.get("details") if isinstance(oracle.get("details"), Mapping) else {}
    hidden = details.get("hidden_tests") if isinstance(details.get("hidden_tests"), Mapping) else {}
    if hidden.get("sandbox_id"):
        return str(hidden["sandbox_id"])
    if oracle_sandbox and oracle_sandbox.get("sandbox_id"):
        return str(oracle_sandbox["sandbox_id"])
    return None


def _fixture_digest(root: Path) -> str | None:
    """The fixture's own digest, recomputed exactly the way ``prepare_fixture`` computes it.

    A different hash (file-by-file sha256, directory digest) would never equal the frozen value and
    would fail every cell for a reason that has nothing to do with the run.
    """

    from experiments.evaluation.models import canonical_digest

    if not root.is_dir():
        return None
    written = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }
    return canonical_digest(dict(sorted(written.items())))


def _changed_paths(fixture: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    """Seed vs export, by content hash. The seed on disk is the baseline that actually ran."""

    root = Path(str(fixture.get("path") or ""))
    baseline = workspace_hashes(root) if root.is_dir() else {}
    current = workspace_hashes(workspace)
    return changes_against_baseline(baseline, current)


def integrity_report(
    *,
    cell: Mapping[str, Any],
    evidence: Mapping[str, Any],
    run_dir: Path | None,
    run_identity: str | None,
    expected_run_id: str | None,
    result_path: Path | None,
    fixture: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
    oracle: Mapping[str, Any] | None,
    oracle_sandbox: Mapping[str, Any] | None,
    agent_sandbox: Mapping[str, Any] | None,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Every common integrity check, from the facts the runner actually holds."""

    checks: list[dict[str, Any]] = []
    fixture = fixture or {}
    export = export or {}
    oracle = oracle or {}
    oracle_sandbox = oracle_sandbox or {}
    agent_sandbox = agent_sandbox or {}
    correctness = evidence.get("correctness") or {}

    def add(name: str, verdict: Mapping[str, Any], *, common: bool = True) -> None:
        checks.append({"check": name, **dict(verdict), "affects_verdict": bool(common)})

    identity_ok = (
        evidence.get("cell_id") == cell.get("cell_id")
        and evidence.get("plan_id") == cell.get("plan_id")
        and (expected_run_id is None or run_identity == expected_run_id)
    )
    add(
        "run_identity",
        _status(
            ok=identity_ok,
            detail=(
                f"cell={evidence.get('cell_id')!r} plan={evidence.get('plan_id')!r} "
                f"run={run_identity!r} expected={expected_run_id!r}"
            ),
        ),
    )

    fixture_digest = fixture.get("seed_digest")
    fixture_now = _fixture_digest(Path(str(fixture["path"]))) if fixture.get("path") else None
    add(
        "frozen_fixture",
        _status(
            ok=bool(fixture.get("matches_frozen_seed"))
            and fixture_digest is not None
            and fixture_now == fixture_digest
            and fixture.get("frozen_seed_digest") == fixture_digest,
            detail=(
                f"matches_frozen_seed={fixture.get('matches_frozen_seed')} "
                f"seed_digest={fixture_digest!r} recomputed={fixture_now!r}"
            ),
        ),
    )

    add(
        "raw_evidence",
        _status(
            ok=bool(
                result_path and Path(result_path).is_file() and Path(result_path).stat().st_size > 0
            ),
            detail=f"result={str(result_path) if result_path else None}",
        ),
    )

    # The five required counters decide whether the cell's consumption is measurable. The cache
    # split is optional evidence: a split the provider did not report is priced as a miss and must
    # not make a fully measured cell fail its own accounting check.
    missing = [
        key for key in executor.REQUIRED_USAGE_FIELDS if not isinstance((usage or {}).get(key), int)
    ]
    add(
        "resource_accounting",
        _status(
            ok=not missing,
            detail="complete" if not missing else f"unknown: {','.join(sorted(missing))}",
        ),
    )

    workspace_path = export.get("workspace_path")
    workspace = Path(str(workspace_path)) if workspace_path else None
    recorded = export.get(executor.EXPORT_DIRECTORY_DIGEST)
    recomputed = (
        "sha256:" + executor.workspace_digest(workspace)
        if workspace is not None and workspace.is_dir()
        else None
    )
    add(
        "final_export",
        _status(
            ok=bool(
                workspace is not None and workspace.is_dir() and recorded and recomputed == recorded
            ),
            detail=f"path={workspace_path!r} recorded={recorded!r} recomputed={recomputed!r}",
        ),
    )

    task = TASKS_BY_ID.get(str(cell.get("task_id")))
    allowed = tuple(str(item) for item in (task.allowed_paths if task else ()))
    forbidden = tuple(str(item) for item in (task.forbidden_paths if task else ()))
    changed = (
        _changed_paths(fixture, workspace)
        if workspace is not None and workspace.is_dir()
        else {"changed": [], "added": [], "removed": []}
    )
    written = sorted({*changed["changed"], *changed["added"], *changed["removed"]})
    outside = [path for path in written if not path_matches(path, allowed)]
    blocked = [path for path in written if path_matches(path, forbidden)]
    add(
        "path_policy",
        _status(
            ok=bool(workspace is not None and workspace.is_dir() and not outside and not blocked),
            detail=(
                f"written={len(written)} outside_allowed={outside[:5]} forbidden={blocked[:5]}"
            ),
        ),
    )

    add(
        "host_separation",
        _status(
            ok=bool(fixture_now is not None and fixture_now == fixture_digest),
            detail=f"host fixture digest {fixture_now!r} vs frozen {fixture_digest!r}",
        ),
    )

    oracle_sandbox_id = _oracle_sandbox_id(oracle, oracle_sandbox)
    export_files = len(workspace_hashes(workspace)) if workspace and workspace.is_dir() else None
    scored = oracle.get("workspace_files")
    if isinstance(scored, int) and not isinstance(scored, bool):
        # The goal path records the file count it scored; it must be the exported directory's.
        binding_ok = export_files is not None and scored == export_files
        binding_detail = f"scored_files={scored} export_files={export_files}"
    else:
        # The evaluation runners score the workspace_export they just produced; their oracle record
        # carries no separate file count, so the binding is the export's existence plus the mode.
        binding_ok = bool(workspace is not None and workspace.is_dir())
        binding_detail = f"scored the exported directory; export_files={export_files}"
    add(
        "oracle_binding",
        _status(
            ok=bool(
                str(oracle.get("execution_mode")) == "sandbox" and oracle_sandbox_id and binding_ok
            ),
            detail=(
                f"mode={oracle.get('execution_mode')!r} sandbox={oracle_sandbox_id!r} "
                f"{binding_detail}"
            ),
        ),
    )

    agent_sandbox_id = agent_sandbox.get("sandbox_id") or agent_sandbox.get("sandbox_handle")
    destroyed = agent_sandbox.get("destroyed")
    if destroyed is None:
        destroyed = agent_sandbox.get("sandbox_destroyed")
    if oracle_sandbox_id and agent_sandbox_id:
        isolation_ok: bool | None = str(oracle_sandbox_id) != str(agent_sandbox_id)
        isolation_basis = "two recorded sandbox ids"
    elif oracle_sandbox_id and destroyed is True:
        # The evaluation runners record that the agent container was destroyed rather than its id.
        # An oracle that carries its own id, created after that destroy, cannot inherit its state.
        isolation_ok = True
        isolation_basis = (
            "the agent container was destroyed before the oracle's own sandbox existed"
        )
    else:
        isolation_ok = None
        isolation_basis = "no comparable sandbox identity"
    add(
        "oracle_isolation",
        _status(
            ok=isolation_ok,
            detail=(
                f"oracle={oracle_sandbox_id!r} agent={agent_sandbox_id!r} "
                f"agent_destroyed={destroyed!r} ({isolation_basis})"
            ),
        ),
    )
    add(
        "cleanup",
        _status(
            ok=(not oracle.get("cleanup_failure") and (destroyed is True or destroyed is None))
            if (oracle or destroyed is not None)
            else None,
            detail=(
                f"oracle_cleanup={oracle.get('cleanup_failure')!r} agent_destroyed={destroyed!r}"
            ),
        ),
    )

    kind = correctness.get("failure_kind")
    succeeded = correctness.get("agent_success") is True
    measurable = succeeded or str(kind) in executor.NORMAL_FAILURE_KINDS
    add(
        "failure_measurable",
        _status(
            ok=measurable,
            detail=f"agent_success={correctness.get('agent_success')!r} kind={kind!r}",
        ),
    )

    # Architecture-specific mechanisms: recorded, never used to cancel a common check.
    add(
        "atlas_ownership_enforcement",
        {
            "status": normalize.NOT_APPLICABLE
            if str(cell.get("execution_mode")) in {"native", "atlas_static_dag"}
            else normalize.UNKNOWN,
            "detail": (
                "this arm runs no Atlas ownership mechanism; the static DAG's concurrent write "
                "sets are checked by the plan, which is a planning property"
                if str(cell.get("execution_mode")) in {"native", "atlas_static_dag"}
                else "reported by the goal runtime's session, not by the common gate"
            ),
        },
        common=False,
    )
    add(
        "atlas_delegation_plane",
        {
            "status": normalize.NOT_APPLICABLE
            if str(cell.get("execution_mode")) in {"native", "atlas_static_dag"}
            else normalize.UNKNOWN,
            "detail": "no delegation plane exists on this arm"
            if str(cell.get("execution_mode")) in {"native", "atlas_static_dag"}
            else "reported under delegation evidence",
        },
        common=False,
    )

    decisive = [item for item in checks if item["affects_verdict"]]
    failed = [item["check"] for item in decisive if item["status"] == normalize.FAIL]
    unknown = [item["check"] for item in decisive if item["status"] == normalize.UNKNOWN]
    verdict = normalize.FAIL if failed else normalize.UNKNOWN if unknown else normalize.PASS
    return {
        "verdict": verdict,
        "failed": failed,
        "unknown": unknown,
        "checks": checks,
        "workspace_changes": changed,
        "run_dir": str(run_dir) if run_dir else None,
        "basis": "the eleven common checks; architecture-specific rows never cancel one",
    }


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
    *,
    config: executor.RunnerConfig | None = None,
    provider_factory: Any | None = None,
    scripted_run: Any | None = None,
) -> executor.CellRunner:
    """A0: one native agent, one guarded cell budget, per-request metering."""

    factory = (
        provider_factory if provider_factory is not None else getattr(config, "llm_factory", None)
    )

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation.closure1b import native_baseline

        started = time.monotonic()
        root = _cell_root(context, cell)
        fixture = executor.prepare_fixture(TASKS_BY_ID[str(cell["task_id"])], root / "fixture")
        fixture_dir = Path(fixture["path"])
        settings, _task = _settings(cell, runs_root=root / "native-runs", multi=False)
        task_events: list[dict[str, Any]] = []

        result = await native_baseline.run_native_sample(
            _benchmark_task(cell, multi=False),
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
            runs_root=root / "native-runs",
            budget_limit_tokens=int(cell["token_budget"]),
            task_events=task_events,
            provider_factory=factory,
        )
        payload = _result_dict(result)
        oracle = _oracle_record(payload)
        export = _export_evidence(payload)
        requests = _requests_from(scripted_run)
        environment = payload.get("environment") or {}
        latency = {
            "agent_execution_ms": environment.get("agent_latency_ms"),
            "sandbox_provision_ms": (payload.get("metrics") or {}).get("provision_latency_ms"),
            "export_ms": (payload.get("metrics") or {}).get("export_latency_ms"),
            "oracle_ms": (payload.get("metrics") or {}).get("oracle_latency_ms"),
            "end_to_end_ms": (payload.get("metrics") or {}).get("latency_ms"),
        }
        evidence = normalize.from_native_result(
            cell, payload, oracle=oracle, export=export, requests=requests, latency=latency
        )
        evidence["resources"]["task_events"] = task_events
        # The cap the runner was actually configured with, not the cap the cell declared.
        evidence["resources"]["applied_turn_caps"] = {"primary_agent": int(settings.max_turns)}
        run_dir = Path(str(payload.get("run_dir") or root))
        run_identity = str((payload.get("identity") or {}).get("run_id") or run_dir.name)
        sandbox = dict(environment.get("sandbox") or {})
        sandbox.setdefault("destroyed", (payload.get("safety") or {}).get("sandbox_destroyed"))
        report = integrity_report(
            cell=cell,
            evidence=evidence,
            run_dir=run_dir,
            run_identity=run_identity,
            expected_run_id=None,
            result_path=run_dir / "result.json",
            fixture=fixture,
            export=export,
            oracle=oracle,
            oracle_sandbox=None,
            agent_sandbox=sandbox,
            usage=_usage_of(evidence),
        )
        normalize.apply_integrity(evidence, report)
        evidence["adapter_problems"] = normalize.validate_evidence(evidence)
        _write_json(root / "evidence.json", evidence)
        return executor.CellRun(
            session=evidence,
            run_dir=run_dir,
            fixture_root=fixture_dir,
            oracle=oracle or None,
            duration_s=time.monotonic() - started,
            fixture=fixture,
            export=export or None,
            expected_run_id=run_identity,
            timings=latency,
        )

    run.execution_mode = "native"  # type: ignore[attr-defined]
    run.is_stage_f_runner = True  # type: ignore[attr-defined]
    return run


# -- A2 ----------------------------------------------------------------------------------------


def static_dag_cell_runner(
    *,
    config: executor.RunnerConfig | None = None,
    provider_factory: Any | None = None,
    scripted_run: Any | None = None,
) -> executor.CellRunner:
    """A2: the frozen human decomposition, run by the scheduler on one shared budget."""

    factory = (
        provider_factory if provider_factory is not None else getattr(config, "llm_factory", None)
    )

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        from experiments.evaluation import runner as eval_runner

        started = time.monotonic()
        root = _cell_root(context, cell)
        fixture = executor.prepare_fixture(TASKS_BY_ID[str(cell["task_id"])], root / "fixture")
        fixture_dir = Path(fixture["path"])
        settings, _task = _settings(cell, runs_root=root / "dag-runs", multi=True)
        benchmark = _benchmark_task(cell, multi=True)

        result = await eval_runner.run_sample(
            benchmark,
            settings=settings,
            repetition_id=int(cell.get("repetition") or 1),
            provider_factory=factory,
        )
        payload = _result_dict(result)
        oracle = _oracle_record(payload)
        export = _export_evidence(payload)
        requests = _requests_from(scripted_run)
        environment = payload.get("environment") or {}
        metrics = payload.get("metrics") or {}
        latency = {
            "agent_execution_ms": environment.get("agent_latency_ms"),
            "sandbox_provision_ms": metrics.get("provision_latency_ms"),
            "export_ms": metrics.get("export_latency_ms"),
            "oracle_ms": metrics.get("oracle_latency_ms"),
            "end_to_end_ms": metrics.get("latency_ms"),
        }
        evidence = normalize.from_dag_result(
            cell, payload, oracle=oracle, export=export, requests=requests, latency=latency
        )
        # The per-node caps the scheduler was actually given, from the benchmark task it ran.
        evidence["resources"]["applied_turn_caps"] = {
            step.task_id: int(step.max_turns or settings.max_turns) for step in benchmark.workers
        }
        run_dir = Path(str(payload.get("run_dir") or root))
        run_identity = str((payload.get("identity") or {}).get("run_id") or run_dir.name)
        sandbox = dict(environment.get("sandbox") or {})
        sandbox.setdefault("destroyed", metrics.get("sandbox_destroyed"))
        report = integrity_report(
            cell=cell,
            evidence=evidence,
            run_dir=run_dir,
            run_identity=run_identity,
            expected_run_id=None,
            result_path=run_dir / "result.json",
            fixture=fixture,
            export=export,
            oracle=oracle,
            oracle_sandbox=None,
            agent_sandbox=sandbox,
            usage=_usage_of(evidence),
        )
        normalize.apply_integrity(evidence, report)
        evidence["adapter_problems"] = normalize.validate_evidence(evidence)
        _write_json(root / "evidence.json", evidence)
        return executor.CellRun(
            session=evidence,
            run_dir=run_dir,
            fixture_root=fixture_dir,
            oracle=oracle or None,
            duration_s=time.monotonic() - started,
            fixture=fixture,
            export=export or None,
            expected_run_id=run_identity,
            timings=latency,
        )

    run.execution_mode = "atlas_static_dag"  # type: ignore[attr-defined]
    run.is_stage_f_runner = True  # type: ignore[attr-defined]
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
    *,
    config: executor.RunnerConfig | None = None,
    provider_name: str | None = None,
    provider_factory: Any | None = None,
    scripted_run: Any | None = None,
) -> executor.CellRunner:
    """A1/A3: the production goal runtime, normalised into Unified Cell Evidence.

    With no arguments this *is* the production runner: ``live_cell_runner`` on the default
    ``RunnerConfig``, which takes the provider from the environment exactly as every earlier stage
    does. ``config`` (or the older ``provider_name`` / ``provider_factory`` pair) replaces only the
    provider seam; nothing about the goal runtime, the session it writes, the sandbox, the export or
    the oracle changes. The wrapper then normalises that session instead of letting the legacy
    audit read it.
    """

    base = config or executor.RunnerConfig()
    if provider_name is not None or provider_factory is not None:
        base = dataclasses.replace(
            base,
            llm_provider=provider_name or base.llm_provider,
            llm_factory=provider_factory if provider_factory is not None else base.llm_factory,
        )
    inner = executor.live_cell_runner(base)

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        result = await inner(cell, context)
        if result.session is None:
            return result
        root = _cell_root(context, cell)
        session = dict(result.session)
        main = session.get("main") or {}
        proposals = _conversation_proposals(result.run_dir, main.get("task_id"))
        requests = _requests_from(scripted_run)
        latency = dict(getattr(result, "timings", None) or {})
        evidence = normalize.from_main_child_session(
            cell,
            session,
            oracle=result.oracle,
            export=result.export,
            proposals=proposals,
            requests=requests,
            latency=latency,
        )
        report = integrity_report(
            cell=cell,
            evidence=evidence,
            run_dir=result.run_dir,
            run_identity=str(session.get("run_id") or ""),
            expected_run_id=result.expected_run_id,
            result_path=(Path(result.run_dir) / "session.json") if result.run_dir else None,
            fixture=result.fixture,
            export=result.export,
            oracle=result.oracle,
            oracle_sandbox=result.oracle_sandbox,
            agent_sandbox=session.get("sandbox"),
            usage=_usage_of(evidence),
        )
        normalize.apply_integrity(evidence, report)
        evidence["adapter_problems"] = normalize.validate_evidence(evidence)
        _write_json(root / "evidence.json", evidence)
        _write_json(root / "proposals.json", {"proposals": proposals})
        result.session = evidence
        return result

    #: Read by ``cell_runner_for``'s callers and by the routing regression: the mode a closure
    #: serves is a property of the closure, not something a caller has to remember.
    run.execution_mode = "main_driven"  # type: ignore[attr-defined]
    run.is_stage_f_runner = True  # type: ignore[attr-defined]
    run.keeps_session_record = True  # type: ignore[attr-defined]
    run.is_live = getattr(inner, "is_live", False)  # type: ignore[attr-defined]
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
            # The primary agent's own provider split: A0 is priced by the same formula as a Main.
            "main_cache_hit_tokens": _count(primary, "cache_hit_tokens"),
            "main_cache_miss_tokens": _count(primary, "cache_miss_tokens"),
            "child_cache_hit_tokens": 0,
            "child_cache_miss_tokens": 0,
        }
    if attribution == "dag_all_children":
        return {
            "main_cumulative_input_tokens": 0,
            "main_completion_tokens": 0,
            "child_total_prompt_tokens": _count(children_block, "prompt_tokens"),
            "child_completion_tokens": _count(children_block, "completion_tokens"),
            "system_total_tokens": system_total,
            # A2 has no Main: what a Main would call its own split is the DAG's aggregate split, and
            # it is priced as child cache, not invented as a Main.
            "main_cache_hit_tokens": 0,
            "main_cache_miss_tokens": 0,
            "child_cache_hit_tokens": _count(children_block, "cache_hit_tokens"),
            "child_cache_miss_tokens": _count(children_block, "cache_miss_tokens"),
        }
    child_prompt = _count(children_block, "prompt_tokens")
    structural_zero = child_prompt == 0
    return {
        "main_cumulative_input_tokens": _count(main, "prompt_tokens"),
        "main_completion_tokens": _count(main, "completion_tokens"),
        "child_total_prompt_tokens": child_prompt,
        "child_completion_tokens": _count(children_block, "completion_tokens"),
        "system_total_tokens": system_total,
        # Both halves of A1/A3 are priced from their own reported split where the provider gave one.
        # An arm that delegated nothing has a measured zero, not an unknown split.
        "main_cache_hit_tokens": _count(main, "cache_hit_tokens"),
        "main_cache_miss_tokens": _count(main, "cache_miss_tokens"),
        "child_cache_hit_tokens": 0
        if structural_zero
        else _count(children_block, "cache_hit_tokens"),
        "child_cache_miss_tokens": 0
        if structural_zero
        else _count(children_block, "cache_miss_tokens"),
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
    integrity = evidence.get("integrity") or {}

    export_path = _export_path(evidence)
    checks = [
        {"check": "evidence_schema", "ok": not problems, "detail": "; ".join(problems)[:400]},
        {
            # The common integrity gate. ``UNKNOWN`` is not a pass: a check that could not be
            # evaluated does not establish that the cell ran inside its contract.
            "check": "execution_integrity",
            "ok": integrity.get("verdict") == normalize.PASS,
            "detail": (
                f"verdict={integrity.get('verdict')!r} failed={integrity.get('failed')} "
                f"unknown={integrity.get('unknown')}"
            ),
        },
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
            # Only the five required counters decide completeness. The cache split is optional
            # evidence: a missing split is priced as a miss, and it must not stop a cell whose
            # consumption is otherwise fully measured.
            "check": "usage_complete",
            "ok": all(metrics.get(field) is not None for field in executor.REQUIRED_USAGE_FIELDS),
            "detail": "unknown: "
            + ",".join(
                field for field in executor.REQUIRED_USAGE_FIELDS if metrics.get(field) is None
            )
            if any(metrics.get(field) is None for field in executor.REQUIRED_USAGE_FIELDS)
            else "complete",
        },
    ]
    if run.error:
        checks.append({"check": "runner_returned", "ok": False, "detail": run.error[:300]})
    failed = [item["check"] for item in checks if not item["ok"]]
    stops: list[str] = []
    for name, stop in (
        ("evidence_schema", executor.STOP_MODE_VIOLATION),
        ("execution_integrity", executor.STOP_INTEGRITY),
        ("oracle_environment", executor.STOP_ORACLE_ENVIRONMENT),
        ("artifact_exported", executor.STOP_EXPORT),
        ("usage_complete", executor.STOP_UNKNOWN_USAGE),
        ("runner_returned", executor.STOP_EXECUTION_EXCEPTION),
    ):
        if name in failed and stop not in stops:
            stops.append(stop)

    usage_report = executor.validate_usage(metrics)
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
        # What the estimate was computed from, so a reader can see whether the provider reported a
        # cache split or whether the whole input was priced as a miss.
        "cost_basis": {
            "cache_split": usage_report["cache_split"],
            "child_cache_split": usage_report["child_cache_split"],
            "pricing_digest": prices and authority.digest_of(dict(prices)) or None,
            "formula": (
                "cache_hit_input * hit_rate + cache_miss_input * miss_rate + output * output_rate"
            ),
            "estimate": True,
        },
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
