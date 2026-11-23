"""Stage B-E2C: proxy bypass, pre-agent evidence, and the calibration retry identity.

All network activity is local: a loopback health server and a loopback "proxy" that answers 502.
No test starts a sandbox, calls DeepSeek, or writes to the historical c1 evidence.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_benchmark import protocol, schema
from experiments.stage_benchmark.execution import (
    adapter,
    admission,
    collector,
    network,
    offline,
    provision,
    runner,
)
from experiments.stage_benchmark.execution.models import CellRequest, ExecutionBlocked


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        body = b'{"status": "healthy"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


class _BadProxyHandler(BaseHTTPRequestHandler):
    """A proxy that accepts the connection and then fails, like a stopped VPN client."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self.send_response(502)
        self.end_headers()
        self.wfile.write(b"proxy error")

    def log_message(self, *args: Any) -> None:
        return


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def health_server() -> Iterator[str]:
    server, endpoint = _serve(_HealthHandler)
    yield endpoint
    server.shutdown()


@pytest.fixture
def bad_proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    server, endpoint = _serve(_BadProxyHandler)
    monkeypatch.setenv("HTTP_PROXY", f"http://{endpoint}")
    monkeypatch.setenv("HTTPS_PROXY", f"http://{endpoint}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    yield endpoint
    server.shutdown()


def _document() -> dict[str, Any]:
    return protocol.read_protocol()


# --------------------------------------------------------------------------- 1-4. proxy


def test_a_live_proxy_is_used_when_no_bypass_is_configured(
    bad_proxy: str, health_server: str
) -> None:
    """The hazard itself: httpx sends the sandbox request to the proxy and gets its 502."""

    import httpx

    response = httpx.get(f"http://{health_server}/health", timeout=5.0)
    # The proxy answered, not the sandbox: this is the E2B failure in miniature.
    assert response.status_code == 502


def test_bypassing_the_proxy_makes_the_health_probe_succeed(
    bad_proxy: str, health_server: str
) -> None:
    result = network.probe_health(health_server)
    assert result["ok"] is True
    assert result["code"] == network.SANDBOX_HEALTH_READY
    assert health_server.split(":")[0] in result["bypass"]


def test_an_unreachable_endpoint_is_classified_not_raised() -> None:
    result = network.probe_health("127.0.0.1:1")
    assert result["ok"] is False
    assert result["code"] in {
        network.SANDBOX_ENDPOINT_UNREACHABLE,
        network.SANDBOX_HEALTH_TIMEOUT,
    }


def test_no_proxy_merges_existing_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "example.internal,10.0.0.5")
    info = network.apply_proxy_bypass("172.30.1.9:8080")
    assert "example.internal" in info["bypass"] and "10.0.0.5" in info["bypass"]
    assert "172.30.1.9" in info["bypass"]
    assert info["preserved_existing"] == ["example.internal", "10.0.0.5"]


def test_provider_proxy_entries_are_not_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A documentation address: the test must not imply any particular local port is free.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    network.apply_proxy_bypass("172.30.1.9:8080")
    import os

    assert os.environ["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert "172.30.1.9" in os.environ["NO_PROXY"]


def test_the_execution_layer_never_mentions_a_vpn_port() -> None:
    """12000 belongs to the user's VPN. The harness may not bind, close or special-case it."""

    offenders = [
        path.name
        for path in (Path(provision.__file__).parent).glob("*.py")
        if "12000" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_memory_quantity_renders_an_integer_without_a_decimal() -> None:
    assert provision.memory_quantity(1.0) == "1Gi"
    assert provision.memory_quantity(1) == "1Gi"
    assert provision.memory_quantity(2.0) == "2Gi"
    assert provision.memory_quantity(0.5) == "0.5Gi"


def test_memory_quantity_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        provision.memory_quantity(0)
    with pytest.raises(ValueError, match="positive"):
        provision.memory_quantity(-1.0)


def test_built_options_carry_the_integer_memory_quantity(tmp_path: Path) -> None:
    document = _document()
    cell = provision.calibration_cell()
    fixture = provision.materialize_calibration_fixture(tmp_path / "fixture")
    options = provision.build_options(
        cell=cell,
        fixture_repo=fixture,
        runs_root=tmp_path / "runs",
        run_id="test",
        document=document,
        offline=True,
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
    )
    assert options.config.sandbox.memory == "1Gi"
    assert options.config.sandbox.cpu == "1"


def test_a_changed_wsl_address_updates_the_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    first = network.apply_proxy_bypass("172.28.1.5:8080")
    second = network.apply_proxy_bypass("172.28.9.9:8080")
    assert "172.28.1.5" in first["bypass"]
    assert "172.28.9.9" in second["bypass"]
    # The old address is kept, not because it is hard-coded, but because NO_PROXY is cumulative.
    assert "172.28.1.5" in second["bypass"]


def test_endpoint_is_read_fresh_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPEN_SANDBOX_ENDPOINT", "172.30.4.4:8080")
    assert network.sandbox_endpoint() == ("172.30.4.4:8080", "env:OPEN_SANDBOX_ENDPOINT")
    assert network.bypass_hosts()[0] == "172.30.4.4"
    monkeypatch.setenv("OPEN_SANDBOX_ENDPOINT", "172.30.5.5:8080")
    assert network.bypass_hosts()[0] == "172.30.5.5"


# --------------------------------------------------------------------------- 5-7. preflight


def test_health_probe_failure_blocks_before_the_marker(tmp_path: Path) -> None:
    cell = provision.cell_from_plan(_document(), "slugkit.P1.r1")
    result = admission.preflight(
        evidence_root=tmp_path / "evidence",
        cell=cell,
        probe_sandbox=True,
        sandbox_probe=lambda: {
            "ok": False,
            "code": network.SANDBOX_HEALTH_TIMEOUT,
            "endpoint": "172.28.0.1:8080",
            "status_code": None,
            "detail": "ReadTimeout",
        },
    )
    assert result["status"] == "BLOCKED"
    assert any("SANDBOX_HEALTH_TIMEOUT" in problem for problem in result["problems"])
    assert not (tmp_path / "evidence").exists()
    assert result["sandbox"]["probed"] is True


def test_health_probe_never_reaches_the_model(tmp_path: Path) -> None:
    calls: list[str] = []

    async def must_not_run(options: Any) -> dict[str, Any]:
        calls.append("run_goal")
        return {}

    request = CellRequest(
        cell=provision.cell_from_plan(_document(), "slugkit.P1.r1"),
        runs_root=tmp_path / "runs",
        evidence_root=tmp_path / "evidence",
        live=True,
        run_fn=must_not_run,
        sandbox_probe=lambda: {
            "ok": False,
            "code": network.SANDBOX_ENDPOINT_UNREACHABLE,
            "endpoint": "172.28.0.1:8080",
            "status_code": None,
            "detail": "refused",
        },
    )
    with pytest.raises(ExecutionBlocked, match="sandbox_health"):
        asyncio.run(runner.execute_cell(request))
    assert calls == []
    assert not admission.attempt_path(
        admission.cell_dir(tmp_path / "evidence", request.cell)
    ).is_file()


def test_preflight_reports_the_bypass_and_endpoint() -> None:
    result = admission.preflight(
        evidence_root=Path("missing-evidence-root"),
        probe_sandbox=True,
        sandbox_probe=lambda: {
            "ok": True,
            "code": network.SANDBOX_HEALTH_READY,
            "endpoint": "172.28.7.7:8080",
            "status_code": 200,
            "detail": "healthy",
            "bypass": "172.28.7.7",
            "host": "172.28.7.7",
        },
    )
    assert result["sandbox"]["probed"] is True
    assert result["sandbox"]["code"] == network.SANDBOX_HEALTH_READY
    probe_checks = [item for item in result["checks"] if item["label"] == "sandbox_health"]
    assert probe_checks and probe_checks[0]["ok"] is True


# --------------------------------------------------------------------------- 8-13. pre-agent


def _write_pre_agent_run(options: Any, *, failure: str) -> dict[str, Any]:
    run_dir = Path(options.runs_root) / options.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "run_id": options.run_id,
        "status": "failed",
        "failure": failure,
        "main": {"stop_reason": None},
        "metrics": {"main_requests": 0, "system_total_tokens": 0, "child_total_prompt_tokens": 0},
        "provider": "deepseek",
        "model": "deepseek-chat",
        "isolation": "sandbox",
        "main_prompt": {
            "version": "main-v1.0",
            "digest": protocol.prompt_spec("P1").digest,
            "chars": 1,
            "source": "override",
        },
        "sandbox": {
            "provider": "opensandbox",
            "provider_is_real": True,
            "created": False,
            "destroyed": False,
            "error": None,
        },
        "verification": None,
        "delegation": {"batches": []},
    }
    (run_dir / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (run_dir / "transcript.jsonl").write_text(
        json.dumps({"seq": 1, "kind": "lifecycle"}) + "\n", encoding="utf-8"
    )
    return session


async def _run_pre_agent(tmp_path: Path) -> Any:
    cell = provision.cell_from_plan(_document(), "slugkit.P1.r1")

    async def run(options: Any) -> dict[str, Any]:
        return _write_pre_agent_run(
            options, failure="sandbox provider 'opensandbox' is unavailable: ReadTimeout"
        )

    request = CellRequest(
        cell=cell,
        runs_root=tmp_path / "runs",
        evidence_root=tmp_path / "evidence",
        protocol_document=_document(),
        run_fn=run,
        host_oracle=None,
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
    )
    return await runner.execute_cell(request)


async def test_pre_agent_failure_produces_an_incomplete_analysis(tmp_path: Path) -> None:
    result = await _run_pre_agent(tmp_path)
    assert result.failure_kind == "sandbox_creation"
    assert result.ok is False
    analysis = json.loads((result.evidence_dir / "analysis.json").read_text("utf-8"))
    assert analysis["status"] == "INCOMPLETE"
    assert analysis["record"] is None
    assert analysis["degraded"]["stage"] == "PRE_AGENT"
    assert "tool_conversations.json" in analysis["degraded"]["not_applicable"]
    assert analysis["valid"] is False


async def test_missing_workspace_patch_is_a_gap_not_a_fabrication(tmp_path: Path) -> None:
    result = await _run_pre_agent(tmp_path)
    bundle = result.evidence_dir
    assert bundle is not None
    assert not (bundle / "workspace.patch").exists()
    assert not (bundle / "tool_conversations.json").exists()
    assert any("not applicable" in gap for gap in result.gaps)


async def test_string_session_failure_does_not_raise() -> None:
    session = {
        "status": "failed",
        "failure": "sandbox provider 'opensandbox' is unavailable: ReadTimeout",
        "sandbox": {"created": False},
        "metrics": {"main_requests": 0},
    }
    gaps: list[str] = []
    view = adapter.build_view(session, oracle=None, gaps=gaps)
    assert view["execution_integrity"]["stage"] == "PRE_AGENT"
    assert view["execution_integrity"]["primary_failure"].startswith("sandbox provider")
    assert view["goal_correctness"]["status"] == "UNKNOWN"


async def test_collector_failure_does_not_overwrite_the_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_: Any) -> dict[str, Any]:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(collector, "collect", boom)
    result = await _run_pre_agent(tmp_path)
    assert result.failure_kind == "sandbox_creation"
    assert any("collector_failure" in item for item in result.secondary_failures)


async def test_aggregation_failure_does_not_overwrite_the_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise AttributeError("adapter exploded")

    monkeypatch.setattr(adapter, "build_view", boom)
    result = await _run_pre_agent(tmp_path)
    assert result.failure_kind == "sandbox_creation"
    assert any("aggregation_failure" in item for item in result.secondary_failures)


async def test_marker_reaches_a_terminal_state_on_failure(tmp_path: Path) -> None:
    result = await _run_pre_agent(tmp_path)
    marker = admission.attempt_path(admission.cell_dir(tmp_path / "evidence", result.cell))
    payload = admission.read_json(marker)
    assert payload["state"] == "FAILED"
    assert payload["retries_allowed"] is False


async def test_a_marker_write_failure_keeps_the_original_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admission, "mark_attempt", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    result = await _run_pre_agent(tmp_path)
    assert result.failure_kind == "sandbox_creation"
    assert any("marker_update_failure" in item for item in result.secondary_failures)
    marker = admission.attempt_path(admission.cell_dir(tmp_path / "evidence", result.cell))
    # The claim is durable even when every state write fails; it is never silently revoked.
    assert admission.read_json(marker)["state"] in {"CLAIMED", "RUNNING"}


# --------------------------------------------------------------------------- 14-19. identity


def test_the_retry_identity_is_new_and_outside_the_plan() -> None:
    cell = provision.calibration_cell()
    assert cell.cell_id == "CALIBRATION.workgrid.c3"
    assert cell.cell_id != provision.CALIBRATION["previous_attempt"]
    assert cell.cell_id not in {item["cell_id"] for item in _document()["plan"]["cells"]}
    assert provision.verify_calibration_identity() == []


def test_two_calibration_identities_get_separate_evidence_paths(tmp_path: Path) -> None:
    retry_root = tmp_path / "retry"
    old_root = tmp_path / "old"
    from experiments.stage_benchmark.execution.models import CellIdentity

    old = CellIdentity(
        task_id="workgrid",
        arm_id="P1",
        repetition=0,
        cell_id="CALIBRATION.workgrid.c1",
        in_matrix=False,
    )
    assert admission.cell_dir(retry_root, provision.calibration_cell()) != admission.cell_dir(
        old_root, old
    )


def test_a_consumed_authorization_cannot_authorize_the_retry() -> None:
    cell = provision.calibration_cell()
    preflight = {
        "protocol_digest": "sha256:p",
        "plan_digest": "sha256:l",
        "runtime_digest": "sha256:r",
        "runner_source_digest": "sha256:s",
    }
    record = {
        "status": "APPROVED",
        "approved_by": "Owner (Melon235)",
        "approval_commit": "a" * 40,
        "approved_at": "2026-11-20T00:00:00+00:00",
        "cells": ["CALIBRATION.workgrid.c1"],
        "approved_digests": {
            "protocol": "sha256:p",
            "plan": "sha256:l",
            "runtime": "sha256:r",
            "runner_source": "sha256:s",
        },
        "consumed": True,
        "cost_ceiling_usd": 1.0,
    }
    gate = admission.authorization_gate(record, cell=cell, preflight_result=preflight, live=True)
    assert gate["ready"] is False
    assert any("cell_not_authorized" in problem for problem in gate["problems"])
    assert any("authorization_already_consumed" in problem for problem in gate["problems"])


def test_recovery_still_returns_without_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = asyncio.run(_run_pre_agent(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    monkeypatch.setattr(
        runner, "_run_goal", lambda: (_ for _ in ()).throw(AssertionError("provider reached"))
    )
    recovered = collector.recover_evidence(
        bundle, cell=result.cell, source_digest=admission.source_identity()["digest"]
    )
    assert recovered["analysis_version"] == adapter.ANALYSIS_VERSION
    assert recovered["raw_digests_unchanged"] is True
    assert recovered["degraded"] is True
    assert recovered["status"] == "INCOMPLETE"


def test_host_oracle_stays_unknown_for_the_retry(tmp_path: Path) -> None:
    result = asyncio.run(_run_pre_agent(tmp_path))
    assert result.goal_correctness == "UNKNOWN"
    oracle = json.loads((result.evidence_dir / "oracle.json").read_text("utf-8"))
    assert oracle["ok"] is None
    assert oracle["source"] == "UNKNOWN"


def test_frozen_prompt_and_runtime_digests_are_unchanged() -> None:
    frozen = admission.read_json(admission.EXECUTION_DIR.parent / "freeze.json")
    for arm in protocol.ARM_IDS:
        assert protocol.prompt_spec(arm).digest == frozen["prompts"][arm]["digest"]
    e1 = admission.read_json(admission.EXECUTION_DIR.parent / "freeze.json")
    from experiments.stage_benchmark import run_stage_b as stage_runner

    assert stage_runner.source_identity()["tree_digest"] == e1["runtime"]["tree_digest"]


def test_protocol_and_plan_digests_are_unchanged() -> None:
    frozen = admission.read_json(admission.EXECUTION_DIR.parent / "freeze.json")
    assert (
        admission.digest_bytes(protocol.PROTOCOL_PATH.read_bytes()) == frozen["protocol"]["digest"]
    )
    assert admission.read_json(protocol.PLAN_PATH)["plan"]["digest"] == frozen["plan"]["digest"]


def test_schema_still_validates_a_reconstructed_record(tmp_path: Path) -> None:
    result = asyncio.run(_run_pre_agent(tmp_path))
    assert result.evidence_dir is not None
    payload = json.loads((result.evidence_dir / "analysis.json").read_text("utf-8"))
    assert payload["schema_version"] == schema.SCHEMA_VERSION
    assert payload["execution_analysis_version"] == adapter.ANALYSIS_VERSION
    assert payload["degraded"]["not_applicable"]
