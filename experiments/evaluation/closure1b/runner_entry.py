# ruff: noqa: E501
"""Closure-1B entry: the native AnyCode baseline units N0 (single) and N1 (multi).

    python -m experiments.evaluation.closure1b.runner_entry --unit validation-serial-N0 --preflight-only
    python -m experiments.evaluation.closure1b.runner_entry --units validation-parallel-N1

The units are `native` units: AnyCode's own agent loop and DAG over the shared OpenSandbox
environment. They are distinct from the historical Final-4 `N0` unit in
``experiments/evaluation/final4/runner_entry.py``, which stays as a Host-copy baseline and keeps its
old numbers. Nothing here rewrites that manifest, those unit results or the 22/24 run inventory.

Live model calls require ``DEEPSEEK_API_KEY``; the deterministic acceptance path
(``experiments.evaluation.closure1b.acceptance``) registers a scripted provider and never calls a
model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from experiments.evaluation.closure1b.native_baseline import run_native_sample
from experiments.evaluation.final4 import runner_entry as final4_entry
from experiments.evaluation.final4.validation_tasks import DATASET_VERSION as TASK_DATASET_VERSION
from experiments.evaluation.final4.validation_tasks import TASKS as VALIDATION_TASKS
from experiments.evaluation.final4.validation_tasks import Task
from experiments.evaluation.models import BenchmarkTask
from experiments.evaluation.runner import RunnerSettings

from atlasos.config import load_config
from atlasos.sandbox import is_real_sandbox_provider
from atlasos.sandbox.provider import build_sandbox_provider

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "closure1b"
BASELINE_CONFIG = REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"

#: New, recognisable experiment identity. The Final-4 ids are untouched.
BENCHMARK_ID = "atlasos-closure1b-native-sandbox-v1"
MAX_TURN_PER_WORKER = final4_entry.MAX_TURN_PER_WORKER
TOKEN_BUDGET = final4_entry.TOKEN_BUDGET
MAX_TOOL_CALLS = final4_entry.MAX_TOOL_CALLS
TIMEOUT_SECONDS = final4_entry.TIMEOUT_SECONDS
TEMPERATURE = 0.0
MAX_TOKENS = 8000

UNITS: dict[str, dict[str, Any]] = {
    "N0": {"mode": "coding", "label": "native AnyCode single"},
    "N1": {"mode": "multi_worker", "label": "native AnyCode multi over the candidate DAG"},
}


def _task_by_id(task_id: str) -> Task:
    for task in VALIDATION_TASKS:
        if task.task_id == task_id:
            return task
    raise SystemExit(f"unknown task_id {task_id!r}")


def settings_for(configuration: str, *, runs_root: Path) -> RunnerSettings:
    """The Atlas-equivalent settings: same model, temperature, turn/token/tool limits, oracle.

    ``budget_mode`` stays ``legacy`` on purpose. The native arm has no shared budget guard, so it
    must not be recorded as if it had one; the same public thresholds are applied post hoc and the
    difference is written into the run's environment record.
    """

    if configuration not in UNITS:
        raise SystemExit(f"unknown configuration {configuration!r}")
    return RunnerSettings(
        benchmark_id=BENCHMARK_ID,
        dataset_version=TASK_DATASET_VERSION,
        scorer_version="oracle-1",
        config_path=BASELINE_CONFIG,
        runs_root=runs_root,
        timeout_seconds=TIMEOUT_SECONDS,
        temperature=TEMPERATURE,
        max_turns=MAX_TURN_PER_WORKER,
        max_tokens=MAX_TOKENS,
        token_budget=TOKEN_BUDGET,
        budget_mode="legacy",
        max_tool_calls=MAX_TOOL_CALLS,
        progressive_tools=False,
        handoff_mode="legacy",
        tool_error_mode="corrected",
        oracle_mode="sandbox",
        delegation_route_mode="off",
        context_plane_expectation="unspecified",
        prompt_layout="legacy",
        request_evidence=False,
    )


def build_task(task: Task, *, multi: bool) -> BenchmarkTask:
    """The frozen Final-4 task, reused rather than re-authored."""

    return final4_entry.build_task(task, multi=multi)


def preflight(configuration: str) -> dict[str, Any]:
    """Resolve the unit's environment before any container or model call exists."""

    if configuration not in UNITS:
        raise SystemExit(f"unknown configuration {configuration!r}")
    config = load_config(BASELINE_CONFIG)
    provider = build_sandbox_provider(
        config.sandbox.provider,
        opensandbox_options=None,
    )
    resolved = {
        "unit": configuration,
        "config_path": str(BASELINE_CONFIG),
        "sandbox_provider": config.sandbox.provider,
        "sandbox_image": config.sandbox.image,
        "sandbox_network": config.sandbox.network,
        "workspace_root": config.sandbox.workspace_root,
        "real_provider": is_real_sandbox_provider(provider, expected_name=config.sandbox.provider),
        "engine": "anycode-0.9.0-native-agents-with-neutral-opensandbox-tool-adapter",
    }
    if not resolved["real_provider"]:
        raise SystemExit(
            f"EVALUATION_CONFIG_ERROR: {configuration} resolves a non-real sandbox provider "
            f"{config.sandbox.provider!r}"
        )
    return resolved


async def run_unit(
    task_id: str,
    configuration: str,
    *,
    runs_root: Path,
    repetition_id: int = 0,
) -> dict[str, Any]:
    task = _task_by_id(task_id)
    settings = settings_for(configuration, runs_root=runs_root)
    resolved = preflight(configuration)
    multi = UNITS[configuration]["mode"] == "multi_worker"
    events: list[dict[str, Any]] = []
    result = await run_native_sample(
        build_task(task, multi=multi),
        settings=settings,
        repetition_id=repetition_id,
        runs_root=runs_root,
        config_path=BASELINE_CONFIG,
        task_events=events,
    )
    return {
        "unit": f"{task_id}-{configuration}",
        "runner": "native",
        "task_id": task_id,
        "configuration": configuration,
        "run_dir": result.run_dir,
        "run_id": result.identity.run_id,
        "oracle_pass": bool(result.oracle.passed),
        "success": bool(result.success),
        "failure_class": result.failure_class,
        "failure_reason": result.failure_reason,
        "config_digest": result.identity.config_digest,
        "resolved": resolved,
        "worker_count": len(result.worker_results),
        "tool_calls": result.metrics.tool_calls,
        "model_turns": result.metrics.model_turns,
        "sandbox_created": result.metrics.sandbox_created,
        "sandbox_destroyed": result.metrics.sandbox_destroyed,
        "prompt_tokens": result.metrics.prompt_tokens,
        "completion_tokens": result.metrics.completion_tokens,
        "estimated_cost_usd": result.metrics.estimated_cost_usd,
        "latency_ms": result.metrics.latency_ms,
        "sandbox": result.environment.get("sandbox"),
        "budget": result.environment.get("budget_check"),
        "task_events": events,
    }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="run closure-1b native baseline units")
    parser.add_argument("--unit", help="single unit, e.g. validation-serial-N0")
    parser.add_argument("--units", help="comma separated units")
    parser.add_argument("--runs-root", default=str(RUNS_ROOT))
    parser.add_argument("--repetition", type=int, default=0)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    requested = args.units.split(",") if args.units else ([args.unit] if args.unit else [])
    if not requested:
        print("nothing to do: pass --unit or --units")
        return 2
    runs_root = Path(args.runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for raw in requested:
        unit = raw.strip()
        task_id, _, configuration = unit.rpartition("-")
        if configuration not in UNITS:
            print(f"skip {unit}: unknown configuration {configuration!r}")
            continue
        print(f"=== {task_id} / {configuration} ===", flush=True)
        if args.preflight_only:
            summary.append({"unit": unit, "resolved": preflight(configuration)})
        else:
            if not os.environ.get("DEEPSEEK_API_KEY"):
                print("LIVE_PROVIDER_BLOCKED: no DEEPSEEK_API_KEY")
                return 2
            summary.append(
                asyncio.run(
                    run_unit(
                        task_id,
                        configuration,
                        runs_root=runs_root,
                        repetition_id=args.repetition,
                    )
                )
            )
        print(json.dumps(summary[-1], indent=2, sort_keys=True)[:1200], flush=True)
    (runs_root / "unit_summary.json").write_text(
        json.dumps(
            {
                "benchmark_id": BENCHMARK_ID,
                "dataset_version": TASK_DATASET_VERSION,
                "units": summary,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
