# ruff: noqa: E501
"""Closure-1B B6: deterministic acceptance of the native baseline on a real OpenSandbox.

No model is called. The agent loop is real, the tool registry and executor are real, the container
is real; only the provider is a script replaying the frozen reference solution's tool calls.

    python -m experiments.evaluation.closure1b.acceptance

Exit codes: ``0`` accepted, ``3`` environment blocked (nothing was faked), ``1`` a check failed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.closure1b.native_baseline import run_native_sample
from experiments.evaluation.closure1b.runner_entry import (
    BASELINE_CONFIG,
    _task_by_id,
    build_task,
    settings_for,
)
from experiments.evaluation.closure1b.scripted import (
    assignment_marker,
    register_assignment_aware_provider,
    register_scripted_provider,
    solution_calls,
)

from atlasos.config import load_config
from atlasos.sandbox import build_sandbox_provider, is_real_sandbox_provider
from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure1b" / "acceptance.json"

SERIAL = "validation-serial"
PARALLEL = "validation-parallel"


def _provider(config: Any) -> Any:
    sandbox = config.sandbox
    return build_sandbox_provider(
        sandbox.provider,
        opensandbox_options=OpenSandboxOptions(
            endpoint=sandbox.endpoint,
            protocol=sandbox.protocol,
            timeout_minutes=sandbox.timeout_minutes,
            ready_timeout_seconds=sandbox.ready_timeout_seconds,
            cpu=sandbox.cpu,
            memory=sandbox.memory,
            use_server_proxy=sandbox.use_server_proxy,
        ),
    )


def _overlap(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Wall-clock overlap between two independent workers, from the orchestrator's own events.

    Two DAG nodes are not concurrency. The orchestrator emits ``agent_start``/``agent_complete`` per
    agent; a pair counts as concurrent only when each started before the other finished.
    """

    starts: dict[str, str] = {}
    ends: dict[str, str] = {}
    for event in events:
        agent = str(event.get("agent") or "")
        if not agent:
            continue
        if event["type"] == "agent_start":
            starts[agent] = str(event["ts"])
        elif event["type"] == "agent_complete":
            ends[agent] = str(event["ts"])
    return {
        "starts": starts,
        "ends": ends,
        "workers_started": len(starts),
        "workers_completed": len(ends),
    }


def _pair_overlapped(starts: dict[str, str], ends: dict[str, str], a: str, b: str) -> bool:
    from datetime import datetime as dt

    def parse(value: str) -> dt:
        return dt.fromisoformat(value)

    if a not in starts or b not in starts or a not in ends or b not in ends:
        return False
    # Overlap means each started before the other finished.
    return parse(starts[a]) < parse(ends[b]) and parse(starts[b]) < parse(ends[a])


async def _arm(
    *,
    task_id: str,
    configuration: str,
    provider_name: str,
    runs_root: Path,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    task = _task_by_id(task_id)
    multi = configuration == "N1"
    settings = settings_for(configuration, runs_root=runs_root)
    result = await run_native_sample(
        build_task(task, multi=multi),
        settings=settings,
        repetition_id=0,
        runs_root=runs_root,
        config_path=BASELINE_CONFIG,
        provider_name=provider_name,
        task_events=events,
    )
    export = Path(result.workspace_export) if result.workspace_export else None
    written = {}
    if export is not None and export.is_dir():
        for name in sorted(task.solution):
            path = export / name
            written[name] = {
                "present": path.is_file(),
                "matches_reference": path.is_file()
                and path.read_text(encoding="utf-8") == task.solution[name],
            }
    return {
        "unit": f"{task_id}-{configuration}",
        "oracle_pass": bool(result.oracle.passed),
        "oracle_kind": result.oracle.failure_kind,
        "oracle_reasons": list(result.oracle.failure_reasons),
        "success": bool(result.success),
        "failure_class": result.failure_class,
        "failure_reason": result.failure_reason,
        "sandbox": result.environment.get("sandbox"),
        "container": result.environment.get("container"),
        "sandbox_destroyed": bool(result.metrics.sandbox_destroyed),
        "sandbox_destroy": result.environment.get("sandbox_destroy"),
        "tool_calls": result.metrics.tool_calls,
        "model_turns": result.metrics.model_turns,
        "worker_count": len(result.worker_results),
        "writes": written,
        "patch_digest": result.patch_digest,
        "export_dir": str(export) if export else None,
        "latency_ms": result.metrics.latency_ms,
        "provision_latency_ms": result.metrics.provision_latency_ms,
        "agent_latency_ms": result.environment.get("agent_latency_ms"),
        "export_latency_ms": result.environment.get("export_latency_ms"),
        "run_dir": result.run_dir,
    }


async def run_acceptance(*, out_path: Path = DEFAULT_OUT, runs_root: Path | None = None) -> int:
    config = load_config(BASELINE_CONFIG)
    provider = _provider(config)
    report: dict[str, Any] = {
        "stage": "closure-1b",
        "generated_at": datetime.now(UTC).isoformat(),
        "kind": "deterministic_real_sandbox_acceptance",
        "model_calls": 0,
        "engine": "anycode-0.9.0-native-agents-with-neutral-opensandbox-tool-adapter",
        "config": {
            "file": str(BASELINE_CONFIG.relative_to(REPO_ROOT)),
            "provider": config.sandbox.provider,
            "image": config.sandbox.image,
            "endpoint": config.sandbox.endpoint,
            "network": config.sandbox.network,
            "workspace_root": config.sandbox.workspace_root,
            "cpu": config.sandbox.cpu,
            "memory": config.sandbox.memory,
        },
    }
    health = await provider.health()
    report["provider_health"] = {"status": health.status, "message": health.message}
    if health.status == "unavailable" or not is_real_sandbox_provider(
        provider, expected_name=config.sandbox.provider
    ):
        report["status"] = "BLOCKED"
        report["blocked_reason"] = (
            f"the local OpenSandbox provider is not usable: {health.status} ({health.message})"
        )
        _write(out_path, report)
        return 3

    workdir = Path(tempfile.mkdtemp(prefix="closure1b-acceptance-"))
    try:
        runs_root = runs_root or workdir / "runs"
        serial = _task_by_id(SERIAL)
        register_scripted_provider(
            "closure1b-scripted-n0",
            solution_calls(serial, None),
            final_text="implemented clean_rows and build_report",
        )
        n0_events: list[dict[str, Any]] = []
        n0 = await _arm(
            task_id=SERIAL,
            configuration="N0",
            provider_name="closure1b-scripted-n0",
            runs_root=runs_root,
            events=n0_events,
        )
        report["n0"] = n0
        report["n0"]["task_events"] = n0_events

        parallel = _task_by_id(PARALLEL)
        scripts = {
            assignment_marker(worker): solution_calls(parallel, str(worker["task_id"]))
            for worker in parallel.workers
        }
        register_assignment_aware_provider("closure1b-scripted-n1", scripts)
        n1_events: list[dict[str, Any]] = []
        n1 = await _arm(
            task_id=PARALLEL,
            configuration="N1",
            provider_name="closure1b-scripted-n1",
            runs_root=runs_root,
            events=n1_events,
        )
        overlap = _overlap(n1_events)
        independent = [
            assignment_marker(worker) for worker in parallel.workers if not worker["depends_on"]
        ]
        independent_agents = [
            f"native:{worker['task_id']}" for worker in parallel.workers if not worker["depends_on"]
        ]
        concurrent = len(independent_agents) == 2 and _pair_overlapped(
            overlap["starts"], overlap["ends"], independent_agents[0], independent_agents[1]
        )
        report["n1"] = n1
        report["n1"]["concurrency"] = {
            **overlap,
            "independent_workers": independent,
            "independent_agents": independent_agents,
            "independent_pair_overlapped": concurrent,
            "basis": (
                "orchestrator agent_start/agent_complete timestamps of the two dependency-free "
                "workers, not the DAG shape"
            ),
        }
        report["n1"]["task_events"] = n1_events
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    checks = {
        "n0_oracle_passed": bool(report["n0"]["oracle_pass"]),
        "n1_oracle_passed": bool(report["n1"]["oracle_pass"]),
        "n0_wrote_reference_solution": all(
            item["matches_reference"] for item in report["n0"]["writes"].values()
        ),
        "n1_wrote_reference_solution": all(
            item["matches_reference"] for item in report["n1"]["writes"].values()
        ),
        "n0_sandbox_destroyed": bool(report["n0"]["sandbox_destroyed"]),
        "n1_sandbox_destroyed": bool(report["n1"]["sandbox_destroyed"]),
        "n0_isolation_passed": bool(
            (report["n0"]["sandbox"] or {}).get("isolation", {}).get("isolation_checks_passed")
        ),
        "n1_isolation_passed": bool(
            (report["n1"]["sandbox"] or {}).get("isolation", {}).get("isolation_checks_passed")
        ),
        "n0_linux_container": (report["n0"]["container"] or {}).get("uname") == "Linux",
        "n1_linux_container": (report["n1"]["container"] or {}).get("uname") == "Linux",
        "n0_head_matches_source_commit": (
            (report["n0"]["container"] or {}).get("head_commit")
            == (report["n0"]["sandbox"] or {}).get("source_commit")
        ),
        "n1_head_matches_source_commit": (
            (report["n1"]["container"] or {}).get("head_commit")
            == (report["n1"]["sandbox"] or {}).get("source_commit")
        ),
        "n1_independent_workers_overlapped": bool(
            report["n1"]["concurrency"]["independent_pair_overlapped"]
        ),
        "n1_has_three_workers": report["n1"]["worker_count"] == 3,
        "tool_calls_were_real": report["n0"]["tool_calls"] >= 5
        and report["n1"]["tool_calls"] >= 15,
    }
    report["checks"] = checks
    report["status"] = "REAL_SANDBOX_ACCEPTED" if all(checks.values()) else "ACCEPTANCE_FAILED"
    _write(out_path, report)
    return 0 if all(checks.values()) else 1


def sanitize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Strip absolute Host paths from the committed evidence.

    The run directories are transient by construction — the acceptance deletes its workdir — so
    publishing the path would only leak a machine layout that no longer exists. The facts stay.
    """

    for arm in ("n0", "n1"):
        entry = report.get(arm)
        if not isinstance(entry, dict):
            continue
        if entry.get("run_dir"):
            entry["run_dir"] = "transient (deleted after the run)"
        if entry.get("export_dir"):
            entry["export_dir"] = Path(str(entry["export_dir"])).name
    text = json.dumps(report, indent=2, sort_keys=True)
    for pattern in (r"[A-Za-z]:\\\\+Users\\\\+[^\"]+", r"[A-Za-z]:\\+Users\\+[^\"]+"):
        text = re.sub(pattern, "<host-temp>", text)
    return json.loads(text)


def _write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_report(report), indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report.get("checks")}, indent=2))


def main() -> int:
    return asyncio.run(run_acceptance())


if __name__ == "__main__":
    raise SystemExit(main())
