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
    FORMAL_BUDGET_PROFILE,
    FORMAL_COST_CEILING_USD,
    FORMAL_REPETITIONS,
    SCHEDULE_FROZEN,
    TOTAL_EXECUTIONS,
    FormalPlanError,
    aggregate_plan_summaries,
    authorization_probe,
    candidate_approval,
    check_candidate,
    formal_plan,
    formal_plan_id,
    formal_plan_payload,
    repetition_cells,
    rotated_arms,
    rotated_tasks,
    schedule_audit,
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


def test_tasks_form_blocks_of_six_cells_and_rotate_across_repetitions() -> None:
    for repetition in FORMAL_REPETITIONS:
        cells = repetition_cells(repetition)
        assert [cell.task_id for cell in cells[:6]] == [cells[0].task_id] * 6
        assert [cell.task_id for cell in cells[6:12]] == [cells[6].task_id] * 6
        assert [cell.task_id for cell in cells[12:]] == [cells[12].task_id] * 6
        assert len({cells[index].task_id for index in (0, 6, 12)}) == 3
    # Repetition 0 still starts with the catalogue order and the unrotated arm order.
    assert rotated_tasks(0) == TASK_ORDER
    assert [cell.arm_id for cell in repetition_cells(0)[:6]] == list(ARM_ORDER)
    # Every task occupies each third exactly once across the three repetitions.
    audit = schedule_audit()
    assert audit["every_task_in_each_third_once"] is True


def test_the_frozen_schedule_is_still_reproducible() -> None:
    """The S07 order stays computable, so a future reader can diff the two schedules."""

    for repetition in FORMAL_REPETITIONS:
        cells = repetition_cells(repetition, schedule=SCHEDULE_FROZEN)
        assert [cell.task_id for cell in cells[:6]] == [TASK_ORDER[0]] * 6
        assert [cell.task_id for cell in cells[6:12]] == [TASK_ORDER[1]] * 6
        assert [cell.task_id for cell in cells[12:]] == [TASK_ORDER[2]] * 6
    frozen = schedule_audit(schedule=SCHEDULE_FROZEN)
    assert frozen["every_task_in_each_third_once"] is False


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


def test_the_three_plans_are_wired_and_still_refused() -> None:
    """Wiring is not authorization: every formal id resolves and every one is rejected."""

    exposed = pilot_runner.plan_scope_ids()
    for repetition in FORMAL_REPETITIONS:
        plan_id = formal_plan_id(repetition)
        assert plan_id in exposed
        plan, record = pilot_runner.pilot_scope(plan_id)
        assert plan.plan_id == plan_id
        assert len(pilot_runner.plan_cells_for(plan_id)) == CELLS_PER_REPETITION
        assert record["status"] == "PENDING_OWNER_APPROVAL"
        assert record["approved_plan_digest"] == plan.digest()
    # The two consumed plans keep their ids and states; nothing here rewrote them.
    assert pilot_runner.PILOT_APPROVAL["status"] == "CONSUMED_BY_EXECUTION"
    assert pilot_runner.ROUTING_PAIR_APPROVAL["status"] == "CONSUMED_BY_EXECUTION"


def test_the_benchmark_approval_alone_still_authorizes_nothing() -> None:
    """R-1: the definition approval is real, and it is not a scope approval."""

    definition = protocol.approval_gate()
    assert definition["status"] == "APPROVED"
    assert definition["execution_ready"] is True
    runnable = []
    for plan_id in pilot_runner.plan_scope_ids():
        _, record = pilot_runner.pilot_scope(plan_id)
        if record.get("status") == "APPROVED":
            runnable.append(plan_id)
    assert runnable == []


def test_every_plan_has_its_own_summary_file_and_run_root() -> None:
    names = {
        plan_id: pilot_runner.LIVE_SUMMARY_FILENAMES[plan_id]
        for plan_id in pilot_runner.plan_scope_ids()
    }
    assert len(set(names.values())) == len(names)
    roots = [pilot_runner.default_runs_root(plan_id) for plan_id in pilot_runner.plan_scope_ids()]
    assert len(set(roots)) == len(roots)


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


def test_the_fifty_four_identities_are_unique_across_plans() -> None:
    identities = [
        (cell.cell_id, repetition)
        for repetition in FORMAL_REPETITIONS
        for cell in repetition_cells(repetition)
    ]
    assert len(identities) == len(set(identities)) == TOTAL_EXECUTIONS
    # The same cell really does run three times; only the repetition makes it a different execution.
    assert len({cell_id for cell_id, _ in identities}) == CELLS_PER_REPETITION


def test_every_plan_binds_the_candidate_budget_profile() -> None:
    for repetition in FORMAL_REPETITIONS:
        plan = formal_plan(repetition)
        assert plan.budget_profile_digest == FORMAL_BUDGET_PROFILE.digest()
        assert plan.budget_profile().cell_token_budget == 500_000
        assert plan.budget_profile().multi_worker_max_turns == 20
    # Moving the profile moves the digest: the plan cannot keep an approval across a budget change.
    changed = dataclasses.replace(formal_plan(0), budget_profile_digest="0" * 64)
    assert changed.digest() != formal_plan(0).digest()


def _fake_summary(repetition: int) -> dict:
    plan = formal_plan(repetition)
    return {
        "pilot_plan_id": plan.plan_id,
        "pilot_plan_digest": plan.digest(),
        "cost_ceiling_usd": plan.estimated_cost_ceiling_usd,
        "cells": [
            {
                "cell_id": cell.cell_id,
                "repetition_id": repetition,
                "status": "PASSED",
                "oracle_passed": True,
                "provider_prompt_tokens": 1_000,
                "provider_completion_tokens": 100,
                "cache_hit_tokens": 900,
                "cache_miss_tokens": 100,
                "model_calls": 3,
                "estimated_cost_usd": 0.01,
            }
            for cell in repetition_cells(repetition)
        ],
    }


def test_three_summaries_aggregate_into_one_matrix() -> None:
    merged = aggregate_plan_summaries(
        [_fake_summary(repetition) for repetition in FORMAL_REPETITIONS]
    )
    assert merged["status"] == "AGGREGATED"
    assert merged["plans_present"] == 3
    assert merged["cells_observed"] == merged["cells_planned"] == 54
    assert merged["oracle_passed"] == 54
    assert merged["provider_prompt_tokens"] == 54_000
    assert merged["model_calls"] == 162
    assert merged["cost_reconciliation"]["within_ceiling"] is True


def test_an_incomplete_or_duplicated_matrix_is_refused_or_flagged() -> None:
    partial = aggregate_plan_summaries([_fake_summary(0)])
    assert partial["status"] == "INCOMPLETE"
    assert partial["cells_observed"] == 18
    duplicated = aggregate_plan_summaries([_fake_summary(0), _fake_summary(0)])
    assert duplicated["status"] == "REFUSED"
    assert any("twice" in problem for problem in duplicated["problems"])


def test_a_summary_from_another_plan_cannot_be_merged() -> None:
    foreign = _fake_summary(0)
    foreign["pilot_plan_id"] = pilot_runner.PILOT_PLAN_ID
    merged = aggregate_plan_summaries([foreign])
    assert merged["status"] == "REFUSED"
    assert any("formal plans" in problem for problem in merged["problems"])


def test_a_missing_number_keeps_the_total_unknown() -> None:
    summaries = [_fake_summary(repetition) for repetition in FORMAL_REPETITIONS]
    summaries[1]["cells"][3]["cache_hit_tokens"] = None
    merged = aggregate_plan_summaries(summaries)
    assert merged["cache_hit_tokens"] is None
    # Every other term is still reported; one unknown does not poison the whole table.
    assert merged["provider_prompt_tokens"] == 54_000


def test_the_two_topologies_of_one_task_share_the_graded_task() -> None:
    for cell_id in ("closure2-pipeline-migration::A0", "closure2-pipeline-migration::A1-P"):
        task_id, _, arm_id = cell_id.partition("::")
        cell = Cell(task_id, arm_id)
        built = cell.benchmark_task()
        assert built.sample_id == task_id
        assert built.required_artifacts
        assert built.allowed_paths and built.forbidden_paths
        assert built.hidden_files and built.visible_files
