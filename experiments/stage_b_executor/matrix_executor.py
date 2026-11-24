"""Host-side executor for the frozen 36-cell Stage B matrix.

This is experiment orchestration, not runtime: it never imports a provider, never builds a sandbox,
and never touches the frozen harness. It drives the existing ``runner --execute-cell`` CLI once per
cell, in the frozen order, under one global lock, and applies the Owner-approved stop policy.

Owner-approved policy: ``MEASURED_AGENT_FAILURE_CONTINUE``.

* A normal solve failure (oracle failed, public tests failed, budget exhausted, main hit its turn
  limit) is a *measured observation*: it is recorded as a real FAILED sample and the matrix goes on.
* Anything else stops the matrix: provider or infrastructure faults, unknown failure kinds, unknown
  goal or oracle state, missing or drifted evidence, a failed publication audit, an unclean sandbox,
  unknown usage or a broken token reconciliation, an unexpected/unobserved exit code, a timeout, a
  claim/authorization/identity mismatch, or a budget stop.

The executor never retries, never skips, never cleans a claim, and never resumes: a stopped or
crashed matrix leaves its lock, its dispatch log and its ledger behind on purpose.
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

EXECUTOR_VERSION = "stage-b-matrix-executor/1.0.0"
POLICY_MEASURED_CONTINUE = "MEASURED_AGENT_FAILURE_CONTINUE"
LOCK_NAME = ".matrix-executor.lock"
REPORT_NAME = "matrix-report.json"
DISPATCH_LOG = "dispatch.jsonl"
LEDGER_LOG = "ledger.jsonl"
LOG_DIR = "_executor"

VERDICT_PASSED = "PASSED"
VERDICT_FAILED = "FAILED"
VERDICT_INVALID = "INVALID"

#: Failure kinds the Owner pre-registered as valid observations.
CONTINUABLE_FAILURE_KINDS = frozenset(
    {"oracle_failed", "public_tests_failed", "budget_exhaustion", "main_max_turns"}
)
#: Evidence a cell must have produced before any verdict may be trusted.
REQUIRED_EVIDENCE = (
    "session.json",
    "attempt-1.json",
    "transcript.jsonl",
    "provider_usage.json",
    "public_tests.json",
    "oracle.json",
    "sandbox_lifecycle.json",
    "analysis.json",
    "evidence_manifest.json",
)

STOP_CELL_INVALID = "cell_invalid"
STOP_INFRASTRUCTURE = "infrastructure_failure"
STOP_UNKNOWN_FAILURE_KIND = "unknown_failure_kind"
STOP_GOAL_UNKNOWN = "goal_unknown"
STOP_EXIT_NONZERO = "exit_code_nonzero"
STOP_EXIT_MISMATCH = "exit_code_result_mismatch"
STOP_EXIT_UNOBSERVED = "exit_code_unobserved"
STOP_TIMEOUT = "timeout"
STOP_CLAIM_CONSUMED = "claim_consumed"
STOP_CLAIM_INVALID = "claim_invalid"
STOP_USAGE_UNKNOWN = "usage_unknown"
STOP_USAGE_MISMATCH = "usage_reconciliation_failed"
STOP_RESIDUAL_SANDBOX = "residual_sandbox"
STOP_EVIDENCE_MISSING = "evidence_missing"
STOP_EVIDENCE_DRIFT = "evidence_digest_mismatch"
STOP_PUBLICATION_REFUSED = "publication_refused"
STOP_SECONDARY_FAILURE = "secondary_failure"
STOP_IDENTITY_MISMATCH = "identity_mismatch"
STOP_AUTHORIZATION_CHANGED = "authorization_changed"
STOP_COST_REACHED = "cost_stop_reached"
STOP_TOKENS_REACHED = "token_stop_reached"


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
class CellOutcome:
    order: int
    cell_id: str
    task_id: str
    evidence_dir: str
    verdict: str = VERDICT_INVALID
    failure_kind: str | None = None
    problems: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    exit_code: int | None = None
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- primitives


def digest_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def pull_request_head(repo_root: Path, number: int) -> str | None:
    """The head of a remote pull request, read anonymously. None when it cannot be read."""

    try:
        output = _git(repo_root, "ls-remote", "origin", f"refs/pull/{number}/head")
    except (subprocess.CalledProcessError, OSError):
        return None
    line = output.split()
    return line[0] if line else None


# --------------------------------------------------------------------------- usage and cost


def system_usage(evidence: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Every token counter the matrix grades on, or None when any of them is unusable.

    A missing counter is never zero: it means the run's usage is unknown, and an unknown run cannot
    be priced, reconciled or counted.
    """

    payload = _read_json(evidence / "provider_usage.json")
    if payload is None:
        return None, ["provider_usage.json is missing or unreadable"]
    keys = (
        "main_prompt_hit_tokens",
        "main_prompt_miss_tokens",
        "main_completion_tokens",
        "child_total_prompt_tokens",
        "child_completion_tokens",
        "system_total_tokens",
    )
    problems = [
        f"{key} is not a non-negative integer"
        for key in keys
        if not _is_count(payload.get(key))
    ]
    if problems:
        return None, problems

    usage: dict[str, Any] = {key: int(payload[key]) for key in keys}
    usage["main_requests"] = payload.get("main_requests")
    main_prompt = usage["main_prompt_hit_tokens"] + usage["main_prompt_miss_tokens"]
    total = (
        main_prompt
        + usage["main_completion_tokens"]
        + usage["child_total_prompt_tokens"]
        + usage["child_completion_tokens"]
    )
    if total != usage["system_total_tokens"]:
        return None, [
            f"token reconciliation failed: main+child={total} system_total="
            f"{usage['system_total_tokens']}"
        ]
    usage["reconciled_total_tokens"] = total
    return usage, []


def cost_of(usage: Mapping[str, Any], pricing: Mapping[str, Any]) -> dict[str, Any]:
    """Frozen-price estimate for Main + workers. A child cache split is never visible.

    The provider reports no cache split for worker requests, so child prompt tokens are billed at
    the cache-miss rate. That is the conservative choice and it is recorded as an estimate, not as a
    provider invoice.
    """

    hit_rate = pricing["cache_hit_input_per_million"]
    miss_rate = pricing["cache_miss_input_per_million"]
    out_rate = pricing["output_per_million"]

    main_hit = usage["main_prompt_hit_tokens"] / 1e6 * hit_rate
    main_miss = usage["main_prompt_miss_tokens"] / 1e6 * miss_rate
    main_out = usage["main_completion_tokens"] / 1e6 * out_rate
    child_prompt = usage["child_total_prompt_tokens"] / 1e6 * miss_rate
    child_out = usage["child_completion_tokens"] / 1e6 * out_rate
    total = main_hit + main_miss + main_out + child_prompt + child_out
    return {
        "main_cache_hit_usd": round(main_hit, 9),
        "main_cache_miss_usd": round(main_miss, 9),
        "main_completion_usd": round(main_out, 9),
        "child_prompt_usd": round(child_prompt, 9),
        "child_completion_usd": round(child_out, 9),
        "child_prompt_billed_as": "cache_miss_input",
        "child_cache_estimated": True,
        "total_usd": round(total, 9),
        "basis": "frozen estimate.json price list, not a provider invoice",
    }


# --------------------------------------------------------------------------- evidence audit


def verify_manifest(evidence: Path) -> list[str]:
    """Recompute every digest the manifest declares. Read-only."""

    manifest = _read_json(evidence / "evidence_manifest.json")
    if manifest is None:
        return ["evidence_manifest.json is missing or unreadable"]
    problems: list[str] = []
    files = manifest.get("files")
    raw = manifest.get("raw_digests")
    if not isinstance(files, dict) or not files:
        problems.append("the manifest declares no published files")
        files = {}
    if not isinstance(raw, dict):
        problems.append("the manifest declares no raw digests")
        raw = {}

    for name, declared in sorted(files.items()):
        path = evidence / str(name).replace("/", "\\")
        if not path.is_file():
            problems.append(f"declared file is missing: {name}")
            continue
        if digest_file(path) != declared:
            problems.append(f"declared digest does not match: {name}")

    for name, declared in sorted(raw.items()):
        path = evidence / "_raw" / str(name).replace("/", "\\")
        if not path.is_file():
            problems.append(f"declared raw file is missing: {name}")
            continue
        if digest_file(path) != declared:
            problems.append(f"declared raw digest does not match: {name}")
    return problems


def verify_required_evidence(evidence: Path) -> list[str]:
    return [f"required evidence is missing: {name}" for name in REQUIRED_EVIDENCE
            if not (evidence / name).is_file()]


# --------------------------------------------------------------------------- cell audit


def audit_cell(
    *,
    repo_root: Path,
    cell_root: Path,
    cell_id: str,
    task_id: str,
    repetition: int,
    exit_code: int | None,
    timed_out: bool,
    harness_source_digest: str,
    authorization_digest: str,
) -> CellOutcome:
    """Grade one finished cell against the pre-registered policy. Read-only."""

    sys.path.insert(0, str(repo_root))
    from experiments.stage_benchmark.execution import collector

    outcome = CellOutcome(order=0, cell_id=cell_id, task_id=task_id, evidence_dir=str(cell_root))
    evidence = cell_root / "evidence"

    if timed_out:
        outcome.stops.append(STOP_TIMEOUT)
        outcome.problems.append("the cell exceeded its timeout and was killed")
    if exit_code is None:
        outcome.stops.append(STOP_EXIT_UNOBSERVED)
        outcome.problems.append("the OS exit code was never observed")

    claim = _read_json(cell_root / "attempts" / "attempt-1.json")
    if claim is None:
        outcome.stops.append(STOP_EVIDENCE_MISSING)
        outcome.problems.append("no claim marker")
    else:
        if claim.get("state") not in {"COLLECTED", "FAILED"}:
            outcome.stops.append(STOP_CLAIM_INVALID)
            outcome.problems.append(f"claim state is {claim.get('state')!r}")
        if claim.get("retries_allowed") is not False:
            outcome.stops.append(STOP_CLAIM_INVALID)
            outcome.problems.append("the claim allows a retry")
        claimed_cell = claim.get("cell") or {}
        if claimed_cell.get("cell_id") != cell_id or claimed_cell.get("repetition") != repetition:
            outcome.stops.append(STOP_IDENTITY_MISMATCH)
            outcome.problems.append("the claim names a different cell")
        if claim.get("runner_source_digest") != harness_source_digest:
            outcome.stops.append(STOP_IDENTITY_MISMATCH)
            outcome.problems.append("the claim records a different runner source")
        if claim.get("authorization_digest") != authorization_digest:
            outcome.stops.append(STOP_IDENTITY_MISMATCH)
            outcome.problems.append("the claim records a different authorization")

    result = _read_json(cell_root / "result.json")
    if result is None:
        outcome.stops.append(STOP_EVIDENCE_MISSING)
        outcome.problems.append("no result.json")
        result = {}
    else:
        outcome.failure_kind = result.get("failure_kind")
        recorded = (result.get("digests") or {}).get("runner_source")
        if recorded != harness_source_digest:
            outcome.stops.append(STOP_IDENTITY_MISMATCH)
            outcome.problems.append("result.json records a different runner source")
        status = result.get("status")
        expected_exit = 0 if status == "COMPLETED" else 1
        settled = status in {"COMPLETED", "FAILED"}
        if exit_code is not None and settled and exit_code != expected_exit:
            outcome.stops.append(STOP_EXIT_MISMATCH)
            outcome.problems.append(
                f"exit code {exit_code} does not match result status {status!r}"
            )
        secondary = result.get("secondary_failures") or []
        if secondary:
            outcome.stops.append(STOP_SECONDARY_FAILURE)
            outcome.problems.extend(f"secondary failure: {item}" for item in secondary)

    evidence_problems = verify_required_evidence(evidence) if evidence.is_dir() else [
        "the evidence directory is missing"
    ]
    if evidence_problems:
        outcome.stops.append(STOP_EVIDENCE_MISSING)
        outcome.problems.extend(evidence_problems)
    else:
        manifest_problems = verify_manifest(evidence)
        if manifest_problems:
            outcome.stops.append(STOP_EVIDENCE_DRIFT)
            outcome.problems.extend(manifest_problems)
        audit = collector.publication_audit(evidence, task_id=task_id)
        if audit["ok"] is not True:
            outcome.stops.append(STOP_PUBLICATION_REFUSED)
            outcome.problems.extend(f"publication audit: {item}" for item in audit["problems"])

    lifecycle = _read_json(evidence / "sandbox_lifecycle.json") or {}
    sandbox = lifecycle.get("sandbox") or {}
    if sandbox.get("destroyed") is not True or sandbox.get("retained") is True:
        outcome.stops.append(STOP_RESIDUAL_SANDBOX)
        outcome.problems.append(
            f"sandbox not destroyed cleanly: destroyed={sandbox.get('destroyed')!r} "
            f"retained={sandbox.get('retained')!r}"
        )
    if lifecycle.get("cleanup_failure"):
        outcome.stops.append(STOP_RESIDUAL_SANDBOX)
        outcome.problems.append(f"cleanup failure: {lifecycle['cleanup_failure']}")

    usage, usage_problems = system_usage(evidence) if evidence.is_dir() else (None, ["no evidence"])
    if usage is None:
        outcome.stops.append(
            STOP_USAGE_MISMATCH if any("reconciliation" in item for item in usage_problems)
            else STOP_USAGE_UNKNOWN
        )
        outcome.problems.extend(usage_problems)
    else:
        outcome.usage = usage

    # Verdict: only a clean result or a pre-registered failure is a usable observation.
    status = result.get("status")
    kind = result.get("failure_kind")
    if outcome.stops:
        outcome.verdict = VERDICT_INVALID
    elif status == "COMPLETED" and kind in {None, "none"}:
        outcome.verdict = VERDICT_PASSED
    elif status == "FAILED" and kind in CONTINUABLE_FAILURE_KINDS:
        outcome.verdict = VERDICT_FAILED
    elif kind in {"goal_unknown", None} or status is None:
        outcome.verdict = VERDICT_INVALID
        outcome.stops.append(STOP_GOAL_UNKNOWN)
        outcome.problems.append(
            f"cannot form a registered observation: status={status!r} kind={kind!r}"
        )
    else:
        outcome.verdict = VERDICT_INVALID
        outcome.stops.append(STOP_UNKNOWN_FAILURE_KIND)
        outcome.problems.append(f"failure kind {kind!r} is not a registered observation")

    return outcome


# --------------------------------------------------------------------------- config


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate the executor configuration. Every mismatch is a refusal."""

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
        "freeze_raw_sha256",
        "freeze_content_digest",
        "authorization_path",
        "stop_policy",
        "pull_request",
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
    if config["stop_policy"] != POLICY_MEASURED_CONTINUE:
        raise ExecutorError(f"stop_policy={config['stop_policy']!r} is not the approved policy")
    if not isinstance(config["cost_stop_usd"], (int, float)) or config["cost_stop_usd"] <= 0:
        raise ExecutorError("cost_stop_usd is not an approved, positive target")
    if config["token_budget_total"] != 36 * config["token_budget_per_cell"]:
        raise ExecutorError("token_budget_total does not match 36 x per-cell budget")
    for key in ("evidence_root", "runs_root"):
        if not config[key]:
            raise ExecutorError(f"{key} is empty; a cell must not choose its own directory")
    return config


def preflight_config(
    config: Mapping[str, Any], *, repo_root: Path, verify_pr: bool = True
) -> list[str]:
    """Re-derive every identity the config claims. Read-only."""

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
    if digest_file(admission.FREEZE_PATH) != config["freeze_raw_sha256"]:
        problems.append("freeze raw digest does not match the config")
    if admission.content_digest(admission.FREEZE_PATH) != config["freeze_content_digest"]:
        problems.append("freeze content digest does not match the config")
    planned = [item["cell_id"] for item in sorted(plan["plan"]["cells"], key=lambda c: c["order"])]
    if planned != config["order"]:
        problems.append("execution order does not match the frozen plan")
    estimate = json.loads(
        (repo_root / "experiments/stage_benchmark/estimate.json").read_text(encoding="utf-8")
    )
    if config["pricing"] != estimate["pricing"]:
        problems.append("pricing does not match the frozen estimate")

    if verify_pr:
        number = config["pull_request"]["number"]
        head = _git(repo_root, "rev-parse", "HEAD")
        remote = pull_request_head(repo_root, number)
        if remote is None:
            problems.append(f"PR #{number} head could not be read")
        elif remote != head:
            problems.append(f"PR #{number} head {remote} is not the working HEAD {head}")
    return problems


# --------------------------------------------------------------------------- authorization


def authorization_problems(
    config: Mapping[str, Any],
    authorization: Mapping[str, Any] | None,
    *,
    config_digest: str | None,
    executor_digest: str | None,
    head: str | None,
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
    if head is None or authorization.get("executor_commit") != head:
        problems.append("executor commit mismatch")
    if authorization.get("stop_policy") != config["stop_policy"]:
        problems.append("stop policy mismatch")
    freeze = authorization.get("freeze_binding") or {}
    if freeze.get("version") != config["freeze_version"]:
        problems.append("freeze version mismatch")
    if freeze.get("raw_sha256") != config["freeze_raw_sha256"]:
        problems.append("freeze raw digest mismatch")
    if freeze.get("content_digest") != config["freeze_content_digest"]:
        problems.append("freeze content digest mismatch")
    if authorization.get("c3_rerun_allowed") is True:
        problems.append("the authorization allows re-running the calibration")
    return problems


# --------------------------------------------------------------------------- ledger


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fresh_log(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        raise ExecutorError(f"an execution ledger already exists and is never overwritten: {path}")


def _acquire_lock(evidence_root: Path, *, label: str) -> Path:
    evidence_root.mkdir(parents=True, exist_ok=True)
    lock = evidence_root / LOCK_NAME
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise MatrixLocked(f"another executor owns this matrix: {lock}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(), "label": label}
            )
            + "\n"
        )
    return lock


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
    return LaunchResult(
        returncode=None if timed_out else process.returncode,
        pid=process.pid,
        timed_out=timed_out,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
    )


# --------------------------------------------------------------------------- matrix


def run_matrix(
    config: Mapping[str, Any],
    *,
    repo_root: Path,
    authorization: Mapping[str, Any] | None,
    config_digest: str | None = None,
    executor_digest: str | None = None,
    launcher: Callable[..., LaunchResult] = default_launcher,
    lock_label: str = "matrix",
    verify_pr: bool = True,
) -> dict[str, Any]:
    """Run the matrix once under the approved policy. It either finishes or stops with a reason."""

    head = _git(repo_root, "rev-parse", "HEAD")
    gate = authorization_problems(
        config,
        authorization,
        config_digest=config_digest,
        executor_digest=executor_digest,
        head=head,
    )
    if gate:
        raise ExecutorError(
            "no usable authorization: the matrix will not start a single cell: " + "; ".join(gate)
        )
    assert authorization is not None

    problems = preflight_config(config, repo_root=repo_root, verify_pr=verify_pr)
    if problems:
        raise ExecutorError("executor preflight failed: " + "; ".join(problems))

    sys.path.insert(0, str(repo_root))
    from experiments.stage_benchmark.execution import admission

    evidence_root = Path(config["evidence_root"])
    runs_root = Path(config["runs_root"])
    dispatch_path = evidence_root / DISPATCH_LOG
    ledger_path = evidence_root / LEDGER_LOG
    evidence_root.mkdir(parents=True, exist_ok=True)
    _fresh_log(dispatch_path)
    _fresh_log(ledger_path)

    authorization_file = Path(config["authorization_path"])
    authorization_sha = digest_file(authorization_file)
    authorization_digest = admission.canonical_digest(dict(authorization))

    lock = _acquire_lock(evidence_root, label=lock_label)
    started_at = datetime.now(UTC).isoformat()
    outcomes: list[CellOutcome] = []
    total_cost = 0.0
    total_tokens = 0
    stop_reason: str | None = None
    stopped_at_order: int | None = None

    try:
        for index, cell in enumerate(config["cells"], start=1):
            cell_root = evidence_root / cell["cell_id"] / f"r{cell['repetition']}"
            existing = admission.prior_attempt(cell_root)
            if existing is not None:
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
                str(authorization_file),
                "--evidence-root",
                config["evidence_root"],
                "--runs-root",
                config["runs_root"],
                "--sandbox-endpoint",
                config["sandbox_endpoint"],
            ]
            _append_jsonl(
                dispatch_path,
                {
                    "order": cell["order"],
                    "cell_id": cell["cell_id"],
                    "repetition": cell["repetition"],
                    "dispatched_at": datetime.now(UTC).isoformat(),
                    "argv": argv,
                    "authorization_sha256": authorization_sha,
                    "authorization_digest": authorization_digest,
                    "config_digest": config_digest,
                    "executor_digest": executor_digest,
                    "executor_commit": head,
                    "cost_before_usd": total_cost,
                    "tokens_before": total_tokens,
                },
            )

            launch = launcher(
                argv,
                cell_root=cell_root,
                cwd=repo_root,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=float(config["cell_timeout_seconds"]),
            )

            outcome = audit_cell(
                repo_root=repo_root,
                cell_root=cell_root,
                cell_id=cell["cell_id"],
                task_id=cell["task_id"],
                repetition=cell["repetition"],
                exit_code=launch.returncode,
                timed_out=launch.timed_out,
                harness_source_digest=config["harness_source_digest"],
                authorization_digest=authorization_digest,
            )
            outcome.order = cell["order"]
            outcome.exit_code = launch.returncode
            outcome.pid = launch.pid
            outcome.started_at = launch.started_at
            outcome.finished_at = launch.finished_at
            outcome.duration_seconds = launch.duration_seconds
            outcome.stdout_path = str(stdout_path)
            outcome.stderr_path = str(stderr_path)

            if outcome.usage:
                priced = cost_of(outcome.usage, config["pricing"])
                cell_cost = float(priced["total_usd"])
                outcome.cost_usd = cell_cost
                total_cost = round(total_cost + cell_cost, 9)
                total_tokens += int(outcome.usage["system_total_tokens"])
                outcome.usage = {**outcome.usage, "cost": priced}
            outcomes.append(outcome)

            _append_jsonl(
                ledger_path,
                {
                    "order": cell["order"],
                    "cell_id": cell["cell_id"],
                    "verdict": outcome.verdict,
                    "failure_kind": outcome.failure_kind,
                    "exit_code": outcome.exit_code,
                    "problems": outcome.problems,
                    "stops": outcome.stops,
                    "usage": outcome.usage,
                    "cost_usd": outcome.cost_usd,
                    "cumulative_cost_usd": total_cost,
                    "cumulative_tokens": total_tokens,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )

            if digest_file(authorization_file) != authorization_sha:
                stop_reason, stopped_at_order = STOP_AUTHORIZATION_CHANGED, cell["order"]
                break
            if outcome.stops:
                stop_reason, stopped_at_order = outcome.stops[0], cell["order"]
                break
            if index < len(config["cells"]):
                if total_cost >= float(config["cost_stop_usd"]):
                    stop_reason, stopped_at_order = STOP_COST_REACHED, cell["order"]
                    break
                if total_tokens >= int(config["token_budget_total"]):
                    stop_reason, stopped_at_order = STOP_TOKENS_REACHED, cell["order"]
                    break
    finally:
        lock.unlink(missing_ok=True)

    started_cells = len(outcomes)
    counts = {
        "cells_started": started_cells,
        "cells_observed": sum(1 for item in outcomes if item.verdict != VERDICT_INVALID),
        "cells_passed": sum(1 for item in outcomes if item.verdict == VERDICT_PASSED),
        "cells_failed": sum(1 for item in outcomes if item.verdict == VERDICT_FAILED),
        "cells_invalid_or_unknown": sum(
            1 for item in outcomes if item.verdict == VERDICT_INVALID
        ),
        "cells_not_run": len(config["cells"]) - started_cells,
    }
    report = {
        "executor_version": EXECUTOR_VERSION,
        "executor_digest": executor_digest,
        "executor_commit": head,
        "config_digest": config_digest,
        "authorization_sha256": authorization_sha,
        "authorization_digest": authorization_digest,
        "stop_policy": config["stop_policy"],
        "freeze_version": config["freeze_version"],
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "evidence_root": str(evidence_root),
        "runs_root": str(runs_root),
        "cells_planned": len(config["cells"]),
        **counts,
        "stop_reason": stop_reason,
        "stopped_at_order": stopped_at_order,
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "cost_stop_usd": config["cost_stop_usd"],
        "token_budget_total": config["token_budget_total"],
        "pricing_basis": "frozen estimate.json price list, not a provider invoice",
        "cells": [item.to_dict() for item in outcomes],
        "dispatch_log": str(dispatch_path),
        "ledger_log": str(ledger_path),
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
    parser.add_argument("--no-verify-pr", action="store_true")
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
            verify_pr=not args.no_verify_pr,
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
                "cells_passed": report["cells_passed"],
                "cells_failed": report["cells_failed"],
                "cells_invalid_or_unknown": report["cells_invalid_or_unknown"],
                "cells_not_run": report["cells_not_run"],
                "total_cost_usd": report["total_cost_usd"],
                "report": report["report_path"],
            }
        )
    )
    return 0 if report["stop_reason"] is None else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
