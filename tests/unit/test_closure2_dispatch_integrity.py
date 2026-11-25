"""Closure-2 S08: the post-cell dispatch integrity gate.

Offline only. The dispatch loop, the ledger and the record mapping are the production ones; the
executor, the provider and the sandbox are fakes, and every fake run writes a real
``result.json`` so the evidence gate is exercised instead of being stubbed away.

The question every test here asks is the same: after this kind of cell, does the plan start another
one? The approved answer is a short allow list, and everything else stops.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from experiments.evaluation.closure2 import pilot_runner, s08_canary
from experiments.evaluation.closure2.catalog import TASKS
from experiments.evaluation.closure2.pilot_ledger import PilotAlreadyClaimed, PilotLedger
from experiments.evaluation.closure2.pilot_runner import Cell, PilotPlan

P1 = "closure2-pipeline-migration"
CELLS = (Cell(P1, "A0"), Cell(P1, "A1-P"), Cell(P1, "A2-P"))
CEILING = 0.45


# ------------------------------------------------------------------------------------- fake results


def _run_dir(tmp_path: Path, name: str, *, result: Any = "auto") -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    if result == "auto":
        (directory / "result.json").write_text('{"probe": true}\n', encoding="utf-8")
    elif result == "directory":
        # A directory where the file should be: present, but not readable as a file.
        (directory / "result.json").mkdir(parents=True, exist_ok=True)
    return directory


def fake_result(tmp_path: Path, name: str, **overrides: Any) -> SimpleNamespace:
    oracle_passed = overrides.pop("oracle_passed", True)
    evidence = overrides.pop("evidence", "auto")
    raw_dir = overrides.pop("run_dir", None) or _run_dir(tmp_path, name, result=evidence)
    metrics = SimpleNamespace(
        prompt_tokens=overrides.pop("prompt_tokens", 1000),
        completion_tokens=overrides.pop("completion_tokens", 200),
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        estimated_cost_usd=overrides.pop("estimated_cost_usd", 0.01),
        model_turns=3,
        tool_calls=5,
        latency_ms=1000.0,
        sandbox_created=overrides.pop("sandbox_created", True),
        sandbox_destroyed=overrides.pop("sandbox_destroyed", True),
        worker_selection=["w1"],
        extra={},
    )
    identity = SimpleNamespace(
        run_id=f"run-{name}",
        repetition_id=0,
        model="deepseek-chat",
        git_commit="probe",
        git_dirty=False,
    )
    oracle = SimpleNamespace(
        passed=oracle_passed,
        environment_failure=overrides.pop("environment_failure", False),
        failure_kind=None,
    )
    return SimpleNamespace(
        identity=identity,
        metrics=metrics,
        oracle=oracle,
        environment={"sandbox_image": "atlas-uat-python:3.12", "workspace_isolation": "probe"},
        run_dir=str(raw_dir),
        agent_success=overrides.pop("agent_success", oracle_passed),
        success=overrides.pop("success", oracle_passed),
        failure_class=overrides.pop(
            "failure_class", "TASK_SUCCESS" if oracle_passed else "ORACLE_FAILURE"
        ),
        failure_reason=overrides.pop("failure_reason", None),
        deliverable_verified=oracle_passed,
    )


# ---------------------------------------------------------------------------------- the harness


def _approve(monkeypatch: pytest.MonkeyPatch, cells: tuple[Cell, ...], ceiling: float) -> PilotPlan:
    selected = tuple(cell.cell_id for cell in cells)
    plan = dataclasses.replace(
        pilot_runner.PILOT_PLAN,
        cells=selected,
        repetitions=(0,),
        max_cell_executions=len(selected),
        estimated_cost_ceiling_usd=ceiling,
    )
    monkeypatch.setattr(pilot_runner, "PILOT_PLAN", plan)
    monkeypatch.setattr(
        pilot_runner,
        "PILOT_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": "test",
            "approval_commit": "test",
            "approved_at": "2026-11-25T00:00:00+00:00",
        },
    )
    return plan


def _ledger(monkeypatch: pytest.MonkeyPatch, path: Path) -> Path:
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: PilotLedger(path))
    return path


def _run(
    cells: tuple[Cell, ...],
    *,
    runs_root: Path,
    executor: Any,
    plan_id: str = pilot_runner.PILOT_PLAN_ID,
) -> dict[str, Any]:
    return asyncio.run(
        pilot_runner.run_live_pilot(
            cells,
            runs_root=runs_root,
            repetition_id=0,
            executor=executor,
            live_intent=True,
            plan_id=plan_id,
        )
    )


def _rows(path: Path, plan_digest: str) -> dict[str, str]:
    return {row["cell_id"]: row["status"] for row in PilotLedger(path).cell_rows(plan_digest)}


# ------------------------------------------------------------------------------- the policy table


@pytest.mark.parametrize(
    "failure_class",
    ["TASK_SUCCESS", "ORACLE_FAILURE", "BUDGET_EXCEEDED"],
)
def test_a_task_outcome_does_not_block(failure_class: str) -> None:
    record = {
        "failure_class": failure_class,
        "sandbox_created": True,
        "sandbox_destroyed": True,
        "sandbox_creation_status": "SANDBOX_CREATED_CONFIRMED",
        "oracle_environment_failure": False,
        "estimated_cost_usd": 0.01,
        "estimated_cost_usd_state": "COMPLETE",
        "provider_token_usage_observed": True,
        "raw_evidence_state": "PRESENT",
    }
    block = pilot_runner._dispatch_integrity_block(record)
    assert block["blocked"] is False
    assert block["reasons"] == []


@pytest.mark.parametrize(
    ("failure_class", "code"),
    [
        ("CLEANUP_FAILURE", "SANDBOX_CLEANUP_UNCONFIRMED"),
        ("ENVIRONMENT_FAILURE", "ENVIRONMENT_FAILURE"),
        ("SANDBOX_FAILURE", "SANDBOX_FAILURE"),
        ("MODEL_FAILURE", "PROVIDER_FAILURE"),
        ("RUNTIME_FAILURE", "RUNTIME_FAILURE"),
        ("TIMEOUT", "EXECUTION_TIMEOUT"),
        ("HOST_ISOLATION_VIOLATION", "HOST_ISOLATION_VIOLATION"),
        ("FORBIDDEN_WORKSPACE_CHANGE", "WORKSPACE_POLICY_VIOLATION"),
        ("NOT_EVALUATED", "NOT_EVALUATED"),
        ("HARNESS_ERROR", "UNCLASSIFIED_FAILURE"),
        (None, "UNCLASSIFIED_FAILURE"),
    ],
)
def test_a_harness_failure_blocks_with_its_own_code(failure_class: str | None, code: str) -> None:
    record = {
        "failure_class": failure_class,
        "sandbox_created": True,
        "sandbox_destroyed": True,
        "sandbox_creation_status": "SANDBOX_CREATED_CONFIRMED",
        "oracle_environment_failure": False,
        "estimated_cost_usd": 0.01,
        "estimated_cost_usd_state": "COMPLETE",
        "provider_token_usage_observed": True,
        "raw_evidence_state": "PRESENT",
    }
    block = pilot_runner._dispatch_integrity_block(record)
    assert block["blocked"] is True
    assert block["reasons"][0] == code


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        (
            {"sandbox_created": None, "sandbox_creation_status": "SANDBOX_CREATION_UNKNOWN"},
            "SANDBOX_LIFECYCLE_UNKNOWN",
        ),
        ({"sandbox_created": True, "sandbox_destroyed": False}, "SANDBOX_CLEANUP_UNCONFIRMED"),
        ({"sandbox_created": True, "sandbox_destroyed": None}, "SANDBOX_CLEANUP_UNCONFIRMED"),
        ({"sandbox_created": False}, "SANDBOX_NOT_CREATED"),
        ({"oracle_environment_failure": True}, "ORACLE_ENVIRONMENT_FAILURE"),
        ({"estimated_cost_usd_state": "UNKNOWN", "estimated_cost_usd": None}, "COST_UNKNOWN"),
        ({"estimated_cost_usd_state": "INVALID"}, "COST_INVALID"),
        ({"provider_token_usage_observed": None}, "TOKEN_USAGE_UNKNOWN"),
        ({"provider_token_usage_observed": False}, "TOKEN_USAGE_UNKNOWN"),
        ({"raw_evidence_state": "MISSING"}, "EVIDENCE_MISSING"),
        ({"raw_evidence_state": "UNREADABLE"}, "EVIDENCE_UNREADABLE"),
        ({"raw_evidence_state": None}, "EVIDENCE_UNKNOWN"),
    ],
)
def test_each_lifecycle_gap_blocks_with_its_own_code(overrides: dict[str, Any], code: str) -> None:
    record = {
        "failure_class": "TASK_SUCCESS",
        "sandbox_created": True,
        "sandbox_destroyed": True,
        "sandbox_creation_status": "SANDBOX_CREATED_CONFIRMED",
        "oracle_environment_failure": False,
        "estimated_cost_usd": 0.01,
        "estimated_cost_usd_state": "COMPLETE",
        "provider_token_usage_observed": True,
        "raw_evidence_state": "PRESENT",
        **overrides,
    }
    block = pilot_runner._dispatch_integrity_block(record)
    assert block["blocked"] is True
    assert code in block["reasons"]


def test_a_budget_cell_without_observed_usage_is_allowed() -> None:
    """A cell stopped before spending anything reports no usage: complete, not missing."""

    record = {
        "failure_class": "BUDGET_EXCEEDED",
        "sandbox_created": True,
        "sandbox_destroyed": True,
        "sandbox_creation_status": "SANDBOX_CREATED_CONFIRMED",
        "oracle_environment_failure": False,
        "estimated_cost_usd": 0.0,
        "estimated_cost_usd_state": "COMPLETE",
        "provider_token_usage_observed": False,
        "raw_evidence_state": "PRESENT",
    }
    assert pilot_runner._dispatch_integrity_block(record)["blocked"] is False


# ------------------------------------------------------------------------------ the live loop


def test_a_cleanup_failure_stops_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    ledger_path = _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(
            tmp_path,
            cell.arm_id,
            oracle_passed=False,
            sandbox_destroyed=False,
            failure_class="CLEANUP_FAILURE",
            failure_reason="sandbox cleanup failed",
        )

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert calls == [CELLS[0].cell_id], "only the first cell may be dispatched"
    assert summary["cell_count"] == 1
    assert summary["stopped_reason"] == "SANDBOX_CLEANUP_UNCONFIRMED"
    assert summary["not_started_cells"] == [CELLS[1].cell_id, CELLS[2].cell_id]
    assert summary["execution_integrity_status"] == "BLOCKED_DISPATCH_INTEGRITY"
    assert summary["execution_integrity_ok"] is False
    assert summary["dispatch_integrity"]["cell_id"] == CELLS[0].cell_id
    assert summary["dispatch_integrity"]["is_last_cell"] is False
    assert summary["execution_claim"]["claim_status"] == "CONSUMED"
    rows = _rows(ledger_path, summary["pilot_plan_digest"])
    assert rows[CELLS[0].cell_id] == "COMPLETED", "the cell did run; the ledger says so"
    assert rows[CELLS[1].cell_id] == "CLAIMED"
    assert rows[CELLS[2].cell_id] == "CLAIMED"


def test_a_middle_cell_blocks_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        if cell.arm_id == "A1-P":
            return fake_result(
                tmp_path, "mid", sandbox_destroyed=False, failure_class="CLEANUP_FAILURE"
            )
        return fake_result(tmp_path, cell.arm_id)

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert calls == [CELLS[0].cell_id, CELLS[1].cell_id]
    assert summary["not_started_cells"] == [CELLS[2].cell_id]
    assert summary["dispatch_integrity"]["cell_id"] == CELLS[1].cell_id


def test_the_last_cell_still_marks_the_run_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing follows the last cell, so the summary itself has to carry the defect."""

    _approve(monkeypatch, CELLS, CEILING)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        if cell.arm_id == "A2-P":
            return fake_result(
                tmp_path, "last", failure_class="CLEANUP_FAILURE", sandbox_destroyed=False
            )
        return fake_result(tmp_path, cell.arm_id)

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert calls == [cell.cell_id for cell in CELLS], "the last cell did run"
    assert summary["cell_count"] == 3
    assert summary["stopped_reason"] == "SANDBOX_CLEANUP_UNCONFIRMED"
    assert summary["not_started_cells"] == []
    assert summary["execution_integrity_status"] == "BLOCKED_DISPATCH_INTEGRITY"
    assert summary["dispatch_integrity"]["is_last_cell"] is True


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"sandbox_created": None}, "SANDBOX_LIFECYCLE_UNKNOWN"),
        ({"sandbox_created": False}, "SANDBOX_NOT_CREATED"),
        ({"environment_failure": True}, "ORACLE_ENVIRONMENT_FAILURE"),
        ({"failure_class": "HOST_ISOLATION_VIOLATION"}, "HOST_ISOLATION_VIOLATION"),
        ({"failure_class": "TIMEOUT"}, "EXECUTION_TIMEOUT"),
        ({"failure_class": "MODEL_FAILURE"}, "PROVIDER_FAILURE"),
        ({"prompt_tokens": None, "completion_tokens": None}, "TOKEN_USAGE_UNKNOWN"),
        ({"evidence": "missing"}, "EVIDENCE_MISSING"),
        ({"evidence": "directory"}, "EVIDENCE_UNREADABLE"),
    ],
)
def test_each_gap_stops_before_the_second_cell(
    overrides: dict[str, Any], code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    ledger_path = _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(tmp_path, cell.arm_id, **overrides)

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert len(calls) == 1, f"{code} must stop before cell 2"
    assert summary["stopped_reason"] == code
    rows = _rows(ledger_path, summary["pilot_plan_digest"])
    assert rows[CELLS[1].cell_id] == "CLAIMED"
    assert rows[CELLS[2].cell_id] == "CLAIMED"


def test_a_normal_oracle_failure_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(tmp_path, cell.arm_id, oracle_passed=False)

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert calls == [cell.cell_id for cell in CELLS]
    assert summary["stopped_reason"] is None
    assert summary["execution_integrity_status"] == "COMPLETE"
    assert [record["failure_class"] for record in summary["cells"]] == ["ORACLE_FAILURE"] * 3
    assert [record["oracle_passed"] for record in summary["cells"]] == [False] * 3


def test_a_measured_budget_exhaustion_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(
            tmp_path, cell.arm_id, oracle_passed=False, failure_class="BUDGET_EXCEEDED"
        )

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert len(calls) == 3
    assert summary["stopped_reason"] is None
    assert summary["execution_integrity_status"] == "COMPLETE"


def test_the_cost_ceiling_still_stops_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integrity gate is added beside the cost gate, never instead of it."""

    _approve(monkeypatch, CELLS, CEILING)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(tmp_path, cell.arm_id, estimated_cost_usd=0.30)

    summary = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)

    assert len(calls) == 2
    assert summary["stopped_reason"] == "COST_CEILING_EXCEEDED"
    assert summary["execution_integrity_status"] == "COMPLETE", (
        "a cost stop is not an integrity stop"
    )
    assert summary["cost_ceiling_breached"] is True


def test_a_consumed_plan_cannot_be_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    ledger_path = _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(tmp_path, cell.arm_id)

    first = _run(CELLS, runs_root=tmp_path / "runs", executor=executor)
    assert len(calls) == 3
    with pytest.raises(PilotAlreadyClaimed):
        _run(CELLS, runs_root=tmp_path / "runs", executor=executor)
    assert len(calls) == 3
    rows = _rows(ledger_path, first["pilot_plan_digest"])
    assert set(rows.values()) == {"COMPLETED"}


# ------------------------------------------------------------------- the approved canary shape


def _canary_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approve revision 2 in memory only, so the real seven-cell plan can be driven offline."""

    from experiments.evaluation.closure2 import s08_canary_revision

    plan = s08_canary_revision.canary_v2_plan()
    monkeypatch.setattr(
        s08_canary_revision,
        "CANARY_V2_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": "test (in memory)",
            "approval_commit": "test",
            "approved_at": "2026-11-25T00:00:00+00:00",
        },
    )


def test_the_canary_shape_dispatches_all_seven_when_every_cell_is_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must not over-block: a healthy sweep of the real scope still runs end to end."""

    _canary_approval(monkeypatch)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")
    cells = pilot_runner.plan_cells_for("closure2-s08-canary-2")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        return fake_result(tmp_path, cell.arm_id.replace("-", ""), estimated_cost_usd=0.001)

    summary = _run(
        cells, runs_root=tmp_path / "runs", executor=executor, plan_id="closure2-s08-canary-2"
    )

    assert calls == [cell.cell_id for cell in cells]
    assert summary["cell_count"] == 7
    assert summary["stopped_reason"] is None
    assert summary["not_started_cells"] == []
    assert summary["execution_integrity_status"] == "COMPLETE"


@pytest.mark.parametrize("blocked_index", [0, 3, 6])
def test_the_canary_shape_stops_at_the_kth_cell(
    blocked_index: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First, middle and last cell of the approved plan: k+1 never reaches the executor."""

    _canary_approval(monkeypatch)
    ledger_path = _ledger(monkeypatch, tmp_path / f"ledger-{blocked_index}.sqlite")
    cells = pilot_runner.plan_cells_for("closure2-s08-canary-2")
    calls: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell.cell_id)
        if len(calls) == blocked_index + 1:
            return fake_result(
                tmp_path,
                f"blocked-{blocked_index}",
                failure_class="CLEANUP_FAILURE",
                sandbox_destroyed=False,
            )
        return fake_result(tmp_path, f"ok-{len(calls)}", estimated_cost_usd=0.001)

    summary = _run(
        cells, runs_root=tmp_path / "runs", executor=executor, plan_id="closure2-s08-canary-2"
    )

    assert len(calls) == blocked_index + 1
    assert summary["cell_count"] == blocked_index + 1
    assert summary["stopped_reason"] == "SANDBOX_CLEANUP_UNCONFIRMED"
    assert summary["not_started_cells"] == [cell.cell_id for cell in cells[blocked_index + 1 :]]
    assert summary["execution_integrity_status"] == "BLOCKED_DISPATCH_INTEGRITY"
    assert summary["execution_claim"]["claim_status"] == "CONSUMED"
    rows = _rows(ledger_path, summary["pilot_plan_digest"])
    for cell in cells[blocked_index + 1 :]:
        assert rows[cell.cell_id] == "CLAIMED"


def test_the_cli_reports_a_blocked_dispatch_as_a_non_success_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(monkeypatch, CELLS, CEILING)
    _ledger(monkeypatch, tmp_path / "ledger.sqlite")

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        return fake_result(
            tmp_path, "cli", failure_class="CLEANUP_FAILURE", sandbox_destroyed=False
        )

    # Only the executor is substituted: the CLI, the gate, the ledger and summary are production.
    monkeypatch.setattr(pilot_runner, "_default_executor", executor)
    exit_code = pilot_runner.main(
        [
            "--plan",
            pilot_runner.PILOT_PLAN_ID,
            "--live",
            "--confirm-live",
            "--cells",
            ",".join(cell.cell_id for cell in CELLS),
            # ``--out`` is a file path: the plan-scoped summary is written beside it.
            "--out",
            str(tmp_path / "run.json"),
        ]
    )

    assert exit_code == 4, "a blocked dispatch is not a successful pilot"
    summary = json.loads((tmp_path / "pilot-live-summary.json").read_text(encoding="utf-8"))
    assert summary["dispatch_integrity"]["reasons"] == ["SANDBOX_CLEANUP_UNCONFIRMED"]


def test_the_canary_cells_are_the_frozen_ones() -> None:
    """A cheap guard against the revision quietly changing the experiment."""

    assert pilot_runner.plan_cells_for("closure2-s08-canary-2") == s08_canary.canary_cells()
    assert [cell.benchmark_task().sample_id for cell in s08_canary.canary_cells()] == [
        cell.task_id for cell in s08_canary.canary_cells()
    ]
    assert {cell.task_id for cell in s08_canary.canary_cells()} <= {task.task_id for task in TASKS}
