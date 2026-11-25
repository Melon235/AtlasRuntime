"""Closure-2 S07: the S08 canary contract, prepared and unauthorized.

Offline only. The two claims worth testing are "the contract covers what it says it covers" and "the
shipped gate still refuses it, before anything is created".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from experiments.evaluation.closure2 import pilot_runner, s08_canary
from experiments.evaluation.closure2.candidate_budget import CANDIDATE_PROFILE
from experiments.evaluation.closure2.formal_plan import formal_plan
from experiments.evaluation.closure2.pilot_runner import Cell
from experiments.evaluation.closure2.s08_canary import (
    CANARY_APPROVAL,
    CANARY_COVERAGE,
    CANARY_ORDER,
    REQUIRED_COVERAGE,
    authorization_template,
    canary_cells,
    canary_plan,
    check_contract,
    contract_payload,
    coverage_matrix,
)

TASK = "closure2-pipeline-migration"


def test_the_contract_covers_what_it_claims() -> None:
    assert check_contract() == []
    assert set(CANARY_COVERAGE) == set(CANARY_ORDER)
    matrix = coverage_matrix()
    for requirement in REQUIRED_COVERAGE:
        assert matrix[requirement], requirement


def test_the_canary_covers_both_engines_both_layouts_and_the_refusal() -> None:
    cells = canary_cells()
    arms = {cell.arm_id for cell in cells}
    assert arms == {"N0", "N1", "A0", "A1-L", "A1-P", "A2-P"}
    assert any(cell.spec.runner == "native" and cell.spec.topology == "single" for cell in cells)
    assert any(cell.spec.runner == "native" and cell.spec.topology == "multi" for cell in cells)
    assert any(cell.spec.runner == "atlas" and cell.spec.topology == "single" for cell in cells)
    assert any(cell.spec.runner == "atlas" and cell.spec.topology == "multi" for cell in cells)
    assert {cell.spec.prompt_layout for cell in cells} == {
        "native",
        "legacy",
        "shared_prefix",
    }
    negative = [cell for cell in cells if cell.task_id != TASK]
    assert [(cell.task_id, cell.arm_id) for cell in negative] == [
        ("closure2-state-machine-refactor", "A2-P")
    ]


def test_the_canary_runs_the_owner_approved_budget() -> None:
    plan = canary_plan()
    assert plan.budget_profile_digest == CANDIDATE_PROFILE.digest()
    assert plan.budget_profile().multi_worker_max_turns == 20
    assert plan.budget_profile().cell_token_budget == 500_000
    assert plan.budget_profile().single_max_turns == 36
    assert plan.repetitions == (0,)
    assert plan.max_cell_executions == len(CANARY_ORDER) == 7
    assert plan.estimated_cost_ceiling_usd == 0.45


def test_the_canary_plan_is_wired_into_the_dispatcher() -> None:
    assert pilot_runner.S08_CANARY_PLAN_ID in pilot_runner.plan_scope_ids()
    plan, record = pilot_runner.pilot_scope(pilot_runner.S08_CANARY_PLAN_ID)
    assert plan.plan_id == canary_plan().plan_id
    assert plan.digest() == canary_plan().digest()
    assert record["status"] == "PENDING_OWNER_APPROVAL"
    assert tuple(cell.cell_id for cell in pilot_runner.plan_cells_for(plan.plan_id)) == CANARY_ORDER


def test_the_shipped_gate_refuses_the_canary_for_approval_only() -> None:
    plan = canary_plan()
    gate = pilot_runner.pilot_authorization_gate(
        cells=canary_cells(),
        repetition_id=0,
        plan=plan,
        record=CANARY_APPROVAL,
    )
    assert gate["execution_ready"] is False
    assert all("approval" in problem or "approver" in problem for problem in gate["problems"])


def test_the_canary_digest_is_not_any_other_scope() -> None:
    digests = {
        pilot_runner.PILOT_PLAN.digest(),
        pilot_runner.ROUTING_PAIR_PLAN.digest(),
        *(formal_plan(repetition).digest() for repetition in (0, 1, 2)),
    }
    assert canary_plan().digest() not in digests


def test_the_template_is_pending_and_names_only_its_own_digest() -> None:
    template = authorization_template()
    assert template["status"] == "PENDING_OWNER_APPROVAL"
    assert template["approved_plan_digest"] == canary_plan().digest()
    for key in ("approved_by", "approval_commit", "approved_at"):
        assert template[key] is None
    assert template["scope"]["attempts"] == 1
    assert template["scope"]["worker_max_turns"] == 20
    assert template["scope"]["per_cell_token_budget"] == 500_000
    assert any("APPROVAL_RECORD" in item for item in template["cannot_be_satisfied_by"])
    assert any("S06" in item for item in template["cannot_be_satisfied_by"])


def test_a_pending_canary_cannot_start_a_live_run(tmp_path: Path) -> None:
    """The refusal happens before a provider, a sandbox or a ledger claim exists."""

    called = False

    async def executor(*_args: object, **_kwargs: object) -> object:  # pragma: no cover
        nonlocal called
        called = True
        raise AssertionError("the executor must never be reached")

    with pytest.raises(pilot_runner.PilotNotAuthorized):
        import asyncio

        asyncio.run(
            pilot_runner.run_live_pilot(
                canary_cells(),
                runs_root=tmp_path,
                repetition_id=0,
                executor=executor,
                live_intent=True,
                plan_id=pilot_runner.S08_CANARY_PLAN_ID,
            )
        )
    assert called is False


def test_the_payload_states_that_nothing_is_authorized() -> None:
    payload = contract_payload()
    assert payload["status"] == "PREPARED_PENDING_AUTHORIZATION"
    assert payload["authorized"] is False
    assert payload["model_calls"] == 0
    assert payload["sandbox_runs"] == 0
    assert payload["contract_problems"] == []
    assert payload["coverage_complete"] is True
    assert payload["execution_order"] == list(CANARY_ORDER)
    assert [cell["order"] for cell in payload["cells"]] == list(range(1, 8))
    assert payload["exclusions"]["declared_before_execution"] is True


def test_every_acceptance_criterion_names_its_evidence() -> None:
    payload = contract_payload()
    criteria = payload["acceptance_criteria"]
    assert len(criteria) == 13
    for item in criteria:
        assert {"id", "criterion", "evidence"} <= set(item)
        assert item["evidence"].strip()
    joined = " ".join(item["evidence"] for item in criteria)
    for field in (
        "executed_worker_count",
        "resolved_route",
        "GATE_2_DEPENDENCY_COUPLING",
        "oracle_passed",
        "execution_integrity_status",
    ):
        assert field in joined


def test_the_canary_is_excluded_from_the_formal_matrix() -> None:
    payload = contract_payload()
    exclusions = payload["exclusions"]
    assert "excluded from the S11 aggregate" in exclusions["statement"]
    assert "never reused as a formal sample" in exclusions["formal_identity_note"]
    assert payload["benchmark_id"] != formal_plan(0).benchmark_id


def test_a_single_cell_swap_would_change_the_canary_digest() -> None:
    plan = canary_plan()
    swapped = plan.__class__(
        plan_id=plan.plan_id,
        benchmark_id=plan.benchmark_id,
        preregistration_digest=plan.preregistration_digest,
        runner_revision=plan.runner_revision,
        cells=tuple(reversed(plan.cells)),
        repetitions=plan.repetitions,
        max_cell_executions=plan.max_cell_executions,
        estimated_cost_ceiling_usd=plan.estimated_cost_ceiling_usd,
        budget_profile_digest=plan.budget_profile_digest,
    )
    assert swapped.digest() != plan.digest()


def test_the_canary_cells_are_real_frozen_cells() -> None:
    for cell in canary_cells():
        assert isinstance(cell, Cell)
        built = cell.benchmark_task()
        assert built.sample_id == cell.task_id
        assert built.workers
    assert s08_canary.CANARY_PLAN_REVISION == "closure-2-s08-canary-1"
