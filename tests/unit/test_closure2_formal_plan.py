"""Closure-2 S07: the candidate 54-cell formal matrix.

Offline only. No sandbox, no provider, no API key, no approval: every assertion is about the order,
the identities and the refusal of the shipped gate.
"""

from __future__ import annotations

import dataclasses
from collections import Counter

import pytest
from experiments.evaluation.closure2 import formal_plan as formal_plan_module
from experiments.evaluation.closure2 import pilot_runner, protocol
from experiments.evaluation.closure2.catalog import TASKS, build_task
from experiments.evaluation.closure2.formal_plan import (
    CELLS_PER_REPETITION,
    FORMAL_COST_CEILING_USD,
    FORMAL_REPETITIONS,
    TOTAL_EXECUTIONS,
    FormalPlanError,
    authorization_probe,
    candidate_approval,
    check_candidate,
    formal_plan,
    formal_plan_id,
    formal_plan_payload,
    repetition_cells,
    rotated_arms,
)
from experiments.evaluation.closure2.pilot_runner import ARM_ORDER, TASK_ORDER, Cell

from atlasos.workers.routing import extract_declared_paths


def test_the_matrix_is_fifty_four_executions() -> None:
    assert CELLS_PER_REPETITION == 18
    assert len(FORMAL_REPETITIONS) == 3
    planned = sum(len(repetition_cells(repetition)) for repetition in FORMAL_REPETITIONS)
    assert planned == TOTAL_EXECUTIONS == 54


def test_every_task_arm_pair_appears_once_per_repetition_and_three_times_overall() -> None:
    overall: Counter[str] = Counter()
    for repetition in FORMAL_REPETITIONS:
        cells = repetition_cells(repetition)
        assert len({cell.cell_id for cell in cells}) == len(cells)
        per_repetition = Counter(cell.cell_id for cell in cells)
        assert set(per_repetition.values()) == {1}
        overall.update(per_repetition)
    expected = {f"{task}::{arm}" for task in TASK_ORDER for arm in ARM_ORDER}
    assert set(overall) == expected
    assert set(overall.values()) == {len(FORMAL_REPETITIONS)}


def test_the_candidate_satisfies_its_own_contract() -> None:
    assert check_candidate() == []


def test_the_arm_order_rotates_and_is_a_permutation_each_time() -> None:
    orders = [rotated_arms(repetition) for repetition in FORMAL_REPETITIONS]
    for order in orders:
        assert sorted(order) == sorted(ARM_ORDER)
    assert len({tuple(order) for order in orders}) == len(FORMAL_REPETITIONS)
    # Each arm must change position between repetitions, otherwise the rotation is decorative.
    for arm in ARM_ORDER:
        positions = [order.index(arm) for order in orders]
        assert len(set(positions)) == len(positions)


def test_tasks_keep_the_frozen_order_inside_a_repetition() -> None:
    cells = repetition_cells(0)
    assert [cell.task_id for cell in cells[:6]] == [TASK_ORDER[0]] * 6
    assert [cell.task_id for cell in cells[6:12]] == [TASK_ORDER[1]] * 6
    assert [cell.task_id for cell in cells[12:]] == [TASK_ORDER[2]] * 6
    assert [cell.arm_id for cell in cells[:6]] == list(ARM_ORDER)


def test_the_three_repetitions_have_distinct_and_stable_digests() -> None:
    digests = [formal_plan(repetition).digest() for repetition in FORMAL_REPETITIONS]
    assert len(set(digests)) == len(FORMAL_REPETITIONS)
    assert digests == [formal_plan(repetition).digest() for repetition in FORMAL_REPETITIONS]


def test_an_unplanned_repetition_is_refused() -> None:
    for bad in (-1, 3, 99):
        with pytest.raises(FormalPlanError):
            repetition_cells(bad)
        with pytest.raises(FormalPlanError):
            formal_plan_id(bad)


def test_the_cost_ceiling_is_inside_the_plan_identity() -> None:
    assert not hasattr(pilot_runner, "formal_plan")  # absent from the live runner on purpose
    plan = formal_plan(0)
    raised = dataclasses.replace(plan, estimated_cost_ceiling_usd=FORMAL_COST_CEILING_USD + 1)
    assert raised.digest() != plan.digest()


def test_the_candidate_is_not_reachable_from_the_live_cli() -> None:
    exposed = pilot_runner.plan_scope_ids()
    assert set(exposed) == {"closure2-pilot-1", "closure2-routing-pair-1"}
    for repetition in FORMAL_REPETITIONS:
        assert formal_plan_id(repetition) not in exposed


def test_the_shipped_gate_refuses_every_repetition_for_approval_only() -> None:
    for repetition in FORMAL_REPETITIONS:
        probe = authorization_probe(repetition)
        assert probe["execution_ready"] is False
        assert probe["cells"] == CELLS_PER_REPETITION
        assert probe["plan_digest"] == formal_plan(repetition).digest()
        # Contract-level refusals would mean the plan is wrong; only the approval may be missing.
        assert all(
            "approval" in problem or "approver" in problem for problem in probe["problems"]
        ), probe["problems"]


def test_a_granted_approval_would_be_the_only_missing_piece() -> None:
    plan = formal_plan(0)
    granted = {
        **candidate_approval(0),
        "status": "APPROVED",
        "approved_by": "repository_owner",
        "approval_commit": "0" * 40,
        "approved_at": "2026-11-25T00:00:00+00:00",
    }
    assert candidate_approval(0)["approved_plan_digest"] == plan.digest()
    gate = pilot_runner.pilot_authorization_gate(
        cells=repetition_cells(0),
        repetition_id=0,
        plan=plan,
        record=granted,
    )
    assert gate["problems"] == []
    assert gate["execution_ready"] is True


def test_each_repetition_authorizes_exactly_its_own_repetition() -> None:
    for repetition in FORMAL_REPETITIONS:
        plan = formal_plan(repetition)
        assert plan.repetitions == (repetition,)
        assert plan.max_cell_executions == CELLS_PER_REPETITION
        assert plan.runner_revision == formal_plan_module.FORMAL_PLAN_REVISION
        assert plan.preregistration_digest == protocol.C2A_R1_PREREGISTRATION_DIGEST


def test_single_topology_never_starts_a_worker() -> None:
    for task in TASKS:
        single = build_task(task, topology="single")
        multi = build_task(task, topology="multi")
        assert len(single.workers) == 1
        assert single.workers[0].task_id == "main"
        assert len(multi.workers) > 1
        assert multi.workers[0].depends_on == ()


def test_the_negative_task_has_one_worker_in_wave_zero_and_a_shared_write() -> None:
    task = next(item for item in TASKS if item.task_id == "closure2-state-machine-refactor")
    multi = build_task(task, topology="multi")
    wave_zero = [step for step in multi.workers if not step.depends_on]
    assert len(wave_zero) == 1
    assert len(multi.workers) == 2
    declared = [extract_declared_paths(step.prompt)[0] for step in multi.workers]
    assert all("lifecycle/state.py" in paths for paths in declared)


def test_the_positive_tasks_have_two_disjoint_workers_in_wave_zero() -> None:
    for task_id in ("closure2-pipeline-migration", "closure2-plugin-migration"):
        task = next(item for item in TASKS if item.task_id == task_id)
        multi = build_task(task, topology="multi")
        wave_zero = [step for step in multi.workers if not step.depends_on]
        assert len(wave_zero) == 2
        left, right = (set(extract_declared_paths(step.prompt)[0]) for step in wave_zero)
        assert left and right
        assert left.isdisjoint(right)


def test_arm_topologies_match_the_frozen_matrix() -> None:
    workers_by_task = {
        "closure2-pipeline-migration": 3,
        "closure2-plugin-migration": 3,
        "closure2-state-machine-refactor": 2,
    }
    for repetition in FORMAL_REPETITIONS:
        for cell in repetition_cells(repetition):
            expected = "single" if cell.arm_id in {"N0", "A0"} else "multi"
            assert cell.spec.topology == expected
            if expected == "single":
                assert len(cell.benchmark_task().workers) == 1
            else:
                assert len(cell.benchmark_task().workers) == workers_by_task[cell.task_id]


def test_the_payload_is_offline_and_unauthorized() -> None:
    payload = formal_plan_payload()
    assert payload["status"] == "CANDIDATE_NOT_FROZEN_NOT_AUTHORIZED"
    assert payload["live_runnable"] is False
    assert payload["model_calls"] == 0
    assert payload["sandbox_runs"] == 0
    assert payload["seed"] is None
    assert payload["randomised"] is False
    assert payload["total_executions"] == 54
    assert payload["contract_ok"] is True
    assert payload["preregistration_digest"] == protocol.C2A_R1_PREREGISTRATION_DIGEST
    for item in payload["repetitions"]:
        assert item["authorization_probe"]["execution_ready"] is False
        assert item["candidate_approval"]["status"] == "PENDING_OWNER_APPROVAL"
        assert len(item["cells"]) == CELLS_PER_REPETITION
        assert {cell["arm_id"] for cell in item["cells"]} == set(ARM_ORDER)


def test_the_plan_is_not_authorized_by_the_benchmark_approval_alone() -> None:
    """The definition approval and the scope approval are separate layers, and both are required."""

    definition = protocol.approval_gate()
    assert definition["status"] == "APPROVED"
    for repetition in FORMAL_REPETITIONS:
        gate = pilot_runner.pilot_authorization_gate(
            cells=repetition_cells(repetition),
            repetition_id=repetition,
            plan=formal_plan(repetition),
            record=candidate_approval(repetition),
        )
        assert gate["execution_ready"] is False


def test_a_single_cell_swap_changes_the_digest() -> None:
    plan = formal_plan(0)
    cells = list(plan.cells)
    cells[0], cells[1] = cells[1], cells[0]
    swapped = dataclasses.replace(plan, cells=tuple(cells))
    assert swapped.digest() != plan.digest()


def test_the_two_topologies_of_one_task_share_the_graded_task() -> None:
    for cell_id in ("closure2-pipeline-migration::A0", "closure2-pipeline-migration::A1-P"):
        task_id, _, arm_id = cell_id.partition("::")
        cell = Cell(task_id, arm_id)
        built = cell.benchmark_task()
        assert built.sample_id == task_id
        assert built.required_artifacts
        assert built.allowed_paths and built.forbidden_paths
        assert built.hidden_files and built.visible_files
