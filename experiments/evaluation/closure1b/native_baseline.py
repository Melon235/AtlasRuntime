"""Closure-1B: N0/N1, the native AnyCode baseline on the shared OpenSandbox environment.

N0 = native single, N1 = native multi over the same candidate DAG as the Atlas multi arm.

Native semantics that are kept (nothing here replaces them):

* AnyCode 0.9.0's own agent loop, DAG scheduler, wave execution and native dependency context
  (``Relevant output from prerequisite tasks``);
* AnyCode's ``ToolRegistry`` / ``ToolExecutor``, with only the six basic coding tools registered;
* one shared workspace per run — every worker of a multi arm edits the same container checkout.

Atlas mechanisms that are **not** injected: no Context Plane, no ``ScopedWorker``, no action gate,
no durable ledger, no progressive disclosure, no ``tool_search`` / ``dispatch_action`` /
``history.read`` / ``handoff.read``, no delegation router, no shared budget guard, no sandbox
binding manager.

Shared infrastructure that *is* reused, because holding it constant is what makes the comparison an
environment comparison rather than an implementation comparison: ``OpenSandboxProvider``,
``GitProjectProvisioner``, ``export_patch``/``export_workspace``, ``evaluate_oracle_isolated``, the
provider usage adapter and the fixture staging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.types import ToolSecurityPolicy
from dotenv import load_dotenv
from experiments.evaluation.closure1b.neutral_lifecycle import (
    NeutralLifecycleError,
    NeutralSandbox,
    close_neutral_sandbox,
    open_neutral_sandbox,
    probe_container_environment,
)
from experiments.evaluation.final4.neutral_adapter import (
    NEUTRAL_TOOLS,
    NeutralEngine,
    SandboxToolBackend,
)
from experiments.evaluation.fixtures import (
    materialize_fixture,
    stage_git_repo,
    workspace_hashes,
)
from experiments.evaluation.models import (
    EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION,
    BenchmarkTask,
    EvalRunResult,
    FailureClass,
    OracleOutcome,
    RunIdentity,
    RuntimeMetrics,
    canonical_digest,
    effective_agent_max_turns,
    effective_public_task_digest,
)
from experiments.evaluation.runner import (
    EvaluationEnvironmentError,
    ModelRecordingAdapter,
    RunnerSettings,
    _aggregate_observations,
    _build_provider,
    _classify_exception,
    _environment_note,
    _estimate_cost,
    _jsonable,
    _project_config,
    build_deepseek_adapter,
    evaluation_resilience,
)
from experiments.evaluation.sandboxed_oracle import (
    OracleSandboxConfig,
    evaluate_oracle_isolated,
    oracle_failure_class,
)
from experiments.evaluation.stage_g3_4a_integrity import FIXTURE_COMMIT_LABEL

from atlasos.config import load_config
from atlasos.experiments import git_commit, git_dirty
from atlasos.sandbox import export_patch, export_workspace, is_real_sandbox_provider

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The exact identity under which these numbers must be read. It names the adapter, because the
#: native arm is *not* an unmodified AnyCode full-stack run.
ENGINE_ID = "anycode-0.9.0-native-agents-with-neutral-opensandbox-tool-adapter"

_NATIVE_SYSTEM_TEMPLATE = (
    "You are a coding agent. Work only inside the provided workspace. Use the available tools to "
    "inspect, edit and test the repository. Do not ask questions. Do not modify tests.\n\n"
    "{environment_note}"
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def native_task_turn(step: Any) -> str:
    """The model-visible task turn: the same public facts the Atlas arm gets.

    ``shared_context`` is the block every worker of the sample receives identically; ``prompt`` is
    that worker's own assignment. Concatenating them here is the native organisation of the same
    information — it deliberately does not reproduce Atlas's block layout, profile or handoff.
    """

    shared = getattr(step, "shared_context", "") or ""
    body = getattr(step, "prompt", "") or ""
    return f"{shared}\n\n{body}" if shared else body


async def run_native_sample(
    task: BenchmarkTask,
    *,
    settings: RunnerSettings,
    repetition_id: int = 0,
    runs_root: Path,
    config_path: Path | None = None,
    provider_name: str | None = None,
    turn_timeout_seconds: int | None = None,
    task_events: list[dict[str, Any]] | None = None,
) -> EvalRunResult:
    """Run one sample through native AnyCode inside a real OpenSandbox container.

    ``provider_name`` is set only by the deterministic acceptance harness, which registers its own
    scripted provider; in that mode no model is called and no API key is required.
    ``task_events`` collects the orchestrator's own start/complete events, so concurrency is
    observed rather than inferred from the DAG shape.
    """

    load_dotenv(REPO_ROOT / ".env", override=False)
    config = load_config(config_path or settings.config_path)
    project_config = _project_config(config)
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    run_id = f"native-{uuid.uuid4().hex[:12]}"
    run_dir = (
        runs_root / settings.benchmark_id / task.sample_id / f"r{repetition_id}-{run_id}"
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture_dir = run_dir / "fixture"
    materialize_fixture(task, fixture_dir)
    baseline_hashes = workspace_hashes(fixture_dir)
    stage_git_repo(fixture_dir, label=FIXTURE_COMMIT_LABEL)
    host_hashes_before = workspace_hashes(fixture_dir)

    model = settings.model_override or "deepseek-chat"
    scripted = provider_name is not None
    identity = RunIdentity(
        benchmark_id=settings.benchmark_id,
        dataset_version=settings.dataset_version,
        scorer_version=settings.scorer_version,
        sample_id=task.sample_id,
        repetition_id=repetition_id,
        run_id=run_id,
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        config_digest=canonical_digest(
            {
                "engine": ENGINE_ID,
                "settings": settings.digest(),
                "sandbox": {
                    "provider": config.sandbox.provider,
                    "image": config.sandbox.image,
                    "network": config.sandbox.network,
                    "workspace_root": config.sandbox.workspace_root,
                },
            }
        ),
        fixture_digest=task.fixture_digest,
        model="scripted" if scripted else model,
        provider="scripted" if scripted else config.provider.name,
        started_at=started_at,
    )

    observations: list[Any] = []
    resolved_models: list[str] = []
    metrics = RuntimeMetrics()
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    final_output = ""
    agent_success = False
    tool_calls: list[dict[str, Any]] = []
    worker_results: dict[str, Any] = {}
    workspace_export: Path | None = None
    patch_digest: str | None = None
    cleanup_error: str | None = None
    sandbox: NeutralSandbox | None = None
    provider = None
    engine: NeutralEngine | None = None
    export_latency_ms: float | None = None
    agent_latency_ms: float | None = None
    environment: dict[str, Any] = {
        "engine": ENGINE_ID,
        "engine_claim": (
            "AnyCode 0.9.0 native agent/orchestration with a neutral OpenSandbox tool adapter. "
            "Not an unmodified upstream full-stack environment."
        ),
        "workspace_isolation": "remote_opensandbox_container",
        "real_sandbox_provider": False,
        "temperature": settings.temperature,
        "max_turns": settings.max_turns,
        "worker_max_turns": settings.worker_max_turns,
        "max_tokens": settings.max_tokens,
        "token_budget": settings.token_budget,
        "max_tool_calls": settings.max_tool_calls,
        "budget_enforcement": (
            "post_hoc: the native arm has no Atlas shared budget guard, so the same public "
            "thresholds are applied after the run and to the wall-clock timeout"
        ),
        "tools": list(NEUTRAL_TOOLS),
        "scripted_provider": scripted,
        # Same function the Atlas arm calls, so an arm comparison can be gated on digest equality.
        "task_identity": {
            "fixture_digest": identity.fixture_digest,
            "effective_public_task_digest": effective_public_task_digest(task),
            "effective_public_task_digest_version": EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION,
        },
    }

    try:
        provider = _build_provider(config)
        if not is_real_sandbox_provider(provider, expected_name=config.sandbox.provider):
            raise EvaluationEnvironmentError(
                f"sandbox provider {config.sandbox.provider!r} is not a real provider; the native "
                "baseline never falls back to a Host workspace"
            )
        health = await provider.health()
        environment["sandbox_health"] = health.status
        if health.status == "unavailable":
            raise EvaluationEnvironmentError(f"sandbox provider is unavailable: {health.message}")

        if provider_name is None:
            if not os.environ.get("DEEPSEEK_API_KEY"):
                raise EvaluationEnvironmentError("DEEPSEEK_API_KEY is not configured")
            provider_name = f"atlas-native-{uuid.uuid4().hex[:12]}"

            async def factory(**_: Any) -> ModelRecordingAdapter:
                inner = build_deepseek_adapter(model, observations.append)
                return ModelRecordingAdapter(inner, resolved_models)

            register_provider_factory(provider_name, factory)

        context = ExecutionContext(
            principal="system:native-baseline",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes={"run_id": run_id, "sample_id": task.sample_id},
        )
        provision_started = time.perf_counter()
        sandbox = await open_neutral_sandbox(
            provider,
            project_config,
            run_id=run_id,
            task_id=task.sample_id,
            context=context,
            fixture_repo=fixture_dir,
        )
        metrics.provision_latency_ms = round((time.perf_counter() - provision_started) * 1000.0, 3)
        metrics.sandbox_created = True
        environment["real_sandbox_provider"] = True
        environment["sandbox"] = {
            "provider": sandbox.provider_name,
            "sandbox_id": sandbox.sandbox_id,
            "image": config.sandbox.image,
            "network": config.sandbox.network,
            "workspace_root": sandbox.workspace_root,
            "source_commit": sandbox.source_commit,
            "source_digest": sandbox.source_digest,
            "provision_commands": sandbox.provision_commands,
            "provision_uploads": sandbox.provision_uploads,
            "timings_ms": dict(sandbox.timings_ms),
            "isolation": sandbox.isolation,
        }
        with contextlib.suppress(NeutralLifecycleError):
            # Evidence, not workload: the same three facts Atlas runs under.
            environment["container"] = await probe_container_environment(provider, sandbox)

        backend = SandboxToolBackend(
            provider=provider,
            handle=sandbox.handle,
            workspace_root=project_config.workspace_root,
        )

        def on_progress(event: Any) -> None:
            if task_events is None:
                return
            data = getattr(event, "data", None)
            task_events.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "type": str(getattr(event, "type", "")),
                    "task": str(getattr(event, "task", "") or ""),
                    "agent": str(getattr(event, "agent", "") or ""),
                    "title": getattr(data, "title", None) if data is not None else None,
                }
            )

        # Native semantics: upstream agent loop, DAG scheduler and tool registry. ``NeutralEngine``
        # only swaps in the registry/executor pair that executes the six tools in the container.
        engine = NeutralEngine(backend, OrchestratorConfig(on_progress=on_progress))
        environment_note = _environment_note(project_config.workspace_root)
        agent_configs = [
            AgentConfig(
                name=f"native:{step.task_id}",
                model=model,
                provider=provider_name,
                system_prompt=_NATIVE_SYSTEM_TEMPLATE.format(environment_note=environment_note),
                tools=list(NEUTRAL_TOOLS),
                max_turns=effective_agent_max_turns(
                    step_max_turns=step.max_turns,
                    settings_max_turns=settings.max_turns,
                    worker_max_turns=settings.worker_max_turns,
                ),
                max_tokens=settings.max_tokens,
                temperature=settings.temperature,
                provider_resilience=evaluation_resilience(),
                tool_security=ToolSecurityPolicy(workspace_root=project_config.workspace_root),
            )
            for step in task.workers
        ]
        team = engine.create_team("native", TeamConfig(name="native", agents=agent_configs))
        title_by_id = {step.task_id: f"{task.sample_id}-{step.task_id}" for step in task.workers}
        task_specs = [
            TaskSpec(
                title=title_by_id[step.task_id],
                description=native_task_turn(step),
                assignee=f"native:{step.task_id}",
                depends_on=[title_by_id[item] for item in step.depends_on],
            )
            for step in task.workers
        ]
        agent_started = time.perf_counter()
        result = await asyncio.wait_for(
            engine.run_tasks(team, task_specs),
            timeout=turn_timeout_seconds or settings.timeout_seconds,
        )
        agent_latency_ms = round((time.perf_counter() - agent_started) * 1000.0, 3)
        environment["agent_latency_ms"] = agent_latency_ms
        agent_success = bool(result.success)
        for agent, agent_result in result.agent_results.items():
            calls = [
                {
                    "agent": agent,
                    "tool_name": record.tool_name,
                    "input": _jsonable(record.input),
                    "output": str(record.output)[:4000],
                    "duration_ms": round(float(record.duration) * 1000.0, 3),
                }
                for record in agent_result.tool_calls
            ]
            tool_calls.extend(calls)
            worker_results[agent] = {
                "success": bool(agent_result.success),
                "output": str(agent_result.output or ""),
                "tool_calls": len(calls),
                "token_usage": _jsonable(agent_result.token_usage),
            }
        final_output = "\n".join(
            str(value.get("output", "")) for value in worker_results.values() if value.get("output")
        )

        export_started = time.perf_counter()
        patch = await export_patch(
            provider,
            sandbox.handle,
            base_commit=sandbox.source_commit,
            workspace_root=sandbox.workspace_root,
        )
        patch_digest = patch.patch_digest
        (run_dir / "workspace.patch").write_text(patch.patch_text, encoding="utf-8")
        workspace_export = run_dir / "workspace"
        await export_workspace(
            provider,
            sandbox.handle,
            destination=workspace_export,
            workspace_root=sandbox.workspace_root,
        )
        export_latency_ms = round((time.perf_counter() - export_started) * 1000.0, 3)
        environment["export_latency_ms"] = export_latency_ms
    except TimeoutError:
        failure_class = "TIMEOUT"
        failure_reason = "native sample exceeded the wall clock limit"
    except BaseException as error:  # noqa: BLE001 - every failure is classified and persisted
        if not isinstance(error, Exception):
            raise
        if isinstance(error, NeutralLifecycleError):
            if error.cleanup_failure:
                cleanup_error = error.cleanup_failure
            environment["sandbox_failure"] = {
                "stage": error.stage,
                "type": type(error).__name__,
                "sandbox_id": error.sandbox_id,
                "created": error.sandbox_id is not None,
                "destroy_confirmed": error.destroy_confirmed,
                "cleanup_failure": error.cleanup_failure,
            }
        failure_class = classify_native_failure(error)
        failure_reason = f"{type(error).__name__}: {error}"
    finally:
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        if provider is not None and sandbox is not None:
            confirmed, destroy_error = await close_neutral_sandbox(provider, sandbox)
            metrics.sandbox_destroyed = confirmed
            environment["sandbox_destroy"] = {
                "confirmed": confirmed,
                "error": destroy_error,
            }
            if not confirmed and cleanup_error is None:
                cleanup_error = destroy_error or "provider did not confirm destroy"
            if not confirmed:
                if failure_class is None:
                    failure_class = "CLEANUP_FAILURE"
                    failure_reason = f"sandbox cleanup failed: {cleanup_error}"
                elif failure_reason is not None:
                    failure_reason = (
                        f"{failure_reason}; sandbox cleanup also failed: {cleanup_error}"
                    )

        host_hashes_after = workspace_hashes(fixture_dir)
        host_unchanged = host_hashes_before == host_hashes_after
        if not host_unchanged and failure_class is None:
            failure_class = "HOST_ISOLATION_VIOLATION"
            failure_reason = "the Host fixture changed during a native sandbox run"

        aggregate = _aggregate_observations(observations)
        metrics.model_turns = len(observations)
        metrics.tool_calls = len(tool_calls)
        metrics.prompt_tokens = int(aggregate["prompt_tokens"]) if observations else None
        metrics.completion_tokens = int(aggregate["completion_tokens"]) if observations else None
        metrics.cache_hit_tokens = (
            int(aggregate["cache_hit_tokens"])
            if aggregate["cache_hit_tokens"] is not None
            else None
        )
        metrics.cache_miss_tokens = (
            int(aggregate["cache_miss_tokens"])
            if aggregate["cache_miss_tokens"] is not None
            else None
        )
        metrics.estimated_cost_usd = _estimate_cost(observations, config.pricing)
        metrics.extra["cache_metrics_complete"] = bool(aggregate["cache_metrics_complete"])
        metrics.extra["inference_latency_ms"] = aggregate["inference_latency_ms"]
        metrics.extra["export_latency_ms"] = export_latency_ms
        metrics.extra["agent_latency_ms"] = agent_latency_ms
        metrics.worker_selection = sorted(worker_results)
        metrics.host_isolation_violations = 0 if host_unchanged else 1
        environment["resolved_models"] = sorted(set(resolved_models))
        environment["worker_count"] = len(task.workers)
        environment["tool_calls"] = len(tool_calls)
        environment["budget_check"] = _post_hoc_budget(settings, metrics)

    # Same public thresholds as the Atlas arm, evaluated after the fact.
    budget = environment["budget_check"]
    if failure_class is None and budget["exceeded"]:
        failure_class = "BUDGET_EXCEEDED"
        failure_reason = "; ".join(budget["reasons"])

    if workspace_export is not None and workspace_export.is_dir():
        oracle_started = time.perf_counter()
        oracle = await evaluate_oracle_isolated(
            task,
            workspace=workspace_export,
            baseline_hashes=baseline_hashes,
            tool_calls=tool_calls,
            worker_results=worker_results,
            run_dir=run_dir,
            provider=provider,
            config=OracleSandboxConfig(image=config.sandbox.image),
            host_fixture_unchanged=host_unchanged,
            run_id=run_id,
        )
        metrics.extra["oracle_latency_ms"] = round(
            (time.perf_counter() - oracle_started) * 1000.0, 3
        )
    else:
        oracle = OracleOutcome(
            passed=False,
            checks={"workspace_exported": False},
            failure_reasons=["the native sandbox workspace was not exported"],
            environment_failure=True,
            failure_kind="WORKSPACE_NOT_EXPORTED",
        )
    oracle_class = native_oracle_failure_class(oracle)
    environment["oracle"] = {
        "passed": bool(oracle.passed),
        "kind": oracle.failure_kind,
        "mode": oracle.execution_mode,
        "environment_failure": bool(oracle.environment_failure),
        "classification": oracle_class,
    }
    if failure_class is None and oracle_class is not None:
        failure_class = oracle_class
        failure_reason = "; ".join(oracle.failure_reasons)[:2000] or "oracle failed"
    if failure_class is None:
        failure_class = "TASK_SUCCESS"

    success = failure_class == "TASK_SUCCESS" and oracle.passed
    finished_at = datetime.now(UTC)
    metrics.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    final_result = EvalRunResult(
        identity=identity,
        task=task,
        success=success,
        failure_class=failure_class,
        failure_reason=failure_reason,
        final_output=final_output,
        agent_success=agent_success,
        tool_calls=tool_calls,
        worker_results=worker_results,
        oracle=oracle,
        metrics=metrics,
        run_dir=str(run_dir),
        fixture_dir=str(fixture_dir),
        workspace_export=str(workspace_export) if workspace_export else None,
        patch_digest=patch_digest,
        started_at=started_at,
        finished_at=finished_at,
        environment=environment,
        safety={
            "host_fixture_unchanged": host_unchanged,
            "sandbox_destroyed": metrics.sandbox_destroyed,
            "real_sandbox_provider": bool(environment["real_sandbox_provider"]),
            "workspace_isolation": "remote_opensandbox_container",
            "host_fallback_used": False,
            "cleanup_error": cleanup_error,
        },
    )
    _write_json(run_dir / "result.json", final_result.as_dict())
    ledger = runs_root / settings.benchmark_id / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(final_result.as_dict()), sort_keys=True) + "\n")
    return final_result


def native_oracle_failure_class(oracle: OracleOutcome) -> FailureClass | None:
    """The shared oracle rule, in the type this arm's result field expects.

    A failed hidden test is ``ORACLE_FAILURE``; a dead oracle sandbox, a timeout or a broken oracle
    environment is ``ENVIRONMENT_FAILURE``. ``None`` means the oracle did not fail and no failure
    class may be introduced.
    """

    failure = oracle_failure_class(oracle)
    if failure is None:
        return None
    return cast("FailureClass", failure)


def classify_native_failure(error: BaseException) -> FailureClass:
    """Map a native-arm exception onto the shared failure classes.

    A sandbox create/provision/isolation failure is an **environment** defect: nothing the model or
    the agent loop did can cause it, and reporting it as ``RUNTIME_FAILURE`` would let an
    infrastructure problem read as an agent-capability result.

    ``_classify_exception`` (shared with the Atlas arm) does not know this hierarchy, and teaching
    it about an evaluation-only module would point the dependency the wrong way, so the one extra
    case lives here and everything else is delegated unchanged.
    """

    if isinstance(error, NeutralLifecycleError):
        return "ENVIRONMENT_FAILURE"
    return _classify_exception(error)


def _post_hoc_budget(settings: RunnerSettings, metrics: RuntimeMetrics) -> dict[str, Any]:
    """The same public thresholds the Atlas arm enforces, applied after the run.

    The two arms do not share an enforcement mechanism — Atlas rejects an over-budget request
    before it is sent, the native arm has no such guard. That difference is recorded, not hidden.
    """

    reasons: list[str] = []
    total_tokens = None
    if metrics.prompt_tokens is not None and metrics.completion_tokens is not None:
        total_tokens = metrics.prompt_tokens + metrics.completion_tokens
        if settings.token_budget > 0 and total_tokens > settings.token_budget:
            reasons.append(f"tokens {total_tokens} > budget {settings.token_budget}")
    if settings.max_tool_calls and metrics.tool_calls > settings.max_tool_calls:
        reasons.append(f"tool calls {metrics.tool_calls} > limit {settings.max_tool_calls}")
    return {
        "mode": "post_hoc",
        "total_tokens": total_tokens,
        "token_budget": settings.token_budget,
        "tool_calls": metrics.tool_calls,
        "max_tool_calls": settings.max_tool_calls,
        "exceeded": bool(reasons),
        "reasons": reasons,
        "enforcement_note": (
            "Atlas rejects a request that would breach the run budget before sending it; the "
            "native arm has no guard, so this is an after-the-fact verdict on the same thresholds"
        ),
    }
