"""Closure-2B-0: the dry-run entry point, and the two refusals that guard a live run.

The dry run is executed for real (in-process and through ``python -m``); the live path is never
executed here. What is proved about live is structural: no sandbox provider is constructed, no
provider adapter is built, and the executor is not reached while the approval is pending.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from experiments.evaluation.closure2 import pilot_runner, protocol
from experiments.evaluation.closure2.pilot_ledger import PilotLedger
from experiments.evaluation.closure2.pilot_runner import (
    Cell,
    build_dry_run_manifest,
    run_live_pilot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_DRY_RUN = REPO_ROOT / "experiments" / "results" / "closure2" / "pilot-dry-run.json"

APPROVED = {
    "status": "APPROVED",
    "approved_digest": protocol.C2A_R1_PREREGISTRATION_DIGEST,
    "approved_by": "integration-test",
    "approval_commit": "0" * 40,
    "approved_at": "2026-11-17T00:00:00+00:00",
}


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return build_dry_run_manifest()


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never let a test touch the per-user ledger, which is permanent once written."""

    path = tmp_path / "pilot-ledger.sqlite"
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: PilotLedger(path))
    return path


def authorize(monkeypatch: pytest.MonkeyPatch, cells: tuple[Cell, ...]) -> str:
    """Approve both layers and return the plan digest, without touching the real records."""

    import dataclasses

    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED))
    plan = dataclasses.replace(
        pilot_runner.PILOT_PLAN,
        cells=tuple(cell.cell_id for cell in cells),
        repetitions=(0,),
        max_cell_executions=len(cells),
        estimated_cost_ceiling_usd=5.0,
    )
    monkeypatch.setattr(pilot_runner, "PILOT_PLAN", plan)
    monkeypatch.setattr(
        pilot_runner,
        "PILOT_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": "integration-test",
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-17T00:00:00+00:00",
        },
    )
    return plan.digest()


def test_the_dry_run_manifest_is_complete(manifest: dict[str, Any]) -> None:
    assert manifest["mode"] == "DRY_RUN"
    assert manifest["evidence_schema_version"] == pilot_runner.EVIDENCE_SCHEMA_VERSION
    assert manifest["cell_count"] == 18
    assert manifest["contract_ok"] is True
    # The approval is an owner decision, so it is compared with the live record rather than a
    # constant: what must hold is that the manifest reports the gate it was built under.
    assert manifest["execution_ready"] == protocol.approval_gate()["execution_ready"]
    assert manifest["approval"]["status"] == protocol.APPROVAL_RECORD["status"]
    assert {cell["arm_id"] for cell in manifest["cells"]} == set(pilot_runner.ARM_ORDER)
    assert len({cell["task_id"] for cell in manifest["cells"]}) == 3


def test_the_dry_run_reports_the_pilot_scope_it_does_not_authorize(
    manifest: dict[str, Any],
) -> None:
    """The plan identity is visible before execution; its approval is not granted by this stage."""

    plan = manifest["pilot_plan"]
    assert plan["plan_id"] == "closure2-pilot-1"
    assert plan["plan_digest"] == pilot_runner.PILOT_PLAN.digest()
    assert plan["approved_plan_digest"] == pilot_runner.PILOT_APPROVAL["approved_plan_digest"]
    assert plan["approval_status"] == pilot_runner.PILOT_APPROVAL["status"]
    assert (
        plan["execution_ready"]
        == pilot_runner.pilot_authorization_gate(cells=pilot_runner.pilot_cells(), repetition_id=0)[
            "execution_ready"
        ]
    )
    assert plan["cells"] == list(pilot_runner.PILOT_PLAN.cells)
    assert len(plan["cells"]) == 8
    assert plan["repetitions"] == [0]
    assert plan["max_cell_executions"] == 8
    # The ceiling is an owner decision frozen into the plan identity, not a constant of planning.
    assert plan["estimated_cost_ceiling_usd"] == pilot_runner.PILOT_PLAN.estimated_cost_ceiling_usd
    assert plan["cost_policy"]["hard_enforcement"] is False
    assert manifest["pilot_execution_ready"] == plan["execution_ready"]


def test_the_dry_run_binds_the_native_config(manifest: dict[str, Any]) -> None:
    assert manifest["native_config_digest"] == pilot_runner.FROZEN_NATIVE_CONFIG_DIGEST
    assert manifest["frozen_native_config_digest"] == pilot_runner.FROZEN_NATIVE_CONFIG_DIGEST


def test_a_dry_run_builds_no_sandbox_and_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strongest available offline proof: constructing either one raises."""

    from atlasos.sandbox import provider as sandbox_provider

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the dry run must not construct a sandbox provider")

    monkeypatch.setattr(sandbox_provider, "build_sandbox_provider", forbidden)
    monkeypatch.setattr(pilot_runner, "_default_executor", forbidden, raising=False)
    manifest = build_dry_run_manifest()
    assert manifest["cell_count"] == 18
    assert manifest["agent_sandbox_runs_total"] == 0
    assert manifest["agent_sandbox_runs_complete"] is True


def test_the_dry_run_is_reproducible(tmp_path: Path) -> None:
    first = build_dry_run_manifest()
    second = build_dry_run_manifest()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_the_cli_writes_the_manifest(tmp_path: Path) -> None:
    out = tmp_path / "pilot-dry-run.json"
    exit_code = pilot_runner.main(["--dry-run", "--out", str(out), "--runs-root", str(tmp_path)])
    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "DRY_RUN"
    assert payload["cell_count"] == 18
    assert payload["contains_live_outcomes"] is False
    assert payload["agent_sandbox_runs_total"] == 0
    assert payload["pilot_execution_ready"] is False
    assert str(pilot_runner.REPO_ROOT) not in out.read_text(encoding="utf-8")


def test_the_module_entry_point_runs(tmp_path: Path) -> None:
    out = tmp_path / "manifest.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.evaluation.closure2.pilot_runner",
            "--dry-run",
            "--out",
            str(out),
            "--runs-root",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["cell_count"] == 18
    # A separate process must reproduce the same gate verdict, whatever it currently is.
    assert payload["execution_ready"] == protocol.approval_gate()["execution_ready"]


def test_the_cli_refuses_live_without_the_second_confirmation(tmp_path: Path) -> None:
    out = tmp_path / "never.json"
    exit_code = pilot_runner.main(["--live", "--out", str(out), "--runs-root", str(tmp_path)])
    assert exit_code == 2
    assert not out.exists()


PENDING = {
    "status": "PENDING_OWNER_APPROVAL",
    "approved_digest": None,
    "approved_by": None,
    "approval_commit": None,
    "approved_at": None,
}


def _forbid_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a real engine call impossible even if a gate were to fail open."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused live run must never reach the engine")

    monkeypatch.setattr(pilot_runner, "_default_executor", forbidden, raising=False)


def test_the_cli_refuses_live_while_the_approval_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending is installed explicitly: the live default is an owner decision, not a fixture."""

    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(PENDING))
    _forbid_the_engine(monkeypatch)
    out = tmp_path / "never.json"
    exit_code = pilot_runner.main(
        ["--live", "--confirm-live", "--out", str(out), "--runs-root", str(tmp_path)]
    )
    assert exit_code == 3
    assert not out.exists()


def test_a_live_run_never_reaches_the_executor_while_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(PENDING))
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        raise AssertionError("unreachable")

    with pytest.raises(PermissionError):
        asyncio.run(
            run_live_pilot(
                (Cell("closure2-pipeline-migration", "A0"),),
                runs_root=tmp_path,
                executor=executor,
                live_intent=True,
            )
        )
    assert calls == []


def test_the_cli_reports_a_consumed_claim_as_its_own_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    """A consumed plan is refused with a ledger code, not lumped into a harness failure."""

    cell = Cell("closure2-pipeline-migration", "A0")
    digest = authorize(monkeypatch, (cell,))
    PilotLedger(isolated_ledger).claim_plan(
        plan_digest=digest,
        plan_id=pilot_runner.PILOT_PLAN.plan_id,
        cells=[(cell.cell_id, 0)],
        runner_revision="integration-test",
    )

    out = tmp_path / "never.json"
    exit_code = pilot_runner.main(
        ["--live", "--confirm-live", "--out", str(out), "--runs-root", str(tmp_path)]
    )

    assert exit_code == 3
    assert not (tmp_path / "pilot-live-summary.json").exists()


def test_a_live_run_refuses_a_plan_that_was_already_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    from experiments.evaluation.closure2.pilot_ledger import PilotAlreadyClaimed

    cell = Cell("closure2-pipeline-migration", "A0")
    digest = authorize(monkeypatch, (cell,))
    PilotLedger(isolated_ledger).claim_plan(
        plan_digest=digest,
        plan_id=pilot_runner.PILOT_PLAN.plan_id,
        cells=[(cell.cell_id, 0)],
        runner_revision="integration-test",
    )
    calls: list[Any] = []

    async def executor(cell_: Cell, **kwargs: Any) -> Any:
        calls.append(cell_)
        raise AssertionError("unreachable")

    with pytest.raises(PilotAlreadyClaimed):
        asyncio.run(
            run_live_pilot((cell,), runs_root=tmp_path, executor=executor, live_intent=True)
        )
    assert calls == []


#: Fields that record an owner decision or a generation time. They move when a scope is approved,
#: while the committed artifact is frozen pre-approval evidence and is deliberately not rewritten.
_STATE_FIELDS = (
    "generated_at",
    "approval",
    "execution_ready",
    "pilot_execution_ready",
    "pilot_plan",
)


def _frozen_view(payload: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in payload.items() if key not in _STATE_FIELDS}
    view["cells"] = [
        {key: value for key, value in cell.items() if key != "approval_status"}
        for cell in payload["cells"]
    ]
    return view


def test_the_committed_artifact_matches_a_fresh_dry_run(manifest: dict[str, Any]) -> None:
    """The committed manifest is evidence: a stale copy must fail rather than drift silently.

    The pilot scope block is excluded because approving the USD 2.00 ceiling moved that digest to
    ``0f61f84a…``; the artifact keeps the scope it was generated under and is not regenerated.
    """

    assert COMMITTED_DRY_RUN.is_file(), "the dry-run artifact is part of the evidence"
    committed = json.loads(COMMITTED_DRY_RUN.read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(manifest))
    assert _frozen_view(committed) == _frozen_view(fresh)
    assert fresh["pilot_plan"]["plan_digest"] == pilot_runner.PILOT_PLAN.digest()
