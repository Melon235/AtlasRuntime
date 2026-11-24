"""Host-side executor for the frozen 36-cell Stage B matrix.

This is experiment orchestration, not runtime: it never imports a provider, never builds a sandbox,
and never touches the frozen harness. It drives the existing ``runner --execute-cell`` CLI once per
cell, in the frozen order, under one global lock, and stops the whole matrix at the first cell that
is not a clean completion.

What it guarantees, and what it refuses to do:

* fixed order, one cell at a time, one attempt per cell;
* a claim that already exists stops the matrix instead of being reused or removed;
* a real OS exit code, stdout, stderr and pid per cell, captured to disk;
* after every cell: result, claim, evidence manifest, publication audit and sandbox cleanup;
* cumulative cost at the frozen price list, and no next cell once the stop target is reached;
* STOP (never retry, never skip) when usage, integrity or cleanup is unknown.

The executor carries no retry logic and no resume path. A crashed process leaves the lock and the
claims behind on purpose: recovering is an Owner decision, not something this file decides.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXECUTOR_VERSION = "stage-b-matrix-executor/0.1.0"
LOCK_NAME = ".matrix-executor.lock"
REPORT_NAME = "matrix-report.json"
LOG_DIR = "_executor"

STOP_CELL_FAILED = "cell_failed"
STOP_EXIT_NONZERO = "exit_code_nonzero"
STOP_EXIT_UNOBSERVED = "exit_code_unobserved"
STOP_TIMEOUT = "timeout"
STOP_CLAIM_CONSUMED = "claim_consumed"
STOP_USAGE_UNKNOWN = "usage_unknown"
STOP_INTEGRITY_UNKNOWN = "integrity_unknown"
STOP_RESIDUAL_SANDBOX = "residual_sandbox"
STOP_EVIDENCE_MISSING = "evidence_missing"
STOP_COST_REACHED = "cost_stop_reached"
STOP_PUBLICATION_REFUSED = "publication_refused"


class ExecutorError(RuntimeError):
    """A precondition failed before the matrix could start."""


class MatrixLocked(ExecutorError):
    """Another matrix executor owns the evidence root."""


@dataclass(frozen=True)
class LaunchResult:
    returncode: int | None
    pid: int | None
    timed_out: bool
    started_at: str
    finished_at: str
    duration_seconds: float


@dataclass
class CellRun:
    order: int
    cell_id: str
    evidence_dir: str
    exit_code: int | None = None
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    status: str | None = None
    failure_kind: str | None = None
    problems: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "cell_id": self.cell_id,
            "evidence_dir": self.evidence_dir,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "failure_kind": self.failure_kind,
            "problems": self.problems,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
        }


def digest_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate the executor configuration. Every mismatch is a refusal, not a warning."""

    config = json.loads(path.read_text(encoding="utf-8"))
    required = (
        "cells",
        "order",
        "evidence_root",
        "runs_root",
        "sandbox_endpoint",
        "python",
        "cell_timeout_seconds",
        "token_budget_per_cell",
        "token_budget_total",
        "cost_stop_usd",
        "pricing",
        "plan_digest",
        "protocol_digest",
        "runtime_digest",
        "harness_source_digest",
        "freeze_version",
        "authorization_path",
    )
    missing = [name for name in required if name not in config]
    if missing:
        raise ExecutorError(f"executor config is missing: {', '.join(missing)}")
    if len(config["cells"]) != 36:
        raise ExecutorError(f"executor config must carry 36 cells, not {len(config['cells'])}")
    if config["order"] != [cell["cell_id"] for cell in config["cells"]]:
        raise ExecutorError("order does not match the cell list")
    if len(set(config["order"])) != 36:
        raise ExecutorError("order contains a duplicate cell id")
    if config["cost_stop_usd"] is None:
        raise ExecutorError("cost_stop_usd is not approved; the matrix cannot start")
    if config["token_budget_total"] != 36 * config["token_budget_per_cell"]:
        raise ExecutorError("token_budget_total does not match 36 x per-cell budget")
    for key in ("evidence_root", "runs_root"):
        if not config[key]:
            raise ExecutorError(f"{key} is empty; a cell must not choose its own directory")
    return config


def preflight_config(config: Mapping[str, Any], *, repo_root: Path) -> list[str]:
    """Re-derive the identities the config claims. Read-only."""

    sys.path.insert(0, str(repo_root))
    from experiments.stage_benchmark import protocol
    from experiments.stage_benchmark import run_stage_b as stage_runner
    from experiments.stage_benchmark.execution import admission

    problems: list[str] = []
    if admission.source_identity()["digest"] != config["harness_source_digest"]:
        problems.append("harness source digest does not match the config")
    if stage_runner.source_identity()["tree_digest"] != config["runtime_digest"]:
        problems.append("runtime tree digest does not match the config")
    if digest_file(protocol.PROTOCOL_PATH) != config["protocol_digest"]:
        problems.append("protocol digest does not match the config")
    plan = json.loads(protocol.PLAN_PATH.read_text(encoding="utf-8"))
    if plan["plan"]["digest"] != config["plan_digest"]:
        problems.append("plan digest does not match the config")
    frozen = admission.read_json(admission.FREEZE_PATH)
    if frozen["freeze_version"] != config["freeze_version"]:
        problems.append("freeze version does not match the config")
    if frozen["source"]["digest"] != config["harness_source_digest"]:
        problems.append("freeze source digest does not match the config")
    planned = [item["cell_id"] for item in sorted(plan["plan"]["cells"], key=lambda c: c["order"])]
    if planned != config["order"]:
        problems.append("execution order does not match the frozen plan")
    estimate = json.loads(
        (repo_root / "experiments/stage_benchmark/estimate.json").read_text(encoding="utf-8")
    )
    if config["pricing"] != estimate["pricing"]:
        problems.append("pricing does not match the frozen estimate")
    return problems


# --------------------------------------------------------------------------- launcher


def default_launcher(
    argv: list[str],
    *,
    cell_root: Path,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
) -> LaunchResult:
    """Run the cell CLI and capture the real OS exit code. A timeout is a stop, never a retry."""

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat()
    started_monotonic = time.monotonic()
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        process = subprocess.Popen(
            argv, cwd=str(cwd), stdout=out, stderr=err, stdin=subprocess.DEVNULL
        )
        try:
            process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            timed_out = True
    finished = datetime.now(UTC).isoformat()
    return LaunchResult(
        returncode=None if timed_out else process.returncode,
        pid=process.pid,
        timed_out=timed_out,
        started_at=started,
        finished_at=finished,
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
    )


# --------------------------------------------------------------------------- cell audit


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def cell_usage(evidence: Path) -> dict[str, Any] | None:
    """Real provider usage, or None when any counter is missing. Unknown is not zero."""

    payload = _read_json(evidence / "provider_usage.json")
    if payload is None:
        return None
    keys = ("main_prompt_hit_tokens", "main_prompt_miss_tokens", "main_completion_tokens")
    usage = {key: payload.get(key) for key in keys}
    if any(not isinstance(value, int) for value in usage.values()):
        return None
    usage["main_requests"] = payload.get("main_requests")
    usage["system_total_tokens"] = payload.get("system_total_tokens")
    return usage


def cost_of(usage: Mapping[str, Any], pricing: Mapping[str, Any]) -> float:
    hit = usage["main_prompt_hit_tokens"] / 1e6 * pricing["cache_hit_input_per_million"]
    miss = usage["main_prompt_miss_tokens"] / 1e6 * pricing["cache_miss_input_per_million"]
    out = usage["main_completion_tokens"] / 1e6 * pricing["output_per_million"]
    return round(hit + miss + out, 9)


def audit_cell(
    *,
    repo_root: Path,
    cell_root: Path,
    task_id: str,
    exit_code: int | None,
    timed_out: bool,
) -> tuple[list[str], dict[str, Any] | None, list[str]]:
    """Return ``(problems, usage, stop_reasons)`` for one finished cell. Read-only."""

    sys.path.insert(0, str(repo_root))
    from experiments.stage_benchmark.execution import collector

    problems: list[str] = []
    stops: list[str] = []
    evidence = cell_root / "evidence"

    if timed_out:
        problems.append("the cell exceeded its timeout and was killed")
        stops.append(STOP_TIMEOUT)
    if exit_code is None:
        problems.append("the OS exit code was never observed")
        stops.append(STOP_EXIT_UNOBSERVED)
    elif exit_code != 0:
        problems.append(f"the cell exited with {exit_code}")
        stops.append(STOP_EXIT_NONZERO)

    claim = _read_json(cell_root / "attempts" / "attempt-1.json")
    if claim is None:
        problems.append("no claim marker")
        stops.append(STOP_EVIDENCE_MISSING)
    else:
        if claim.get("state") not in {"COLLECTED", "FAILED"}:
            problems.append(f"claim state is {claim.get('state')!r}")
        if claim.get("retries_allowed") is not False:
            problems.append("the claim allows a retry")

    result = _read_json(cell_root / "result.json")
    if result is None:
        problems.append("no result.json")
        stops.append(STOP_EVIDENCE_MISSING)
    else:
        if result.get("status") != "COMPLETED":
            problems.append(f"result status is {result.get('status')!r}")
        if result.get("failure_kind") not in {None, "none"}:
            problems.append(f"failure_kind is {result.get('failure_kind')!r}")
        if result.get("execution_integrity") != "OK":
            problems.append(f"execution_integrity is {result.get('execution_integrity')!r}")
            stops.append(STOP_INTEGRITY_UNKNOWN)

    manifest = _read_json(evidence / "evidence_manifest.json")
    if manifest is None:
        problems.append("no evidence manifest")
        stops.append(STOP_EVIDENCE_MISSING)
    else:
        publication = manifest.get("publication") or {}
        if publication.get("ok") is not True:
            problems.append("the manifest does not record a passing publication audit")
            stops.append(STOP_PUBLICATION_REFUSED)

    if evidence.is_dir():
        audit = collector.publication_audit(evidence, task_id=task_id)
        if audit["ok"] is not True:
            problems.extend(f"publication audit: {item}" for item in audit["problems"])
            stops.append(STOP_PUBLICATION_REFUSED)
    else:
        problems.append("the evidence directory is missing")
        stops.append(STOP_EVIDENCE_MISSING)

    lifecycle = _read_json(evidence / "sandbox_lifecycle.json")
    if lifecycle is None:
        problems.append("no sandbox lifecycle record")
        stops.append(STOP_EVIDENCE_MISSING)
    else:
        sandbox = lifecycle.get("sandbox") or {}
        if sandbox.get("destroyed") is not True or sandbox.get("retained") is True:
            problems.append(
                f"sandbox not destroyed cleanly: destroyed={sandbox.get('destroyed')!r} "
                f"retained={sandbox.get('retained')!r}"
            )
            stops.append(STOP_RESIDUAL_SANDBOX)
        if lifecycle.get("cleanup_failure"):
            problems.append(f"cleanup failure: {lifecycle['cleanup_failure']}")
            stops.append(STOP_RESIDUAL_SANDBOX)

    usage = cell_usage(evidence) if evidence.is_dir() else None
    if usage is None:
        problems.append("provider usage is unknown")
        stops.append(STOP_USAGE_UNKNOWN)

    if result is not None and result.get("status") != "COMPLETED" and not stops:
        stops.append(STOP_CELL_FAILED)
    return problems, usage, stops


# --------------------------------------------------------------------------- executor


def authorization_problems(
    config: Mapping[str, Any],
    authorization: Mapping[str, Any] | None,
    *,
    config_digest: str | None,
    executor_digest: str | None,
) -> list[str]:
    """Every binding a signed record must satisfy before one cell may start."""

    if not authorization:
        return ["no signed authorization record"]
    problems: list[str] = []
    if authorization.get("status") != "APPROVED":
        problems.append(f"status={authorization.get('status')!r}")
    for name in ("approved_by", "approval_commit", "approved_at"):
        if not authorization.get(name):
            problems.append(f"missing {name}")
    if authorization.get("consumed") is True:
        problems.append("the authorization is already consumed")
    approved_cells = set(authorization.get("cells") or ())
    planned = {cell["cell_id"] for cell in config["cells"]}
    if approved_cells != planned:
        problems.append("the authorization does not cover exactly the planned cells")
    if authorization.get("attempts_allowed") != 1:
        problems.append(f"attempts_allowed={authorization.get('attempts_allowed')!r}")
    if authorization.get("attempts_per_cell") != 1:
        problems.append(f"attempts_per_cell={authorization.get('attempts_per_cell')!r}")
    ceiling = authorization.get("cost_ceiling_usd")
    if not isinstance(ceiling, (int, float)) or ceiling <= 0:
        problems.append("no approved cost ceiling")
    elif float(ceiling) != float(config["cost_stop_usd"]):
        problems.append("the approved ceiling differs from the executor stop target")
    digests = authorization.get("approved_digests") or {}
    for name, expected in (
        ("protocol", config["protocol_digest"]),
        ("plan", config["plan_digest"]),
        ("runtime", config["runtime_digest"]),
        ("runner_source", config["harness_source_digest"]),
    ):
        if digests.get(name) != expected:
            problems.append(f"digest mismatch: {name}")
    if authorization.get("executor_digest") != executor_digest:
        problems.append("executor digest mismatch")
    if authorization.get("config_digest") != config_digest:
        problems.append("executor config digest mismatch")
    freeze = authorization.get("freeze_binding") or {}
    if freeze.get("version") != config["freeze_version"]:
        problems.append("freeze version mismatch")
    if freeze.get("raw_sha256") != config.get("freeze_raw_sha256"):
        problems.append("freeze raw digest mismatch")
    if freeze.get("content_digest") != config.get("freeze_content_digest"):
        problems.append("freeze content digest mismatch")
    if authorization.get("c3_rerun_allowed") is True:
        problems.append("the authorization allows re-running the calibration")
    return problems


def _acquire_lock(evidence_root: Path, *, label: str) -> Path:
    evidence_root.mkdir(parents=True, exist_ok=True)
    lock = evidence_root / LOCK_NAME
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise MatrixLocked(f"another executor owns this matrix: {lock}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(),
                                 "label": label}) + "\n")
    return lock


def run_matrix(
    config: Mapping[str, Any],
    *,
    repo_root: Path,
    authorization: Mapping[str, Any] | None,
    config_digest: str | None = None,
    executor_digest: str | None = None,
    launcher: Callable[..., LaunchResult] = default_launcher,
    lock_label: str = "matrix",
) -> dict[str, Any]:
    """Run the whole matrix once. It either completes 36 cells or stops with a reason."""

    gate = authorization_problems(
        config,
        authorization,
        config_digest=config_digest,
        executor_digest=executor_digest,
    )
    if gate:
        raise ExecutorError(
            "no usable authorization: the matrix will not start a single cell: "
            + "; ".join(gate)
        )

    problems = preflight_config(config, repo_root=repo_root)
    if problems:
        raise ExecutorError("executor preflight failed: " + "; ".join(problems))

    evidence_root = Path(config["evidence_root"])
    runs_root = Path(config["runs_root"])
    lock = _acquire_lock(evidence_root, label=lock_label)
    started_at = datetime.now(UTC).isoformat()
    runs: list[CellRun] = []
    total_cost = 0.0
    total_tokens = 0
    stop_reason: str | None = None
    stopped_at_order: int | None = None

    try:
        for index, cell in enumerate(config["cells"], start=1):
            cell_root = evidence_root / cell["cell_id"] / f"r{cell['repetition']}"
            record = CellRun(
                order=cell["order"],
                cell_id=cell["cell_id"],
                evidence_dir=str(cell_root),
            )

            sys.path.insert(0, str(repo_root))
            from experiments.stage_benchmark.execution import admission

            existing = admission.prior_attempt(cell_root)
            if existing is not None:
                record.problems.append(f"claim already exists: {existing}")
                runs.append(record)
                stop_reason, stopped_at_order = STOP_CLAIM_CONSUMED, cell["order"]
                break

            log_dir = evidence_root / LOG_DIR / cell["cell_id"]
            stdout_path = log_dir / "stdout.log"
            stderr_path = log_dir / "stderr.log"
            argv = [
                config["python"],
                "-m",
                "experiments.stage_benchmark.execution.runner",
                "--execute-cell",
                cell["cell_id"],
                "--live",
                "--confirm-live",
                "--authorization",
                config["authorization_path"],
                "--evidence-root",
                config["evidence_root"],
                "--runs-root",
                config["runs_root"],
                "--sandbox-endpoint",
                config["sandbox_endpoint"],
            ]
            launch = launcher(
                argv,
                cell_root=cell_root,
                cwd=repo_root,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=float(config["cell_timeout_seconds"]),
            )
            record.exit_code = launch.returncode
            record.pid = launch.pid
            record.started_at = launch.started_at
            record.finished_at = launch.finished_at
            record.duration_seconds = launch.duration_seconds
            record.stdout_path = str(stdout_path)
            record.stderr_path = str(stderr_path)

            cell_problems, usage, stops = audit_cell(
                repo_root=repo_root,
                cell_root=cell_root,
                task_id=cell["task_id"],
                exit_code=launch.returncode,
                timed_out=launch.timed_out,
            )
            record.problems.extend(cell_problems)
            if usage is not None:
                record.usage = dict(usage)
                record.cost_usd = cost_of(usage, config["pricing"])
                total_cost = round(total_cost + record.cost_usd, 9)
                total_tokens += int(usage["system_total_tokens"] or 0)
            result = _read_json(cell_root / "result.json") or {}
            record.status = result.get("status")
            record.failure_kind = result.get("failure_kind")
            runs.append(record)

            if stops:
                stop_reason, stopped_at_order = stops[0], cell["order"]
                break
            if index < len(config["cells"]) and total_cost >= float(config["cost_stop_usd"]):
                stop_reason, stopped_at_order = STOP_COST_REACHED, cell["order"]
                break
    finally:
        lock.unlink(missing_ok=True)

    finished_at = datetime.now(UTC).isoformat()
    report = {
        "executor_version": EXECUTOR_VERSION,
        "executor_digest": executor_digest,
        "config_digest": config_digest,
        "authorization_digest": authorization.get("digest") if authorization else None,
        "freeze_version": config["freeze_version"],
        "started_at": started_at,
        "finished_at": finished_at,
        "evidence_root": str(evidence_root),
        "runs_root": str(runs_root),
        "cells_planned": len(config["cells"]),
        "cells_started": len(runs),
        "cells_completed": sum(
            1 for item in runs if item.status == "COMPLETED" and not item.problems
        ),
        "stop_reason": stop_reason,
        "stopped_at_order": stopped_at_order,
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "cost_stop_usd": config["cost_stop_usd"],
        "cells": [item.to_dict() for item in runs],
        "retry": "FORBIDDEN",
        "resume": "FORBIDDEN",
    }
    report_path = evidence_root / REPORT_NAME
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--config`` and ``--authorization``. Refuses to run without a signed record."""

    import argparse

    parser = argparse.ArgumentParser(description="Stage B 36-cell host-side matrix executor")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    config = load_config(args.config)
    authorization = None
    if args.authorization and args.authorization.is_file():
        authorization = json.loads(args.authorization.read_text(encoding="utf-8"))
    try:
        report = run_matrix(
            config,
            repo_root=args.repo_root,
            authorization=authorization,
            config_digest=digest_file(args.config),
            executor_digest=digest_file(Path(__file__)),
            lock_label=str(args.config),
        )
    except (ExecutorError, MatrixLocked) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}))
        return 3
    print(
        json.dumps(
            {
                "status": "COMPLETED" if report["stop_reason"] is None else "STOPPED",
                "stop_reason": report["stop_reason"],
                "cells_started": report["cells_started"],
                "cells_completed": report["cells_completed"],
                "total_cost_usd": report["total_cost_usd"],
                "report": report["report_path"],
            }
        )
    )
    return 0 if report["stop_reason"] is None else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
