"""Stage B-E2F: the goal-sandbox provisioning gate.

The E2E smoke proved create/execute/isolate/destroy on a bare container. It never proved that the
*goal* path works: that a frozen WorkGrid fixture travels into the container, that
``GitProjectProvisioner`` clones it into ``/workspace/project`` and that the sandbox can still be
destroyed afterwards. This module is that gate, and only that gate.

What it composes — the runtime's own path, not a second one::

    GoalSandbox.start()
      -> build_sandbox_provider(options.provider)         real OpenSandbox only
      -> GitProjectProvisioner.inspect(fixture)           clean git repo, bundle, source commit
      -> ProjectSandboxManager.ensure_binding()           create, verify, upload, clone, verify
      -> probe_network_none / probe_host_separation       isolation evidence
    GoalSandbox.close()
      -> ProjectSandboxManager.close()                    destroy

What it never does: no ``run_goal``, no agent loop, no model request, no public suite, no hidden
oracle, no calibration marker, no benchmark cell. The goal string is never rendered, because the
gate does not build one.

Two durable guarantees:

* a one-shot claim is written with ``os.O_CREAT | os.O_EXCL`` (the same mechanism
  ``admission.claim_attempt`` uses) *before* the first provider call, and it is never deleted;
* the authorization is bound to the source/identity digests, so an approval for a different harness,
  runtime, protocol, plan or fixture cannot start a container.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atlasos.app.goal_sandbox import GoalSandbox, GoalSandboxError, GoalSandboxOptions
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.sandbox.models import (
    SandboxCapabilityError,
    SandboxEnvironmentBlocked,
    SandboxFileTransferError,
    SandboxIsolationError,
    SandboxProviderError,
    SandboxProvisionError,
)
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED

from .. import protocol
from .. import run_stage_b as stage_runner
from . import admission, network, provision

EXECUTION_DIR = Path(__file__).resolve().parent
SMOKE_ROOT = EXECUTION_DIR.parents[1] / "stage_b_smoke"

SMOKE_ID = "SANDBOX.SMOKE.stage-b-e2f-provisioning"
SMOKE_VERSION = "stage-b-goal-provisioning-smoke/1.0"

EVIDENCE_DIR = SMOKE_ROOT / "e2f-provisioning"
CLAIM_DIR = EVIDENCE_DIR / "claims"
AUTHORIZATION_PATH = SMOKE_ROOT / "e2f-authorization.json"

TASK_ID = f"{SMOKE_ID}.main"
WORKER_ID = f"{SMOKE_ID}.worker"

#: The gate starts at most one container. The claim below is what makes it true across processes;
#: this field is only the scope the Owner declares.
MAX_SANDBOXES = 1

REQUIRED_PERMISSIONS = ("allow_container_create", "allow_container_destroy")
FORBIDDEN_PERMISSIONS = (
    "deepseek_requests_allowed",
    "agent_runner_allowed",
    "calibration_allowed",
    "benchmark_cells_allowed",
)
#: The authorization must name the exact bytes and experiment identity it approves.
REQUIRED_DIGESTS = (
    "smoke",
    "harness_source",
    "gate_source",
    "runtime",
    "protocol",
    "plan",
    "fixture",
    "git_commit",
)

#: The workspace the runtime provisions. One constant, read from ``provision``.
WORKSPACE_ROOT = provision.WORKSPACE_ROOT

#: Post-teardown probe. It exists on a Linux image and under the deterministic fake alike; ``true``
#: does not exist in cmd.exe and would make an unreachable handle look inconclusive.
RESIDUAL_PROBE = 'python3 -c "print(1)"'

VERDICT_PASS = "PROVISIONING_SMOKE_PASS"
VERDICT_FAILED = "PROVISIONING_SMOKE_FAILED"
VERDICT_UNKNOWN = "PROVISIONING_SMOKE_UNKNOWN"
VERDICT_BLOCKED = "PROVISIONING_SMOKE_BLOCKED"

FAILURE_KINDS = (
    "none",
    "authorization",
    "identity",
    "duplicate_claim",
    "claim_write",
    "fixture",
    "sandbox_environment",
    "sandbox_capability",
    "sandbox_create",
    "sandbox_binding",
    "fixture_upload",
    "git_clone",
    "workspace",
    "isolation",
    "cleanup",
    "evidence_write",
    "unexpected",
)


class ClaimAlreadyExists(RuntimeError):
    """This authorization digest was already claimed. Never delete the claim to retry."""


class ClaimWriteError(RuntimeError):
    """The claim could not be written atomically. No provider call is allowed after this."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- 1. identity


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def gate_source_identity() -> dict[str, Any]:
    """Digest this module. The authorization names the gate it approves, not a directory."""

    path = Path(__file__).resolve()
    return {"path": str(path), "digest": _digest_bytes(path.read_bytes())}


def fixture_manifest(root: Path) -> dict[str, str]:
    """Relative path -> ``sha256`` for every file a provisioned fixture would carry."""

    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        files[relative.as_posix()] = _digest_bytes(path.read_bytes())
    return files


def fixture_identity() -> dict[str, Any]:
    """The frozen WorkGrid fixture, re-derived from bytes. Read-only; never regenerated."""

    frozen = provision.s2_frozen_identity()
    expected = dict(frozen["fixture"]["files"])
    digest = admission.digest_text(json.dumps(expected, sort_keys=True))
    return {
        "source": provision.CALIBRATION["source"],
        "files": expected,
        "file_count": len(expected),
        "digest": digest,
        "frozen_digest": frozen["fixture"]["fixture_digest"],
        "problems": provision.verify_calibration_identity(),
    }


def experiment_identity(
    document: Mapping[str, Any] | None = None,
    *,
    runtime: Mapping[str, Any] | None = None,
    fixture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the authorization must name, computed rather than asserted."""

    document = dict(document or protocol.read_protocol())
    fixture = dict(fixture or fixture_identity())
    harness = admission.source_identity()
    gate = gate_source_identity()
    runtime = dict(runtime or stage_runner.source_identity())
    plan = admission.read_json(protocol.PLAN_PATH)["plan"]["digest"]
    return {
        "smoke": SMOKE_ID,
        "harness_source": harness["digest"],
        "gate_source": gate["digest"],
        "runtime": runtime["tree_digest"],
        "protocol": admission.digest_bytes(protocol.PROTOCOL_PATH.read_bytes()),
        "protocol_version": document.get("protocol_version"),
        "plan": plan,
        "fixture": fixture["digest"],
        "fixture_source": fixture["source"],
        "git_commit": stage_runner.git_identity()["git_head"],
        "harness": harness,
        "gate": gate,
        "runtime_identity": runtime,
        "fixture_identity": fixture,
    }


def approved_digests(identity: Mapping[str, Any]) -> dict[str, str]:
    """The exact digest map an approval must carry. Printed for the Owner; never written here."""

    return {name: str(identity[name]) for name in REQUIRED_DIGESTS}


def identity_checks(
    document: Mapping[str, Any],
    *,
    runtime: Mapping[str, Any],
    fixture: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Read-only checks that the experiment identity has not moved. No provider is touched."""

    # The runtime digest lives in the E1 freeze; the protocol and plan digests live in the
    # execution freeze. Both are read, neither is rewritten.
    execution_frozen = admission.read_json(admission.FREEZE_PATH)
    e1_frozen = admission.read_json(stage_runner.FREEZE_PATH)
    checks = [
        _check(
            document.get("protocol_version") == protocol.PROTOCOL_VERSION == "1.1.0",
            "protocol_version_unchanged",
            str(document.get("protocol_version")),
        ),
        _check(
            admission.digest_bytes(protocol.PROTOCOL_PATH.read_bytes())
            == execution_frozen["protocol"]["digest"],
            "protocol_digest_unchanged",
            execution_frozen["protocol"]["digest"],
        ),
        _check(
            admission.read_json(protocol.PLAN_PATH)["plan"]["digest"]
            == execution_frozen["plan_digest"]
            == stage_runner.HISTORICAL_PLAN_DIGEST,
            "plan_digest_unchanged",
            execution_frozen["plan_digest"],
        ),
        # The runtime is frozen; the harness may move ahead of freeze 1.4, the runtime may not.
        _check(
            runtime["tree_digest"] == e1_frozen["runtime"]["tree_digest"],
            "runtime_source_unchanged",
            runtime["tree_digest"],
        ),
        _check(
            not fixture["problems"],
            "workgrid_fixture_identity_unchanged",
            "; ".join(fixture["problems"]),
        ),
        _check(
            fixture["digest"] == fixture["frozen_digest"],
            "workgrid_fixture_digest_matches_s2",
            f"{fixture['digest']} vs {fixture['frozen_digest']}",
        ),
    ]
    resources = provision.sandbox_resource_audit(document)
    checks.append(
        _check(resources["ok"], "sandbox_resource_configuration", str(resources["config"]))
    )
    isolation = provision.isolation_checks(dict(document))
    checks.append(_check(isolation["ok"], "host_oracle_isolated", "; ".join(isolation["problems"])))
    return checks


def _check(ok: bool, label: str, detail: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "label": label, "detail": detail}


# --------------------------------------------------------------------------- 2. authorization


def authorization_gate(
    record: Mapping[str, Any] | None,
    *,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """A dedicated, identity-bound, single-create approval. Nothing here creates anything."""

    problems: list[str] = []
    if not record:
        return {
            "ready": False,
            "problems": ["no_provisioning_authorization"],
            "digest": None,
            "approved_by": None,
            "approval_commit": None,
            "approved_at": None,
        }
    if record.get("status") != "APPROVED":
        problems.append(f"status={record.get('status')!r}")
    if record.get("smoke_id") != SMOKE_ID:
        problems.append(f"smoke_id={record.get('smoke_id')!r}")
    for name in ("approved_by", "approval_commit", "approved_at"):
        if not record.get(name):
            problems.append(f"missing_{name}")
    for name in REQUIRED_PERMISSIONS:
        if record.get(name) is not True:
            problems.append(f"missing_permission:{name}")
    for name in FORBIDDEN_PERMISSIONS:
        if record.get(name) is True:
            problems.append(f"forbidden_permission:{name}")
    if int(record.get("max_sandboxes") or 0) != MAX_SANDBOXES:
        problems.append(f"max_sandboxes must be {MAX_SANDBOXES}")
    if record.get("consumed") is True:
        problems.append("authorization_already_consumed")

    approved = record.get("approved_digests")
    if not isinstance(approved, Mapping):
        problems.append("missing_approved_digests")
        approved = {}
    for name, value in approved_digests(identity).items():
        if approved.get(name) != value:
            problems.append(f"digest_mismatch:{name}")
    return {
        "ready": not problems,
        "problems": problems,
        "digest": admission.canonical_digest(record),
        "approved_by": record.get("approved_by"),
        "approval_commit": record.get("approval_commit"),
        "approved_at": record.get("approved_at"),
    }


def read_authorization(path: Path | None = None) -> Mapping[str, Any] | None:
    """Read the Owner's approval record. A missing file is an absent approval, never a default."""

    target = Path(path) if path is not None else AUTHORIZATION_PATH
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 3. one-shot claim


def claim_path(claim_root: Path, authorization_digest: str) -> Path:
    """One file per authorization digest: two processes cannot share a claim path."""

    name = authorization_digest.removeprefix("sha256:")
    return Path(claim_root) / f"attempt-{name}.json"


def claim_once(
    *,
    claim_root: Path,
    authorization_digest: str,
    payload: Mapping[str, Any],
) -> Path:
    """Claim the attempt atomically. Never deletes, never reissues, never overwrites.

    ``os.O_CREAT | os.O_EXCL`` is the same durable primitive ``admission.claim_attempt`` uses: the
    kernel decides the winner, so two processes cannot both proceed.
    """

    marker = claim_path(claim_root, authorization_digest)
    body = json.dumps(dict(payload), indent=2, sort_keys=True, default=str).encode("utf-8")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ClaimWriteError(
            f"cannot create the claim directory {marker.parent}: {error}"
        ) from error
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise ClaimAlreadyExists(f"claim already exists: {marker}") from error
    except OSError as error:
        raise ClaimWriteError(f"cannot create claim {marker}: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise ClaimWriteError(f"cannot write claim {marker}: {error}") from error
    return marker


def mark_claim(marker: Path, state: str) -> None:
    """Advance the claim's state in place. It is never removed, including after a failure."""

    payload = admission.read_json(marker)
    payload["state"] = state
    payload["updated_at"] = _now()
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- 4. the request


@dataclass(frozen=True)
class ProvisioningRequest:
    authorization: Mapping[str, Any] | None = None
    #: ``None`` means the module default, resolved when the gate runs (never at import time).
    evidence_dir: Path | None = None
    claim_root: Path | None = None
    #: Resolved from the environment when omitted; never a hard-coded address.
    endpoint: str | None = None
    #: Scratch root for the fixture repository. A fresh directory is allocated when omitted.
    scratch_root: Path | None = None
    #: Test seam for the health probe. The live default goes through the runtime adapter.
    health_probe: Callable[[], Mapping[str, Any]] | None = None
    #: Test seam for the fixture. The default is the frozen WorkGrid materialiser.
    fixture_factory: Callable[[Path], Path] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- 5. the gate


def _atlas_config() -> AtlasConfig:
    return AtlasConfig(
        tools=ToolsConfig(mode="progressive"),
        sandbox=provision.sandbox_config(),
    )


def execution_context() -> Any:
    """The same trusted identity shape a goal run hands to ``GoalSandbox.start``."""

    from anycode.identity.context import ExecutionContext

    return ExecutionContext(
        principal="system:goal-provisioning-smoke",
        tenant_scope="atlas-smoke",
        classification="internal",
        attributes={
            "run_id": SMOKE_ID,
            "task_id": TASK_ID,
            "worker_id": WORKER_ID,
            "atlas_role": "main",
            "delegation_depth": 0,
            "delegation_root": SMOKE_ID,
            "stage": "B-E2F",
        },
    )


def _quote(script: str, *args: str) -> str:
    """A ``python3 -c`` command that survives both bash and cmd.exe without shell quoting games."""

    if '"' in script:
        raise ValueError("the inline script must not contain a double quote")
    return f'python3 -c "{script}"' + "".join(f" {item}" for item in args)


def workspace_commands(expected_files: tuple[str, ...]) -> tuple[tuple[str, str, bool], ...]:
    """Host-owned, read-only checks. ``pwd`` is required: it must be the declared workspace root."""

    presence = (
        "import os,sys;"
        "missing=[p for p in sys.argv[1:] if not os.path.isfile(p)];"
        "print(('MISSING:'+','.join(missing)) if missing else 'ALL_PRESENT')"
    )
    markers = repr([marker.lower() for marker in provision.LEAK_MARKERS])
    leak = (
        "import os;"
        f"markers=[m.encode() for m in {markers}];"
        "paths=[os.path.join(d,f) for d,_,fs in os.walk('.') for f in fs];"
        "hits=[p for p in paths if '.git' not in p.split(os.sep) "
        "and any(m in open(p,'rb').read().lower() for m in markers)];"
        "print(('LEAK:'+','.join(sorted(hits))) if hits else 'CLEAN')"
    )
    return (
        ("pwd", "pwd", True),
        ("git_dir", "git rev-parse --git-dir", True),
        ("head", "git rev-parse HEAD", True),
        ("status", "git status --porcelain", True),
        ("files", _quote(presence, *expected_files), True),
        ("leaks", _quote(leak), True),
    )


async def _workspace_checks(
    sandbox: GoalSandbox, expected_files: tuple[str, ...]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the fixed checks with the real workspace as the command cwd."""

    results: list[dict[str, Any]] = []
    for name, command, required in workspace_commands(expected_files):
        try:
            outcome = await sandbox.run_public_command(command, timeout_seconds=60)
        except Exception as error:  # noqa: BLE001 - a broken check is evidence, not a crash
            results.append(
                {
                    "name": name,
                    "command": command,
                    "required": required,
                    "ok": False,
                    "exit_code": None,
                    "exit_ok": False,
                    "status": "ERROR",
                    "output": f"{type(error).__name__}: {error}",
                }
            )
            continue
        exit_code = outcome.get("exit_code")
        results.append(
            {
                "name": name,
                "command": command,
                "required": required,
                # The runtime's own status string is recorded but never trusted for the verdict:
                # a transport success with an unobserved exit status is not a pass.
                "status": outcome.get("status"),
                "ok": bool(outcome.get("ok")),
                "exit_code": exit_code,
                "exit_ok": bool(outcome.get("ok")) and exit_code == 0,
                "output": (outcome.get("output") or "")[-2000:],
                "output_digest": outcome.get("output_digest"),
                "elapsed_ms": outcome.get("elapsed_ms"),
            }
        )

    by_name = {item["name"]: item for item in results}

    def output(name: str) -> str:
        return str(by_name.get(name, {}).get("output") or "")

    head = output("head").strip().splitlines()[0].strip() if output("head").strip() else ""
    status_output = output("status").strip()
    files_output = output("files").strip()
    leaks_output = output("leaks").strip()
    summary = {
        "workspace_root": WORKSPACE_ROOT,
        "cwd": output("pwd").strip(),
        "git_dir": output("git_dir").strip(),
        "head": head,
        "clean": status_output == "",
        "files_present": files_output.endswith("ALL_PRESENT"),
        "leak_scan": "CLEAN" if leaks_output.endswith("CLEAN") else leaks_output,
        "missing": files_output[len("MISSING:") :] if "MISSING:" in files_output else "",
    }
    return results, summary


def _workspace_verdict(summary: Mapping[str, Any], *, expected_commit: str) -> dict[str, Any]:
    """Turn the raw check output into named facts. Each one is True, False or None."""

    return {
        "cwd_is_declared_root": summary.get("cwd") == WORKSPACE_ROOT,
        "git_repository": bool(summary.get("git_dir")),
        "source_commit": summary.get("head") == expected_commit if expected_commit else None,
        "clean_tree": bool(summary.get("clean")),
        "required_files": bool(summary.get("files_present")),
        "no_oracle_leak": summary.get("leak_scan") == "CLEAN",
    }


def _three_state_all(values: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return ``(all_true, unconfirmed_names)``. ``None`` is never treated as a pass."""

    unconfirmed = [name for name, value in values.items() if value is None]
    return (all(value is True for value in values.values()), unconfirmed)


def classify_failure(error: BaseException) -> str:
    """Name the failure by the layer that failed. Never reported as a coding-goal failure."""

    if isinstance(error, SandboxIsolationError):
        return "isolation"
    if isinstance(error, SandboxEnvironmentBlocked):
        return "sandbox_environment"
    if isinstance(error, SandboxCapabilityError):
        return "sandbox_capability"
    if isinstance(error, SandboxFileTransferError):
        return "fixture_upload"
    if isinstance(error, SandboxProvisionError):
        text = str(error).lower()
        if "clone" in text:
            return "git_clone"
        if "upload" in text or "bundle" in text:
            return "fixture_upload"
        return "sandbox_binding"
    if isinstance(error, SandboxProviderError):
        return "sandbox_create"
    if isinstance(error, GoalSandboxError):
        return "sandbox_binding"
    return "unexpected"


def _write_evidence(path: Path, payload: dict[str, Any]) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    except OSError as error:
        return f"{type(error).__name__}: {error}"
    return None


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def _provision_fixture(factory: Callable[[Path], Path] | None, scratch: Path) -> dict[str, Any]:
    """Materialise a fresh, clean git repository from the frozen WorkGrid fixture."""

    materialize = factory or provision.materialize_calibration_fixture
    root = materialize(scratch / "fixture")
    status = _git(root, "status", "--porcelain")
    commit = _git(root, "rev-parse", "HEAD")
    manifest = fixture_manifest(root)
    frozen = provision.s2_frozen_identity()["fixture"]
    problems: list[str] = []
    if status.strip():
        problems.append(f"fixture working tree is dirty: {status.strip()[:200]}")
    if not commit:
        problems.append("fixture has no commit")
    if manifest != dict(frozen["files"]):
        missing = sorted(set(frozen["files"]) - set(manifest))
        extra = sorted(set(manifest) - set(frozen["files"]))
        drifted = sorted(
            name
            for name, digest in manifest.items()
            if name in frozen["files"] and digest != frozen["files"][name]
        )
        problems.append(
            f"fixture bytes differ from the frozen identity: missing={missing} extra={extra} "
            f"drifted={drifted}"
        )
    return {
        "root": str(root),
        "source": provision.CALIBRATION["source"],
        "source_commit": commit,
        "clean": not status.strip(),
        "files": len(manifest),
        "digest": admission.digest_text(json.dumps(manifest, sort_keys=True)),
        "problems": problems,
    }


async def run_provisioning_smoke(request: ProvisioningRequest) -> dict[str, Any]:
    """One authorization, one claim, at most one real container, always a destroy attempt."""

    evidence_dir = Path(request.evidence_dir or EVIDENCE_DIR)
    claim_root = Path(request.claim_root or CLAIM_DIR)
    identity = experiment_identity()
    evidence: dict[str, Any] = {
        "smoke_id": SMOKE_ID,
        "smoke_version": SMOKE_VERSION,
        "started_at": _now(),
        "model_requests": 0,
        "calibration_attempts": 0,
        "benchmark_cells": 0,
        "not_run": [
            "goal_run",
            "agent_loop",
            "public_suite",
            "hidden_oracle",
            "model_request",
            "calibration_attempt",
            "benchmark_cell",
            "workspace_export",
        ],
        "metadata": dict(request.metadata),
        "identity": {
            "smoke": identity["smoke"],
            "git": stage_runner.git_identity(),
            "harness_source": identity["harness"],
            "gate_source": identity["gate"],
            "runtime": {
                "tree_digest": identity["runtime"],
                "file_count": identity["runtime_identity"]["file_count"],
            },
            "protocol": {
                "version": identity["protocol_version"],
                "digest": identity["protocol"],
            },
            "plan": identity["plan"],
            "fixture": identity["fixture_identity"],
        },
    }

    gate = authorization_gate(request.authorization, identity=identity)
    evidence["authorization"] = {
        "ready": gate["ready"],
        "problems": gate["problems"],
        "digest": gate["digest"],
        "approved_by": gate["approved_by"],
        "approval_commit": gate["approval_commit"],
        "approved_at": gate["approved_at"],
    }
    if not gate["ready"]:
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_BLOCKED,
            failure_kind="authorization",
            detail="no usable provisioning authorization; no provider was contacted",
        )

    checks = identity_checks(
        protocol.read_protocol(),
        runtime=identity["runtime_identity"],
        fixture=identity["fixture_identity"],
    )
    evidence["gate_checks"] = checks
    problems = [f"{item['label']}: {item['detail']}" for item in checks if not item["ok"]]
    if problems:
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_BLOCKED,
            failure_kind="identity",
            detail="experiment identity moved: " + "; ".join(problems),
        )

    # -- the claim is durable before the first provider call --------------------------------
    assert gate["digest"] is not None
    try:
        marker = claim_once(
            claim_root=claim_root,
            authorization_digest=str(gate["digest"]),
            payload={
                "smoke_id": SMOKE_ID,
                "smoke_version": SMOKE_VERSION,
                "state": "CLAIMED",
                "claimed_at": _now(),
                "authorization_digest": gate["digest"],
                "identity": approved_digests(identity),
                "retries_allowed": False,
                "max_sandboxes": MAX_SANDBOXES,
            },
        )
    except ClaimAlreadyExists as error:
        evidence["claim"] = {"created": False, "path": None, "error": str(error)}
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_BLOCKED,
            failure_kind="duplicate_claim",
            detail=f"{error}; a failed attempt is never retried by deleting its claim",
        )
    except ClaimWriteError as error:
        evidence["claim"] = {"created": False, "path": None, "error": str(error)}
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_BLOCKED,
            failure_kind="claim_write",
            detail=f"{error}; no provider call is allowed without a durable claim",
        )
    evidence["claim"] = {
        "created": True,
        "path": str(marker),
        "digest": _digest_bytes(marker.read_bytes()),
        "state": "CLAIMED",
    }

    # The run directory holds the binding store, the ownership lock and the Host-only sentinel.
    # If it cannot be created the attempt is over before any provider call is made.
    run_dir = evidence_dir / "sandbox"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _safe_mark(marker, "FAILED")
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_FAILED,
            failure_kind="evidence_write",
            detail=f"the evidence root is not writable: {type(error).__name__}: {error}",
        )

    endpoint, endpoint_source = (
        (request.endpoint, "argument") if request.endpoint else network.sandbox_endpoint()
    )
    bypass = network.apply_proxy_bypass(endpoint)
    evidence["environment"] = {
        "endpoint": endpoint,
        "endpoint_source": "argument" if request.endpoint else endpoint_source,
        "bypass": bypass["bypass"],
        "added": bypass["added"],
        "proxy_env": {
            name: bool(os.environ.get(name))
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        },
    }

    scratch = (
        Path(request.scratch_root)
        if request.scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="atlas-e2f-provisioning-"))
    )
    scratch.mkdir(parents=True, exist_ok=True)
    evidence["scratch"] = str(scratch)

    try:
        fixture = _provision_fixture(request.fixture_factory, scratch)
    except Exception as error:  # noqa: BLE001 - a broken fixture is evidence, not a crash
        evidence["fixture"] = {"materialized": False, "error": f"{type(error).__name__}: {error}"}
        _safe_mark(marker, "FAILED")
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_FAILED,
            failure_kind="fixture",
            detail=f"{type(error).__name__}: {error}",
        )
    evidence["fixture"] = {"materialized": True, **fixture}
    if fixture["problems"]:
        _safe_mark(marker, "FAILED")
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_FAILED,
            failure_kind="fixture",
            detail="; ".join(fixture["problems"]),
        )

    sandbox_settings = provision.sandbox_config()
    evidence["create_request"] = {
        "provider": sandbox_settings.provider,
        "endpoint": sandbox_settings.endpoint,
        "image": sandbox_settings.image,
        "cpu": sandbox_settings.cpu,
        "memory": sandbox_settings.memory,
        "network": sandbox_settings.network,
        "workspace_root": sandbox_settings.workspace_root,
        "require_real": True,
        "smoke_id": SMOKE_ID,
        "run_id": SMOKE_ID,
        "task_id": TASK_ID,
    }

    health = (
        request.health_probe() if request.health_probe else network.probe_runtime_health(endpoint)
    )
    evidence["health"] = dict(health)
    if not health.get("ok"):
        _safe_mark(marker, "BLOCKED")
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_BLOCKED,
            failure_kind="sandbox_environment",
            detail=f"OpenSandbox health is not ready: {health.get('code')} {health.get('detail')}",
        )

    evidence["workspace"] = {"root": WORKSPACE_ROOT, "checks": [], "verdict": {}}
    evidence["isolation"] = {"network_probe": "NOT_RUN", "host_separation": {"status": "NOT_RUN"}}
    evidence["cleanup"] = {"attempted": False, "destroyed": False, "residual": None}
    sandbox: GoalSandbox | None = None
    started = False
    primary: dict[str, Any] | None = None
    secondary: list[dict[str, Any]] = []

    try:
        try:
            options = GoalSandboxOptions(
                provider=sandbox_settings.provider,
                source_repo=Path(fixture["root"]),
                image=sandbox_settings.image,
                workspace_root=WORKSPACE_ROOT,
                retain_on_failure=sandbox_settings.retain_on_failure,
                require_real=True,
                tool_error_mode=TOOL_ERROR_MODE_CORRECTED,
            )
            sandbox = GoalSandbox(
                options,
                config=_atlas_config(),
                run_id=SMOKE_ID,
                run_dir=run_dir,
            )
            await sandbox.start(task_id=TASK_ID, context=execution_context(), worker_id=WORKER_ID)
            started = True
        except Exception as error:  # noqa: BLE001 - classified, never re-raised as a crash
            primary = {
                "kind": classify_failure(error),
                "detail": f"{type(error).__name__}: {error}",
            }

        if sandbox is not None:
            evidence["sandbox"] = {
                "started": started,
                **sandbox.evidence.as_dict(),
            }
            evidence["isolation"] = {
                "network_probe": sandbox.evidence.network_probe,
                "host_separation": dict(sandbox.evidence.host_separation),
            }

        if started and sandbox is not None:
            expected_files = tuple(sorted(identity["fixture_identity"]["files"]))
            try:
                results, summary = await _workspace_checks(sandbox, expected_files)
                evidence["workspace"] = {
                    "root": WORKSPACE_ROOT,
                    "checks": results,
                    "summary": summary,
                    "verdict": _workspace_verdict(
                        summary, expected_commit=str(fixture["source_commit"])
                    ),
                }
            except Exception as error:  # noqa: BLE001
                primary = primary or {
                    "kind": "workspace",
                    "detail": f"{type(error).__name__}: {error}",
                }
                evidence["workspace"]["error"] = primary["detail"]
    finally:
        if sandbox is None:
            # The goal sandbox could not even be constructed: nothing was created, and no handle
            # exists to destroy. The state is still stated rather than left implicit.
            evidence["cleanup"] = {
                "attempted": False,
                "destroyed": False,
                "residual": None,
                "state": "NOT_APPLICABLE",
                "probe": "no sandbox object was constructed",
                "error": None,
                "detail": "the goal sandbox was never constructed; no container was requested",
            }
        else:
            cleanup = await _close(sandbox, started=started, failed=primary is not None)
            evidence["cleanup"] = cleanup
            if cleanup.get("error"):
                secondary.append({"kind": "cleanup", "detail": str(cleanup["error"])})
            elif cleanup.get("state") == "RESIDUAL":
                secondary.append(
                    {
                        "kind": "cleanup",
                        "detail": "the sandbox still answered after close; a container may remain",
                    }
                )
            evidence["sandbox_final"] = {"started": started, **sandbox.evidence.as_dict()}

    checks_map = _gate_checks(evidence, started=started)
    evidence["checks"] = checks_map
    ok, unconfirmed = _three_state_all(checks_map)
    evidence["unconfirmed"] = unconfirmed
    evidence["primary_failure"] = primary
    evidence["secondary_failures"] = secondary
    _safe_mark(marker, "COMPLETED" if ok else "FAILED")

    if ok:
        return _finish(evidence, evidence_dir, verdict=VERDICT_PASS, failure_kind="none", detail="")
    if primary is None and not unconfirmed:
        primary = {"kind": "workspace", "detail": "a required provisioning check failed"}
        evidence["primary_failure"] = primary
    if primary is None:
        return _finish(
            evidence,
            evidence_dir,
            verdict=VERDICT_UNKNOWN,
            failure_kind="unexpected",
            detail="no failure was raised, but these facts are unconfirmed: "
            + ", ".join(unconfirmed),
        )
    return _finish(
        evidence,
        evidence_dir,
        verdict=VERDICT_FAILED,
        failure_kind=str(primary["kind"]),
        detail=str(primary["detail"]),
    )


async def _close(sandbox: GoalSandbox, *, started: bool, failed: bool = False) -> dict[str, Any]:
    """Use the runtime's own teardown. A cleanup failure never replaces the primary failure."""

    error: str | None = None
    destroyed = False
    try:
        destroyed = bool(await sandbox.close(failed=failed or not started))
    except Exception as failure:  # noqa: BLE001 - recorded, never raised
        error = f"{type(failure).__name__}: {failure}"

    # A probe is only possible while the runtime still exposes a binding. A provisioning failure
    # destroys internally without handing the handle back, so nothing can be confirmed from here.
    probe_available = sandbox.ready and sandbox.evidence.created
    residual: bool | None = None
    probe = "NOT_RUN"
    if probe_available:
        try:
            outcome = await sandbox.run_public_command(RESIDUAL_PROBE, timeout_seconds=15)
        except Exception as failure:  # noqa: BLE001 - an unanswered probe is not proof
            probe = f"probe raised {type(failure).__name__}: {failure}"
        else:
            reachable = bool(outcome.get("ok")) and outcome.get("exit_code") == 0
            residual = bool(reachable)
            probe = "reachable after close" if reachable else "unreachable after close"
        if destroyed and residual is None:
            # The destroy was answered explicitly; the probe merely corroborates it.
            residual = False
            probe += " (destroy confirmed; probe inconclusive)"
    else:
        residual = None
        probe = (
            "no binding survived start(); a provisioning failure destroys internally without "
            "exposing a handle"
        )
    state = (
        "RESIDUAL"
        if residual is True
        else "DESTROYED"
        if destroyed and residual is False
        else "UNKNOWN"
    )
    detail = {
        "RESIDUAL": "the sandbox still answered after close; verify and destroy it manually",
        "DESTROYED": "the runtime destroyed the sandbox and the handle is unreachable",
        "UNKNOWN": (
            "the remote container state is unconfirmed. Check the OpenSandbox server for a "
            f"container labelled atlas_run_id={SMOKE_ID!r} before assuming it is absent."
        ),
    }[state]
    return {
        "attempted": True,
        "destroyed": destroyed,
        "residual": residual,
        "state": state,
        "probe": probe,
        "error": error,
        "detail": detail,
    }


def _gate_checks(evidence: Mapping[str, Any], *, started: bool) -> dict[str, Any]:
    """The verdict inputs. ``None`` means unconfirmed, and an unconfirmed fact never passes."""

    sandbox = evidence.get("sandbox") or {}
    workspace = (evidence.get("workspace") or {}).get("verdict") or {}
    isolation = evidence.get("isolation") or {}
    separation = isolation.get("host_separation") or {}
    cleanup = evidence.get("cleanup") or {}
    create = evidence.get("create_request") or {}
    checks: dict[str, Any] = {
        "authorization": bool((evidence.get("authorization") or {}).get("ready")),
        "claim": bool((evidence.get("claim") or {}).get("created")),
        "fixture": bool((evidence.get("fixture") or {}).get("materialized"))
        and not (evidence.get("fixture") or {}).get("problems"),
        "health": bool((evidence.get("health") or {}).get("ok")),
        "provider_is_real": sandbox.get("provider_is_real") if started else False,
        "created": bool(sandbox.get("created")) if started else False,
        "binding_ready": (sandbox.get("binding_state") == "READY") if started else False,
        "workspace_cwd": workspace.get("cwd_is_declared_root") if started else False,
        "workspace_git": workspace.get("git_repository") if started else False,
        "workspace_commit": workspace.get("source_commit") if started else False,
        "workspace_clean": workspace.get("clean_tree") if started else False,
        "workspace_files": workspace.get("required_files") if started else False,
        "no_oracle_leak": workspace.get("no_oracle_leak") if started else False,
        "network_none": _probe_value(isolation.get("network_probe")),
        "host_separation": _separation_value(separation),
        "cleanup": _cleanup_value(cleanup),
        "resources": create.get("cpu") == "1"
        and create.get("memory") == "1Gi"
        and create.get("network") == "none",
        "no_model": evidence.get("model_requests") == 0,
        "no_calibration": evidence.get("calibration_attempts") == 0,
        "no_benchmark_cells": evidence.get("benchmark_cells") == 0,
    }
    return checks


def _cleanup_value(cleanup: Mapping[str, Any]) -> Any:
    """``True`` only for a confirmed destroy. ``None`` means the remote state is unconfirmed."""

    if cleanup.get("residual") is True:
        return False
    if cleanup.get("destroyed") and cleanup.get("residual") is False:
        return True
    return None


def _probe_value(value: Any) -> Any:
    if value == "PASS":
        return True
    if value in {"FAIL", "NOT_RUN", "NOT_APPLICABLE_FAKE_PROVIDER"}:
        return False
    return None


def _separation_value(separation: Mapping[str, Any]) -> Any:
    if separation.get("status") == "NOT_APPLICABLE_FAKE_PROVIDER":
        return False
    if not separation:
        return None
    confirmed = (
        separation.get("host_only_visible") is False
        and separation.get("sandbox_only_visible_on_host") is False
        and separation.get("sandbox_only_read_back") is True
    )
    return confirmed


def _safe_mark(marker: Path, state: str) -> None:
    # The claim itself is durable; the state field is a convenience, never a precondition.
    with contextlib.suppress(Exception):
        mark_claim(marker, state)


def _finish(
    evidence: dict[str, Any],
    evidence_dir: Path,
    *,
    verdict: str,
    failure_kind: str,
    detail: str,
) -> dict[str, Any]:
    evidence["verdict"] = verdict
    evidence["failure_kind"] = failure_kind
    evidence["status"] = {
        VERDICT_PASS: "PASSED",
        VERDICT_FAILED: "FAILED",
        VERDICT_UNKNOWN: "UNKNOWN",
        VERDICT_BLOCKED: "BLOCKED",
    }[verdict]
    if detail:
        evidence["detail"] = detail
    evidence["finished_at"] = _now()
    path = Path(evidence_dir) / "provisioning-result.json"
    error = _write_evidence(path, evidence)
    if error is not None:
        evidence["evidence_error"] = error
        evidence["status"] = "EVIDENCE_NOT_WRITTEN"
    return evidence


def main(argv: list[str] | None = None) -> int:
    """CLI: one attempt, one verdict. It needs an explicit authorization file to do anything."""

    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Stage B-E2F goal provisioning gate")
    parser.add_argument("--authorization", type=Path, default=AUTHORIZATION_PATH)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--claim-root", type=Path, default=CLAIM_DIR)
    parser.add_argument("--sandbox-endpoint", metavar="HOST:PORT")
    parser.add_argument("--scratch-root", type=Path)
    args = parser.parse_args(argv)
    if args.sandbox_endpoint:
        os.environ["OPEN_SANDBOX_ENDPOINT"] = args.sandbox_endpoint
    authorization = read_authorization(args.authorization)
    result = asyncio.run(
        run_provisioning_smoke(
            ProvisioningRequest(
                authorization=authorization,
                evidence_dir=args.evidence_dir,
                claim_root=args.claim_root,
                endpoint=args.sandbox_endpoint or os.environ.get("OPEN_SANDBOX_ENDPOINT"),
                scratch_root=args.scratch_root,
            )
        )
    )
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "verdict": result.get("verdict"),
                "failure_kind": result.get("failure_kind"),
                "claim": result.get("claim"),
                "checks": result.get("checks"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.get("verdict") == VERDICT_PASS else 1


if __name__ == "__main__":  # pragma: no cover - the module is driven by the harness CLI
    sys.exit(main())


__all__ = [
    "AUTHORIZATION_PATH",
    "CLAIM_DIR",
    "EVIDENCE_DIR",
    "MAX_SANDBOXES",
    "SMOKE_ID",
    "SMOKE_VERSION",
    "ClaimAlreadyExists",
    "ClaimWriteError",
    "ProvisioningRequest",
    "authorization_gate",
    "claim_once",
    "claim_path",
    "classify_failure",
    "execution_context",
    "experiment_identity",
    "fixture_identity",
    "fixture_manifest",
    "identity_checks",
    "mark_claim",
    "read_authorization",
    "run_provisioning_smoke",
    "workspace_commands",
]
