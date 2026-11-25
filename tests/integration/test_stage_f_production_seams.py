"""Stage F production seams: the defects the Owner review found, as regression tests.

Each test names the defect it would have caught. Nothing here calls a model, opens a sandbox or
consumes a claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.evaluation.models import OracleOutcome
from experiments.stage_b_plus import executor
from experiments.stage_f import arms, cumulative, normalize, plan, runners

REPO = Path(__file__).resolve().parents[2]


def _cell(arm_id: str = "A1") -> dict:
    return next(cell for cell in plan.batch_cells(1) if cell["arm_id"] == arm_id)


def test_every_production_task_lookup_resolves_a_stage_f_task() -> None:
    """The gate and the live runner used the frozen Closure-2 catalog only."""

    source = (REPO / "experiments" / "stage_b_plus" / "executor.py").read_text(encoding="utf-8")
    assert 'contract.TASKS_BY_ID[cell["task_id"]]' not in source
    task = executor.task_for_cell({"task_id": "f-x2-distributed-diagnosis"})
    assert task.task_id == "f-x2-distributed-diagnosis"
    assert executor.task_for_cell({"task_id": "closure2-pipeline-migration"}).task_id


def test_a_cell_turn_cap_reaches_the_production_runner_config() -> None:
    """RunnerConfig defaulted to 20 Main turns and no cell could change it."""

    base = executor.RunnerConfig()
    assert base.main_max_turns == 20
    assert executor.cell_runner_config({"max_turns": 40}, base).main_max_turns == 40
    assert executor.cell_runner_config({}, base).main_max_turns == 20


def test_a0_admission_is_arm_specific() -> None:
    """A0/A2 were admitted through a GoalRunOptions they never run."""

    report, detail = runners.arm_admission(
        _cell("A0"),
        fixture={"matches_frozen_seed": True},
        config=executor.RunnerConfig(),
    )
    assert detail["admission"] == "ARM_SPECIFIC"
    assert detail["worker_profile"] is None
    assert detail["token_budget"] == 1_000_000
    assert report["status"] == "READY"
    assert {item["check"] for item in report["checks"]} >= {
        "shared_budget",
        "budget_guard",
        "turns",
    }


def test_the_result_adapter_uses_as_dict_and_refuses_a_guess() -> None:
    """A0 returned {} when ``to_dict`` was missing, and A2 ran ``dict(result)``."""

    class WithAsDict:
        def as_dict(self) -> dict[str, object]:
            return {"agent_success": True}

    assert runners._result_dict(WithAsDict()) == {"agent_success": True}

    class WithoutAsDict:
        def to_dict(self) -> dict[str, object]:
            return {"agent_success": True}

    with pytest.raises(TypeError):
        runners._result_dict(WithoutAsDict())


def test_the_oracle_status_is_derived_from_the_real_outcome_fields() -> None:
    """OracleOutcome has no ``status``; an environment failure is not a test failure."""

    assert runners._oracle_status(OracleOutcome(passed=True).as_dict()) == "PASS"
    assert runners._oracle_status(OracleOutcome(passed=False).as_dict()) == "FAIL"
    assert (
        runners._oracle_status(OracleOutcome(passed=False, environment_failure=True).as_dict())
        == "ERROR"
    )
    assert (
        runners._oracle_status(
            OracleOutcome(passed=False, cleanup_failure="destroy refused").as_dict()
        )
        == "ERROR"
    )


def test_the_stage_f_audit_returns_the_dispatcher_contract() -> None:
    """The audit returned neither delta_cost_usd nor cumulative usage."""

    cell = _cell("A0")
    evidence = normalize.from_native_result(
        cell,
        {
            "agent_success": True,
            "failure_class": None,
            "metrics": {
                "prompt_tokens": 1200,
                "completion_tokens": 200,
                "cache_hit_tokens": 800,
                "cache_miss_tokens": 400,
                "peak_context_tokens": 900,
            },
            "budget": {"limit_tokens": 1_000_000, "used_tokens": 1400},
            "environment": {"budget_enforcement": "pre_admission_guard"},
        },
        oracle={"status": "PASS", "passed": True},
        export={"static_acceptance": "PASS", "execution_integrity": "PASS"},
    )
    run = executor.CellRun(
        session=evidence, run_dir=None, fixture_root=None, oracle=None, duration_s=1.5
    )
    outcome = executor.audit_cell(
        cell=cell,
        run=run,
        plan={"plan_id": cell["plan_id"], "scope": cell["scope"]},
        usage_before={
            "main_cumulative_input_tokens": 10_000,
            "main_completion_tokens": 0,
            "child_total_prompt_tokens": 0,
            "child_completion_tokens": 0,
            "system_total_tokens": 10_000,
        },
        cost_before=0.5,
        prices=dict(executor.cost.stage_b_estimate.pricing()),
    )
    for field in ("delta_cost_usd", "cost_usd", "verdict", "usage", "stops", "duration_s"):
        assert field in outcome, field
    assert outcome["usage"]["system_total_tokens"] == 11_400
    assert outcome["cost_usd"] is not None and outcome["cost_usd"] > 0.5
    assert outcome["verdict"] == "PASS"


def test_cumulative_settlement_refuses_an_unknown_prior_batch(tmp_path: Path) -> None:
    """Three independent plans could each start with the full ceiling."""

    manifest = plan.manifest()
    b2 = cumulative.settlement(manifest, batch=2, evidence_root=tmp_path)
    assert b2["unknown_consumption"] is True
    assert b2["may_start"] is False

    evidence = tmp_path / plan.BATCH_PLAN_IDS[1]
    evidence.mkdir(parents=True)
    (evidence / "matrix-report.json").write_text(
        json.dumps(
            {
                "total_tokens": 30_000_000,
                "total_cost_usd": 30.0,
                "usage_completeness": cumulative.COMPLETE,
                "stop_reason": "NONE",
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    b2 = cumulative.settlement(manifest, batch=2, evidence_root=tmp_path)
    assert b2["unknown_consumption"] is False
    assert b2["may_start"] is True
    assert b2["allowed_tokens"] == min(32_000_000, 96_000_000 - 30_000_000)
    assert b2["allowed_cost_usd"] == pytest.approx(min(32.0, 96.0 - 30.0))


def test_the_runner_and_adapter_code_is_inside_the_signed_identity() -> None:
    """runners.py decides what a cell runs but was outside EXECUTOR_FILES."""

    for relative in (
        "experiments/stage_f/arms.py",
        "experiments/stage_f/plan.py",
        "experiments/stage_f/normalize.py",
        "experiments/stage_f/runners.py",
    ):
        assert relative in executor.EXECUTOR_FILES, relative


def test_the_stage_f_budget_and_turn_caps_are_the_approved_ones() -> None:
    assert arms.CELL_BUDGET["token_budget"] == 1_000_000
    assert arms.TOKEN_STOP_PER_BATCH == 32_000_000
    assert arms.TOKEN_STOP_TOTAL == 96_000_000
    assert arms.COST_STOP_PER_CELL == 1.00
    assert arms.COST_STOP_PER_BATCH == 32.00
    assert arms.COST_STOP_TOTAL == 96.00
