"""Stage B-E2F: the goal provisioning gate, exercised offline.

Every test here uses the deterministic fake provider, a temporary fixture repository and a
temporary evidence root. None of them creates a real container, calls a model, writes a calibration
marker or consumes a benchmark cell. The live verdict stays NOT_RUN until the Owner authorises it.

The fake provider runs real subprocesses on the Host, so it exercises the same
``GitProjectProvisioner`` / ``ProjectSandboxManager`` code a real container would. The two isolation
probes are replaced: they would otherwise attempt real egress, and their results are covered by the
E2E smoke.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anycode.sandbox import SandboxCapabilities, SandboxCommandResult, SandboxFileResult
from experiments.stage_benchmark.execution import e2f_provisioning as e2f
from experiments.stage_benchmark.execution import network, provision
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.app import goal_sandbox as goal_sandbox_module
from atlasos.sandbox.validation import HostSeparationEvidence

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = "/workspace/project"


# --------------------------------------------------------------------------- fixtures & fakes


def _capabilities() -> SandboxCapabilities:
    """A fake that declares the real provider's contract, so the gate runs its real checks."""

    return SandboxCapabilities(
        provider="opensandbox",
        isolation="container",
        networking="none",
        persistent_filesystem=True,
        snapshots=False,
        command_streaming=False,
        cancellation=True,
        file_transfer=True,
        evidence=True,
    )


class GateProvider(FakeSandboxProvider):
    """The M5 fake plus the two things a provisioning gate needs to observe."""

    def __init__(self, root: Path, **kwargs: Any) -> None:
        super().__init__(root, capabilities=_capabilities(), **kwargs)
        self.pwd_output = WORKSPACE
        self.command_overrides: dict[str, Any] = {}
        #: ``FakeSandboxProvider.fail_clone`` matches the literal ``git clone``, and the runtime
        #: clones with ``git -c core.autocrlf=false clone``. This flag matches any clone command.
        self.fail_clone_commands = False
        #: Every ``build_sandbox_provider`` call, so a test can see which endpoint was bound.
        self.provider_calls: list[dict[str, Any]] = []
        #: Raise from ``create`` / ``destroy`` to script a failing or cancelled lifecycle step.
        self.create_hook: Callable[[], None] | None = None
        self.create_after_hook: Callable[[], None] | None = None
        self.destroy_hook: Callable[[], None] | None = None
        #: Counted before the hooks, so a hook cannot hide that the provider was asked.
        self.create_attempts = 0
        self.destroy_attempts = 0
        #: Raise from ``execute`` for a named check, e.g. to cancel during the workspace checks.
        self.raise_on_command: dict[str, BaseException] = {}

    async def create(self, spec: Any) -> Any:
        # counted before any hook, so a hook cannot hide that the provider was asked
        self.create_attempts += 1
        if self.create_hook is not None:
            self.create_hook()
        created = await super().create(spec)
        if self.create_after_hook is not None:
            self.create_after_hook()
        return created

    async def destroy(self, handle: Any) -> Any:
        self.destroy_attempts += 1
        if self.destroy_hook is not None:
            self.destroy_hook()
        return await super().destroy(handle)

    async def execute(self, handle: Any, command: Any) -> Any:
        raised = self.raise_on_command.get(command.command)
        if raised is not None:
            raise raised
        override = self.command_overrides.get(command.command)
        if override is not None:
            return override
        if command.command == "pwd":
            return SandboxCommandResult(ok=True, exit_code=0, stdout=f"{self.pwd_output}\n")
        if self.fail_clone_commands and "clone" in command.command:
            return SandboxCommandResult(ok=False, exit_code=1, stderr="clone failed")
        return await super().execute(handle, command)


def _healthy() -> dict[str, Any]:
    return {
        "ok": True,
        "code": network.SANDBOX_HEALTH_READY,
        "endpoint": "127.0.0.1:9",
        "status_code": 200,
        "detail": "healthy",
    }


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GateProvider:
    """Install the fake as the provider the goal sandbox would have built.

    The claim namespace is harness state, not request data, so test isolation happens here: a test
    moves the module constant, exactly as a deployment would configure it.
    """

    monkeypatch.setattr(e2f, "CLAIM_DIR", tmp_path / "claims")
    fake = GateProvider(tmp_path / "sandboxes")

    def _factory(name: str, **kwargs: Any) -> GateProvider:
        fake.provider_calls.append({"name": name, **kwargs})
        return fake

    monkeypatch.setattr(goal_sandbox_module, "build_sandbox_provider", _factory)

    async def network_pass(_provider: Any, _handle: Any) -> str:
        return "PASS"

    async def separation_clean(_provider: Any, _handle: Any, *, host_dir: Path) -> Any:
        return HostSeparationEvidence(
            host_only_visible=False,
            sandbox_only_visible_on_host=False,
            sandbox_only_read_back=True,
        )

    monkeypatch.setattr(goal_sandbox_module, "probe_network_none", network_pass)
    monkeypatch.setattr(goal_sandbox_module, "probe_host_separation", separation_clean)
    return fake


@pytest.fixture(scope="session")
def identity_digests() -> dict[str, str]:
    return e2f.approved_digests(e2f.experiment_identity())


def _authorization(digests: dict[str, str], **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "status": "APPROVED",
        "smoke_id": e2f.SMOKE_ID,
        "approved_by": "Owner (test only)",
        "approved_at": "2026-11-23T00:00:00+00:00",
        "approval_commit": "0" * 40,
        "allow_container_create": True,
        "allow_container_destroy": True,
        "deepseek_requests_allowed": False,
        "agent_runner_allowed": False,
        "calibration_allowed": False,
        "benchmark_cells_allowed": False,
        "max_sandboxes": 1,
        "approved_digests": dict(digests),
    }
    record.update(overrides)
    return record


def _request(
    tmp_path: Path,
    *,
    authorization: dict[str, Any] | None,
    **overrides: Any,
) -> e2f.ProvisioningRequest:
    """Build a request. The claim namespace is not a request field: ``provider`` moves it."""

    kwargs: dict[str, Any] = {
        "authorization": authorization,
        "evidence_dir": tmp_path / "evidence",
        "endpoint": "127.0.0.1:9",
        "scratch_root": tmp_path / "scratch",
        "health_probe": _healthy,
    }
    kwargs.update(overrides)
    return e2f.ProvisioningRequest(**kwargs)


def _run(request: e2f.ProvisioningRequest) -> dict[str, Any]:
    return asyncio.run(e2f.run_provisioning_smoke(request))


def _claims(tmp_path: Path) -> list[Path]:
    root = tmp_path / "claims"
    return sorted(root.glob("attempt-*.json")) if root.is_dir() else []


def _workspace_command_map() -> dict[str, str]:
    """The exact commands the gate will run for the frozen fixture, keyed by check name."""

    files = tuple(sorted(e2f.fixture_identity()["files"]))
    return {name: command for name, command, _ in e2f.workspace_commands(files)}


# --------------------------------------------------------------------------- 1. authorization


def test_no_authorization_blocks_before_any_provider_call(tmp_path: Path, provider: Any) -> None:
    result = _run(_request(tmp_path, authorization=None))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert result["failure_kind"] == "authorization"
    assert result["authorization"]["digest"] is None
    assert provider.create_count == 0
    assert _claims(tmp_path) == []
    assert Path(result["evidence_path"]).is_file()
    assert result["status"] == "BLOCKED"


def test_an_unapproved_record_blocks(tmp_path: Path, provider: Any, identity_digests: Any) -> None:
    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests, status="PENDING"),
        )
    )
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert any("status=" in problem for problem in result["authorization"]["problems"])
    assert provider.create_count == 0


def test_a_wrong_smoke_id_blocks(tmp_path: Path, provider: Any, identity_digests: Any) -> None:
    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests, smoke_id="SANDBOX.SMOKE.stage-b-e2e"),
        )
    )
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert any("smoke_id=" in problem for problem in result["authorization"]["problems"])
    assert provider.create_count == 0


def test_a_missing_permission_blocks(tmp_path: Path, provider: Any, identity_digests: Any) -> None:
    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests, allow_container_destroy=False),
        )
    )
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert "missing_permission:allow_container_destroy" in result["authorization"]["problems"]
    assert provider.create_count == 0


def test_a_forbidden_permission_blocks(
    tmp_path: Path, provider: Any, identity_digests: Any
) -> None:
    for name in ("deepseek_requests_allowed", "agent_runner_allowed", "calibration_allowed"):
        record = _authorization(identity_digests, **{name: True})
        result = _run(_request(tmp_path / name, authorization=record))
        assert result["verdict"] == e2f.VERDICT_BLOCKED
        assert any(f"forbidden_permission:{name}" in p for p in result["authorization"]["problems"])
    assert provider.create_count == 0


def test_a_digest_mismatch_blocks(tmp_path: Path, provider: Any, identity_digests: Any) -> None:
    tampered = dict(identity_digests)
    tampered["runtime"] = "sha256:" + "0" * 64
    result = _run(_request(tmp_path, authorization=_authorization(tampered)))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert "digest_mismatch:runtime" in result["authorization"]["problems"]
    assert provider.create_count == 0


def test_max_sandboxes_alone_is_not_a_one_shot_approval(
    tmp_path: Path, provider: Any, identity_digests: Any
) -> None:
    result = _run(
        _request(tmp_path, authorization=_authorization(identity_digests, max_sandboxes=2))
    )
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert provider.create_count == 0


def test_a_consumed_authorization_blocks(
    tmp_path: Path, provider: Any, identity_digests: Any
) -> None:
    record = _authorization(identity_digests, consumed=True)
    result = _run(_request(tmp_path, authorization=record))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert "authorization_already_consumed" in result["authorization"]["problems"]
    assert provider.create_count == 0


def test_a_moved_runtime_blocks_even_with_a_matching_approval(
    tmp_path: Path, provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval is bound to bytes: approving a moved runtime cannot unlock a container."""

    real = e2f.stage_runner.source_identity

    def moved() -> dict[str, Any]:
        identity = dict(real())
        identity["tree_digest"] = "sha256:" + "f" * 64
        return identity

    monkeypatch.setattr(e2f.stage_runner, "source_identity", moved)
    digests = e2f.approved_digests(e2f.experiment_identity())
    result = _run(_request(tmp_path, authorization=_authorization(digests)))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert result["failure_kind"] == "identity"
    assert any("runtime_source_unchanged" in check["label"] for check in result["gate_checks"])
    assert provider.create_count == 0
    assert _claims(tmp_path) == []


def test_authorization_file_is_never_created_by_the_gate(tmp_path: Path) -> None:
    """An absent approval file is an absent approval. The gate never writes one."""

    assert e2f.read_authorization(tmp_path / "absent.json") is None
    assert not (tmp_path / "absent.json").exists()


# --------------------------------------------------------------------------- 2. fixture


def test_the_fixture_materialises_clean_and_committed(tmp_path: Path) -> None:
    fixture = e2f._provision_fixture(None, tmp_path / "scratch")
    assert fixture["problems"] == []
    assert fixture["clean"] is True
    assert fixture["files"] == 21
    head = subprocess.run(
        ["git", "-C", fixture["root"], "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == fixture["source_commit"]


def test_a_dirty_fixture_is_refused_before_any_create(
    tmp_path: Path, provider: Any, identity_digests: Any
) -> None:
    def dirty(destination: Path) -> Path:
        root = provision.materialize_calibration_fixture(destination)
        (root / "workgrid" / "__init__.py").write_text("tampered = True\n", encoding="utf-8")
        return root

    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests),
            fixture_factory=dirty,
        )
    )
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "fixture"
    assert provider.create_count == 0
    # The attempt was already claimed; a failed attempt keeps its claim.
    assert len(_claims(tmp_path)) == 1


# --------------------------------------------------------------------------- 3. happy path


def test_the_gate_provisions_the_workspace_and_destroys_the_sandbox(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))

    assert result["verdict"] == e2f.VERDICT_PASS, json.dumps(result, indent=2, default=str)
    assert result["status"] == "PASSED"
    assert provider.create_count == 1
    assert provider.destroy_count == 1
    assert result["checks"]["provider_is_real"] is True
    assert result["checks"]["binding_ready"] is True
    assert result["checks"]["workspace_commit"] is True
    assert result["checks"]["workspace_clean"] is True
    assert result["checks"]["workspace_files"] is True
    assert result["checks"]["no_oracle_leak"] is True
    assert result["checks"]["network_none"] is True
    assert result["checks"]["host_separation"] is True
    assert result["checks"]["cleanup"] is True
    assert result["cleanup"]["state"] == "DESTROYED"
    assert result["cleanup"]["residual"] is False
    assert result["model_requests"] == 0
    assert result["calibration_attempts"] == 0
    assert result["benchmark_cells"] == 0


def test_the_evidence_records_the_whole_contract(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    payload = json.loads(Path(result["evidence_path"]).read_text(encoding="utf-8"))
    for key in (
        "smoke_id",
        "authorization",
        "identity",
        "create_request",
        "fixture",
        "sandbox",
        "workspace",
        "isolation",
        "cleanup",
        "model_requests",
        "calibration_attempts",
        "benchmark_cells",
        "primary_failure",
        "secondary_failures",
        "checks",
        "verdict",
        "status",
    ):
        assert key in payload, key
    assert payload["identity"]["runtime"]["tree_digest"].startswith("sha256:")
    assert payload["identity"]["protocol"]["version"] == "1.1.0"
    assert payload["identity"]["plan"].startswith("sha256:")
    assert payload["identity"]["git"]["git_head"]
    assert payload["create_request"]["cpu"] == "1"
    assert payload["create_request"]["memory"] == "1Gi"
    assert payload["create_request"]["network"] == "none"
    assert payload["create_request"]["require_real"] is True
    assert payload["fixture"]["source_commit"] == payload["workspace"]["summary"]["head"]
    assert payload["sandbox"]["sandbox_id"]
    assert payload["sandbox"]["binding_id"]
    assert payload["workspace"]["root"] == WORKSPACE
    assert payload["isolation"]["network_probe"] == "PASS"
    assert "goal_run" in payload["not_run"]


def test_the_gate_never_reaches_the_agent_loop(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    from atlasos.app import main_driven

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the provisioning gate must never call run_goal")

    original = main_driven.run_goal
    main_driven.run_goal = forbidden  # type: ignore[assignment]
    try:
        result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    finally:
        main_driven.run_goal = original  # type: ignore[assignment]
    assert result["verdict"] == e2f.VERDICT_PASS
    source = Path(e2f.__file__).read_text(encoding="utf-8")
    assert "from atlasos.app.main_driven import" not in source


def test_the_gate_never_writes_a_calibration_marker(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert list(tmp_path.rglob("attempt-1.json")) == []
    assert not any("CALIBRATION" in str(path) for path in tmp_path.rglob("*"))


# --------------------------------------------------------------------------- 4. workspace faults


def _state_override(**commands: Any) -> dict[str, Any]:
    return commands


def test_a_workspace_outside_the_declared_root_fails(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.pwd_output = "/tmp/not-the-workspace"
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["checks"]["workspace_cwd"] is False
    assert provider.destroy_count == 1


def test_a_head_that_does_not_match_the_source_commit_fails(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.command_overrides["git rev-parse HEAD"] = SandboxCommandResult(
        ok=True, exit_code=0, stdout="0" * 40 + "\n"
    )
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["checks"]["workspace_commit"] is False
    assert result["failure_kind"] == "workspace"
    assert provider.destroy_count == 1


def test_a_dirty_workspace_fails(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.command_overrides["git status --porcelain"] = SandboxCommandResult(
        ok=True, exit_code=0, stdout=" M workgrid/__init__.py\n"
    )
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["checks"]["workspace_clean"] is False


def test_a_missing_fixture_file_fails(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.command_overrides[_workspace_command_map()["files"]] = SandboxCommandResult(
        ok=True, exit_code=0, stdout="MISSING:README.md\n"
    )
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["checks"]["workspace_files"] is False
    assert result["workspace"]["summary"]["missing"] == "README.md"


def test_the_oracle_source_must_not_be_visible_in_the_workspace(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.command_overrides[_workspace_command_map()["leaks"]] = SandboxCommandResult(
        ok=True, exit_code=0, stdout="LEAK:README.md\n"
    )
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["checks"]["no_oracle_leak"] is False


def test_the_workspace_check_commands_are_fixed_and_read_only() -> None:
    commands = e2f.workspace_commands(("README.md", "workgrid/__init__.py"))
    assert [name for name, _, _ in commands] == [
        "pwd",
        "git_dir",
        "head",
        "status",
        "files",
        "leaks",
    ]
    for _name, command, required in commands:
        assert required is True
        assert command.startswith(("pwd", "git ", 'python3 -c "import os'))


# --------------------------------------------------------------------------- 5. provisioning faults


def test_an_unavailable_endpoint_blocks_before_the_fixture(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests),
            health_probe=lambda: {
                "ok": False,
                "code": network.SANDBOX_ENDPOINT_UNREACHABLE,
                "endpoint": "127.0.0.1:9",
                "status_code": None,
                "detail": "refused",
            },
        )
    )
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert result["failure_kind"] == "sandbox_environment"
    assert provider.create_count == 0
    assert "sandbox" not in result


def test_a_create_failure_is_named_and_leaves_the_state_unknown(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.fail_create = True
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "sandbox_create"
    assert provider.create_count == 0
    # No handle was ever handed back, so the manager had nothing to destroy and the remote state
    # cannot be confirmed from here.
    assert provider.destroy_count == 0
    assert result["cleanup"]["destroyed"] is False
    assert result["cleanup"]["residual"] is None
    assert result["cleanup"]["state"] == "UNKNOWN"
    assert result["cleanup"]["error"] is not None
    assert "atlas_run_id" in result["cleanup"]["detail"]


def test_an_upload_failure_is_named(
    tmp_path: Path, provider: GateProvider, identity_digests: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(handle: Any, path: str, data: bytes) -> Any:
        return SandboxFileResult(ok=False)

    monkeypatch.setattr(provider, "write_file", refused)
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "fixture_upload"
    assert provider.create_count == 1
    assert provider.destroy_count == 1
    assert result["cleanup"]["state"] == "UNKNOWN"
    assert "atlas_run_id" in result["cleanup"]["detail"]


def test_a_git_clone_failure_is_named(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.fail_clone_commands = True
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "git_clone"
    assert provider.create_count == 1
    assert provider.destroy_count == 1


def test_an_isolation_failure_stops_after_create_and_still_destroys(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def egress_reachable(_provider: Any, _handle: Any) -> str:
        return "FAIL"

    monkeypatch.setattr(goal_sandbox_module, "probe_network_none", egress_reachable)
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "isolation"
    assert provider.create_count == 1
    assert provider.destroy_count == 1
    assert result["isolation"]["network_probe"] == "FAIL"
    assert result["checks"]["cleanup"] is True


def test_an_unknown_egress_probe_is_not_a_pass(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown(_provider: Any, _handle: Any) -> str:
        return "UNKNOWN"

    monkeypatch.setattr(goal_sandbox_module, "probe_network_none", unknown)
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    # An inconclusive probe is neither a pass nor a proven failure.
    assert result["checks"]["network_none"] is None
    assert "network_none" in result["unconfirmed"]
    assert result["verdict"] == e2f.VERDICT_UNKNOWN


def test_a_destroy_failure_is_reported_as_a_residual(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.fail_destroy = True
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["cleanup"]["destroyed"] is False
    assert result["cleanup"]["residual"] is True
    assert result["cleanup"]["state"] == "RESIDUAL"
    assert result["checks"]["cleanup"] is False
    assert any("cleanup" in item["kind"] for item in result["secondary_failures"])


def test_a_destroy_exception_does_not_mask_the_primary_failure(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.raise_destroy = True
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["cleanup"]["error"] is not None
    assert "provider destroy unavailable" in result["cleanup"]["error"]
    assert result["verdict"] == e2f.VERDICT_FAILED


# --------------------------------------------------------------------------- 6. claims


def test_the_same_authorization_cannot_create_a_second_container(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    authorization = _authorization(identity_digests)
    first = _run(_request(tmp_path, authorization=authorization))
    assert first["verdict"] == e2f.VERDICT_PASS
    assert provider.create_count == 1

    second = _run(_request(tmp_path, authorization=authorization))
    assert second["verdict"] == e2f.VERDICT_BLOCKED
    assert second["failure_kind"] == "duplicate_claim"
    assert provider.create_count == 1
    claim = json.loads(_claims(tmp_path)[0].read_text(encoding="utf-8"))
    assert claim["state"] == "COMPLETED"


def test_a_failed_attempt_keeps_its_claim(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    authorization = _authorization(identity_digests)
    provider.fail_clone_commands = True
    first = _run(_request(tmp_path, authorization=authorization))
    assert first["failure_kind"] == "git_clone"
    marker = _claims(tmp_path)[0]
    assert json.loads(marker.read_text(encoding="utf-8"))["state"] == "FAILED"

    provider.fail_clone_commands = False
    second = _run(_request(tmp_path, authorization=authorization))
    assert second["verdict"] == e2f.VERDICT_BLOCKED
    assert second["failure_kind"] == "duplicate_claim"
    assert marker.is_file()
    assert provider.create_count == 1


def test_a_claim_write_failure_blocks_before_create(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    blocking = tmp_path / "claims"
    blocking.write_text("not a directory\n", encoding="utf-8")
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert result["failure_kind"] == "claim_write"
    assert provider.create_count == 0


def test_the_claim_is_atomic_across_processes(tmp_path: Path) -> None:
    """Two operating-system processes race for one authorization. Exactly one wins."""

    claim_root = tmp_path / "claims"
    digest = "sha256:" + "ab" * 32
    deadline = time.time() + 3.0
    worker = tmp_path / "claim_worker.py"
    worker.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from experiments.stage_benchmark.execution import e2f_provisioning as e2f\n"
        "deadline = float(sys.argv[3])\n"
        "while time.time() < deadline:\n"
        "    time.sleep(0.002)\n"
        "try:\n"
        "    path = e2f.claim_once(\n"
        "        claim_root=Path(sys.argv[4]),\n"
        "        authorization_digest=sys.argv[5],\n"
        "        payload={'pid': sys.argv[6]},\n"
        "    )\n"
        "except e2f.ClaimAlreadyExists as error:\n"
        "    print(json.dumps({'ok': False, 'error': str(error)}))\n"
        "    raise SystemExit(3)\n"
        "print(json.dumps({'ok': True, 'path': str(path)}))\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(worker),
        str(REPO_ROOT),
        str(REPO_ROOT / "src"),
        str(deadline),
        str(claim_root),
        digest,
        "worker",
    ]
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=120) for process in processes]
    codes = [process.returncode for process in processes]

    assert sorted(codes) == [0, 3], outputs
    winners = [json.loads(stdout) for stdout, _ in outputs if '"ok": true' in stdout]
    assert len(winners) == 1
    assert len(list(claim_root.glob("attempt-*.json"))) == 1


# --------------------------------------------------------------------------- 7. evidence faults


def test_an_unwritable_evidence_root_blocks_before_create(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    blocking = tmp_path / "evidence"
    blocking.write_text("not a directory\n", encoding="utf-8")
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["status"] == "EVIDENCE_NOT_WRITTEN"
    assert result["failure_kind"] == "evidence_write"
    assert "evidence_error" in result
    assert provider.create_count == 0


# ------------------------------------------------- 8. frozen-runtime risk (not modified here)
#
# ``GoalSandbox.run_public_command`` maps ``ok=True, exit_code=None`` to ``status="PASS"``. The
# defect is real but it is inside the frozen runtime, so this stage only records it: the
# reproduction below is expected to fail until an authorised patch lands, and ``strict=True`` turns
# a silent fix into a test failure so the marker cannot outlive the bug.
#
# Minimal patch suggestion (NOT applied, needs Owner approval):
#     status = "ERROR" if not ok
#              else "UNKNOWN" if exit_code is None
#              else "PASS" if int(exit_code) == 0 else "FAIL"
#
# Exposure today: the real OpenSandbox adapter coerces a missing exit code to 0 before returning
# (`opensandbox_provider.py`, "if exit_code is None: exit_code = 0"), so a live run cannot reach
# this path. The gate also makes its own decision from ``ok`` and ``exit_code`` and never trusts
# ``status``, so E2F's own verdict is not affected.


@pytest.mark.xfail(
    strict=True,
    reason="frozen runtime: run_public_command reports an unobserved exit status as PASS",
)
async def test_an_unobserved_exit_status_is_not_reported_as_a_pass(tmp_path: Path) -> None:
    from atlasos.app.goal_sandbox import GoalSandbox, GoalSandboxOptions
    from atlasos.config import AtlasConfig, ToolsConfig

    class Unobserved:
        async def execute(self, _handle: Any, _command: Any) -> Any:
            return SandboxCommandResult(ok=True, exit_code=None, stdout="unknown")

    options = GoalSandboxOptions(
        provider="fake",
        require_real=False,
        provider_factory=lambda _path: Unobserved(),  # never called: start() is bypassed
    )
    sandbox = GoalSandbox(
        options,
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        run_id="e2f-runtime-risk",
        run_dir=tmp_path,
    )
    # White-box: the three attributes ``run_public_command`` requires, without a container.
    sandbox._provider = Unobserved()  # noqa: SLF001 - reproduction of the frozen behaviour
    sandbox._binding = SimpleNamespace(  # noqa: SLF001  # type: ignore[assignment]
        sandbox_handle=SimpleNamespace(id="sbx-none"),
        workspace_root=WORKSPACE,
    )
    sandbox._task_id = "e2f-runtime-risk"  # noqa: SLF001

    outcome = await sandbox.run_public_command("pwd")
    assert outcome["ok"] is True
    assert outcome["exit_code"] is None
    assert outcome["status"] != "PASS"


# --------------------------------------------------------------------------- 9. CLI


def test_the_cli_blocks_without_an_authorization_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.stage_benchmark.execution import runner

    monkeypatch.setattr(e2f, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(e2f, "CLAIM_DIR", tmp_path / "claims")
    monkeypatch.setattr(e2f, "AUTHORIZATION_PATH", tmp_path / "absent.json")
    monkeypatch.setenv("OPEN_SANDBOX_ENDPOINT", "127.0.0.1:9")
    code = runner.main(["--goal-provisioning-smoke"])
    assert code == 1
    written = sorted(
        (tmp_path / "evidence" / "attempts").glob("blocked-*/provisioning-result.json")
    )
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["verdict"] == e2f.VERDICT_BLOCKED
    assert payload["failure_kind"] == "authorization"
    assert not (tmp_path / "claims").exists()


def test_the_cli_offers_no_way_to_move_the_claim_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag that redirects the claim root would hand one authorization a second claim."""

    monkeypatch.setattr(e2f, "CLAIM_DIR", tmp_path / "claims")
    with pytest.raises(SystemExit) as exit_info:
        e2f.main(["--claim-root", str(tmp_path / "elsewhere")])
    assert exit_info.value.code == 2
    assert not (tmp_path / "elsewhere").exists()


# --------------------------------------------------------------------------- 10. workspace verdict
#
# The check text is not the verdict. A required command only counts when its transport succeeded
# *and* it reported exit 0; otherwise the expected stdout must not be read as a pass.


def _override(provider: GateProvider, name: str, **kwargs: Any) -> None:
    provider.command_overrides[_workspace_command_map()[name]] = SandboxCommandResult(**kwargs)


def test_a_required_check_with_a_non_zero_exit_code_is_not_a_pass(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    _override(provider, "pwd", ok=True, exit_code=2, stdout=f"{WORKSPACE}\n")
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] != e2f.VERDICT_PASS
    assert result["workspace"]["exit_ok"]["pwd"] is False
    assert result["checks"]["workspace_exit_codes"] is False
    assert result["checks"]["workspace_cwd"] is False
    assert result["failure_kind"] == "workspace"
    assert provider.create_count == 1
    assert provider.destroy_count == 1


def test_a_required_check_with_an_unobserved_exit_code_is_not_a_pass(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    _override(provider, "files", ok=True, exit_code=None, stdout="ALL_PRESENT\n")
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] != e2f.VERDICT_PASS
    assert result["workspace"]["exit_ok"]["files"] is False
    assert result["checks"]["workspace_files"] is False
    assert result["checks"]["workspace_exit_codes"] is False
    assert result["failure_kind"] == "workspace"


def test_a_required_check_whose_transport_failed_is_not_a_pass(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    _override(provider, "status", ok=False, exit_code=0, stdout="")
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] != e2f.VERDICT_PASS
    assert result["workspace"]["exit_ok"]["status"] is False
    assert result["checks"]["workspace_clean"] is False
    assert result["failure_kind"] == "workspace"


# --------------------------------------------------------------------------- 11. immutable evidence


def _attempt_results(tmp_path: Path) -> list[Path]:
    root = tmp_path / "evidence" / "attempts"
    return sorted(root.glob("*/provisioning-result*.json")) if root.is_dir() else []


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_replayed_authorization_cannot_touch_the_first_result(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    record = _authorization(identity_digests)
    first = _run(_request(tmp_path, authorization=record))
    assert first["verdict"] == e2f.VERDICT_PASS
    original = Path(first["evidence_path"])
    before = _sha(original)

    second = _run(_request(tmp_path, authorization=record))
    assert second["verdict"] == e2f.VERDICT_BLOCKED
    assert second["failure_kind"] == "duplicate_claim"
    assert provider.create_count == 1
    assert _sha(original) == before
    assert Path(second["evidence_path"]) != original
    assert Path(second["evidence_path"]).is_file()
    # the replay never enters the winner's attempt directory: it owns no attempt of its own
    assert Path(second["evidence_path"]).parent != original.parent
    assert Path(second["evidence_path"]).parent.name.startswith("blocked-")
    assert original.name == e2f.RESULT_NAME


def test_a_run_without_authorization_cannot_overwrite_a_successful_result(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    first = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert first["verdict"] == e2f.VERDICT_PASS
    original = Path(first["evidence_path"])
    before = _sha(original)

    blocked = _run(_request(tmp_path, authorization=None))
    assert blocked["verdict"] == e2f.VERDICT_BLOCKED
    assert _sha(original) == before
    assert Path(blocked["evidence_path"]) != original


def test_two_authorizations_do_not_share_run_state(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    one = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    two = _run(
        _request(tmp_path, authorization=_authorization(identity_digests, approved_by="Owner #2"))
    )
    assert one["verdict"] == e2f.VERDICT_PASS
    assert two["verdict"] == e2f.VERDICT_PASS
    assert one["attempt"] != two["attempt"]
    assert one["evidence_path"] != two["evidence_path"]
    assert one["create_request"]["run_id"] != two["create_request"]["run_id"]
    assert one["create_request"]["task_id"] != two["create_request"]["task_id"]
    run_one = Path(one["evidence_dir"]) / "sandbox"
    run_two = Path(two["evidence_dir"]) / "sandbox"
    assert run_one != run_two
    assert (run_one / "sandbox_bindings.db").is_file()
    assert (run_two / "sandbox_bindings.db").is_file()
    assert provider.create_count == 2


# --------------------------------------------------------------------------- 12. fail closed


def test_an_evidence_write_failure_is_never_reported_as_a_pass(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken(_directory: Path, _payload: dict[str, Any]) -> tuple[str | None, Path | None]:
        return "OSError: the evidence volume is read-only", None

    monkeypatch.setattr(e2f, "_write_evidence_exclusive", _broken)
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))

    assert result["verdict"] == e2f.VERDICT_UNKNOWN
    assert result["status"] == "EVIDENCE_NOT_WRITTEN"
    assert result["failure_kind"] == "evidence_write"
    assert result["gate_conclusion"] == e2f.VERDICT_PASS
    assert result["evidence_error"].startswith("OSError")
    # the sandbox was still provisioned and still destroyed
    assert provider.create_count == 1
    assert provider.destroy_count == 1
    assert result["cleanup"]["state"] == "DESTROYED"
    # the claim is not rolled back to make a retry possible
    assert len(_claims(tmp_path)) == 1


def test_a_primary_failure_and_an_evidence_failure_are_both_kept(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override(provider, "pwd", ok=True, exit_code=2, stdout=f"{WORKSPACE}\n")
    monkeypatch.setattr(
        e2f,
        "_write_evidence_exclusive",
        lambda _directory, _payload: ("OSError: disk full", None),
    )
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["primary_failure"]["kind"] == "workspace"
    assert result["gate_failure_kind"] == "workspace"
    assert result["failure_kind"] == "evidence_write"
    assert result["status"] == "EVIDENCE_NOT_WRITTEN"
    assert provider.destroy_count == 1


def test_the_cli_does_not_exit_zero_when_the_evidence_was_not_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unknown(_request: e2f.ProvisioningRequest) -> dict[str, Any]:
        return {"verdict": e2f.VERDICT_UNKNOWN, "status": "EVIDENCE_NOT_WRITTEN"}

    monkeypatch.setattr(e2f, "run_provisioning_smoke", _unknown)
    assert e2f.main([]) == 1

    async def _passed(_request: e2f.ProvisioningRequest) -> dict[str, Any]:
        return {"verdict": e2f.VERDICT_PASS, "status": "PASSED"}

    monkeypatch.setattr(e2f, "run_provisioning_smoke", _passed)
    assert e2f.main([]) == 0


# --------------------------------------------------------------------------- 13. claim namespace


def test_the_public_request_contract_has_no_claim_namespace_field() -> None:
    """The one-shot namespace is harness state. A request field would be a second-claim lever."""

    fields = {item.name for item in dataclasses.fields(e2f.ProvisioningRequest)}
    assert "claim_root" not in fields
    assert "test_isolation" not in fields
    # the namespace still exists, it is just not something a caller supplies
    assert isinstance(e2f.CLAIM_DIR, Path)
    assert "claim_root" not in inspect.signature(e2f.run_provisioning_smoke).parameters


def test_a_request_cannot_be_constructed_with_a_claim_namespace() -> None:
    """No supported call shape carries a claim root, so there is nothing to bypass."""

    with pytest.raises(TypeError):
        e2f.ProvisioningRequest(authorization=None, claim_root=Path("x"))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        e2f.ProvisioningRequest(authorization=None, test_isolation=True)  # type: ignore[call-arg]


def test_a_replay_from_another_evidence_root_is_still_refused(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    record = _authorization(identity_digests)
    first = _run(_request(tmp_path, authorization=record))
    assert first["verdict"] == e2f.VERDICT_PASS

    second = _run(_request(tmp_path, authorization=record, evidence_dir=tmp_path / "elsewhere"))
    assert second["verdict"] == e2f.VERDICT_BLOCKED
    assert second["failure_kind"] == "duplicate_claim"
    assert provider.create_count == 1


# --------------------------------------------------------------------------- 14. endpoint binding


def test_the_health_probe_and_the_create_use_the_same_endpoint(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_SANDBOX_ENDPOINT", "10.20.30.40:8080")
    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests),
            endpoint="127.0.0.1:9",
        )
    )
    assert result["verdict"] == e2f.VERDICT_PASS
    assert result["environment"]["endpoint"] == "127.0.0.1:9"
    assert result["create_request"]["endpoint"] == "127.0.0.1:9"
    assert result["checks"]["endpoint_consistent"] is True
    options = provider.provider_calls[0]["opensandbox_options"]
    assert options.endpoint == "127.0.0.1:9"


def test_a_resource_config_that_ignores_the_resolved_endpoint_blocks_before_create(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = provision.sandbox_config
    monkeypatch.setattr(
        provision,
        "sandbox_config",
        lambda *_args, **_kwargs: real(endpoint="10.9.9.9:8080"),
    )
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert result["failure_kind"] == "endpoint_mismatch"
    assert provider.create_count == 0


# --------------------------------------------------------------------------- 15. residual states


class _ClosingSandbox:
    """The three attributes ``_close`` reads, with a scripted destroy and probe."""

    def __init__(self, *, destroyed: bool, created: bool = True, probe: Any = None) -> None:
        self.destroyed = destroyed
        self.ready = True
        self.evidence = SimpleNamespace(created=created)
        self.probe = probe
        self.close_calls = 0

    async def close(self, *, failed: bool = False) -> bool:
        self.close_calls += 1
        return self.destroyed

    async def run_public_command(
        self, _command: str, *, timeout_seconds: int = 120
    ) -> dict[str, Any]:
        if isinstance(self.probe, BaseException):
            raise self.probe
        return dict(self.probe or {})


def _close(sandbox: _ClosingSandbox) -> dict[str, Any]:
    import asyncio

    return asyncio.run(e2f._close(sandbox, started=True))  # type: ignore[arg-type]


def test_a_confirmed_destroy_with_a_broken_probe_is_destroyed() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=True, probe=RuntimeError("record not found")))
    assert cleanup["destroyed"] is True
    assert cleanup["residual"] is False
    assert cleanup["state"] == "DESTROYED"


def test_a_failed_destroy_with_a_broken_probe_is_unknown() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=False, probe=RuntimeError("timeout")))
    assert cleanup["residual"] is None
    assert cleanup["state"] == "UNKNOWN"


def test_a_failed_destroy_with_a_refused_probe_is_unknown() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=False, probe={"ok": False, "exit_code": 1}))
    assert cleanup["residual"] is None
    assert cleanup["state"] == "UNKNOWN"


def test_a_failed_destroy_with_an_unobserved_exit_status_is_unknown() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=False, probe={"ok": True, "exit_code": None}))
    assert cleanup["residual"] is None
    assert cleanup["state"] == "UNKNOWN"


def test_a_failed_destroy_with_a_reachable_sandbox_is_residual() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=False, probe={"ok": True, "exit_code": 0}))
    assert cleanup["residual"] is True
    assert cleanup["state"] == "RESIDUAL"


def test_a_confirmed_destroy_of_a_still_reachable_sandbox_is_residual() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=True, probe={"ok": True, "exit_code": 0}))
    assert cleanup["residual"] is True
    assert cleanup["state"] == "RESIDUAL"


def test_no_handle_means_no_residual_claim() -> None:
    cleanup = _close(_ClosingSandbox(destroyed=False, created=False))
    assert cleanup["residual"] is None
    assert cleanup["state"] == "UNKNOWN"


def test_the_cleanup_hint_names_the_attempt_run_id() -> None:
    import asyncio

    sandbox = _ClosingSandbox(destroyed=False)
    cleanup = asyncio.run(
        e2f._close(sandbox, started=True, run_id="SANDBOX.X.abc")  # type: ignore[arg-type]
    )
    assert "SANDBOX.X.abc" in cleanup["detail"]


# --------------------------------------------------------------------------- 16. forbidden perms


@pytest.mark.parametrize("name", e2f.FORBIDDEN_PERMISSIONS)
def test_a_missing_forbidden_permission_blocks(
    tmp_path: Path, provider: GateProvider, identity_digests: Any, name: str
) -> None:
    record = _authorization(identity_digests)
    record.pop(name)
    result = _run(_request(tmp_path, authorization=record))
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert any(f"forbidden_permission:{name}" in p for p in result["authorization"]["problems"])
    assert provider.create_count == 0


# --------------------------------------------------------------------------- 17. ownership race


def test_a_losing_replay_cannot_occupy_the_winners_result_file(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    """A wins the claim and waits; B is refused, finishes first, and still owns nothing.

    The barrier is an event, not a sleep: B runs exactly while A is parked after its claim.
    """

    record = _authorization(identity_digests)
    claimed = threading.Event()
    release = threading.Event()
    outcome: dict[str, Any] = {}

    def _parked_probe() -> dict[str, Any]:
        claimed.set()
        assert release.wait(60), "the winner was never released"
        return _healthy()

    def _winner() -> None:
        try:
            outcome["a"] = _run(
                _request(tmp_path, authorization=record, health_probe=_parked_probe)
            )
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertions below
            outcome["a_error"] = error

    thread = threading.Thread(target=_winner, daemon=True)
    thread.start()
    try:
        assert claimed.wait(60), "the winner never reached the barrier"
        loser = _run(_request(tmp_path, authorization=record))
    finally:
        release.set()
        thread.join(120)

    assert not thread.is_alive()
    assert "a_error" not in outcome
    winner = outcome["a"]
    assert winner["verdict"] == e2f.VERDICT_PASS
    assert loser["verdict"] == e2f.VERDICT_BLOCKED
    assert loser["failure_kind"] == "duplicate_claim"

    canonical = Path(winner["evidence_path"])
    assert canonical.name == e2f.RESULT_NAME
    assert canonical.parent.name == winner["attempt"]
    assert canonical.parent.name.startswith("attempt-")
    assert Path(loser["evidence_path"]).parent.name.startswith("blocked-")
    assert Path(loser["evidence_path"]) != canonical
    assert canonical.is_file()
    assert Path(loser["evidence_path"]).is_file()

    payload = json.loads(canonical.read_text("utf-8"))
    assert payload["verdict"] == e2f.VERDICT_PASS
    assert payload["claim"]["created"] is True
    assert payload["attempt"] == winner["attempt"]
    assert provider.create_count == 1


# --------------------------------------------------------- 18. post-claim terminal states


def _attempt_result(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    files = sorted((tmp_path / "evidence" / "attempts").glob("attempt-*/provisioning-result*.json"))
    assert len(files) == 1, files
    return files[0], json.loads(files[0].read_text("utf-8"))


def test_a_health_probe_that_raises_leaves_a_terminal_record(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("the adapter is down")

    result = _run(
        _request(tmp_path, authorization=_authorization(identity_digests), health_probe=_boom)
    )
    assert result["verdict"] == e2f.VERDICT_BLOCKED
    assert result["failure_kind"] == "sandbox_environment"
    assert result["create"] == {"attempted": False, "state": "NOT_ATTEMPTED"}
    assert result["primary_failure"]["kind"] == "sandbox_environment"
    assert provider.create_count == 0
    assert Path(result["evidence_path"]).is_file()
    assert len(_claims(tmp_path)) == 1


def test_a_scratch_failure_leaves_a_terminal_record(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("no space for a scratch directory")

    monkeypatch.setattr(e2f.tempfile, "mkdtemp", _boom)
    result = _run(
        _request(
            tmp_path,
            authorization=_authorization(identity_digests),
            scratch_root=None,
        )
    )
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "environment"
    assert result["create"] == {"attempted": False, "state": "NOT_ATTEMPTED"}
    assert provider.create_count == 0
    assert Path(result["evidence_path"]).is_file()
    assert len(_claims(tmp_path)) == 1


def test_a_resource_config_failure_leaves_a_terminal_record(
    tmp_path: Path,
    provider: GateProvider,
    identity_digests: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = provision.sandbox_config

    def _boom(*args: Any, **kwargs: Any) -> Any:
        # the identity audit also calls this, positionally; only the gate's pinned call may fail
        if kwargs.get("endpoint") is not None:
            raise RuntimeError("the frozen resource config is unreadable")
        return real(*args, **kwargs)

    monkeypatch.setattr(provision, "sandbox_config", _boom)
    result = _run(_request(tmp_path, authorization=_authorization(identity_digests)))
    assert result["verdict"] == e2f.VERDICT_FAILED
    assert result["failure_kind"] == "sandbox_environment"
    assert result["create"] == {"attempted": False, "state": "NOT_ATTEMPTED"}
    assert provider.create_count == 0
    assert Path(result["evidence_path"]).is_file()
    assert len(_claims(tmp_path)) == 1


def test_a_cancellation_during_the_start_is_recorded_and_re_raised(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    def _cancel() -> None:
        raise asyncio.CancelledError

    # cancelled after the container exists: the harder of the two cases
    provider.create_after_hook = _cancel
    with pytest.raises(asyncio.CancelledError):
        _run(_request(tmp_path, authorization=_authorization(identity_digests)))

    path, payload = _attempt_result(tmp_path)
    assert payload["verdict"] == e2f.VERDICT_FAILED
    assert payload["failure_kind"] == "cancelled"
    assert payload["primary_failure"]["kind"] == "cancelled"
    # the provider was asked exactly once; the outcome of that create is unconfirmed
    assert provider.create_attempts == 1
    assert payload["create"]["state"] == "UNKNOWN"
    assert payload["create"]["attempted"] in {True, None}
    # the teardown path ran; whether it could reach a container is a separate question
    assert payload["cleanup"]["attempted"] is True
    assert payload["cleanup"]["state"] in {"UNKNOWN", "DESTROYED", "RESIDUAL"}
    assert payload["cleanup"]["residual"] is not False or payload["cleanup"]["destroyed"] is True
    assert len(_claims(tmp_path)) == 1
    assert path.is_file()


def test_a_cancellation_during_the_workspace_checks_is_recorded_and_re_raised(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.raise_on_command[_workspace_command_map()["pwd"]] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        _run(_request(tmp_path, authorization=_authorization(identity_digests)))

    _path, payload = _attempt_result(tmp_path)
    assert payload["verdict"] == e2f.VERDICT_FAILED
    assert payload["failure_kind"] == "cancelled"
    assert payload["create"]["state"] == "CREATED"
    assert provider.create_count == 1
    assert provider.destroy_count == 1
    assert payload["cleanup"]["state"] == "DESTROYED"
    assert len(_claims(tmp_path)) == 1


def test_a_cancellation_with_a_failing_cleanup_keeps_both_failures(
    tmp_path: Path, provider: GateProvider, identity_digests: Any
) -> None:
    provider.raise_on_command[_workspace_command_map()["pwd"]] = asyncio.CancelledError()

    def _destroy_fails() -> None:
        raise RuntimeError("the destroy channel is closed")

    provider.destroy_hook = _destroy_fails
    with pytest.raises(asyncio.CancelledError):
        _run(_request(tmp_path, authorization=_authorization(identity_digests)))

    _path, payload = _attempt_result(tmp_path)
    assert payload["failure_kind"] == "cancelled"
    assert payload["primary_failure"]["kind"] == "cancelled"
    assert any(
        "destroy channel is closed" in str(item.get("detail", ""))
        for item in payload["secondary_failures"]
    )
    # the destroy failed and the handle still answered: the truth is RESIDUAL, never "gone"
    assert payload["cleanup"]["destroyed"] is False
    assert payload["cleanup"]["residual"] is True
    assert payload["cleanup"]["state"] == "RESIDUAL"
    assert provider.create_attempts == 1
    assert provider.destroy_attempts == 1
    assert len(_claims(tmp_path)) == 1
