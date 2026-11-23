"""Stage B-E2E: resource validation and the OpenSandbox smoke path, all offline.

The smoke tests use a fake provider. They prove the ordering, the evidence and the verdict rules;
they do not prove a real container starts. That is Phase B and needs its own authorization.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from experiments.stage_benchmark import protocol
from experiments.stage_benchmark.execution import (
    admission,
    network,
    provision,
    smoke,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _authorization(**overrides: Any) -> dict[str, Any]:
    record = {
        "status": "APPROVED",
        "smoke_id": smoke.SMOKE_ID,
        "approved_by": "Owner (Melon235)",
        "approved_at": "2026-11-23T00:00:00+00:00",
        "approval_commit": "0" * 40,
        "allow_container_create": True,
        "allow_container_destroy": True,
        "deepseek_requests_allowed": False,
        "agent_runner_allowed": False,
        "calibration_allowed": False,
        "benchmark_cells_allowed": False,
        "max_sandboxes": 1,
    }
    record.update(overrides)
    return record


class FakeProvider:
    def __init__(
        self, *, create_error: Exception | None = None, destroy_error: Exception | None = None
    ):
        self.create_calls = 0
        self.destroy_calls = 0
        self.create_error = create_error
        self.destroy_error = destroy_error
        self.destroyed = False
        self.handle = SimpleNamespace(id="sbx-smoke-1")

    async def create(self, spec: Any) -> Any:
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        assert spec.run_id == smoke.SMOKE_ID
        assert spec.network == "none"
        return SimpleNamespace(ok=True, handle=self.handle, error=None)

    async def execute(self, handle: Any, command: Any) -> Any:
        if handle is None or self.destroyed:
            raise RuntimeError("sandbox is gone")
        return SimpleNamespace(ok=True, exit_code=0, stdout="/workspace/project\n", stderr="")

    async def write_file(self, handle: Any, path: str, data: bytes) -> Any:
        self.written = data
        return SimpleNamespace(ok=True, data=data, error=None)

    async def read_file(self, handle: Any, path: str) -> Any:
        return SimpleNamespace(ok=True, data=getattr(self, "written", b""), error=None)

    async def destroy(self, handle: Any) -> Any:
        self.destroy_calls += 1
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed = True
        return SimpleNamespace(ok=True, reference="destroyed", error=None)


def _healthy() -> dict[str, Any]:
    return {
        "ok": True,
        "code": network.SANDBOX_HEALTH_READY,
        "endpoint": "127.0.0.1:9",
        "status_code": 200,
        "detail": "healthy",
    }


async def _isolation_pass(provider: Any, handle: Any) -> str:
    return "PASS"


def _run(tmp_path: Path, provider: FakeProvider, **kwargs: Any) -> dict[str, Any]:
    request = smoke.SmokeRequest(
        evidence_dir=tmp_path / "smoke",
        authorization=kwargs.pop("authorization", _authorization()),
        endpoint="127.0.0.1:9",
        provider_factory=lambda _options: provider,
        health_probe=kwargs.pop("health_probe", _healthy),
        isolation_probe=kwargs.pop("isolation_probe", _isolation_pass),
    )
    return asyncio.run(smoke.run_smoke(request))


# --------------------------------------------------------------------------- 1-3. gate


def test_smoke_authorization_is_required_and_scoped() -> None:
    assert smoke.authorization_gate(None)["ready"] is False
    partial = _authorization(deepseek_requests_allowed=True)
    gate = smoke.authorization_gate(partial)
    assert gate["ready"] is False
    assert any("forbidden_permission" in problem for problem in gate["problems"])
    wrong_id = _authorization(smoke_id="CALIBRATION.workgrid.c3")
    assert smoke.authorization_gate(wrong_id)["ready"] is False
    assert smoke.authorization_gate(_authorization())["ready"] is True


def test_missing_authorization_blocks_without_creating(tmp_path: Path) -> None:
    provider = FakeProvider()
    result = _run(tmp_path, provider, authorization=None)
    assert result["verdict"] == "SANDBOX_SMOKE_BLOCKED"
    assert provider.create_calls == 0
    assert (tmp_path / "smoke" / "smoke-result.json").is_file()


# --------------------------------------------------------------------------- 4-6. health


def test_health_failure_never_reaches_create(tmp_path: Path) -> None:
    provider = FakeProvider()
    result = _run(
        tmp_path,
        provider,
        health_probe=lambda: {
            "ok": False,
            "code": network.SANDBOX_HEALTH_TIMEOUT,
            "endpoint": "127.0.0.1:9",
            "status_code": None,
            "detail": "timeout",
        },
    )
    assert result["verdict"] == "SANDBOX_SMOKE_FAILED"
    assert provider.create_calls == 0
    assert "create" not in result


def test_health_and_create_are_separate_states(tmp_path: Path) -> None:
    provider = FakeProvider(create_error=RuntimeError("invalid resourceLimits.memory"))
    result = _run(tmp_path, provider)
    assert result["health"]["ok"] is True
    assert result["create"]["ok"] is False
    assert "invalid resourceLimits" in result["create"]["error"]
    assert result["verdict"] == "SANDBOX_SMOKE_FAILED"


# --------------------------------------------------------------------------- 7-9. run


def test_smoke_pass_executes_commands_and_destroys(tmp_path: Path) -> None:
    provider = FakeProvider()
    result = _run(tmp_path, provider)
    assert result["checks"]["commands"] is True
    assert result["checks"]["files"] is True
    assert result["checks"]["isolation"] is True
    assert result["checks"]["cleanup"] is True
    assert result["verdict"] == "SANDBOX_SMOKE_PASS"
    assert provider.create_calls == 1
    assert provider.destroy_calls == 1
    assert result["model_requests"] == 0
    assert result["calibration_attempts"] == 0
    assert result["create_request"]["memory"] == "1Gi"
    assert result["create_request"]["cpu"] == "1"
    assert result["create_request"]["network"] == "none"


def test_create_failure_is_recorded_once_and_not_retried(tmp_path: Path) -> None:
    provider = FakeProvider(create_error=RuntimeError("sandbox create rejected"))
    result = _run(tmp_path, provider)
    assert provider.create_calls == 1
    assert provider.destroy_calls == 0
    assert result["cleanup"]["attempted"] is False
    assert result["verdict"] == "SANDBOX_SMOKE_FAILED"


def test_a_required_command_with_a_nonzero_exit_fails_the_verdict(tmp_path: Path) -> None:
    """Transport ok is not process success: the exact defect found in the first real smoke."""

    class ExitTwoProvider(FakeProvider):
        async def execute(self, handle: Any, command: Any) -> Any:
            result = await super().execute(handle, command)
            if command.command == "pwd":
                result.exit_code = 2
                result.ok = True
            return result

    result = _run(tmp_path, ExitTwoProvider())
    pwd = next(item for item in result["commands"] if item["name"] == "pwd")
    assert pwd["ok"] is True and pwd["exit_ok"] is False
    assert result["checks"]["commands"] is False
    assert result["verdict"] == "SANDBOX_SMOKE_FAILED"


def test_an_informational_command_may_fail_without_failing_the_smoke(tmp_path: Path) -> None:
    class WorkspaceMissingProvider(FakeProvider):
        async def execute(self, handle: Any, command: Any) -> Any:
            result = await super().execute(handle, command)
            if command.command.startswith("ls "):
                result.exit_code = 2
                result.ok = True
            return result

    result = _run(tmp_path, WorkspaceMissingProvider())
    workspace = next(item for item in result["commands"] if item["name"] == "workspace")
    assert workspace["required"] is False and workspace["exit_ok"] is False
    assert result["checks"]["commands"] is True
    assert result["verdict"] == "SANDBOX_SMOKE_PASS"


def test_isolation_failure_fails_the_verdict(tmp_path: Path) -> None:
    async def hidden_egress(_provider: Any, _handle: Any) -> str:
        return "FAIL"

    provider = FakeProvider()
    result = _run(tmp_path, provider, isolation_probe=hidden_egress)
    assert result["isolation"] == "FAIL"
    assert result["checks"]["isolation"] is False
    assert result["verdict"] == "SANDBOX_SMOKE_FAILED"


def test_cleanup_failure_is_recorded_not_hidden(tmp_path: Path) -> None:
    provider = FakeProvider(destroy_error=RuntimeError("docker daemon busy"))
    result = _run(tmp_path, provider)
    assert result["cleanup"]["attempted"] is True
    assert result["cleanup"]["destroyed"] is False
    assert "docker daemon busy" in result["cleanup"]["error"]
    assert result["verdict"] == "SANDBOX_SMOKE_FAILED"
    assert result["create"]["sandbox_id"] == "sbx-smoke-1"


def test_smoke_never_creates_a_calibration_marker(tmp_path: Path) -> None:
    provider = FakeProvider()
    _run(tmp_path, provider)
    markers = list(tmp_path.rglob("attempt-1.json"))
    assert markers == []
    assert not any("CALIBRATION" in str(path) for path in tmp_path.rglob("*"))


# --------------------------------------------------------------------------- 10-13. memory


def test_memory_quantity_accepts_the_frozen_value() -> None:
    assert provision.memory_quantity(1.0) == "1Gi"
    assert provision.memory_quantity(0.5) == "0.5Gi"
    assert provision.memory_quantity(2) == "2Gi"


def test_memory_quantity_rejects_non_finite_and_non_numeric() -> None:
    for value in (math.nan, math.inf, -math.inf, "1Gi", None):
        with pytest.raises(ValueError):
            provision.memory_quantity(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        provision.memory_quantity(True)  # type: ignore[arg-type]


def test_memory_quantity_never_silently_rounds() -> None:
    # A sub-milligit value cannot be expressed without changing the limit, so it is rejected
    # rather than becoming 0Gi on the wire.
    with pytest.raises(ValueError, match="precision"):
        provision.memory_quantity(0.0001)
    with pytest.raises(ValueError, match="precision"):
        provision.memory_quantity(1.0004)
    with pytest.raises(ValueError):
        provision.memory_quantity(0)


def test_sandbox_config_carries_the_frozen_resources() -> None:
    sandbox = provision.sandbox_config()
    assert sandbox.provider == "opensandbox"
    assert sandbox.cpu == "1"
    assert sandbox.memory == "1Gi"
    assert sandbox.image == "atlas-uat-python:3.12"
    assert sandbox.network == "none"
    assert sandbox.workspace_root == "/workspace/project"


def test_resource_audit_reports_a_tampered_memory_parameter() -> None:
    document = json.loads(Path(protocol.PROTOCOL_PATH).read_text(encoding="utf-8"))
    document["run_config"]["memory_gib"] = 0.0001
    audit = provision.sandbox_resource_audit(document)
    assert audit["ok"] is False
    assert any("memory" in problem for problem in audit["problems"])


# --------------------------------------------------------------------------- 14-17. wiring


def test_preflight_includes_the_resource_check() -> None:
    result = admission.preflight(evidence_root=Path("missing"), require_offline_report=False)
    labels = {check["label"] for check in result["checks"]}
    assert "sandbox_resource_configuration" in labels
    resource = next(c for c in result["checks"] if c["label"] == "sandbox_resource_configuration")
    assert resource["ok"] is True
    assert "1Gi" in resource["detail"]


def test_preflight_blocks_when_the_resource_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provision,
        "sandbox_resource_audit",
        lambda document=None: {"ok": False, "problems": ["memory_gib invalid"], "config": {}},
    )
    result = admission.preflight(evidence_root=Path("missing"), require_offline_report=False)
    assert result["status"] == "BLOCKED"
    assert any("sandbox_resource_configuration" in problem for problem in result["problems"])


def test_c3_is_prepared_and_outside_the_plan() -> None:
    cell = provision.calibration_cell()
    assert cell.cell_id == "CALIBRATION.workgrid.c3"
    assert cell.cell_id not in {
        item["cell_id"] for item in protocol.read_protocol()["plan"]["cells"]
    }
    assert provision.CALIBRATION["previous_attempts"] == (
        "CALIBRATION.workgrid.c1",
        "CALIBRATION.workgrid.c2",
    )
    assert provision.verify_calibration_identity() == []


def test_smoke_summary_is_not_run_before_any_smoke() -> None:
    """A missing smoke result must never be read as a pass."""

    summary = admission._smoke_summary()
    if (admission.SMOKE_EVIDENCE_DIR / "sandbox-smoke" / "smoke-result.json").is_file():
        assert summary["status"] in {"PASSED", "FAILED"}
    else:
        assert summary["status"] == "NOT_RUN"
        assert summary["verdict"] == "SANDBOX_SMOKE_BLOCKED"


def test_smoke_evidence_records_no_model_and_no_calibration(tmp_path: Path) -> None:
    provider = FakeProvider()
    result = _run(tmp_path, provider)
    payload = json.loads((tmp_path / "smoke" / "smoke-result.json").read_text("utf-8"))
    assert payload["model_requests"] == 0
    assert payload["calibration_attempts"] == 0
    assert payload["benchmark_cells"] == 0
    assert result["status"] == "PASSED"
