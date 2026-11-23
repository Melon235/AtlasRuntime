"""Real OpenSandbox smoke test: create, execute, check isolation, destroy.

This module creates one temporary container through the same adapter and the same frozen resource
settings the benchmark uses. It never builds a goal, never calls a provider model, and never touches
a calibration marker or evidence root.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import network, provision

SMOKE_ID = "SANDBOX.SMOKE.stage-b-e2e"
SMOKE_VERSION = "stage-b-sandbox-smoke/1.1"

REQUIRED_PERMISSIONS = (
    "allow_container_create",
    "allow_container_destroy",
)
FORBIDDEN_PERMISSIONS = (
    "deepseek_requests_allowed",
    "agent_runner_allowed",
    "calibration_allowed",
    "benchmark_cells_allowed",
)

#: (name, command, cwd, required). ``ls /workspace`` is informational: a bare sandbox has no
#: workspace yet; GoalSandbox provisions /workspace/project before a real run.
COMMANDS = (
    ("pwd", "pwd", "/", True),
    ("python", "python3 --version", "/", True),
    ("identity", "id", "/", True),
    ("workspace", "ls -la /workspace", "/", False),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def authorization_gate(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """A dedicated approval is required: creating a container is not covered by the task text."""

    problems: list[str] = []
    if not record:
        return {"ready": False, "problems": ["no_smoke_authorization"], "digest": None}
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
    if int(record.get("max_sandboxes") or 0) != 1:
        problems.append("max_sandboxes must be 1")
    from .admission import canonical_digest

    return {"ready": not problems, "problems": problems, "digest": canonical_digest(record)}


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


@dataclass
class SmokeRequest:
    evidence_dir: Path
    authorization: Mapping[str, Any] | None = None
    endpoint: str | None = None
    #: Test seam. The default is the real adapter.
    provider_factory: Callable[[Any], Any] | None = None
    #: Test seam for the health probe.
    health_probe: Callable[[], dict[str, Any]] | None = None
    #: Test seam for the isolation probe.
    isolation_probe: Callable[[Any, Any], Awaitable[str]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _provider_options(endpoint: str) -> Any:
    from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions

    sandbox = provision.sandbox_config()
    return OpenSandboxOptions(
        endpoint=endpoint,
        cpu=sandbox.cpu,
        memory=sandbox.memory,
        use_server_proxy=sandbox.use_server_proxy,
    )


def _spec() -> Any:
    from anycode.identity import ExecutionContext
    from anycode.sandbox.models import SandboxSpec

    sandbox = provision.sandbox_config()
    return SandboxSpec(
        run_id=SMOKE_ID,
        task_id="sandbox-smoke",
        correlation_id=f"{SMOKE_ID}:create",
        context=ExecutionContext(
            principal="system:sandbox-smoke",
            tenant_scope="atlas-smoke",
            classification="internal",
            attributes={"run_id": SMOKE_ID},
        ),
        image=sandbox.image,
        network=sandbox.network,
        persistent=False,
        labels={"atlas_smoke": "1", "stage": "B-E2E"},
    )


async def run_smoke(request: SmokeRequest) -> dict[str, Any]:
    """One create, bounded commands, one isolation probe, one destroy. Never a retry."""

    evidence: dict[str, Any] = {
        "smoke_id": SMOKE_ID,
        "smoke_version": SMOKE_VERSION,
        "started_at": _now(),
        "model_requests": 0,
        "calibration_attempts": 0,
        "benchmark_cells": 0,
    }
    gate = authorization_gate(request.authorization)
    evidence["authorization"] = {
        "ready": gate["ready"],
        "problems": gate["problems"],
        "digest": gate["digest"],
    }
    if not gate["ready"]:
        evidence.update(status="BLOCKED", verdict="SANDBOX_SMOKE_BLOCKED", finished_at=_now())
        _write(request.evidence_dir / "smoke-result.json", evidence)
        return evidence

    sandbox = provision.sandbox_config()
    endpoint = request.endpoint or network.sandbox_endpoint()[0]
    evidence["environment"] = {
        "endpoint": endpoint,
        "endpoint_source": "argument" if request.endpoint else "env:OPEN_SANDBOX_ENDPOINT",
        "bypass": network.apply_proxy_bypass(endpoint)["bypass"],
        "proxy_env": {
            name: bool(os.environ.get(name))
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        },
    }
    evidence["create_request"] = {
        "provider": sandbox.provider,
        "endpoint": endpoint,
        "image": sandbox.image,
        "cpu": sandbox.cpu,
        "memory": sandbox.memory,
        "network": sandbox.network,
        "workspace_root": sandbox.workspace_root,
        "sandbox_id": SMOKE_ID,
    }

    health = (
        request.health_probe() if request.health_probe else network.probe_runtime_health(endpoint)
    )
    evidence["health"] = health
    if not health.get("ok"):
        evidence.update(status="FAILED", verdict="SANDBOX_SMOKE_FAILED", finished_at=_now())
        _write(request.evidence_dir / "smoke-result.json", evidence)
        return evidence

    options = _provider_options(endpoint)
    evidence["provider_options"] = {
        "endpoint": options.endpoint,
        "cpu": options.cpu,
        "memory": options.memory,
        "use_server_proxy": options.use_server_proxy,
    }
    provider = (
        request.provider_factory(options) if request.provider_factory else _real_provider(options)
    )

    handle = None
    created_ok = False
    create_state = "NOT_ATTEMPTED"
    started = _now()
    try:
        created = await provider.create(_spec())
        evidence["create"] = {
            "ok": bool(created.ok),
            "sandbox_id": getattr(getattr(created, "handle", None), "id", None),
            "error": getattr(created, "error", None),
            "started_at": started,
            "finished_at": _now(),
        }
        created_ok = bool(created.ok) and getattr(created, "handle", None) is not None
        handle = getattr(created, "handle", None)
        # ``ok`` without a handle is a provider contract violation, not a rejected create: the
        # remote outcome is unconfirmed, exactly like a raised exception.
        create_state = "CREATED" if created_ok else ("UNKNOWN" if created.ok else "REJECTED")
        evidence["create"]["state"] = create_state
    except Exception as error:  # noqa: BLE001 - a failed create is evidence, not a crash
        # The provider raised without returning a handle. The remote side effect is unconfirmed:
        # a container that was created but never reported must not be assumed absent.
        create_state = "UNKNOWN"
        evidence["create"] = {
            "ok": False,
            "sandbox_id": None,
            "state": create_state,
            "error": f"{type(error).__name__}: {error}",
            "started_at": started,
            "finished_at": _now(),
        }

    # Every step after a usable handle is guarded: a broken command, file transfer or probe is
    # evidence about the smoke, never a reason to skip the destroy below.
    evidence["step_failures"] = []
    if created_ok and handle is not None:
        evidence["commands"] = await _guarded(
            evidence,
            "commands",
            lambda: _run_commands(provider, handle, request.evidence_dir),
            lambda _detail: [],
        )
        evidence["file_round_trip"] = await _guarded(
            evidence,
            "file_round_trip",
            lambda: _file_round_trip(provider, handle),
            lambda detail: {
                "path": "/tmp/atlas-smoke-probe.txt",
                "write_ok": False,
                "read_ok": False,
                "readback_matches": False,
                "error": detail,
            },
        )
        probe = request.isolation_probe or _default_isolation_probe
        evidence["isolation"] = await _guarded(
            evidence,
            "isolation",
            lambda: probe(provider, handle),
            lambda detail: f"NOT_RUN:{detail}",
        )

    try:
        evidence["cleanup"] = await _destroy(provider, handle, create_state=create_state)
    except Exception as error:  # noqa: BLE001 - cleanup must never replace the recorded evidence
        detail = f"cleanup: {type(error).__name__}: {error}"
        evidence["step_failures"].append({"step": "cleanup", "detail": detail})
        evidence["cleanup"] = {
            "attempted": handle is not None,
            "destroyed": False,
            "residual": None,
            "state": "UNKNOWN",
            "error": detail,
            "detail": "cleanup raised before a result was recorded; residual state is UNKNOWN",
        }

    # A cleanup that left a container behind (or could not be confirmed) is a failure of the smoke.
    # It is appended last, so it can never become the primary failure of an earlier step.
    cleanup = evidence["cleanup"]
    if cleanup.get("attempted") and cleanup.get("state") in {"RESIDUAL", "UNKNOWN"}:
        evidence["step_failures"].append(
            {
                "step": "cleanup",
                "detail": f"cleanup: {cleanup.get('error') or cleanup.get('detail')}",
            }
        )

    # The first failure in execution order is the primary one; nothing recorded later replaces it.
    failures = evidence["step_failures"]
    evidence["primary_failure"] = failures[0]["detail"] if failures else None
    evidence["secondary_failures"] = [item["detail"] for item in failures[1:]]
    evidence["finished_at"] = _now()
    evidence["verdict"] = _verdict(evidence)
    evidence["status"] = "PASSED" if evidence["verdict"] == "SANDBOX_SMOKE_PASS" else "FAILED"
    _write(request.evidence_dir / "smoke-result.json", evidence)
    return evidence


async def _guarded(
    evidence: dict[str, Any],
    step: str,
    factory: Callable[[], Awaitable[Any]],
    on_error: Callable[[str], Any],
) -> Any:
    """Run one evidence step. A failure is recorded and shaped like a negative result."""

    try:
        return await factory()
    except Exception as error:  # noqa: BLE001 - a broken step is evidence, not a crash
        detail = f"{step}: {type(error).__name__}: {error}"
        evidence["step_failures"].append({"step": step, "detail": detail})
        return on_error(detail)


def _real_provider(options: Any) -> Any:
    from atlasos.sandbox.opensandbox_provider import OpenSandboxProvider

    return OpenSandboxProvider(options)


async def _default_isolation_probe(provider: Any, handle: Any) -> str:
    from atlasos.sandbox.validation import probe_network_none

    return await probe_network_none(provider, handle)


async def _run_commands(provider: Any, handle: Any, evidence_dir: Path) -> list[dict[str, Any]]:
    from anycode.sandbox.models import SandboxCommand

    results: list[dict[str, Any]] = []
    for name, command, cwd, required in COMMANDS:
        result = await provider.execute(
            handle, SandboxCommand(command=command, cwd=cwd, timeout_seconds=30)
        )
        results.append(
            {
                "name": name,
                "command": command,
                "required": required,
                # Transport success and process success are different facts; keep both.
                "ok": bool(result.ok),
                "exit_ok": bool(result.ok) and result.exit_code == 0,
                "exit_code": result.exit_code,
                "stdout": (result.stdout or "")[-2000:],
                "stderr": (result.stderr or "")[-1000:],
            }
        )
    _write(evidence_dir / "commands.json", {"commands": results})
    return results


async def _file_round_trip(provider: Any, handle: Any) -> dict[str, Any]:
    path = "/tmp/atlas-smoke-probe.txt"
    payload = b"atlas-sandbox-smoke\n"
    written = await provider.write_file(handle, path, payload)
    read = await provider.read_file(handle, path)
    return {
        "path": path,
        "write_ok": bool(written.ok),
        "read_ok": bool(read.ok),
        "readback_matches": bool(read.ok) and (read.data or b"") == payload,
    }


async def _destroy(provider: Any, handle: Any, *, create_state: str = "CREATED") -> dict[str, Any]:
    """Destroy the container and classify what is actually known about the remote state.

    ``residual`` is three-valued on purpose. ``False`` means "no container answered and the destroy
    itself was confirmed"; ``None`` means the remote state is unconfirmed. A failed destroy is never
    corrected by an unreachable handle, because a transport failure and an absent container look
    identical from here.
    """

    if handle is None:
        unconfirmed = create_state == "UNKNOWN"
        return {
            "attempted": False,
            "destroyed": False,
            "residual": None if unconfirmed else False,
            "state": "UNKNOWN" if unconfirmed else "NOT_APPLICABLE",
            "error": None,
            "detail": (
                "create raised without returning a handle; the remote container state is UNKNOWN. "
                "Check the OpenSandbox server for a container labelled "
                f"atlas_run_id={SMOKE_ID!r} before assuming it is absent."
                if unconfirmed
                else "no handle was created; the create was rejected, nothing to destroy"
            ),
        }
    result = None
    error: str | None = None
    try:
        result = await provider.destroy(handle)
    except Exception as failure:  # noqa: BLE001 - cleanup failure is recorded, never hidden
        error = f"{type(failure).__name__}: {failure}"
    destroyed = bool(getattr(result, "ok", False))
    residual, probe_detail = await _residual_probe(provider, handle)
    if not destroyed and residual is False:
        # An unreachable handle after a failed destroy does not prove the container is gone.
        residual = None
    if destroyed and residual is None:
        # The destroy was answered explicitly; the probe merely corroborates it.
        residual = False
        probe_detail += " (destroy confirmed; probe inconclusive)"
    if residual is True:
        state = "RESIDUAL"
    elif residual is False and destroyed:
        state = "DESTROYED"
    else:
        state = "UNKNOWN"
    return {
        "attempted": True,
        "destroyed": destroyed,
        "residual": residual,
        "state": state,
        "probe": probe_detail,
        "error": error,
        "detail": {
            "RESIDUAL": "the sandbox still answers after destroy; a container may be left behind",
            "DESTROYED": "handle unreachable after a confirmed destroy",
            "UNKNOWN": "the remote state could not be confirmed; verify manually",
        }[state],
    }


async def _residual_probe(provider: Any, handle: Any) -> tuple[bool | None, str]:
    """Return ``(reachable, detail)``. ``None`` means the probe itself did not answer."""

    try:
        probe = await provider.execute(handle, _probe_command())
    except Exception as error:  # noqa: BLE001 - an unanswered probe is not proof of absence
        return None, f"probe raised {type(error).__name__}: {error}"
    reachable = bool(getattr(probe, "ok", False))
    return reachable, ("probe reachable" if reachable else "probe refused by the provider")


def _probe_command() -> Any:
    from anycode.sandbox.models import SandboxCommand

    return SandboxCommand(command="true", cwd="/", timeout_seconds=10)


def _verdict(evidence: dict[str, Any]) -> str:
    create = evidence.get("create") or {}
    commands = evidence.get("commands") or []
    file_round_trip = evidence.get("file_round_trip") or {}
    cleanup = evidence.get("cleanup") or {}
    resources = evidence.get("create_request") or {}
    checks = {
        "health": (evidence.get("health") or {}).get("code") == network.SANDBOX_HEALTH_READY,
        "create": bool(create.get("ok")),
        "resources": resources.get("cpu") == "1"
        and resources.get("memory") == "1Gi"
        and resources.get("image") == provision.sandbox_config().image
        and resources.get("network") == "none",
        "commands": bool(commands)
        and all(item.get("exit_ok") for item in commands if item.get("required", True)),
        "files": bool(file_round_trip.get("write_ok"))
        and bool(file_round_trip.get("read_ok"))
        and bool(file_round_trip.get("readback_matches")),
        "isolation": evidence.get("isolation") == "PASS",
        "cleanup": bool(cleanup.get("destroyed")) and cleanup.get("residual") is False,
        "no_model": evidence.get("model_requests") == 0,
        "no_calibration": evidence.get("calibration_attempts") == 0,
    }
    evidence["checks"] = checks
    # Name what is unconfirmed instead of letting a False quietly stand in for "unknown".
    unknown: list[str] = []
    if create.get("state") == "UNKNOWN":
        unknown.append("create_outcome")
    if cleanup.get("residual") is None and cleanup.get("state") == "UNKNOWN":
        unknown.append("cleanup_residual")
    evidence["unconfirmed"] = unknown
    return "SANDBOX_SMOKE_PASS" if all(checks.values()) else "SANDBOX_SMOKE_FAILED"


__all__ = [
    "SMOKE_ID",
    "SMOKE_VERSION",
    "SmokeRequest",
    "authorization_gate",
    "run_smoke",
]
