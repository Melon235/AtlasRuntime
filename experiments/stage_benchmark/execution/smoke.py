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
    create_error: str | None = None
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
    except Exception as error:  # noqa: BLE001 - a failed create is evidence, not a crash
        create_error = f"{type(error).__name__}: {error}"
        evidence["create"] = {
            "ok": False,
            "sandbox_id": None,
            "error": create_error,
            "started_at": started,
            "finished_at": _now(),
        }

    if created_ok and handle is not None:
        evidence["commands"] = await _run_commands(provider, handle, request.evidence_dir)
        evidence["file_round_trip"] = await _file_round_trip(provider, handle)
        probe = request.isolation_probe or _default_isolation_probe
        try:
            evidence["isolation"] = await probe(provider, handle)
        except Exception as error:  # noqa: BLE001
            evidence["isolation"] = f"ERROR:{type(error).__name__}: {error}"

    evidence["cleanup"] = await _destroy(provider, handle)
    evidence["finished_at"] = _now()
    evidence["verdict"] = _verdict(evidence)
    evidence["status"] = "PASSED" if evidence["verdict"] == "SANDBOX_SMOKE_PASS" else "FAILED"
    _write(request.evidence_dir / "smoke-result.json", evidence)
    return evidence


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


async def _destroy(provider: Any, handle: Any) -> dict[str, Any]:
    if handle is None:
        return {
            "attempted": False,
            "destroyed": False,
            "residual": False,
            "detail": "no handle was created; nothing to destroy",
        }
    result = None
    error: str | None = None
    try:
        result = await provider.destroy(handle)
    except Exception as failure:  # noqa: BLE001 - cleanup failure is recorded, never hidden
        error = f"{type(failure).__name__}: {failure}"
    destroyed = bool(getattr(result, "ok", False))
    residual = None
    try:
        probe = await provider.execute(handle, _probe_command())
        residual = bool(getattr(probe, "ok", False))
    except Exception:  # noqa: BLE001 - a dead handle is the expected post-destroy state
        residual = False
    return {
        "attempted": True,
        "destroyed": destroyed,
        "residual": residual,
        "error": error,
        "detail": "verify probe after destroy: reachable"
        if residual
        else "handle unreachable after destroy",
    }


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
    return "SANDBOX_SMOKE_PASS" if all(checks.values()) else "SANDBOX_SMOKE_FAILED"


__all__ = [
    "SMOKE_ID",
    "SMOKE_VERSION",
    "SmokeRequest",
    "authorization_gate",
    "run_smoke",
]
