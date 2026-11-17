"""Closure-2B-0: the pilot runner's arm adapter, contract checks, gate and evidence mapping.

Everything here is offline. No sandbox is created, no provider is built, no API key is read: the
executor is always a stub, and the only "approval" that is ever granted is a dictionary passed in by
a test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from experiments.evaluation.closure2 import pilot_runner, protocol
from experiments.evaluation.closure2.catalog import (
    BENCHMARK_ID,
    TASKS,
    Topology,
    build_task,
)
from experiments.evaluation.closure2.pilot_ledger import (
    PilotAlreadyClaimed,
    PilotLedger,
    PilotLedgerStateInvalid,
)
from experiments.evaluation.closure2.pilot_runner import (
    ARM_ORDER,
    ARM_SPECS,
    FROZEN_MAX_TOKENS_PER_REQUEST,
    FROZEN_NATIVE_CONFIG_DIGEST,
    PILOT_PLAN,
    PROPOSED_PILOT_CELLS,
    ArmSpec,
    Cell,
    PilotContractError,
    PilotNotAuthorized,
    PilotPlan,
    _cost_dispatch_state,
    build_dry_run_manifest,
    cell_descriptor,
    check_frozen_contract,
    cost_policy,
    effective_model,
    frozen_budget,
    native_config_digest,
    normalize_result,
    pilot_authorization_gate,
    pilot_cells,
    plan_cells,
    require_pilot_authorization,
    route_expectation,
    run_live_pilot,
    settings_for,
)
from experiments.evaluation.closure2.protocol import (
    C2A_R1_PREREGISTRATION_DIGEST,
    approval_gate,
)
from experiments.evaluation.models import effective_public_task_digest

P1 = "closure2-pipeline-migration"
P2 = "closure2-plugin-migration"
N1 = "closure2-state-machine-refactor"

APPROVED = {
    "status": "APPROVED",
    "approved_digest": C2A_R1_PREREGISTRATION_DIGEST,
    "approved_by": "unit-test",
    "approval_commit": "0" * 40,
    "approved_at": "2026-11-17T00:00:00+00:00",
}


def describe(task_id: str, arm_id: str) -> dict[str, Any]:
    return cell_descriptor(Cell(task_id, arm_id), runs_root=Path("experiments/runs/closure2"))


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own ledger file.

    The production ledger lives in per-user state and is consumed permanently, so a test that
    used it would break the next run — and would write to the developer's machine.
    """

    path = tmp_path / "pilot-ledger.sqlite"
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: PilotLedger(path))
    return path


def approve_benchmark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer A: approve the frozen benchmark definition."""

    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED))


def approve_pilot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cells: tuple[Cell, ...] | None = None,
    ceiling: float | None = 5.0,
    repetitions: tuple[int, ...] = (0,),
) -> PilotPlan:
    """Layer B: authorize exactly ``cells`` under a ceiling, by replacing the controlled record.

    The production entry point reads the module-level record; it takes no approval argument, so a
    caller cannot forge one. A test can only substitute the record itself.
    """

    selected = tuple(cell.cell_id for cell in cells) if cells is not None else PILOT_PLAN.cells
    plan = dataclasses.replace(
        PILOT_PLAN,
        cells=selected,
        repetitions=repetitions,
        max_cell_executions=len(selected) * len(repetitions),
        estimated_cost_ceiling_usd=ceiling,
    )
    monkeypatch.setattr(pilot_runner, "PILOT_PLAN", plan)
    monkeypatch.setattr(
        pilot_runner,
        "PILOT_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": "unit-test",
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-17T00:00:00+00:00",
        },
    )
    return plan


# ------------------------------------------------------------------ A. the manifest


def test_the_plan_is_exactly_eighteen_frozen_cells() -> None:
    cells = plan_cells()
    assert len(cells) == 18
    assert len(set(cells)) == 18
    assert {(cell.task_id, cell.arm_id) for cell in cells} == {
        (task_id, arm_id) for task_id in (P1, P2, N1) for arm_id in ARM_ORDER
    }


def test_the_plan_uses_only_frozen_tasks_and_arms() -> None:
    frozen_tasks = {task.task_id for task in TASKS}
    assert {cell.task_id for cell in plan_cells()} == frozen_tasks
    assert {cell.arm_id for cell in plan_cells()} == set(ARM_ORDER)
    assert tuple(ARM_SPECS) == ARM_ORDER


def test_the_proposed_live_pilot_is_eight_cells() -> None:
    """The proposal is data, not a result: eight cells, one repetition, not yet approved."""

    assert len(PROPOSED_PILOT_CELLS) == 8
    assert len(pilot_cells()) == 8
    assert (P1, "A2-P") in PROPOSED_PILOT_CELLS
    assert (N1, "A2-P") in PROPOSED_PILOT_CELLS
    assert sum(1 for task_id, _ in PROPOSED_PILOT_CELLS if task_id == P1) == 6


def test_the_dry_run_manifest_claims_no_execution() -> None:
    manifest = build_dry_run_manifest()
    assert manifest["mode"] == "DRY_RUN"
    assert manifest["model_calls"] == 0
    assert manifest["agent_sandbox_runs_total"] == 0
    assert manifest["contains_live_outcomes"] is False
    assert manifest["route_expectation_is_prediction"] is True
    assert manifest["cell_count"] == 18
    assert manifest["contract_ok"] is True
    assert manifest["contract_problems"] == []
    assert manifest["pilot_plan"]["approval_status"] == "PENDING_OWNER_APPROVAL"
    assert manifest["pilot_plan"]["execution_ready"] is False
    assert manifest["pilot_execution_ready"] is False
    assert manifest["pilot_plan"]["estimated_cost_ceiling_usd"] is None
    assert manifest["pilot_plan"]["cost_policy"]["valid"] is False


def test_every_cell_carries_its_frozen_identity() -> None:
    manifest = build_dry_run_manifest()
    assert manifest["preregistration_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    for cell in manifest["cells"]:
        assert cell["preregistration_digest"] == C2A_R1_PREREGISTRATION_DIGEST
        assert cell["benchmark_id"] == BENCHMARK_ID
        assert cell["arm_config_digest"] == protocol.arm_config_digest(cell["arm_id"])
        assert cell["config_semantic_digest"] == protocol.config_semantic_digest(cell["config"])
        assert cell["execution_allowed"] is False
        assert cell["approval_status"] == "PENDING_OWNER_APPROVAL"


def test_the_effective_task_digest_matches_the_topology() -> None:
    for task_id in (P1, P2, N1):
        source = next(task for task in TASKS if task.task_id == task_id)
        for arm_id in ARM_ORDER:
            topology = cast("Topology", ARM_SPECS[arm_id].topology)
            expected = effective_public_task_digest(build_task(source, topology=topology))
            assert describe(task_id, arm_id)["effective_public_task_digest"] == expected


def test_same_topology_arms_share_the_effective_digest() -> None:
    for task_id in (P1, P2, N1):
        assert (
            describe(task_id, "N0")["effective_public_task_digest"]
            == describe(task_id, "A0")["effective_public_task_digest"]
        )
        multi = {
            describe(task_id, arm)["effective_public_task_digest"] for arm in ("N1", "A1-L", "A1-P")
        }
        assert len(multi) == 1


def test_the_layout_pair_differs_only_in_layout_related_fields() -> None:
    left = describe(P1, "A1-L")
    right = describe(P1, "A1-P")
    differing = {key for key in left if left[key] != right[key]}
    assert differing == {
        "arm_id",
        "cell_id",
        "arm_config_digest",
        "config",
        "config_semantic_digest",
        "prompt_layout",
        "settings_digest",
    }
    assert (left["prompt_layout"], right["prompt_layout"]) == ("legacy", "shared_prefix")


def test_a2p_expects_admission_on_p1_p2_and_refusal_on_n1() -> None:
    for task_id in (P1, P2):
        expectation = route_expectation(Cell(task_id, "A2-P"))
        assert expectation["applicable"] is True
        assert expectation["route"] == "delegate_parallel"
        assert expectation["reason_codes"] == ["DELEGATION_ADMITTED"]
        assert expectation["executed_worker_count"] == 3
        assert expectation["executed_topology"] == "multi"
    refusal = route_expectation(Cell(N1, "A2-P"))
    assert refusal["route"] == "direct"
    assert refusal["reason_codes"] == ["SHARED_WRITE_RISK", "UNSTABLE_INTERFACE"]
    assert refusal["blocked_gates"] == ["GATE_2_DEPENDENCY_COUPLING"]
    assert refusal["executed_worker_count"] == 1
    assert refusal["executed_topology"] == "single_worker_direct"


def test_a_non_selective_arm_has_no_route_expectation() -> None:
    for arm_id in ("N0", "N1", "A0", "A1-L", "A1-P"):
        expectation = route_expectation(Cell(P1, arm_id))
        assert expectation["applicable"] is False
        assert expectation["route"] is None


def test_the_manifest_contains_no_host_path() -> None:
    text = json.dumps(build_dry_run_manifest())
    assert str(pilot_runner.REPO_ROOT) not in text
    assert "\\\\Users\\\\" not in text


# ------------------------------------------------------------------ B. the budget mapping


def test_the_frozen_budget_is_the_registered_one() -> None:
    single = frozen_budget("single")
    assert (single["max_turns"], single["token_budget"]) == (36, 260_000)
    assert (single["max_tool_calls"], single["timeout_seconds"]) == (90, 1800)
    multi = frozen_budget("multi")
    assert (multi["max_turns_per_worker"], multi["workers"]) == (12, 3)
    assert multi["max_turns_per_worker"] * multi["workers"] == single["max_turns"]
    assert (multi["token_budget"], multi["max_tool_calls"]) == (260_000, 90)
    assert multi["timeout_seconds"] == single["timeout_seconds"] == 1800


def test_the_settings_override_every_unsafe_runner_default() -> None:
    """``RunnerSettings`` defaults are 14 turns / 120k / 40 calls / 900 s / 4096 tokens."""

    runs = Path("experiments/runs/closure2")
    single = settings_for(Cell(P1, "N0"), runs_root=runs)
    multi = settings_for(Cell(P1, "A1-P"), runs_root=runs)
    assert (single.max_turns, single.token_budget, single.max_tool_calls) == (36, 260_000, 90)
    assert (multi.max_turns, multi.token_budget, multi.max_tool_calls) == (12, 260_000, 90)
    for settings in (single, multi):
        assert settings.timeout_seconds == 1800
        assert settings.max_tokens == FROZEN_MAX_TOKENS_PER_REQUEST == 8000
        assert settings.temperature == 0.0
        assert settings.oracle_mode == "sandbox"


def test_the_budget_semantics_are_recorded_per_engine() -> None:
    assert settings_for(Cell(P1, "N0"), runs_root=Path("x")).budget_mode == "legacy"
    assert settings_for(Cell(P1, "A0"), runs_root=Path("x")).budget_mode == "guarded"
    assert describe(P1, "N0")["budget_semantics"] == "post_hoc_after_run"
    assert describe(P1, "A1-P")["budget_semantics"] == "guarded_before_send"


def test_the_settings_digest_does_not_depend_on_the_checkout_root(tmp_path: Path) -> None:
    first = settings_for(Cell(P1, "A1-P"), runs_root=tmp_path / "a")
    second = settings_for(Cell(P1, "A1-P"), runs_root=tmp_path / "b")
    assert first.digest() == second.digest()


def test_a_drifted_budget_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(protocol.BUDGET["single"], "token_budget", 280_000)
    with pytest.raises(PilotContractError, match="frozen budget drifted"):
        frozen_budget("single")
    problems = check_frozen_contract()
    assert any("token_budget" in problem for problem in problems)


def test_a_drifted_shared_contract_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(protocol.BUDGET["shared"], "sandbox_image", "python:3.11")
    problems = check_frozen_contract()
    assert any("sandbox_image" in problem for problem in problems)


def test_the_frozen_contract_is_clean_on_the_frozen_repository() -> None:
    assert check_frozen_contract() == []


# ------------------------------------------------------------------ C. approval and live safety


def _recording_executor(sink: list[Any]) -> Any:
    async def executor(cell: Cell, **kwargs: Any) -> Any:
        sink.append((cell, kwargs))
        return _fake_result()

    return executor


def test_live_without_explicit_intent_is_refused(tmp_path: Path) -> None:
    calls: list[Any] = []
    with pytest.raises(pilot_runner.LiveExecutionBlocked, match="LIVE_INTENT_NOT_CONFIRMED"):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=False,
            )
        )
    assert calls == []


def test_live_with_a_pending_approval_is_refused_before_any_executor(tmp_path: Path) -> None:
    calls: list[Any] = []
    with pytest.raises(PermissionError):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


@pytest.mark.parametrize(
    "record",
    [
        {**APPROVED, "approved_digest": "0" * 64},
        {**APPROVED, "approved_by": None},
        {**APPROVED, "approval_commit": None},
        {**APPROVED, "approved_at": None},
        {**APPROVED, "status": "PENDING_OWNER_APPROVAL"},
    ],
    ids=["wrong-digest", "no-approver", "no-commit", "no-timestamp", "pending"],
)
def test_live_is_refused_for_any_incomplete_approval(
    record: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "APPROVAL_RECORD", record)
    calls: list[Any] = []
    with pytest.raises(PermissionError):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_a_drifted_budget_makes_the_recorded_approval_void(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget change moves the digest, so the approval stops naming the frozen definition."""

    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED))
    monkeypatch.setitem(protocol.BUDGET["multi"], "max_tool_calls", 120)
    calls: list[Any] = []
    with pytest.raises(PermissionError, match="frozen definition moved"):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A1-P"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_the_contract_check_blocks_a_run_whose_gate_was_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the approval gate says yes, a drifted budget must stop the pilot on its own."""

    monkeypatch.setattr(
        pilot_runner,
        "require_execution_approval",
        lambda **kwargs: {"status": "APPROVED", "approved_digest": "x", "execution_ready": True},
    )
    monkeypatch.setattr(
        pilot_runner,
        "require_pilot_authorization",
        lambda **kwargs: {"plan_id": "simulated", "plan_digest": "x"},
    )
    monkeypatch.setitem(protocol.BUDGET["multi"], "max_tool_calls", 120)
    calls: list[Any] = []
    with pytest.raises(PilotContractError, match="frozen contract violated"):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A1-P"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_an_approved_live_run_reaches_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    plan = approve_pilot(monkeypatch, cells=(Cell(P1, "A0"), Cell(P1, "A1-P")))
    calls: list[Any] = []
    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"), Cell(P1, "A1-P")),
            runs_root=tmp_path,
            executor=_recording_executor(calls),
            live_intent=True,
        )
    )
    assert len(calls) == 2
    assert summary["mode"] == "LIVE"
    assert summary["cell_count"] == 2
    assert summary["pilot_plan_digest"] == plan.digest()
    assert summary["cost_ceiling_usd"] == 5.0
    assert summary["cost_ceiling_guarantee"] == "COST_CEILING_NOT_HARD_ENFORCED"
    assert [record["status"] for record in summary["cells"]] == ["COMPLETED", "COMPLETED"]


def test_a_failing_cell_is_recorded_and_never_reads_as_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raised cell is data, not a crash — but it also makes the spend unmeasurable.

    The cost policy is fail-closed, so dispatch stops: an erased cost could hide any spend.
    """

    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "N0"), Cell(P1, "A0")))

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        raise RuntimeError("sandbox never came up")

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "N0"), Cell(P1, "A0")),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    broken = summary["cells"][0]
    assert broken["status"] == "EXECUTION_ERROR"
    assert broken["failure_class"] == "HARNESS_ERROR"
    assert "sandbox never came up" in broken["failure_reason"]
    assert broken["oracle_passed"] is None
    assert broken["task_success"] is None
    # One unmeasured cell makes the pilot total unknown; it is not a sum of the known ones.
    assert summary["model_calls"] is None
    # The failed cell created an unknown number of sandboxes, so no total is claimed.
    assert broken["sandbox_created"] is None
    assert broken["sandbox_creation_status"] == "SANDBOX_CREATION_UNKNOWN"
    assert summary["agent_sandbox_runs_confirmed_min"] == 0
    assert summary["agent_sandbox_runs_unknown"] == 1
    assert summary["agent_sandbox_runs_total"] is None
    assert summary["agent_sandbox_runs_complete"] is False
    # Unknown spend cannot be proven under the ceiling, so the next cell is not dispatched.
    assert summary["stopped_reason"] == "COST_UNKNOWN"
    assert summary["not_started_cells"] == [Cell(P1, "A0").cell_id]


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(PilotContractError, match="unknown arm_id"):
        assert Cell(P1, "A9").spec is not None
    with pytest.raises(PilotContractError, match="unknown task_id"):
        Cell("closure2-not-a-task", "A0").source_task()


def test_the_default_approval_is_still_pending() -> None:
    gate = approval_gate()
    assert gate["execution_ready"] is False
    assert gate["status"] == "PENDING_OWNER_APPROVAL"


# --------------------------------------------------- R1-01: the pilot scope authorization


def test_the_pilot_plan_is_pinned_to_the_frozen_identity() -> None:
    assert PILOT_PLAN.benchmark_id == BENCHMARK_ID
    assert PILOT_PLAN.preregistration_digest == C2A_R1_PREREGISTRATION_DIGEST
    assert PILOT_PLAN.cells == tuple(f"{task}::{arm}" for task, arm in PROPOSED_PILOT_CELLS)
    assert PILOT_PLAN.repetitions == (0,)
    assert PILOT_PLAN.max_cell_executions == 8
    assert PILOT_PLAN.planned_executions == 8
    assert PILOT_PLAN.estimated_cost_ceiling_usd is None


def test_the_plan_digest_ignores_the_approval_but_not_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = PILOT_PLAN.digest()
    # Approval is a separate record: approving must not move the plan identity.
    approve_pilot(monkeypatch, cells=pilot_cells(), ceiling=None)
    assert pilot_runner.PILOT_PLAN.digest() == before

    # A different scope must move it.
    assert dataclasses.replace(PILOT_PLAN, repetitions=(0, 1)).digest() != before
    assert dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=1.0).digest() != before
    assert dataclasses.replace(PILOT_PLAN, max_cell_executions=99).digest() != before


def test_the_plan_digest_is_reproducible_and_path_free() -> None:
    assert PILOT_PLAN.digest() == PILOT_PLAN.digest()
    payload = json.dumps(PILOT_PLAN.payload())
    assert str(pilot_runner.REPO_ROOT) not in payload
    assert "generated_at" not in payload


def test_the_benchmark_gate_is_evaluated_before_the_pilot_gate(tmp_path: Path) -> None:
    """With both layers pending, the benchmark refusal is the one reported."""

    calls: list[Any] = []
    with pytest.raises(PermissionError):
        asyncio.run(
            run_live_pilot(
                pilot_cells(),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_the_benchmark_gate_alone_does_not_authorize_the_pilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer A approved, layer B pending: still refused, and still before any executor."""

    approve_benchmark(monkeypatch)
    calls: list[Any] = []
    with pytest.raises(PilotNotAuthorized, match="not 'APPROVED'"):
        asyncio.run(
            run_live_pilot(
                pilot_cells(),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_the_pilot_approval_alone_does_not_authorize_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer B approved, layer A pending: refused by the benchmark gate first."""

    approve_pilot(monkeypatch, cells=pilot_cells())
    calls: list[Any] = []
    with pytest.raises(PermissionError):
        asyncio.run(
            run_live_pilot(
                pilot_cells(),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_an_extra_cell_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=pilot_cells())
    requested = (*pilot_cells(), Cell(P2, "A0"))
    calls: list[Any] = []
    with pytest.raises(PilotNotAuthorized, match="extra"):
        asyncio.run(
            run_live_pilot(
                requested,
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_a_subset_of_the_authorized_cells_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial run would need its own authorization record; it must not ride this one."""

    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=pilot_cells())
    calls: list[Any] = []
    with pytest.raises(PilotNotAuthorized, match="missing"):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "N0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_a_duplicate_cell_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approve_benchmark(monkeypatch)
    cell = Cell(P1, "A0")
    approve_pilot(monkeypatch, cells=(cell,))
    calls: list[Any] = []
    with pytest.raises(PilotNotAuthorized, match="twice"):
        asyncio.run(
            run_live_pilot(
                (cell, cell),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_an_unauthorized_repetition_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))
    calls: list[Any] = []
    with pytest.raises(PilotNotAuthorized, match="repetition 1"):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
                repetition_id=1,
            )
        )
    assert calls == []


def test_a_wrong_plan_digest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))
    monkeypatch.setattr(
        pilot_runner,
        "PILOT_APPROVAL",
        {**pilot_runner.PILOT_APPROVAL, "approved_plan_digest": "0" * 64},
    )
    gate = pilot_authorization_gate(cells=(Cell(P1, "A0"),), repetition_id=0)
    assert gate["execution_ready"] is False
    assert any("pilot plan digest" in problem for problem in gate["problems"])
    with pytest.raises(PilotNotAuthorized):
        require_pilot_authorization(cells=(Cell(P1, "A0"),), repetition_id=0)
    assert plan.digest() != "0" * 64


@pytest.mark.parametrize("missing", ["approved_by", "approval_commit", "approved_at"])
def test_an_approval_without_evidence_is_refused(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))
    monkeypatch.setattr(
        pilot_runner, "PILOT_APPROVAL", {**pilot_runner.PILOT_APPROVAL, missing: None}
    )
    gate = pilot_authorization_gate(cells=(Cell(P1, "A0"),), repetition_id=0)
    assert gate["execution_ready"] is False
    assert gate["problems"]


def test_a_missing_cost_ceiling_blocks_the_pilot() -> None:
    policy = cost_policy(dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=None))
    assert policy["valid"] is False
    assert policy["reason"] == "COST_CEILING_NOT_APPROVED"
    assert policy["hard_enforcement"] is False
    assert policy["guarantee"] == "COST_CEILING_NOT_HARD_ENFORCED"


def test_a_non_positive_ceiling_is_refused() -> None:
    policy = cost_policy(dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=0.0))
    assert policy["valid"] is False
    assert policy["reason"] == "COST_CEILING_NOT_POSITIVE"


@pytest.mark.parametrize("ceiling", [None, 0.0])
def test_the_live_entry_point_refuses_without_a_usable_ceiling(
    ceiling: float | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),), ceiling=ceiling)
    calls: list[Any] = []
    with pytest.raises(PilotNotAuthorized, match="cost policy"):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_dispatch_stops_once_the_ceiling_is_crossed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"), Cell(P1, "A1-P")), ceiling=5.0)
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _fake_result(estimated_cost_usd=9.0)

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"), Cell(P1, "A1-P")),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert [cell.arm_id for cell in calls] == ["A0"]
    assert summary["stopped_reason"] == "COST_CEILING_EXCEEDED"
    assert summary["not_started_cells"] == [Cell(P1, "A1-P").cell_id]
    assert summary["estimated_cost_usd_total"] == 9.0
    assert summary["observed_cost_usd_total"] is None


def test_dispatch_stops_when_the_cost_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown cost is not "probably fine": the next cell is not dispatched."""

    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"), Cell(P1, "A1-P")), ceiling=5.0)
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _fake_result(estimated_cost_usd=None)

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"), Cell(P1, "A1-P")),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert [cell.arm_id for cell in calls] == ["A0"]
    assert summary["stopped_reason"] == "COST_UNKNOWN"
    assert summary["estimated_cost_usd_total"] is None


def test_dispatch_continues_within_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"), Cell(P1, "A1-P")), ceiling=5.0)
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _fake_result(estimated_cost_usd=1.0)

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"), Cell(P1, "A1-P")),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert len(calls) == 2
    assert summary["stopped_reason"] is None
    assert summary["not_started_cells"] == []
    assert summary["estimated_cost_usd_total"] == 2.0


# --------------------------------------------------- R1-02: sandbox evidence


def test_a_completed_cell_confirms_sandbox_creation() -> None:
    record = normalize_result(Cell(P1, "A0"), _fake_result())
    assert record["sandbox_creation_status"] == "SANDBOX_CREATED_CONFIRMED"
    assert record["sandbox_created"] is True
    assert record["sandbox_count_scope"] == "agent_sandbox_only"
    assert record["oracle_sandbox_counted"] is False


def test_a_run_that_created_no_sandbox_says_so() -> None:
    record = normalize_result(Cell(P1, "A0"), _fake_result(sandbox_created=False))
    assert record["sandbox_creation_status"] == "SANDBOX_NOT_CREATED_CONFIRMED"
    assert record["sandbox_created"] is False


def test_a_cleanup_failure_does_not_erase_the_creation_fact() -> None:
    result = _fake_result()
    result.metrics.sandbox_destroyed = False
    record = normalize_result(Cell(P1, "A0"), result)
    assert record["sandbox_created"] is True
    assert record["sandbox_destroyed"] is False
    assert record["sandbox_creation_status"] == "SANDBOX_CREATED_CONFIRMED"


def test_an_error_before_a_result_is_an_unknown_sandbox() -> None:
    record = pilot_runner._execution_error_record(Cell(P1, "A0"), RuntimeError("boom"))
    assert record["sandbox_created"] is None
    assert record["sandbox_creation_status"] == "SANDBOX_CREATION_UNKNOWN"
    assert "no lifecycle record" in record["sandbox_evidence_source"]


def test_one_created_and_one_unknown_never_produces_a_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"), Cell(P1, "A1-P")))

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        if cell.arm_id == "A1-P":
            raise RuntimeError("died after provisioning")
        return _fake_result()

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"), Cell(P1, "A1-P")),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert summary["agent_sandbox_runs_confirmed_min"] == 1
    assert summary["agent_sandbox_runs_unknown"] == 1
    assert summary["agent_sandbox_runs_total"] is None
    assert summary["agent_sandbox_runs_complete"] is False
    assert summary["oracle_sandbox_runs"] is None


def test_the_dry_run_never_claims_an_oracle_sandbox_count() -> None:
    manifest = build_dry_run_manifest()
    assert manifest["sandbox_count_scope"] == "agent_sandbox_only"
    assert manifest["oracle_sandbox_runs"] is None
    assert manifest["agent_sandbox_runs_total"] == 0
    assert manifest["agent_sandbox_runs_complete"] is True


# --------------------------------------------------- R1-03: metric normalisation


def test_a_runner_verdict_outranks_zero_admission_rejections() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"),
        _fake_result(
            extra={"budget": {"admission_rejections": 0}}, failure_class="BUDGET_EXCEEDED"
        ),
    )
    assert record["budget_admission_rejected"] == 0
    assert record["budget_final_verdict"] == "EXCEEDED"
    assert record["budget_verdict_basis"] == "runner_failure_class"
    assert record["budget_exceeded"] is True


def test_a_missing_budget_record_stays_unknown() -> None:
    record = normalize_result(Cell(P1, "A1-P"), _fake_result())
    assert record["budget_admission_rejected"] is None
    assert record["budget_rejected_requests"] is None
    assert record["budget_final_verdict"] == "UNKNOWN"
    assert record["budget_exceeded"] is None
    assert record["budget_verdict_basis"] == "no_negative_verdict_from_runner"


def test_oracle_pass_with_unknown_budget_is_unknown_not_false() -> None:
    record = normalize_result(Cell(P1, "A1-P"), _fake_result(oracle_passed=True))
    assert record["oracle_passed"] is True
    assert record["budget_exceeded"] is None
    assert record["deliverable_passed_but_budget_failed"] is None


def test_oracle_failure_makes_the_composite_false() -> None:
    record = normalize_result(Cell(P1, "A1-P"), _fake_result(oracle_passed=False))
    assert record["deliverable_passed_but_budget_failed"] is False


def test_oracle_pass_over_budget_is_true() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"),
        _fake_result(
            oracle_passed=True,
            extra={"budget": {"admission_rejections": 3}},
            failure_class="BUDGET_EXCEEDED",
        ),
    )
    assert record["budget_final_verdict"] == "EXCEEDED"
    assert record["deliverable_passed_but_budget_failed"] is True


def test_native_post_hoc_budget_verdict_is_used() -> None:
    record = normalize_result(
        Cell(P1, "N1"),
        _fake_result(
            environment={"budget_check": {"exceeded": True, "reasons": ["tokens"]}},
            failure_class="BUDGET_EXCEEDED",
        ),
    )
    assert record["budget_observed_limit_exceeded"] is True
    assert record["budget_final_verdict"] == "EXCEEDED"
    assert record["budget_rejected_requests"] is None
    assert record["budget_semantics"] == "post_hoc_after_run"


def test_native_post_hoc_negative_verdict_is_not_exceeded() -> None:
    record = normalize_result(
        Cell(P1, "N1"),
        _fake_result(environment={"budget_check": {"exceeded": False, "reasons": []}}),
    )
    assert record["budget_final_verdict"] == "NOT_EXCEEDED"
    assert record["budget_exceeded"] is False


def test_atlas_pre_send_rejections_are_recorded_as_such() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"), _fake_result(extra={"budget": {"admission_rejections": 2}})
    )
    assert record["budget_final_verdict"] == "EXCEEDED"
    assert record["budget_verdict_basis"] == "pre_send_rejections"
    assert record["budget_rejected_requests"] == 2


def test_token_usage_and_cache_completeness_are_independent() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"), _fake_result(prompt_tokens=1000, completion_tokens=200)
    )
    assert record["provider_token_usage_observed"] is True
    assert record["provider_usage_complete"] is None
    assert "not_exposed_by_runner" in record["provider_usage_source"]
    assert record["cache_usage_complete"] is None
    assert record["cache_split_status"] is None


def test_a_complete_cache_split_does_not_imply_complete_token_usage() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"),
        _fake_result(
            prompt_tokens=1000,
            completion_tokens=200,
            extra={"cache_metrics_complete": True, "cache_split_status": "COMPLETE"},
        ),
    )
    assert record["cache_usage_complete"] is True
    assert record["provider_usage_complete"] is None


def test_partial_usage_is_reported_as_unknown_observation() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"), _fake_result(prompt_tokens=1000, completion_tokens=None)
    )
    assert record["provider_token_usage_observed"] is None
    assert record["provider_completion_tokens"] is None


def test_a_missing_request_count_is_not_written_as_zero() -> None:
    result = _fake_result()
    result.metrics.model_turns = None
    result.metrics.extra = {}
    record = normalize_result(Cell(P1, "N1"), result)
    assert record["model_calls"] is None
    assert record["model_calls_source"] == "not_exposed_by_runner"


def test_the_model_call_semantics_follow_the_source() -> None:
    atlas = normalize_result(
        Cell(P1, "A1-P"), _fake_result(extra={"request_evidence": {"requests_observed": 7}})
    )
    assert atlas["model_calls"] == 7
    assert atlas["model_calls_semantics"] == "requests_observed"
    native = normalize_result(Cell(P1, "N1"), _fake_result())
    assert native["model_calls"] == 3
    assert native["model_calls_semantics"] == "completed_model_turns"


# --------------------------------------------------- R1-04: native config identity


def test_the_native_config_is_the_frozen_one() -> None:
    assert native_config_digest() == FROZEN_NATIVE_CONFIG_DIGEST
    assert FROZEN_NATIVE_CONFIG_DIGEST == (
        "6cb2d1d91badaa235efa4427e5a53bdfc17610931e7b4c13e524406c2a863d14"
    )


@pytest.mark.parametrize(
    ("case", "needle", "replacement"),
    [
        ("pricing", "output_per_million: 1.2", "output_per_million: 1.3"),
        ("tools", "max_results: 5", "max_results: 9"),
        ("provider", "model: deepseek-chat", "model: deepseek-reasoner"),
        ("trajectory", "store_root: trajectory", "store_root: other"),
    ],
)
def test_a_meaningful_native_config_change_moves_the_digest(
    case: str, needle: str, replacement: str, tmp_path: Path
) -> None:
    text = (pilot_runner.REPO_ROOT / pilot_runner.NATIVE_CONFIG).read_text(encoding="utf-8")
    mutated = text.replace(needle, replacement)
    assert mutated != text, f"the frozen native config no longer contains {case} {needle!r}"
    target = tmp_path / pilot_runner.NATIVE_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(mutated, encoding="utf-8", newline="\n")
    assert native_config_digest(root=tmp_path) != FROZEN_NATIVE_CONFIG_DIGEST
    # Restoring the frozen text restores the frozen identity.
    target.write_text(text, encoding="utf-8", newline="\n")
    assert native_config_digest(root=tmp_path) == FROZEN_NATIVE_CONFIG_DIGEST


def test_a_comment_only_native_config_change_keeps_the_digest(tmp_path: Path) -> None:
    text = (pilot_runner.REPO_ROOT / pilot_runner.NATIVE_CONFIG).read_text(encoding="utf-8")
    target = tmp_path / pilot_runner.NATIVE_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# a comment\n" + text + "\n\n", encoding="utf-8", newline="\n")
    assert native_config_digest(root=tmp_path) == FROZEN_NATIVE_CONFIG_DIGEST


def test_the_contract_check_compares_the_native_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert check_frozen_contract() == []
    monkeypatch.setattr(pilot_runner, "native_config_digest", lambda **kwargs: "0" * 64)
    problems = check_frozen_contract()
    assert any(pilot_runner.NATIVE_CONFIG in problem for problem in problems)
    assert any("frozen" in problem for problem in problems)


def test_the_atlas_config_digests_are_unchanged() -> None:
    assert protocol.config_semantic_digest(protocol.LEGACY_CONTEXT_CONFIG) == (
        "16f587b3696e5d4e9f9a7868aa0859c89da876975ef7060f8a2936ce72899eb8"
    )
    assert protocol.config_semantic_digest(protocol.SHARED_PREFIX_CONFIG) == (
        "751beb1506602e250f240f69771514c7c2b569e3903c01060a554455dcfc549a"
    )


# --------------------------------------------------- R2-01: the durable execution lease


def test_a_live_run_claims_the_plan_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    approve_benchmark(monkeypatch)
    plan = approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))
    calls: list[Any] = []

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"),),
            runs_root=tmp_path,
            executor=_recording_executor(calls),
            live_intent=True,
        )
    )

    claim = summary["execution_claim"]
    assert claim["claimed"] is True
    assert claim["claim_status"] == "CONSUMED"
    assert claim["plan_digest"] == plan.digest()
    assert claim["ledger_path"] == str(isolated_ledger)
    stored = PilotLedger(isolated_ledger).get_claim(plan.digest())
    assert stored is not None
    assert stored["claim_status"] == "CONSUMED"
    assert [row["status"] for row in PilotLedger(isolated_ledger).cell_rows(plan.digest())] == [
        "COMPLETED"
    ]

    with pytest.raises(PilotAlreadyClaimed):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert len(calls) == 1, "the second attempt must not reach the executor"


def test_changing_runs_root_does_not_reset_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease is user state, not a results directory: a new runs_root is not a new allowance."""

    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))
    calls: list[Any] = []
    asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"),),
            runs_root=tmp_path / "first-root",
            executor=_recording_executor(calls),
            live_intent=True,
        )
    )
    with pytest.raises(PilotAlreadyClaimed):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path / "second-root",
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert len(calls) == 1


def test_a_pending_pilot_approval_never_creates_a_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    approve_benchmark(monkeypatch)  # Layer B stays pending
    with pytest.raises(PilotNotAuthorized):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor([]),
                live_intent=True,
            )
        )
    assert not isolated_ledger.exists()


def test_a_rejected_claim_leaves_the_executor_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))

    class _RejectingLedger:
        def claim_plan(self, **kwargs: Any) -> Any:
            raise PilotAlreadyClaimed("PILOT_ALREADY_CLAIMED: simulated")

    monkeypatch.setattr(pilot_runner, "_ledger_factory", _RejectingLedger)
    calls: list[Any] = []
    with pytest.raises(PilotAlreadyClaimed):
        asyncio.run(
            run_live_pilot(
                (Cell(P1, "A0"),),
                runs_root=tmp_path,
                executor=_recording_executor(calls),
                live_intent=True,
            )
        )
    assert calls == []


def test_a_ledger_write_failure_is_recorded_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),))

    class _BrokenLease:
        attempt_id = "broken"
        path = Path("broken.sqlite")

        def mark_running(self, *args: Any, **kwargs: Any) -> None:
            raise PilotLedgerStateInvalid("PILOT_LEDGER_STATE_INVALID: simulated")

        mark_completed = mark_running
        mark_failed = mark_running

    class _BrokenLedger:
        def claim_plan(self, **kwargs: Any) -> Any:
            return _BrokenLease()

    monkeypatch.setattr(pilot_runner, "_ledger_factory", _BrokenLedger)
    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"),),
            runs_root=tmp_path,
            executor=_recording_executor([]),
            live_intent=True,
        )
    )
    assert summary["cells"][0]["status"] == "COMPLETED"
    assert len(summary["ledger_write_failures"]) >= 2
    assert all("simulated" in failure for failure in summary["ledger_write_failures"])


# --------------------------------------------------- R2-02: finite cost integrity


@pytest.mark.parametrize(
    ("ceiling", "reason"),
    [
        (None, "COST_CEILING_NOT_APPROVED"),
        (0.0, "COST_CEILING_NOT_POSITIVE"),
        (-1.0, "COST_CEILING_NOT_POSITIVE"),
        (float("nan"), "COST_CEILING_INVALID"),
        (float("inf"), "COST_CEILING_INVALID"),
        (float("-inf"), "COST_CEILING_INVALID"),
        ("5.0", "COST_CEILING_INVALID"),
        (True, "COST_CEILING_INVALID"),
    ],
    ids=["none", "zero", "negative", "nan", "inf", "-inf", "string", "boolean"],
)
def test_an_illegal_ceiling_is_never_accepted(ceiling: Any, reason: str) -> None:
    policy = cost_policy(dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=ceiling))
    assert policy["valid"] is False
    assert policy["reason"] == reason
    assert policy["hard_enforcement"] is False


def test_a_finite_positive_ceiling_is_accepted() -> None:
    policy = cost_policy(dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=12.5))
    assert policy["valid"] is True
    assert policy["ceiling_usd"] == 12.5


def _record_with_cost(cell: Cell, cost: Any) -> dict[str, Any]:
    return normalize_result(cell, _fake_result(estimated_cost_usd=cost))


@pytest.mark.parametrize(
    ("cost", "reason"),
    [
        (float("nan"), "COST_INVALID"),
        (float("inf"), "COST_INVALID"),
        (float("-inf"), "COST_INVALID"),
        (-1.0, "COST_INVALID"),
        (10**400, "COST_INVALID"),
        (None, "COST_UNKNOWN"),
    ],
    ids=["nan", "inf", "-inf", "negative", "overflow", "unknown"],
)
def test_an_unusable_cell_cost_stops_dispatch(cost: Any, reason: str) -> None:
    plan = dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=5.0)
    state = _cost_dispatch_state([_record_with_cost(Cell(P1, "A0"), cost)], plan)
    assert state["can_dispatch"] is False
    assert state["reason"] == reason


def test_an_invalid_cost_is_not_written_as_zero() -> None:
    record = _record_with_cost(Cell(P1, "A0"), float("nan"))
    assert record["estimated_cost_usd"] is None
    assert record["estimated_cost_usd_state"] == "INVALID"


def test_reaching_the_ceiling_stops_the_next_cell() -> None:
    plan = dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=5.0)
    state = _cost_dispatch_state([_record_with_cost(Cell(P1, "A0"), 5.0)], plan)
    assert state["can_dispatch"] is False
    assert state["reason"] == "COST_CEILING_REACHED"


def test_crossing_the_ceiling_stops_the_next_cell() -> None:
    plan = dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=5.0)
    state = _cost_dispatch_state([_record_with_cost(Cell(P1, "A0"), 5.5)], plan)
    assert state["can_dispatch"] is False
    assert state["reason"] == "COST_CEILING_EXCEEDED"


def test_an_overflowing_aggregate_is_invalid_not_infinite() -> None:
    plan = dataclasses.replace(PILOT_PLAN, estimated_cost_ceiling_usd=5.0)
    records = [
        _record_with_cost(Cell(P1, "A0"), 1e308),
        _record_with_cost(Cell(P1, "A1-P"), 1e308),
    ]
    state = _cost_dispatch_state(records, plan)
    assert state["can_dispatch"] is False
    assert state["reason"] == "COST_INVALID"


def test_the_last_cell_overrun_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No next dispatch exists, so the final state has to carry the breach."""

    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),), ceiling=5.0)

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        return _fake_result(estimated_cost_usd=9.0)

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"),),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert summary["stopped_reason"] is None, "nothing was skipped"
    assert summary["cost_ceiling_state"] == "COST_CEILING_EXCEEDED"
    assert summary["cost_ceiling_breached"] is True
    assert summary["final_cost_state"]["spent_usd"] == 9.0


def test_the_last_cell_exactly_at_the_ceiling_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),), ceiling=5.0)

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        return _fake_result(estimated_cost_usd=5.0)

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"),),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert summary["cost_ceiling_state"] == "COST_CEILING_REACHED"
    assert summary["cost_ceiling_breached"] is True


def test_a_completed_run_under_the_ceiling_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_benchmark(monkeypatch)
    approve_pilot(monkeypatch, cells=(Cell(P1, "A0"),), ceiling=5.0)

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        return _fake_result(estimated_cost_usd=1.0)

    summary = asyncio.run(
        run_live_pilot(
            (Cell(P1, "A0"),),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
        )
    )
    assert summary["cost_ceiling_state"] == "WITHIN_CEILING"
    assert summary["cost_ceiling_breached"] is False


# ------------------------------------------------------------------ D. the runner adapter


def test_the_arm_matrix_is_the_frozen_one() -> None:
    expected = {
        "N0": ("native", "single", "native", "off"),
        "N1": ("native", "multi", "native", "off"),
        "A0": ("atlas", "single", "legacy", "off"),
        "A1-L": ("atlas", "multi", "legacy", "off"),
        "A1-P": ("atlas", "multi", "shared_prefix", "off"),
        "A2-P": ("atlas", "multi", "shared_prefix", "selective"),
    }
    for arm_id, (runner, topology, layout, route) in expected.items():
        spec: ArmSpec = ARM_SPECS[arm_id]
        assert (spec.runner, spec.topology, spec.prompt_layout, spec.requested_route) == (
            runner,
            topology,
            layout,
            route,
        )


def test_the_native_engine_gets_every_required_keyword() -> None:
    from experiments.evaluation.closure1b.native_baseline import run_native_sample

    signature = inspect.signature(run_native_sample)
    signature.bind(
        object(),
        settings=object(),
        repetition_id=0,
        runs_root=Path("x"),
        config_path=Path("y"),
    )


def test_the_atlas_engine_gets_every_required_keyword() -> None:
    from experiments.evaluation.runner import run_sample

    signature = inspect.signature(run_sample)
    signature.bind(object(), settings=object(), repetition_id=0, config_path=Path("y"))


def test_the_default_executor_dispatches_by_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    from experiments.evaluation import runner as atlas_runner
    from experiments.evaluation.closure1b import native_baseline

    seen: dict[str, Any] = {}

    async def fake_native(task: Any, **kwargs: Any) -> str:
        seen["native"] = kwargs
        return "native"

    async def fake_atlas(task: Any, **kwargs: Any) -> str:
        seen["atlas"] = kwargs
        return "atlas"

    monkeypatch.setattr(native_baseline, "run_native_sample", fake_native)
    monkeypatch.setattr(atlas_runner, "run_sample", fake_atlas)
    settings = SimpleNamespace(config_path=Path("experiments/configs/x.yaml"))

    native_cell = Cell(P1, "N1")
    atlas_cell = Cell(P1, "A1-P")
    native = asyncio.run(
        pilot_runner._default_executor(
            native_cell,
            settings=settings,
            task=native_cell.benchmark_task(),
            runs_root=Path("runs"),
            repetition_id=0,
        )
    )
    atlas = asyncio.run(
        pilot_runner._default_executor(
            atlas_cell,
            settings=settings,
            task=atlas_cell.benchmark_task(),
            runs_root=Path("runs"),
            repetition_id=0,
        )
    )
    assert (native, atlas) == ("native", "atlas")
    assert seen["native"]["runs_root"] == Path("runs")
    assert seen["atlas"]["config_path"] == settings.config_path


def test_an_unknown_runner_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        pilot_runner.ARM_SPECS,
        "A0",
        ArmSpec("A0", "atlas", "mystery", "single", None, "legacy", "off"),
    )
    cell = Cell(P1, "A0")
    with pytest.raises(PilotContractError, match="unknown runner"):
        asyncio.run(
            pilot_runner._default_executor(
                cell,
                settings=SimpleNamespace(config_path=Path("x")),
                task=cell.benchmark_task(),
                runs_root=Path("runs"),
                repetition_id=0,
            )
        )


# ------------------------------------------------------------------ E. the evidence mapping


def _fake_result(**overrides: Any) -> SimpleNamespace:
    metrics = SimpleNamespace(
        prompt_tokens=overrides.pop("prompt_tokens", 1000),
        completion_tokens=overrides.pop("completion_tokens", 200),
        cache_hit_tokens=overrides.pop("cache_hit_tokens", None),
        cache_miss_tokens=overrides.pop("cache_miss_tokens", None),
        estimated_cost_usd=overrides.pop("estimated_cost_usd", 0.01),
        model_turns=3,
        tool_calls=5,
        latency_ms=1234.0,
        sandbox_created=overrides.pop("sandbox_created", True),
        sandbox_destroyed=overrides.pop("sandbox_destroyed", True),
        worker_selection=list(overrides.pop("workers", ("w1",))),
        extra=overrides.pop("extra", {}),
    )
    identity = SimpleNamespace(
        run_id="eval-abc",
        repetition_id=0,
        model="deepseek-chat",
        git_commit="deadbeef",
        git_dirty=False,
    )
    oracle = SimpleNamespace(
        passed=overrides.pop("oracle_passed", False),
        environment_failure=overrides.pop("environment_failure", False),
        failure_kind=overrides.pop("failure_kind", None),
    )
    environment = {
        "sandbox_image": "atlas-uat-python:3.12",
        "workspace_isolation": "remote_opensandbox_container",
        "agent_latency_ms": 900.0,
        **overrides.pop("environment", {}),
    }
    return SimpleNamespace(
        identity=identity,
        metrics=metrics,
        oracle=oracle,
        environment=environment,
        run_dir="experiments/runs/closure2/x",
        agent_success=overrides.pop("agent_success", True),
        success=overrides.pop("success", False),
        failure_class=overrides.pop("failure_class", "ORACLE_FAILED"),
        failure_reason=overrides.pop("failure_reason", "hidden tests failed"),
    )


def test_an_unknown_cache_split_stays_unknown() -> None:
    record = normalize_result(Cell(P1, "A1-P"), _fake_result())
    assert record["cache_hit_tokens"] is None
    assert record["cache_miss_tokens"] is None
    assert record["cache_usage_complete"] is None
    assert record["provider_usage_complete"] is None
    assert record["provider_usage_source"].startswith("not_exposed_by_runner")


def test_missing_usage_is_never_written_as_zero() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"), _fake_result(prompt_tokens=None, completion_tokens=None)
    )
    assert record["provider_prompt_tokens"] is None
    assert record["provider_completion_tokens"] is None
    assert record["estimated_cost_usd"] == 0.01


def test_a_failed_oracle_is_not_an_environment_failure() -> None:
    record = normalize_result(Cell(P1, "A1-P"), _fake_result(oracle_passed=False))
    assert record["oracle_passed"] is False
    assert record["oracle_environment_failure"] is False
    assert record["agent_success"] is True
    assert record["task_success"] is False


def test_an_environment_failure_is_marked_as_such() -> None:
    record = normalize_result(
        Cell(P1, "A1-P"),
        _fake_result(oracle_passed=False, environment_failure=True, failure_kind="ORACLE_TIMEOUT"),
    )
    assert record["oracle_environment_failure"] is True
    assert record["oracle_failure_kind"] == "ORACLE_TIMEOUT"


def test_budget_exceeded_is_separate_from_oracle_outcome() -> None:
    guarded = {"budget": {"admission_rejections": 2, "limit_tokens": 260_000}}
    record = normalize_result(
        Cell(P1, "A1-P"),
        _fake_result(oracle_passed=True, extra=guarded, failure_class="BUDGET_EXCEEDED"),
    )
    assert record["budget_exceeded"] is True
    assert record["budget_rejected_requests"] == 2
    assert record["oracle_passed"] is True
    assert record["deliverable_passed_but_budget_failed"] is True

    post_hoc = {"budget_check": {"exceeded": True, "reasons": ["tokens 300000 > budget 260000"]}}
    native = normalize_result(
        Cell(P1, "N1"),
        _fake_result(environment=post_hoc, failure_class="BUDGET_EXCEEDED"),
    )
    assert native["budget_exceeded"] is True
    assert native["budget_semantics"] == "post_hoc_after_run"


def test_a2p_on_n1_is_not_recorded_as_a_multi_worker_run() -> None:
    routing = {"decision": {"route": "direct"}, "executed_worker_count": 1}
    record = normalize_result(
        Cell(N1, "A2-P"), _fake_result(extra={"routing": routing}, workers=("w1",))
    )
    assert record["topology"] == "multi"
    assert record["resolved_route"] == "direct"
    assert record["executed_worker_count"] == 1
    assert record["multi_worker_execution"] is False
    assert record["single_worker_downgrade"] is True


def test_a_real_multi_run_is_recorded_as_multi() -> None:
    routing = {"decision": {"route": "delegate_parallel"}, "executed_worker_count": 3}
    record = normalize_result(
        Cell(P1, "A1-P"), _fake_result(extra={"routing": routing}, workers=("a", "b", "c"))
    )
    assert record["multi_worker_execution"] is True
    assert record["single_worker_downgrade"] is False
    assert record["executed_worker_count"] == 3


def test_the_model_calls_source_is_named() -> None:
    with_evidence = normalize_result(
        Cell(P1, "A1-P"),
        _fake_result(extra={"request_evidence": {"requests_observed": 7}}),
    )
    assert with_evidence["model_calls"] == 7
    assert with_evidence["model_calls_source"] == "request_evidence.requests_observed"
    without = normalize_result(Cell(P1, "N1"), _fake_result())
    assert without["model_calls"] == 3
    assert without["model_calls_source"] == "metrics.model_turns"


def test_the_raw_result_path_is_preserved_and_flagged() -> None:
    record = normalize_result(Cell(P1, "A0"), _fake_result())
    assert record["raw_result_path"].endswith("result.json")
    assert record["raw_evidence_preserved"] is True


def test_the_model_is_explicit_on_every_arm() -> None:
    model = effective_model()
    assert model == "deepseek-chat"
    for arm_id in ARM_ORDER:
        assert settings_for(Cell(P1, arm_id), runs_root=Path("x")).model_override == model


def test_the_hidden_oracle_never_reaches_the_workspace() -> None:
    """The pilot builds the task through the frozen builder; it must not stage hidden files."""

    for cell in plan_cells():
        task = cell.benchmark_task()
        assert set(task.hidden_files) & set(task.visible_files) == set()
        for name in task.hidden_files:
            assert not any(name in visible for visible in task.visible_files)
