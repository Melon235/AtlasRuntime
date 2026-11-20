"""Stage B-E2A closure: offline counterexamples for the calibration gate.

Every run here is synthetic. A fake provider and a scratch evidence root are used; no test calls a
model, starts a sandbox, runs calibration, or executes a scored cell.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_benchmark import protocol
from experiments.stage_benchmark.execution import (
    adapter,
    admission,
    collector,
    offline,
    provision,
    runner,
)
from experiments.stage_benchmark.execution.models import (
    AttemptAlreadyStarted,
    CellIdentity,
    CellRequest,
    ExecutionBlocked,
)
from experiments.stage_benchmark.tasks import BY_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


class KeyErrorAfterModel(BaseException):
    """A post-processing fault, not a provider fault."""


def _document() -> dict[str, Any]:
    return protocol.read_protocol()


async def run_cell(
    root: Path,
    cell: CellIdentity,
    *,
    session_kwargs: dict[str, Any] | None = None,
    host_oracle: Any = "DEFAULT",
    sabotage: str | None = None,
) -> Any:
    kwargs = session_kwargs or {}
    if host_oracle == "DEFAULT":
        host_oracle = {
            "source": "fake:host:gate-test",
            "task_id": cell.task_id,
            "ok": kwargs.get("oracle", "PASS") == "PASS",
            "executed_after_main_stops": True,
        }

    async def scripted(options: Any) -> dict[str, Any]:
        payload = offline.synthetic_session(options, **kwargs)
        offline.write_synthetic_run(options, payload)
        if sabotage == "key_error":
            raise KeyErrorAfterModel("post-processing failed after a real provider call")
        return payload

    evidence_root = root / ("calibration" if not cell.in_matrix else "matrix")
    request = CellRequest(
        cell=cell,
        runs_root=root / "runs",
        evidence_root=evidence_root,
        protocol_document=_document(),
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
        run_fn=scripted,
        host_oracle=host_oracle,
    )
    return await runner.execute_cell(request)


def _view(result: Any) -> dict[str, Any]:
    assert result.evidence_dir is not None
    analysis = json.loads((result.evidence_dir / "analysis.json").read_text(encoding="utf-8"))
    return analysis["record"]["execution_view"]


# --------------------------------------------------------------------------- 1-3. entry


def test_calibration_entry_builds_the_right_cell() -> None:
    cell = provision.calibration_cell()
    assert cell.cell_id == "CALIBRATION.workgrid.c1"
    assert cell.arm_id == "P1"
    assert cell.in_matrix is False
    help_text = (
        __import__("subprocess")
        .run(
            [
                __import__("sys").executable,
                "-m",
                "experiments.stage_benchmark.execution.runner",
                "--help",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        .stdout
    )
    assert "--execute-calibration" in help_text
    assert "--recover-calibration" in help_text


def test_calibration_is_not_in_the_plan() -> None:
    document = _document()
    cells = {entry["cell_id"] for entry in document["plan"]["cells"]}
    assert len(cells) == 36
    assert provision.calibration_cell().cell_id not in cells
    assert provision.verify_calibration_identity() == []


def test_no_authorization_cannot_enter_live_path(tmp_path: Path) -> None:
    code = runner.main(["--execute-calibration", "--evidence-root", str(tmp_path)])
    assert code == 3
    cell = provision.calibration_cell()
    with pytest.raises(ExecutionBlocked, match="not authorized"):
        asyncio.run(
            runner.execute_cell(
                CellRequest(
                    cell=cell,
                    runs_root=tmp_path / "runs",
                    evidence_root=tmp_path / "evidence",
                    live=True,
                    calibration=True,
                    protocol_document=_document(),
                )
            )
        )
    assert not (tmp_path / "evidence").exists()


# --------------------------------------------------------------------------- 4-9. verdicts


async def test_router_direct_recovery_with_oracle_pass_is_not_a_goal_failure(
    tmp_path: Path,
) -> None:
    result = await run_cell(
        tmp_path,
        provision.cell_from_plan(_document(), "pager.P1.r1"),
        session_kwargs={"batches": (offline._batch("direct", 0),)},
    )
    assert result.failure_kind == "none"
    assert result.goal_correctness == "PASS"
    assert result.delegation_diagnostics.startswith("ROUTER_REJECTED")


async def test_child_failure_recovery_with_oracle_pass_is_not_a_goal_failure(
    tmp_path: Path,
) -> None:
    result = await run_cell(
        tmp_path,
        provision.cell_from_plan(_document(), "ledgerclose.P1.r1"),
        session_kwargs={"batches": (offline._batch("delegate_one", 1, success=False),)},
    )
    assert result.failure_kind == "none"
    assert result.goal_correctness == "PASS"
    assert "child_failure" in result.delegation_diagnostics


async def test_child_failure_with_oracle_fail_is_a_goal_failure(tmp_path: Path) -> None:
    result = await run_cell(
        tmp_path,
        provision.cell_from_plan(_document(), "ledgerclose.P1.r2"),
        session_kwargs={
            "batches": (offline._batch("delegate_one", 1, success=False),),
            "oracle": "FAIL",
            "acceptance": "FAIL",
            "status": "failed",
        },
    )
    assert result.failure_kind == "oracle_failed"
    assert result.goal_correctness == "FAIL"
    assert result.execution_integrity == "OK"


async def test_provider_success_with_host_oracle_fail_is_not_goal_pass(tmp_path: Path) -> None:
    result = await run_cell(
        tmp_path,
        provision.cell_from_plan(_document(), "cascade.P2.r1"),
        session_kwargs={"status": "succeeded", "acceptance": "PASS", "oracle": "PASS"},
        host_oracle={"source": "fake:host:adversarial", "ok": False},
    )
    assert result.failure_kind == "oracle_failed"
    assert result.goal_correctness == "FAIL"
    assert result.execution_integrity == "OK"


async def test_sandbox_and_host_oracle_disagreement_is_recorded(tmp_path: Path) -> None:
    result = await run_cell(
        tmp_path,
        provision.cell_from_plan(_document(), "cascade.P2.r2"),
        session_kwargs={"oracle": "PASS", "acceptance": "PASS"},
        host_oracle={"source": "fake:host:adversarial", "ok": False},
    )
    view = _view(result)
    assert view["goal_correctness"]["oracle_agreement"] == "ORACLE_DISAGREEMENT"
    assert any(
        "ORACLE_DISAGREEMENT" in gap
        for gap in json.loads((result.evidence_dir / "analysis.json").read_text(encoding="utf-8"))[
            "evidence_gaps"
        ]
    )


async def test_missing_host_oracle_is_unknown_not_a_substitute(tmp_path: Path) -> None:
    result = await run_cell(
        tmp_path,
        provision.cell_from_plan(_document(), "zonelint.P3.r2"),
        host_oracle=None,
    )
    assert result.failure_kind == "goal_unknown"
    assert result.goal_correctness == "UNKNOWN"
    oracle = json.loads((result.evidence_dir / "oracle.json").read_text(encoding="utf-8"))
    assert oracle["ok"] is None and oracle["source"] == "UNKNOWN"
    assert adapter.host_status(oracle) == ("UNKNOWN", "UNKNOWN")


# --------------------------------------------------------------------------- 10-16. evidence


async def test_direct_run_has_no_fake_router_gap(tmp_path: Path) -> None:
    result = await run_cell(tmp_path, provision.cell_from_plan(_document(), "slugkit.P2.r2"))
    decisions = json.loads((result.evidence_dir / "router_decisions.json").read_text("utf-8"))
    assert decisions["status"] == "NOT_APPLICABLE"
    analysis = json.loads((result.evidence_dir / "analysis.json").read_text("utf-8"))
    assert not any("router decision" in gap.lower() for gap in analysis["evidence_gaps"])


def test_scrub_does_not_change_raw_bytes(tmp_path: Path) -> None:
    cell = provision.cell_from_plan(_document(), "slugkit.P1.r1")
    bundle = admission.evidence_dir(admission.cell_dir(tmp_path / "evidence", cell))
    raw = bundle / collector.RAW_DIR
    raw.mkdir(parents=True)
    secret = BY_ID["slugkit"].oracle_source
    (raw / "session.json").write_text(json.dumps({"secret": secret}), encoding="utf-8")
    (bundle / "session.json").write_text(json.dumps({"secret": secret}), encoding="utf-8")
    before = (raw / "session.json").read_bytes()
    redactions = collector.scrub_evidence(bundle)
    assert "session.json" in redactions
    assert (raw / "session.json").read_bytes() == before
    assert secret not in (bundle / "session.json").read_text("utf-8")


async def test_recovery_never_modifies_raw_evidence(tmp_path: Path) -> None:
    result = await run_cell(tmp_path, provision.cell_from_plan(_document(), "pager.P1.r1"))
    assert result.evidence_dir is not None
    raw = result.evidence_dir / collector.RAW_DIR
    before = {
        str(path.relative_to(raw)): path.read_bytes() for path in raw.rglob("*") if path.is_file()
    }
    (result.evidence_dir / "analysis.json").unlink()
    recovered = collector.recover_evidence(
        result.evidence_dir,
        cell=result.cell,
        source_digest=admission.source_identity()["digest"],
    )
    after = {
        str(path.relative_to(raw)): path.read_bytes() for path in raw.rglob("*") if path.is_file()
    }
    assert recovered["ok"] is True
    assert recovered["raw_digests_unchanged"] is True
    assert recovered["analysis_version"] == adapter.ANALYSIS_VERSION
    assert before == after


async def test_duplicate_calibration_is_blocked_and_matrix_remains_clean(tmp_path: Path) -> None:
    cell = provision.calibration_cell()
    first = await run_cell(tmp_path, cell)
    assert first.ok
    with pytest.raises((ExecutionBlocked, AttemptAlreadyStarted)):
        await run_cell(tmp_path, cell)
    preflight = admission.preflight(
        evidence_root=tmp_path / "matrix",
        cell=provision.cell_from_plan(_document(), "slugkit.P1.r1"),
        require_offline_report=False,
    )
    assert preflight["status"] == "READY"


async def test_postprocessing_exception_never_gives_a_second_provider_call(tmp_path: Path) -> None:
    cell = provision.cell_from_plan(_document(), "cascade.P3.r1")
    result = await run_cell(tmp_path, cell, sabotage="key_error")
    assert result.failure_kind in {"provider_exception", "collector_failure"}
    marker = admission.attempt_path(admission.cell_dir(tmp_path / "matrix", cell))
    assert admission.read_json(marker)["retries_allowed"] is False
    with pytest.raises((ExecutionBlocked, AttemptAlreadyStarted)):
        await run_cell(tmp_path, cell)
