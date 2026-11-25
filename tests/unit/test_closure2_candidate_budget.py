"""Closure-2 S07: the owner-approved candidate budget profile and its runtime binding.

Offline only. No sandbox, no provider, no API key. The point of the file is the difference between
"a document says 500 000" and "the object the engine builds carries 500 000 and 20 turns", so the
binding assertions deliberately reach into the two engines' own code paths.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest
from experiments.evaluation.closure1b import native_baseline
from experiments.evaluation.closure2 import pilot_runner
from experiments.evaluation.closure2.candidate_budget import (
    CANDIDATE_BUDGET_REVISION,
    CANDIDATE_PROFILE,
    FROZEN_PROFILE,
    OWNER_DECISION,
    PROFILES_BY_DIGEST,
    UnknownBudgetProfile,
    candidate_payload,
    candidate_registration_digest,
    changed_knobs,
    check_candidate,
    profile_for_digest,
)
from experiments.evaluation.closure2.catalog import TASKS, task_digests
from experiments.evaluation.closure2.pilot_runner import (
    ARM_ORDER,
    Cell,
    frozen_budget,
    settings_for,
)
from experiments.evaluation.closure2.protocol import (
    BUDGET,
    C2A_FROZEN_TASK_DIGESTS,
    C2A_R1_PREREGISTRATION_DIGEST,
    preregistration_digest,
)
from experiments.evaluation.models import effective_agent_max_turns

TASK_IDS = [task.task_id for task in TASKS]


def test_the_candidate_satisfies_its_own_contract() -> None:
    assert check_candidate() == []


def test_only_the_two_approved_knobs_moved() -> None:
    changed = changed_knobs(left=FROZEN_PROFILE, right=CANDIDATE_PROFILE)
    # The token budget is one decision that lands on both topology blocks, so it appears twice.
    assert changed == (
        "multi.max_turns_per_worker",
        "multi.token_budget",
        "single.token_budget",
    )
    assert CANDIDATE_PROFILE.single_max_turns == FROZEN_PROFILE.single_max_turns == 36
    assert CANDIDATE_PROFILE.multi_workers == FROZEN_PROFILE.multi_workers == 3
    assert CANDIDATE_PROFILE.max_tool_calls == FROZEN_PROFILE.max_tool_calls == 90
    assert CANDIDATE_PROFILE.timeout_seconds == FROZEN_PROFILE.timeout_seconds == 1800


def test_the_frozen_profile_is_still_the_protocols_own_table() -> None:
    assert FROZEN_PROFILE.multi_worker_max_turns == BUDGET["multi"]["max_turns_per_worker"] == 12
    assert FROZEN_PROFILE.cell_token_budget == BUDGET["multi"]["token_budget"] == 260_000
    assert FROZEN_PROFILE.single_max_turns == BUDGET["single"]["max_turns"] == 36


def test_the_candidate_has_its_own_digest_and_a_new_registration_identity() -> None:
    assert CANDIDATE_PROFILE.digest() != FROZEN_PROFILE.digest()
    assert set(PROFILES_BY_DIGEST) == {FROZEN_PROFILE.digest(), CANDIDATE_PROFILE.digest()}
    assert profile_for_digest(FROZEN_PROFILE.digest()) is FROZEN_PROFILE
    assert profile_for_digest(CANDIDATE_PROFILE.digest()) is CANDIDATE_PROFILE
    with pytest.raises(UnknownBudgetProfile):
        profile_for_digest("0" * 64)
    candidate_registration = candidate_registration_digest()
    assert candidate_registration not in {FROZEN_PROFILE.digest(), CANDIDATE_PROFILE.digest()}
    assert candidate_registration == candidate_registration_digest()


def test_nothing_about_the_frozen_identity_moved() -> None:
    """The whole reason for a third budget object: 3080f313 and the task table stay put."""

    assert preregistration_digest() == C2A_R1_PREREGISTRATION_DIGEST
    assert pilot_runner.check_frozen_contract() == []
    for task in TASKS:
        live = task_digests(task)
        for key, expected in C2A_FROZEN_TASK_DIGESTS[task.task_id].items():
            assert live[key] == expected, f"{task.task_id}:{key}"


def test_multi_cells_are_built_with_twenty_turns_and_a_shared_500k_budget(tmp_path: Path) -> None:
    settings = settings_for(
        Cell(TASK_IDS[0], "A1-P"),
        runs_root=tmp_path,
        budget_profile=CANDIDATE_PROFILE,
    )
    assert settings.max_turns == 20
    assert settings.worker_max_turns == 20
    assert settings.token_budget == 500_000
    assert settings.max_tool_calls == 90


def test_the_single_arm_keeps_36_turns_and_the_same_500k_budget(tmp_path: Path) -> None:
    """A single-agent arm must not get less budget just because it has no workers."""

    for arm_id in ("N0", "A0"):
        settings = settings_for(
            Cell(TASK_IDS[0], arm_id), runs_root=tmp_path, budget_profile=CANDIDATE_PROFILE
        )
        assert settings.max_turns == 36
        assert settings.worker_max_turns is None
        assert settings.token_budget == 500_000


def test_the_frozen_default_is_unchanged_and_does_not_grow_a_key(tmp_path: Path) -> None:
    frozen = settings_for(Cell(TASK_IDS[0], "A1-P"), runs_root=tmp_path)
    assert (frozen.max_turns, frozen.worker_max_turns, frozen.token_budget) == (12, None, 260_000)
    # A ``None`` override is not in the digest, so every historical settings digest still holds.
    assert dataclasses.replace(frozen, worker_max_turns=20).digest() != frozen.digest()


def test_the_effective_cap_is_one_rule_for_both_engines() -> None:
    assert effective_agent_max_turns(step_max_turns=12, settings_max_turns=12) == 12
    assert effective_agent_max_turns(step_max_turns=12, settings_max_turns=20) == 12
    assert (
        effective_agent_max_turns(step_max_turns=12, settings_max_turns=20, worker_max_turns=20)
        == 20
    )
    assert effective_agent_max_turns(step_max_turns=None, settings_max_turns=36) == 36
    # Both engines call *this* function, rather than two expressions that can drift apart.
    from experiments.evaluation import runner

    assert runner.effective_agent_max_turns is native_baseline.effective_agent_max_turns
    for module in (runner, native_baseline):
        source = inspect.getsource(module)
        assert "step.max_turns or settings.max_turns" not in source


def test_the_frozen_task_still_declares_twelve_while_the_run_uses_twenty(
    tmp_path: Path,
) -> None:
    """The frozen catalogue is not edited; the run-level override is what raises the cap."""

    cell = Cell(TASK_IDS[0], "A1-L")
    assert cell.benchmark_task().workers[0].max_turns == 12
    settings = settings_for(cell, runs_root=tmp_path, budget_profile=CANDIDATE_PROFILE)
    effective = effective_agent_max_turns(
        step_max_turns=cell.benchmark_task().workers[0].max_turns,
        settings_max_turns=settings.max_turns,
        worker_max_turns=settings.worker_max_turns,
    )
    assert effective == 20


def test_all_six_arms_share_one_token_budget(tmp_path: Path) -> None:
    budgets = {}
    for arm_id in ARM_ORDER:
        settings = settings_for(
            Cell(TASK_IDS[0], arm_id), runs_root=tmp_path, budget_profile=CANDIDATE_PROFILE
        )
        budgets[arm_id] = (settings.max_turns, settings.token_budget)
    assert {budget[1] for budget in budgets.values()} == {500_000}
    assert budgets["N0"] == budgets["A0"] == (36, 500_000)
    for arm_id in ("N1", "A1-L", "A1-P", "A2-P"):
        assert budgets[arm_id] == (20, 500_000)


def test_the_frozen_budget_accessor_reports_the_profile_it_was_given() -> None:
    assert frozen_budget("multi")["max_turns_per_worker"] == 12
    assert frozen_budget("multi")["token_budget"] == 260_000
    candidate = frozen_budget("multi", profile=CANDIDATE_PROFILE)
    assert candidate["max_turns_per_worker"] == 20
    assert candidate["token_budget"] == 500_000
    assert candidate["workers"] == 3
    assert frozen_budget("single", profile=CANDIDATE_PROFILE)["max_turns"] == 36


def test_a_plan_that_names_an_unknown_profile_is_refused() -> None:
    plan = pilot_runner.PilotPlan(
        plan_id="closure2-derived-probe",
        benchmark_id="probe",
        preregistration_digest=C2A_R1_PREREGISTRATION_DIGEST,
        runner_revision="probe",
        cells=("closure2-pipeline-migration::N0",),
        repetitions=(0,),
        max_cell_executions=1,
        estimated_cost_ceiling_usd=0.01,
        budget_profile_digest="f" * 64,
    )
    with pytest.raises(UnknownBudgetProfile):
        plan.budget_profile()
    gate = pilot_runner.pilot_authorization_gate(
        cells=(Cell(TASK_IDS[0], "N0"),),
        repetition_id=0,
        plan=plan,
        record={
            "status": "APPROVED",
            "approved_by": "x",
            "approval_commit": "y",
            "approved_at": "z",
        },
    )
    assert gate["execution_ready"] is False
    assert any("budget profile" in problem for problem in gate["problems"])


def test_the_revision_record_carries_its_provenance() -> None:
    payload = candidate_payload()
    assert payload["revision"] == CANDIDATE_BUDGET_REVISION
    assert payload["status"] == "CANDIDATE_NOT_FROZEN_NOT_AUTHORIZED"
    assert payload["owner_decision"]["worker_max_turns"] == OWNER_DECISION["worker_max_turns"] == 20
    assert payload["owner_decision"]["per_cell_token_budget"] == 500_000
    assert payload["supersedes_for_future_stages_only"]["preregistration_digest"] == (
        C2A_R1_PREREGISTRATION_DIGEST
    )
    assert (
        payload["supersedes_for_future_stages_only"]["budget"]["multi"]["max_turns_per_worker"]
        == 12
    )
    assert payload["contract_problems"] == []
    assert payload["model_calls"] == 0
    assert payload["contract_problems"] == []
