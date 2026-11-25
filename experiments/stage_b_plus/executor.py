"""Stage B+ executable entry point: authority, one-shot claim, dispatch, post-cell gate.

This is the smallest thing that can actually run a B+ cell, and it is written so that the *offline*
run and the *live* run go through identical code. What differs is one injected callable:

``cell_runner``
    receives a cell and a context, returns a :class:`CellRun` (the provider session, the run
    directory, the host-oracle verdict and the wall clock). ``live_cell_runner`` builds the real
    DeepSeek adapter and the real OpenSandbox; ``host_cell_runner`` runs the same ``run_goal`` in
    host mode with a scripted provider. Nothing else in this module knows which one it got.

Everything else is unconditional: the plan is resolved from the contract, the authority record is
verified field by field, the dispatch claim is created with ``O_CREAT | O_EXCL`` before any cell
starts, every cell gets its own attempt marker, and every cell ends with :func:`audit_cell` whose
``stops`` decide whether the next cell may start.

What this module refuses to do
------------------------------
* It never invents a missing number. A field the provider did not report is ``None`` and stops the
  matrix as ``UNKNOWN_USAGE``; it is never written as 0.
* It never continues after an infrastructure failure, including on the last cell. A matrix that ends
  on a broken sandbox is reported as stopped, not as complete.
* It never reopens a claim. There is one dispatch per plan and one attempt per cell, and both are
  files created exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2 import preflight as closure2_preflight
from experiments.stage_b_plus import authority as authority_module
from experiments.stage_b_plus import contract, cost

EXECUTOR_VERSION = "bplus-executor-1.0"

#: Files whose bytes *are* the executor identity. A dispatch is bound to their digests, so editing
#: any of them invalidates an existing signature rather than silently running different code.
EXECUTOR_FILES: tuple[str, ...] = (
    "experiments/stage_b_plus/executor.py",
    "experiments/stage_b_plus/authority.py",
    "experiments/stage_b_plus/contract.py",
    "experiments/stage_b_plus/cost.py",
)

# -- stop reasons -----------------------------------------------------------------------------

STOP_NONE = "NONE"
STOP_CLAIM_CONSUMED = "CLAIM_CONSUMED"
STOP_CLAIM_INVALID = "CLAIM_INVALID"
STOP_LEGACY_EVIDENCE = "LEGACY_EVIDENCE_PRESENT"
STOP_PROVIDER_FAILURE = "PROVIDER_FAILURE"
STOP_SANDBOX_FAILURE = "SANDBOX_FAILURE"
STOP_SANDBOX_CLEANUP = "SANDBOX_CLEANUP_FAILURE"
STOP_HOST_ISOLATION = "HOST_ISOLATION_VIOLATION"
STOP_ORACLE_ENVIRONMENT = "ORACLE_ENVIRONMENT_FAILURE"
STOP_UNKNOWN_USAGE = "UNKNOWN_OR_ILLEGAL_USAGE"
STOP_MISSING_EVIDENCE = "MISSING_OR_CORRUPT_EVIDENCE"
STOP_UNKNOWN_FAILURE = "UNKNOWN_FAILURE_KIND"
STOP_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
STOP_EXECUTION_EXCEPTION = "EXECUTION_EXCEPTION"
STOP_LEDGER_INTEGRITY = "LEDGER_INTEGRITY_FAILURE"
STOP_COST_REACHED = "COST_STOP_REACHED"
STOP_TOKEN_REACHED = "TOKEN_STOP_REACHED"
STOP_NOT_AUTHORIZED = "NOT_AUTHORIZED"

#: A failure the runtime measured. These continue: they are the experiment's subject matter, not an
#: infrastructure defect. Anything else stops, including an unclassified exception.
NORMAL_FAILURE_KINDS: tuple[str, ...] = (
    "oracle_failed",
    "public_tests_failed",
    "budget_exhaustion",
    "main_max_turns",
    "acceptance_failed",
    "child_failed",
)

REQUIRED_USAGE_FIELDS: tuple[str, ...] = (
    "main_cumulative_input_tokens",
    "main_completion_tokens",
    "child_total_prompt_tokens",
    "child_completion_tokens",
    "system_total_tokens",
)

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def now() -> str:
    return datetime.now(UTC).strftime(_ISO)


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# -- identity ---------------------------------------------------------------------------------


def executor_identity(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else contract.REPO_ROOT
    files: dict[str, str] = {}
    for relative in EXECUTOR_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"executor identity file is missing: {relative}")
        files[relative] = _digest_bytes(path.read_bytes())
    return {
        "executor_version": EXECUTOR_VERSION,
        "executor_digest": authority_module.digest_of(files),
        "files": files,
    }


def git_head(repo_root: Path | None = None) -> str:
    root = str(repo_root or contract.REPO_ROOT)
    completed = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot resolve git HEAD in {root}: {completed.stderr.strip()[:200]}")
    return completed.stdout.strip()


# -- the cell ---------------------------------------------------------------------------------


@dataclass
class CellRun:
    """One executed cell, as the runner saw it. ``session`` is ``None`` iff the run raised."""

    session: dict[str, Any] | None
    run_dir: Path | None
    fixture_root: Path | None
    oracle: dict[str, Any] | None
    duration_s: float = 0.0
    error: str | None = None
    error_kind: str | None = None
    notes: tuple[str, ...] = ()


CellRunner = Callable[[dict[str, Any], Mapping[str, Any]], Awaitable[CellRun]]


# -- usage and cost ---------------------------------------------------------------------------


def _token_problem(name: str, value: Any) -> str | None:
    if isinstance(value, bool):
        return f"{name} is a bool, not a token count"
    if not isinstance(value, int):
        return f"{name} is {value!r}, not an integer"
    if value < 0:
        return f"{name} is negative ({value})"
    return None


def validate_usage(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every counter a matrix needs, checked for type and sign. Unknown is not zero."""

    problems: list[str] = []
    values: dict[str, Any] = {}
    if not isinstance(metrics, Mapping):
        return {"ok": False, "problems": ["the run carries no metrics block"], "values": {}}
    for field_name in REQUIRED_USAGE_FIELDS:
        value = metrics.get(field_name)
        values[field_name] = value
        problem = _token_problem(field_name, value)
        if value is None:
            problems.append(f"{field_name} was not reported")
        elif problem is not None:
            problems.append(problem)
    if not problems:
        total = (
            int(values["main_cumulative_input_tokens"])
            + int(values["main_completion_tokens"])
            + int(values["child_total_prompt_tokens"])
            + int(values["child_completion_tokens"])
        )
        if total != int(values["system_total_tokens"]):
            problems.append(
                f"system_total_tokens is {values['system_total_tokens']}, the parts sum to {total}"
            )
    return {"ok": not problems, "problems": problems, "values": values}


def cell_cost(metrics: Mapping[str, Any], prices: Mapping[str, Any] | None = None) -> float:
    """Frozen-price estimate. Child prompt tokens are priced as misses and said so."""

    price = dict(prices or cost.stage_b_estimate.pricing())
    hit = int(metrics.get("main_cache_hit_tokens") or 0)
    miss = int(metrics.get("main_cache_miss_tokens") or 0)
    main_input = int(metrics.get("main_cumulative_input_tokens") or 0)
    if not hit and not miss:
        miss = main_input  # no split reported: price the whole input as a miss
    child_prompt = int(metrics.get("child_total_prompt_tokens") or 0)
    completion = int(metrics.get("main_completion_tokens") or 0) + int(
        metrics.get("child_completion_tokens") or 0
    )
    per_million = 1_000_000.0
    return (
        hit * float(price["cache_hit_input_per_million"])
        + (miss + child_prompt) * float(price["cache_miss_input_per_million"])
        + completion * float(price["output_per_million"])
    ) / per_million


# -- the post-cell gate -----------------------------------------------------------------------


def _sandbox_problems(session: Mapping[str, Any]) -> list[str]:
    sandbox = session.get("sandbox")
    if not isinstance(sandbox, Mapping):
        return ["the run carries no sandbox evidence (host mode cannot be dispatched)"]
    problems: list[str] = []
    if sandbox.get("created") is not True:
        problems.append("the sandbox was never created")
    if sandbox.get("retained"):
        problems.append("the sandbox was retained instead of destroyed")
    if sandbox.get("destroyed") is not True:
        problems.append("the sandbox was not destroyed")
    if sandbox.get("error"):
        problems.append(f"the sandbox reported {str(sandbox.get('error'))[:120]}")
    for key in ("network_probe", "host_separation"):
        probe = sandbox.get(key)
        if isinstance(probe, Mapping):
            verdict = probe.get("ok", probe.get("passed"))
            if verdict is False:
                problems.append(f"{key} failed")
    return problems


def audit_cell(
    *,
    cell: Mapping[str, Any],
    run: CellRun,
    plan: Mapping[str, Any],
    usage_before: Mapping[str, int],
    cost_before: float,
    prices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one finished cell and decide whether the matrix may continue.

    Pure: it reads the run and the plan, writes nothing. The dispatch loop is what persists its
    output, so the same function is used by the tests and by the live path.
    """

    stops: list[str] = []
    checks: list[dict[str, Any]] = []
    notes: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    arm = contract.ARMS_BY_ID[cell["arm_id"]]
    check(
        "identity_matches_plan",
        cell.get("plan_id") == plan["plan_id"] and cell.get("scope") == plan["scope"],
        f"cell says {cell.get('plan_id')}/{cell.get('scope')}",
    )
    if not checks[-1]["ok"]:
        stops.append(STOP_IDENTITY_MISMATCH)

    session = run.session
    if session is None:
        check("execution_returned", False, f"{run.error_kind or 'exception'}: {run.error}")
        stops.append(_exception_stop(run))
        usage = dict(usage_before)
        return {
            "cell_id": cell["cell_id"],
            "arm_id": cell["arm_id"],
            "task_id": cell["task_id"],
            "checks": checks,
            "stops": stops,
            "usage": usage,
            "cost_usd": cost_before,
            "delta_cost_usd": 0.0,
            "verdict": "NOT_OBSERVED",
            "duration_s": round(run.duration_s, 3),
            "notes": list(run.notes),
        }
    check("execution_returned", True, f"status={session.get('status')}")

    # runtime status: a measured goal failure continues, an unknown one stops
    failure = session.get("failure") or {}
    kind = str(failure.get("kind") or "") if isinstance(failure, Mapping) else ""
    status = str(session.get("status") or "")
    if status not in {"succeeded", "failed"}:
        check("runtime_status_known", False, f"status is {status!r}")
        stops.append(STOP_UNKNOWN_FAILURE)
    elif status == "failed":
        known = kind in NORMAL_FAILURE_KINDS
        check(
            "failure_is_measurable",
            known,
            f"kind={kind or 'MISSING'}",
        )
        if not known:
            stops.append(STOP_UNKNOWN_FAILURE)
    else:
        check("runtime_status_known", True, "succeeded")

    # sandbox lifecycle
    sandbox_details = _sandbox_problems(session)
    check("sandbox_lifecycle", not sandbox_details, "; ".join(sandbox_details) or "closed")
    if sandbox_details:
        if any("isolation" in item or "probe" in item for item in sandbox_details):
            stops.append(STOP_HOST_ISOLATION)
        elif any("destroy" in item or "retain" in item or "reported" in item for item in
                 sandbox_details):
            stops.append(STOP_SANDBOX_CLEANUP)
        else:
            stops.append(STOP_SANDBOX_FAILURE)

    # disclosure actually applied: the arm's profile and exposure must be what ran
    controls = session.get("delegation_controls") or {}
    profiles = controls.get("profiles") or {}
    exposure = (controls.get("exposure") or {}).get("level")
    profile = profiles.get(arm.profile_id) or {}
    check(
        "worker_profile_matches_arm",
        bool(profile) and profile.get("profile_id", arm.profile_id) == arm.profile_id,
        f"offered={list(profiles)} expected={arm.profile_id}",
    )
    check(
        "exposure_matches_arm",
        exposure == arm.exposure,
        f"applied={exposure!r} expected={arm.exposure!r}",
    )
    if not checks[-1]["ok"] or not checks[-2]["ok"]:
        stops.append(STOP_IDENTITY_MISMATCH)
    check(
        "worker_turns_effective",
        profile.get("effective_max_turns") == int(contract.BUDGET["worker_max_turns"]),
        f"effective={profile.get('effective_max_turns')} "
        f"expected={contract.BUDGET['worker_max_turns']}",
    )

    # usage
    usage_report = validate_usage(session.get("metrics"))
    check("usage_is_complete", usage_report["ok"], "; ".join(usage_report["problems"]) or "ok")
    if not usage_report["ok"]:
        stops.append(STOP_UNKNOWN_USAGE)
    metrics = session.get("metrics") if isinstance(session.get("metrics"), Mapping) else {}
    delta_cost = cell_cost(metrics, prices) if usage_report["ok"] else 0.0

    # oracle: a Host artefact, not the Main's own claim
    oracle = run.oracle or {}
    oracle_state = str(oracle.get("status") or "MISSING")
    check(
        "oracle_executed",
        oracle_state in {"PASS", "FAIL"},
        f"status={oracle_state} kind={oracle.get('kind')}",
    )
    if oracle_state not in {"PASS", "FAIL"}:
        stops.append(
            STOP_ORACLE_ENVIRONMENT
            if oracle.get("kind")
            in {"COLLECT_ERROR", "TIMEOUT", "NO_TESTS_COLLECTED", "NO_TARGETS"}
            else STOP_MISSING_EVIDENCE
        )

    # evidence: the run directory must contain a parseable session.json
    run_dir = run.run_dir
    if run_dir is None or not (Path(run_dir) / "session.json").is_file():
        check("run_evidence_present", False, "session.json is missing")
        stops.append(STOP_MISSING_EVIDENCE)
    else:
        try:
            payload = json.loads((Path(run_dir) / "session.json").read_text(encoding="utf-8"))
            parsed = isinstance(payload, Mapping) and bool(payload)
        except (OSError, json.JSONDecodeError) as error:
            parsed = False
            notes.append(f"session.json is not parseable: {error}")
        check("run_evidence_present", parsed, f"session.json parsed={parsed}")

    # Unknown is not zero. A counter the provider did not report makes the aggregate unknown too,
    # which is exactly what stops the matrix rather than quietly understating it.
    usage = dict(usage_before)
    metrics_in = session.get("metrics") or {}
    for key in REQUIRED_USAGE_FIELDS:
        value = metrics_in.get(key)
        if value is None:
            usage[key] = None
        elif isinstance(value, int) and not isinstance(value, bool):
            usage[key] = int(usage.get(key) or 0) + value

    total_cost = cost_before + delta_cost
    if stops:
        verdict = "STOPPED"
    elif status == "succeeded":
        verdict = "PASS"
    else:
        # A measured goal failure is a valid observation, not a pass and not a stop.
        verdict = "MEASURED_FAILURE"
    return {
        "cell_id": cell["cell_id"],
        "arm_id": cell["arm_id"],
        "task_id": cell["task_id"],
        "profile_id": arm.profile_id,
        "exposure": arm.exposure,
        "status": status,
        "failure_kind": kind or None,
        "oracle": oracle_state,
        "checks": checks,
        "stops": stops,
        "usage": {
            key: usage.get(key) for key in REQUIRED_USAGE_FIELDS
        },
        "delta_cost_usd": round(delta_cost, 6),
        "cost_usd": round(total_cost, 6),
        "verdict": verdict,
        "duration_s": round(run.duration_s, 3),
        "notes": list(run.notes),
    }


def _exception_stop(run: CellRun) -> str:
    if run.error_kind in {"provider", "timeout"}:
        return STOP_PROVIDER_FAILURE
    if run.error_kind == "sandbox":
        return STOP_SANDBOX_FAILURE
    if run.error_kind == "oracle":
        return STOP_ORACLE_ENVIRONMENT
    return STOP_EXECUTION_EXCEPTION


# -- persistence ------------------------------------------------------------------------------


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _claim(path: Path, payload: Mapping[str, Any]) -> bool:
    """Create ``path`` exactly once. Returns False when the claim already exists."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _fresh_ledger(path: Path) -> bool:
    if not path.exists():
        return True
    return path.is_file() and path.stat().st_size == 0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# -- dispatch ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchContext(Mapping[str, Any]):
    """What a runner is allowed to know. A mapping, so a runner reads it like a dict."""

    plan: Mapping[str, Any]
    authority: Mapping[str, Any]
    runs_root: Path
    evidence_root: Path
    head: str
    prices: Mapping[str, Any] = field(default_factory=dict)

    def _fields(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "authority": self.authority,
            "runs_root": self.runs_root,
            "evidence_root": self.evidence_root,
            "head": self.head,
            "prices": self.prices,
        }

    def __getitem__(self, key: str) -> Any:
        return self._fields()[key]

    def __iter__(self) -> Any:
        return iter(self._fields())

    def __len__(self) -> int:
        return len(self._fields())


async def dispatch(
    plan_id: str,
    *,
    runs_root: Path,
    evidence_root: Path,
    authority_path: Path | None = None,
    head: str | None = None,
    cell_runner: CellRunner | None = None,
    repo_root: Path | None = None,
    prices: Mapping[str, Any] | None = None,
    clock: Callable[[], str] = now,
) -> dict[str, Any]:
    """Run one plan, or refuse and say why. The only entry point that may execute a cell."""

    plan = contract.scope_plan(plan_id)
    evidence = Path(evidence_root)
    # A dispatch is a filesystem operation from start to finish; the event loop is only used for the
    # runner itself, so creating the evidence root here is deliberate and not offloaded.
    await asyncio.to_thread(evidence.mkdir, parents=True, exist_ok=True)
    document = contract.protocol_document()
    identity = executor_identity(repo_root)
    actual_head = head or git_head(repo_root)
    record = _read_json(Path(authority_path)) if authority_path else None

    claim_path = evidence / "dispatch.claim"
    authority_verdict = authority_module.verify(
        record,
        plan=plan,
        head=actual_head,
        executor=identity,
        protocol_digest_value=authority_module.protocol_digest(document),
        repo_root=repo_root,
        claim_consumed=claim_path.exists(),
    )
    if not authority_verdict["ready"]:
        report = _refused_report(plan, identity, actual_head, authority_verdict, clock)
        (evidence / "dispatch-refused.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        return report

    ledger_path = evidence / "ledger.jsonl"
    if not _fresh_ledger(ledger_path):
        verdict = dict(authority_verdict, ready=False, problems=["the ledger is not fresh"])
        return _refused_report(plan, identity, actual_head, verdict, clock)

    claimed = _claim(
        claim_path,
        {
            "plan_id": plan["plan_id"],
            "plan_digest": plan["digest"],
            "execution_head": actual_head,
            "executor_digest": identity["executor_digest"],
            "authority_digest": authority_verdict["authority_digest"],
            "claimed_at": clock(),
        },
    )
    if not claimed:
        verdict = dict(authority_verdict, ready=False, problems=["the dispatch claim is consumed"])
        return _refused_report(plan, identity, actual_head, verdict, clock)

    runner = cell_runner or live_cell_runner(repo_root)
    context = DispatchContext(
        plan=plan,
        authority=record or {},
        runs_root=Path(runs_root),
        evidence_root=evidence,
        head=actual_head,
        prices=dict(prices or cost.stage_b_estimate.pricing()),
    )
    started = clock()
    usage: dict[str, int] = dict.fromkeys(REQUIRED_USAGE_FIELDS, 0)
    total_cost = 0.0
    stop_reason = STOP_NONE
    stopped_at: int | None = None
    cells: list[dict[str, Any]] = []

    cost_stop = float((record or {}).get("cost_stop_usd") or 0.0)
    per_cell_stop = float((record or {}).get("per_cell_cost_stop_usd") or 0.0)
    token_stop = int((record or {}).get("token_stop") or 0)

    for cell in plan["cells"]:
        cell_root = evidence / "cells" / cell["cell_id"]
        attempt_path = cell_root / "attempt.json"
        if not _claim(
            attempt_path,
            {
                "cell_id": cell["cell_id"],
                "plan_id": plan["plan_id"],
                "state": "CLAIMED",
                "claimed_at": clock(),
                "attempt_limit": 1,
            },
        ):
            stop_reason, stopped_at = STOP_CLAIM_CONSUMED, int(cell["order"])
            break

        _append_jsonl(
            evidence / "dispatch.jsonl",
            {
                "order": cell["order"],
                "cell_id": cell["cell_id"],
                "cost_before_usd": round(total_cost, 6),
                "tokens_before": dict(usage),
                "dispatched_at": clock(),
                "execution_head": actual_head,
                "plan_digest": plan["digest"],
            },
        )

        try:
            run = await runner(cell, context)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - an exception is a stop, not a shrug
            run = CellRun(
                session=None,
                run_dir=None,
                fixture_root=None,
                oracle=None,
                error=f"{type(error).__name__}: {error}"[:500],
                error_kind="sandbox"
                if "sandbox" in type(error).__name__.lower()
                else "provider"
                if "provider" in type(error).__name__.lower()
                else "exception",
            )

        outcome = audit_cell(
            cell=cell,
            run=run,
            plan=plan,
            usage_before=usage,
            cost_before=total_cost,
            prices=context.prices,
        )
        if outcome["delta_cost_usd"] and outcome["delta_cost_usd"] > per_cell_stop > 0:
            outcome["stops"].append(STOP_COST_REACHED)
            outcome["verdict"] = "STOPPED"
        usage = dict(outcome["usage"])
        total_cost = float(outcome["cost_usd"])
        cells.append(outcome)

        _append_jsonl(
            ledger_path,
            {
                "order": cell["order"],
                "cell_id": cell["cell_id"],
                "verdict": outcome["verdict"],
                "stops": outcome["stops"],
                "cost_usd": outcome["cost_usd"],
                "cumulative_tokens": usage.get("system_total_tokens"),
                "recorded_at": clock(),
            },
        )
        _write_analysis(cell_root, plan, cell, outcome, run)

        if outcome["stops"]:
            stop_reason, stopped_at = outcome["stops"][0], int(cell["order"])
            break
        if cost_stop > 0 and total_cost >= cost_stop:
            stop_reason, stopped_at = STOP_COST_REACHED, int(cell["order"])
            break
        if token_stop > 0 and int(usage.get("system_total_tokens") or 0) >= token_stop:
            stop_reason, stopped_at = STOP_TOKEN_REACHED, int(cell["order"])
            break

    finished = clock()
    report = {
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "executor_files": identity["files"],
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "plan_digest": plan["digest"],
        "execution_head": actual_head,
        "authority_digest": authority_verdict["authority_digest"],
        "authorized_cells": list(authority_verdict["cell_ids"]),
        "is_benchmark_sample": bool(plan["is_benchmark_sample"]),
        "started_at": started,
        "finished_at": finished,
        "cells_planned": len(plan["cells"]),
        "cells_started": len(cells),
        "cells_passed": sum(1 for cell in cells if cell["verdict"] == "PASS"),
        "cells_measured_failure": sum(
            1 for cell in cells if cell["verdict"] == "MEASURED_FAILURE"
        ),
        "cells_stopped": sum(1 for cell in cells if cell["verdict"] == "STOPPED"),
        "cells_not_run": len(plan["cells"]) - len(cells),
        "stop_reason": stop_reason,
        "stopped_at_order": stopped_at,
        "complete": stop_reason == STOP_NONE and len(cells) == len(plan["cells"]),
        "total_tokens": None
        if any(usage.get(key) is None for key in REQUIRED_USAGE_FIELDS)
        else usage.get("system_total_tokens"),
        "total_cost_usd": round(total_cost, 6),
        "cost_stop_usd": cost_stop,
        "token_stop": token_stop,
        "pricing": dict(context.prices),
        "cells": cells,
        "aggregate_includes_sample": bool(plan["is_benchmark_sample"]),
        "retry": "FORBIDDEN",
        "resume": "FORBIDDEN",
    }
    (evidence / "matrix-report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return report


def _refused_report(
    plan: Mapping[str, Any],
    identity: Mapping[str, Any],
    head: str,
    verdict: Mapping[str, Any],
    clock: Callable[[], str],
) -> dict[str, Any]:
    return {
        "stage": contract.STAGE_ID,
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "plan_digest": plan["digest"],
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "execution_head": head,
        "started_at": clock(),
        "finished_at": clock(),
        "cells_planned": len(plan["cells"]),
        "cells_started": 0,
        "cells_not_run": len(plan["cells"]),
        "stop_reason": STOP_NOT_AUTHORIZED,
        "complete": False,
        "problems": list(verdict.get("problems") or []),
        "refused": True,
        "retry": "FORBIDDEN",
    }


def _write_analysis(
    cell_root: Path, plan: Mapping[str, Any], cell: Mapping[str, Any], outcome: Mapping[str, Any],
    run: CellRun
) -> None:
    cell_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "cell_id": cell["cell_id"],
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "arm_id": cell["arm_id"],
        "task_id": cell["task_id"],
        "repetition": cell.get("repetition"),
        "is_benchmark_sample": plan["is_benchmark_sample"],
        "outcome": dict(outcome),
        "oracle": run.oracle,
        "duration_s": round(run.duration_s, 3),
        "notes": list(run.notes),
    }
    (cell_root / "analysis.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# -- runners ----------------------------------------------------------------------------------


def _write_files(root: Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _fixture_root(root: Path, task_id: str) -> Path:
    """The Main's workspace: the seed, and nothing else.

    The reference implementation and the hidden suite are deliberately absent, so there is nothing
    for a run to find by listing the tree.
    """

    task = contract.TASKS_BY_ID[task_id]
    target = root / "fixtures" / task_id
    if target.exists():
        return target
    _write_files(target, dict(task.seed))
    return target


def _host_oracle(
    workspace: Path, task_id: str, dest: Path, *, timeout: int = 300
) -> dict[str, Any]:
    """The independent oracle: hidden tests added to a copy of the finished workspace.

    The copy is the point. The hidden suite never touches the directory the agents worked in, so a
    Main cannot read it even by accident, and a failure cannot be attributed to a mutated workspace.
    The runner itself is Closure-2's, reused unchanged, so a timeout or a collection error is
    classified the same way the frozen selftest classifies it.
    """

    task = contract.TASKS_BY_ID[task_id]
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(workspace, dest)
    _write_files(dest, dict(task.oracle_tests))
    targets = sorted(task.oracle_tests)
    result = closure2_preflight._run_pytest(dest, targets, timeout=timeout)
    kind = str(result.get("kind") or "")
    status = "PASS" if result.get("passed") else "FAIL"
    if kind in {"COLLECT_ERROR", "TIMEOUT", "NO_TESTS_COLLECTED", "NO_TARGETS"}:
        status = "ERROR"
    return {
        "status": status,
        "kind": kind,
        "exit_code": result.get("exit_code"),
        "targets": targets,
        "counts": result.get("counts"),
        "cause_class": result.get("cause_class"),
        "source": "experiments.evaluation.closure2.preflight::_run_pytest",
    }


def live_cell_runner(repo_root: Path | None = None) -> CellRunner:
    """The production runner: real DeepSeek adapter, real OpenSandbox, real public suite.

    It is not exercised by this stage — no credential and no container — and it deliberately builds
    nothing until ``run_goal`` asks, so importing it cannot create either.
    """

    del repo_root

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> CellRun:
        from atlasos.app.goal_sandbox import GoalSandboxOptions
        from atlasos.app.main_driven import GoalRunOptions, run_goal
        from atlasos.app.public_tests import PublicTestContract
        from atlasos.config import AtlasConfig, ToolsConfig

        task = contract.TASKS_BY_ID[cell["task_id"]]
        arm = contract.ARMS_BY_ID[cell["arm_id"]]
        root = Path(context["runs_root"])
        fixture = _fixture_root(root, cell["task_id"])
        run_dir = Path(context["evidence_root"]) / "cells" / cell["cell_id"] / "run"
        started = time.monotonic()
        options = GoalRunOptions(
            goal=contract.GOAL_OF[cell["task_id"]],
            runs_root=run_dir.parent,
            workspace_root=fixture,
            config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
            delegation=contract.delegation_config(),
            provider=contract.BUDGET["provider"],
            model=contract.BUDGET["model"],
            max_turns=int(contract.BUDGET["main_max_turns"]),
            token_budget=int(contract.BUDGET["token_budget"]),
            main_write_paths=tuple(task.allowed_paths),
            child_write_grants=arm.grant_for(cell["task_id"]),
            worker_profiles=(arm.profile_id,),
            worker_max_turns=int(contract.BUDGET["worker_max_turns"]),
            worker_exposure=arm.exposure,
            run_id=f"{cell['cell_id']}-run",
            main_prompt=contract.main_prompt_spec(),
            public_tests=PublicTestContract(
                test_id="bplus-public",
                command="python3 -B -m pytest -q " + " ".join(sorted(task.public_tests)),
                timeout_seconds=240,
                max_output_chars=6000,
                max_calls=int(contract.BUDGET["public_test_max_calls"]),
            ),
            acceptance=contract.acceptance_spec(task),
            sandbox=GoalSandboxOptions(
                provider=contract.BUDGET["sandbox_provider"],
                source_repo=fixture,
                image=contract.BUDGET["image"],
            ),
        )
        try:
            session = await run_goal(options)
        except BaseException as error:  # noqa: BLE001 - classified by the gate
            return CellRun(
                session=None,
                run_dir=None,
                fixture_root=fixture,
                oracle=None,
                duration_s=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}"[:500],
                error_kind="sandbox" if "Sandbox" in type(error).__name__ else "provider",
            )
        # The oracle is a subprocess, so it runs off the event loop rather than inside it.
        oracle = await asyncio.to_thread(
            _host_oracle, fixture, cell["task_id"],
            Path(context["evidence_root"]) / "cells" / cell["cell_id"] / "oracle",
        )
        return CellRun(
            session=session,
            run_dir=Path(session["run_dir"]),
            fixture_root=fixture,
            oracle=oracle,
            duration_s=time.monotonic() - started,
        )

    return run


def host_cell_runner(
    *,
    provider_factory: Callable[[], Any] | None = None,
    script: Sequence[Callable[[list[Any]], list[Any]]] | None = None,
    max_turns: int = 6,
) -> CellRunner:
    """The offline control-flow runner: real ``run_goal``, host mode, scripted provider.

    It exists so the dispatch loop can be exercised end to end without a container. It is *not* a
    live path: a cell executed this way carries no sandbox evidence and the gate says so.
    """

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> CellRun:
        from experiments.stage_b_plus import probe as probe_module

        from atlasos.app.main_driven import GoalRunOptions, run_goal
        from atlasos.config import AtlasConfig, ToolsConfig

        task = contract.TASKS_BY_ID[cell["task_id"]]
        arm = contract.ARMS_BY_ID[cell["arm_id"]]
        root = Path(context["runs_root"])
        fixture = _fixture_root(root, cell["task_id"])
        scripted = (
            provider_factory()
            if provider_factory is not None
            else probe_module.ScriptedProvider(
                main_script=list(script or ()), child_scripts={}
            )
        )
        started = time.monotonic()
        options = GoalRunOptions(
            goal=contract.GOAL_OF[cell["task_id"]],
            runs_root=root / "runs",
            workspace_root=fixture,
            config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
            delegation=contract.delegation_config(),
            provider="fake",
            model=contract.BUDGET["model"],
            max_turns=max_turns,
            main_write_paths=tuple(task.allowed_paths),
            child_write_grants=arm.grant_for(cell["task_id"]),
            worker_profiles=(arm.profile_id,),
            worker_max_turns=int(contract.BUDGET["worker_max_turns"]),
            worker_exposure=arm.exposure,
            provider_factory=lambda: scripted,
            run_id=f"{cell['cell_id']}-host-{int(started * 1000) % 100000}",
            main_prompt=contract.main_prompt_spec(),
        )
        try:
            session = await run_goal(options)
        except BaseException as error:  # noqa: BLE001
            return CellRun(
                session=None,
                run_dir=None,
                fixture_root=fixture,
                oracle=None,
                duration_s=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}"[:500],
                error_kind="exception",
            )
        oracle = await asyncio.to_thread(
            _host_oracle, fixture, cell["task_id"],
            Path(context["evidence_root"]) / "cells" / cell["cell_id"] / "oracle",
        )
        return CellRun(
            session=session,
            run_dir=Path(session["run_dir"]),
            fixture_root=fixture,
            oracle=oracle,
            duration_s=time.monotonic() - started,
            notes=("host mode: no sandbox evidence, so the gate stops this cell",),
        )

    return run


__all__ = [
    "CellRun",
    "CellRunner",
    "DispatchContext",
    "EXECUTOR_FILES",
    "EXECUTOR_VERSION",
    "NORMAL_FAILURE_KINDS",
    "audit_cell",
    "cell_cost",
    "dispatch",
    "executor_identity",
    "git_head",
    "host_cell_runner",
    "live_cell_runner",
    "now",
    "validate_usage",
]
