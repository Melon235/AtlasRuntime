"""Closure-2B-1: the connectivity canary's identity, gate, isolation and evidence mapping.

Everything here is offline. No sandbox is created, no provider is built, no API key is used: the
executor is always a stub, and the only approval that ever exists is a dictionary a test installs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from experiments.evaluation.closure2 import connectivity_canary as canary
from experiments.evaluation.closure2 import pilot_runner, protocol
from experiments.evaluation.closure2.catalog import TASKS, task_digests
from experiments.evaluation.closure2.connectivity_canary import (
    CANARY_APPROVAL,
    CANARY_BENCHMARK_ID,
    CANARY_HARD_CEILINGS,
    CANARY_ID,
    CANARY_PLAN,
    CANARY_TASK_ID,
    CanaryIsolationError,
    CanaryNotAuthorized,
    CanaryPlan,
    build_canary_task,
    build_precheck,
    canary_authorization_gate,
    cost_scenarios,
    normalize_canary_result,
    run_connectivity_canary,
)
from experiments.evaluation.closure2.pilot_ledger import (
    PilotAlreadyClaimed,
    PilotLedger,
    PilotLedgerLocked,
)

APPROVED_BY = "unit-test"


# --------------------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the per-user ledger, and never let two tests share a claim."""

    path = tmp_path / "canary-ledger.sqlite"
    monkeypatch.setattr(canary, "_ledger_factory", lambda: PilotLedger(path))
    return path


def approve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: CanaryPlan | None = None,
    ceiling: float | None = 0.05,
    status: str = "APPROVED",
    digest: str | None = None,
) -> CanaryPlan:
    """Approve a canary plan by substituting the module-level plan and record."""

    target = plan or dataclasses.replace(CANARY_PLAN, estimated_cost_ceiling_usd=ceiling)
    monkeypatch.setattr(canary, "CANARY_PLAN", target)
    monkeypatch.setattr(
        canary,
        "CANARY_APPROVAL",
        {
            "status": status,
            "approved_plan_digest": target.digest() if digest is None else digest,
            "approved_by": APPROVED_BY,
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-18T00:00:00+00:00",
        },
    )
    return target


def make_result(
    *,
    oracle_passed: bool = True,
    environment_failure: bool = False,
    failure_kind: str | None = "HIDDEN_TESTS_PASSED",
    sandbox_created: bool = True,
    sandbox_destroyed: bool = True,
    oracle_sandbox_id: str | None = "oracle-sandbox-1",
    prompt_tokens: int | None = 1000,
    completion_tokens: int | None = 50,
    cache_hit_tokens: int | None = 400,
    cache_miss_tokens: int | None = 600,
    cost: float | None = 0.0012,
    failure_class: str = "TASK_SUCCESS",
    cleanup_error: str | None = None,
    oracle_cleanup_error: str | None = None,
    extra: Any = None,
) -> Any:
    oracle = SimpleNamespace(
        passed=oracle_passed,
        environment_failure=environment_failure,
        failure_kind=failure_kind,
        execution_mode="sandbox",
        cleanup_failure=oracle_cleanup_error,
        details={"hidden_tests": {"sandbox_id": oracle_sandbox_id, "failure_kind": failure_kind}},
    )
    metrics = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        estimated_cost_usd=cost,
        sandbox_created=sandbox_created,
        sandbox_destroyed=sandbox_destroyed,
        tool_calls=4,
        model_turns=5,
        latency_ms=1234.5,
        extra=(
            {"cache_split_status": "COMPLETE", "cache_metrics_complete": True}
            if extra is None
            else extra
        ),
    )
    identity = SimpleNamespace(
        run_id="canary-run-0001",
        model="deepseek-chat",
        provider="deepseek",
        git_commit="0" * 40,
        git_dirty=False,
        repetition_id=0,
    )
    return SimpleNamespace(
        metrics=metrics,
        oracle=oracle,
        identity=identity,
        environment={"sandbox_image": "atlas-uat-python:3.12", "agent_latency_ms": 900.0},
        safety={"cleanup_error": cleanup_error},
        failure_class=failure_class,
        failure_reason=None if failure_class == "TASK_SUCCESS" else failure_class,
        success=oracle_passed and failure_class == "TASK_SUCCESS",
        agent_success=True,
        run_dir="experiments/runs/closure2-canary/canary-add-function/r0-canary-run-0001",
    )


def run_canary(
    tmp_path: Path,
    executor: Any,
    *,
    live_intent: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    return asyncio.run(
        run_connectivity_canary(
            runs_root=tmp_path / "runs",
            results_root=tmp_path / "results",
            live_intent=live_intent,
            executor=executor,
            **kwargs,
        )
    )


# --------------------------------------------------------------------- A. identity and isolation


def test_the_canary_identity_is_not_the_formal_pilot() -> None:
    """The pinned isolation constants still describe the live pilot, and never describe us."""

    assert pilot_runner.PILOT_PLAN.plan_id == canary.FORMAL_PILOT_PLAN_ID
    assert pilot_runner.PILOT_PLAN.digest() == canary.FORMAL_PILOT_PLAN_DIGEST
    assert pilot_runner.PILOT_PLAN.plan_id != CANARY_ID
    assert CANARY_PLAN.digest() != pilot_runner.PILOT_PLAN.digest()
    assert CANARY_BENCHMARK_ID != canary.FORMAL_BENCHMARK_ID


def test_the_canary_task_is_not_a_frozen_benchmark_task() -> None:
    from experiments.evaluation.models import canonical_digest

    task = build_canary_task()
    frozen_ids = {item.task_id for item in TASKS}
    frozen_hidden_paths = {path for item in TASKS for path in item.oracle_tests}
    frozen_seed_digests = {task_digests(item)["seed_digest"] for item in TASKS}
    canary_seed_digest = canonical_digest(dict(sorted(task.visible_files.items())))
    assert task.sample_id == CANARY_TASK_ID
    assert task.sample_id not in frozen_ids
    assert not set(task.hidden_files) & frozen_hidden_paths
    assert canary_seed_digest not in frozen_seed_digests
    assert task.mode == "coding"
    assert len(task.workers) == 1
    assert task.max_turns == canary.CANARY_LIMITS["max_agent_turns"]


def test_the_canary_ledger_is_a_separate_file() -> None:
    canary_ledger = canary.default_canary_ledger_path()
    pilot_ledger = pilot_runner.default_ledger_path()
    assert canary_ledger != pilot_ledger
    assert canary_ledger.parent == pilot_ledger.parent
    with pytest.raises(CanaryIsolationError):
        canary._claim_namespace_is_isolated(pilot_ledger)


def test_canary_evidence_may_not_land_in_the_formal_trees() -> None:
    for formal in (canary.FORMAL_RESULTS_ROOT, canary.FORMAL_RUNS_ROOT):
        with pytest.raises(CanaryIsolationError):
            canary._assert_isolated_output(formal / "anything", "results_root")
        with pytest.raises(CanaryIsolationError):
            canary._assert_isolated_output(formal, "results_root")
    canary._assert_isolated_output(canary.DEFAULT_CANARY_RESULTS_ROOT, "results_root")
    canary._assert_isolated_output(canary.DEFAULT_CANARY_RUNS_ROOT, "runs_root")


def test_the_canary_never_calls_the_live_pilot_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module has no reference to it, and a whole canary run does not reach it."""

    assert not hasattr(canary, "run_live_pilot")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the canary must never call run_live_pilot")

    monkeypatch.setattr(pilot_runner, "run_live_pilot", forbidden)
    approve(monkeypatch)

    async def executor(task: Any, **kwargs: Any) -> Any:
        return make_result()

    summary = run_canary(tmp_path, executor)
    assert summary["status"] == "CANARY_PASSED"


def test_a_canary_run_does_not_touch_the_formal_approval_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    benchmark_before = dict(protocol.APPROVAL_RECORD)
    pilot_before = dict(pilot_runner.PILOT_APPROVAL)
    plan_before = pilot_runner.PILOT_PLAN.digest()
    approve(monkeypatch)

    async def executor(task: Any, **kwargs: Any) -> Any:
        return make_result()

    run_canary(tmp_path, executor)
    assert benchmark_before == protocol.APPROVAL_RECORD
    assert pilot_before == pilot_runner.PILOT_APPROVAL
    assert pilot_runner.PILOT_PLAN.digest() == plan_before
    # No absolute approval status is asserted: the formal records are a different object whose
    # value is an owner decision, and this test's claim is that a canary run leaves them alone.


def test_the_canary_claim_lives_in_its_own_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    plan = approve(monkeypatch)

    async def executor(task: Any, **kwargs: Any) -> Any:
        return make_result()

    summary = run_canary(tmp_path, executor)
    ledger = PilotLedger(isolated_ledger)
    claim = ledger.get_claim(plan.digest())
    assert claim is not None
    assert claim["plan_id"] == CANARY_ID
    assert claim["claim_status"] == "CONSUMED"
    rows = ledger.cell_rows(plan.digest())
    assert [(row["cell_id"], row["status"]) for row in rows] == [(CANARY_TASK_ID, "COMPLETED")]
    assert summary["execution_claim"]["namespace_isolated_from_pilot"] is True
    assert not (tmp_path / "pilot-ledger.sqlite").exists()


def test_a_canary_run_leaves_the_formal_dry_run_manifest_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = canary.FORMAL_DRY_RUN_ARTIFACT
    before_bytes = committed.read_bytes() if committed.is_file() else None
    first = pilot_runner.build_dry_run_manifest()
    approve(monkeypatch)

    async def executor(task: Any, **kwargs: Any) -> Any:
        return make_result()

    run_canary(tmp_path, executor)
    second = pilot_runner.build_dry_run_manifest()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
    assert second["cell_count"] == 18
    assert second["agent_sandbox_runs_total"] == 0
    if before_bytes is not None:
        assert committed.read_bytes() == before_bytes


# ------------------------------------------------------------------------ B. authorization paths


def test_the_default_state_is_consumed_and_unauthorized() -> None:
    """The one authorized attempt has been spent: the record is kept, and it authorises nothing."""

    gate = canary_authorization_gate(live_intent=False)
    assert gate["live_ready"] is False
    assert gate["approval_status"] == "CONSUMED_BY_EXECUTION"
    assert "LIVE_INTENT_NOT_CONFIRMED" in gate["reason_codes"]
    assert "CANARY_APPROVAL_NOT_APPROVED" in gate["reason_codes"]
    # The audit trail survives the consumption: it still names the plan it authorised and when it
    # was spent, so "consumed" is readable instead of being an empty refusal.
    assert CANARY_APPROVAL["approved_plan_digest"] == CANARY_PLAN.digest()
    assert CANARY_APPROVAL["consumed_at"]
    assert CANARY_APPROVAL["consumed_attempt_id"]


def test_a_consumed_authorization_cannot_run_again(tmp_path: Path) -> None:
    """The live default is no longer pending, so the refusal is proved against that default."""

    calls: list[Any] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return make_result()

    with pytest.raises(CanaryNotAuthorized) as caught:
        run_canary(tmp_path, executor)
    assert caught.value.reason_code == "CANARY_APPROVAL_NOT_APPROVED"
    assert calls == []
    assert not (tmp_path / "results" / "canary-summary.json").exists()


def test_a_pending_approval_is_refused_by_explicit_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending is no longer the live default, so it is exercised by installing the record."""

    monkeypatch.setattr(
        canary,
        "CANARY_APPROVAL",
        {
            "status": "PENDING_OWNER_APPROVAL",
            "approved_plan_digest": None,
            "approved_by": None,
            "approval_commit": None,
            "approved_at": None,
        },
    )
    gate = canary_authorization_gate(live_intent=True)
    assert gate["live_ready"] is False
    assert "CANARY_APPROVAL_NOT_APPROVED" in gate["reason_codes"]
    assert "CANARY_APPROVAL_INCOMPLETE" in gate["reason_codes"]


def test_live_without_approval_is_refused_before_the_executor(tmp_path: Path) -> None:
    calls: list[Any] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("unreachable")

    with pytest.raises(CanaryNotAuthorized) as caught:
        run_canary(tmp_path, executor)
    assert caught.value.reason_code in caught.value.reason_codes
    assert "CANARY_APPROVAL_NOT_APPROVED" in caught.value.reason_codes
    assert calls == []
    assert not (tmp_path / "results" / "canary-summary.json").exists()


def test_approval_without_live_intent_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)
    calls: list[Any] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return make_result()

    with pytest.raises(CanaryNotAuthorized) as caught:
        run_canary(tmp_path, executor, live_intent=False)
    assert "LIVE_INTENT_NOT_CONFIRMED" in caught.value.reason_codes
    assert calls == []


def test_a_different_approved_digest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    approve(monkeypatch, digest="f" * 64)
    gate = canary_authorization_gate(live_intent=True)
    assert gate["live_ready"] is False
    assert "CANARY_PLAN_DIGEST_MISMATCH" in gate["reason_codes"]


def test_a_different_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    approve(
        monkeypatch,
        plan=dataclasses.replace(
            CANARY_PLAN, model="deepseek-pro", estimated_cost_ceiling_usd=0.05
        ),
    )
    gate = canary_authorization_gate(live_intent=True)
    assert "CANARY_MODEL_MISMATCH" in gate["reason_codes"]
    assert gate["expected_model"] == canary.CANARY_MODEL


def test_budget_drift_beyond_the_hard_ceiling_is_refused_even_when_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching digest is not enough: an approved drift above the ceiling is still refused."""

    drifted = dataclasses.replace(
        CANARY_PLAN,
        max_agent_turns=CANARY_HARD_CEILINGS["max_agent_turns"] + 1,
        estimated_cost_ceiling_usd=0.05,
    )
    approve(monkeypatch, plan=drifted)
    gate = canary_authorization_gate(live_intent=True)
    assert gate["approved_plan_digest"] == drifted.digest()
    assert gate["live_ready"] is False
    assert "CANARY_BUDGET_DRIFT" in gate["reason_codes"]


def test_budget_drift_within_the_ceiling_changes_the_digest_and_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approve(monkeypatch)
    drifted = dataclasses.replace(canary.CANARY_PLAN, max_agent_turns=5)
    gate = canary_authorization_gate(live_intent=True, plan=drifted)
    assert gate["live_ready"] is False
    assert "CANARY_PLAN_DIGEST_MISMATCH" in gate["reason_codes"]


def test_an_extra_canary_task_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a frozen benchmark task id is refused: the canary owns exactly one task."""

    for task_id in ("closure2-pipeline-migration", "canary-something-else"):
        plan = dataclasses.replace(CANARY_PLAN, task_id=task_id, estimated_cost_ceiling_usd=0.05)
        record = {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": APPROVED_BY,
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-18T00:00:00+00:00",
        }
        gate = canary_authorization_gate(live_intent=True, plan=plan, record=record)
        assert gate["live_ready"] is False
        assert "CANARY_TASK_MISMATCH" in gate["reason_codes"]


def test_more_than_one_attempt_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    approve(
        monkeypatch,
        plan=dataclasses.replace(CANARY_PLAN, attempts=2, estimated_cost_ceiling_usd=0.05),
    )
    gate = canary_authorization_gate(live_intent=True)
    assert "CANARY_ATTEMPTS_EXCEEDED" in gate["reason_codes"]


@pytest.mark.parametrize(
    ("ceiling", "reason"),
    [
        (None, "CANARY_COST_CEILING_NOT_APPROVED"),
        (float("nan"), "CANARY_COST_CEILING_INVALID"),
        (float("inf"), "CANARY_COST_CEILING_INVALID"),
        ("5", "CANARY_COST_CEILING_INVALID"),
        (True, "CANARY_COST_CEILING_INVALID"),
        (-1, "CANARY_COST_CEILING_NOT_POSITIVE"),
        (0, "CANARY_COST_CEILING_NOT_POSITIVE"),
    ],
)
def test_an_illegal_cost_ceiling_is_refused(
    monkeypatch: pytest.MonkeyPatch, ceiling: Any, reason: str
) -> None:
    approve(monkeypatch, ceiling=ceiling)
    gate = canary_authorization_gate(live_intent=True)
    assert gate["live_ready"] is False
    assert reason in gate["reason_codes"]
    assert gate["cost_policy"]["hard_enforcement"] is False
    assert gate["cost_policy"]["guarantee"] == "COST_CEILING_NOT_HARD_ENFORCED"


def test_a_plan_that_collides_with_the_formal_pilot_is_refused() -> None:
    collided = dataclasses.replace(CANARY_PLAN, canary_id=canary.FORMAL_PILOT_PLAN_ID)
    record = {
        "status": "APPROVED",
        "approved_plan_digest": collided.digest(),
        "approved_by": APPROVED_BY,
        "approval_commit": "0" * 40,
        "approved_at": "2026-11-18T00:00:00+00:00",
    }
    gate = canary_authorization_gate(live_intent=True, plan=collided, record=record)
    assert "CANARY_FORMAL_IDENTITY_COLLISION" in gate["reason_codes"]
    assert "CANARY_IDENTITY_MISMATCH" in gate["reason_codes"]


def test_a_second_claim_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    plan = approve(monkeypatch)
    PilotLedger(isolated_ledger).claim_plan(
        plan_digest=plan.digest(),
        plan_id=CANARY_ID,
        cells=[(CANARY_TASK_ID, 0)],
        runner_revision="unit-test",
    )
    calls: list[Any] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return make_result()

    with pytest.raises(PilotAlreadyClaimed):
        run_canary(tmp_path, executor)
    assert calls == []
    assert not (tmp_path / "results" / "canary-summary.json").exists()


# -------------------------------------------------------------------------- C. offline execution


def test_a_passing_canary_records_every_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)
    calls: list[Any] = []

    async def executor(task: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["settings"])
        return make_result()

    summary = run_canary(tmp_path, executor)
    assert len(calls) == 1
    assert summary["status"] == "CANARY_PASSED"
    assert summary["passed"] is True
    assert summary["execution_integrity_status"] == "COMPLETE"
    assert summary["record"]["oracle_passed"] is True
    assert summary["record"]["agent_sandbox_created"] is True
    assert summary["record"]["agent_sandbox_destroyed"] is True
    assert summary["record"]["oracle_sandbox_status"] == "CREATED_CONFIRMED"
    assert summary["auto_retry"] is False
    assert summary["retry_count"] == 0
    evidence = json.loads((tmp_path / "results" / "canary-summary.json").read_text("utf-8"))
    assert evidence["status"] == "CANARY_PASSED"
    assert evidence["execution_claim"]["claim_status"] == "CONSUMED"


def test_the_canary_settings_are_the_canary_contract(tmp_path: Path) -> None:
    settings = canary.canary_settings(runs_root=tmp_path)
    assert settings.benchmark_id == CANARY_BENCHMARK_ID
    assert settings.benchmark_id != canary.FORMAL_BENCHMARK_ID
    assert settings.max_turns == canary.CANARY_LIMITS["max_agent_turns"]
    assert settings.max_tokens == canary.CANARY_LIMITS["max_output_tokens_per_request"]
    assert settings.token_budget == canary.CANARY_LIMITS["max_prompt_token_budget"]
    assert settings.max_tool_calls == canary.CANARY_LIMITS["max_tool_calls"]
    assert settings.timeout_seconds == canary.CANARY_LIMITS["timeout_seconds"]
    assert settings.model_override == canary.CANARY_MODEL
    assert settings.budget_mode == "guarded"
    assert settings.oracle_mode == "sandbox"
    assert settings.delegation_route_mode == "off"
    assert settings.retain_sandbox_on_failure is False


def test_a_provider_error_is_recorded_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    plan = approve(monkeypatch)
    calls: list[int] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("provider exploded")

    summary = run_canary(tmp_path, executor)
    assert len(calls) == 1
    assert summary["status"] == "EXECUTION_ERROR"
    assert summary["passed"] is False
    assert summary["record"] is None
    assert summary["error"]["error_class"] == "RuntimeError"
    assert summary["error"]["sandbox_state"] == "UNKNOWN"
    rows = PilotLedger(isolated_ledger).cell_rows(plan.digest())
    assert [(row["status"], row["failure_reason"]) for row in rows] == [
        ("FAILED", "EXECUTION_ERROR")
    ]


def test_a_sandbox_creation_failure_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(
            oracle_passed=False,
            sandbox_created=False,
            oracle_sandbox_id=None,
            failure_kind="SANDBOX_PROVISIONING_FAILURE",
            failure_class="SANDBOX_FAILURE",
            prompt_tokens=None,
            completion_tokens=None,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            cost=None,
        )

    summary = run_canary(tmp_path, executor)
    assert summary["status"] == "CANARY_FAILED"
    assert summary["record"]["agent_sandbox_created"] is False
    assert summary["record"]["oracle_sandbox_status"] == "NOT_CREATED_CONFIRMED"
    assert summary["record"]["provider_prompt_tokens"] is None


def test_a_cleanup_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(
            oracle_passed=False,
            sandbox_destroyed=False,
            cleanup_error="provider refused destroy",
            failure_class="CLEANUP_FAILURE",
        )

    summary = run_canary(tmp_path, executor)
    assert summary["status"] == "CANARY_FAILED"
    assert summary["record"]["agent_sandbox_destroyed"] is False
    assert summary["record"]["cleanup_error"] == "provider refused destroy"


def test_a_leaked_oracle_sandbox_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correct answer with an oracle sandbox that could not be destroyed is a failed canary."""

    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(oracle_cleanup_error="oracle sandbox destroy refused")

    summary = run_canary(tmp_path, executor)
    assert summary["record"]["oracle_passed"] is True
    assert summary["record"]["oracle_cleanup_failure"] == "oracle sandbox destroy refused"
    assert summary["status"] == "CANARY_FAILED"
    assert summary["passed"] is False


def test_an_oracle_deliverable_failure_is_not_an_environment_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(
            oracle_passed=False, failure_kind="HIDDEN_TESTS_FAILED", failure_class="ORACLE_FAILURE"
        )

    summary = run_canary(tmp_path, executor)
    assert summary["status"] == "CANARY_FAILED"
    assert summary["record"]["oracle_passed"] is False
    assert summary["record"]["oracle_environment_failure"] is False
    assert summary["record"]["oracle_failure_kind"] == "HIDDEN_TESTS_FAILED"
    assert summary["record"]["oracle_sandbox_status"] == "CREATED_CONFIRMED"


def test_an_oracle_environment_failure_is_not_charged_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(
            oracle_passed=False,
            environment_failure=True,
            failure_kind="SANDBOX_PROVISIONING_FAILURE",
            oracle_sandbox_id=None,
            failure_class="ENVIRONMENT_FAILURE",
        )

    summary = run_canary(tmp_path, executor)
    assert summary["record"]["oracle_environment_failure"] is True
    assert summary["record"]["oracle_sandbox_status"] == "NOT_CREATED_CONFIRMED"
    assert summary["status"] == "CANARY_FAILED"


def test_missing_usage_and_missing_cost_stay_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(
            prompt_tokens=None,
            completion_tokens=None,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            cost=None,
            extra={},
        )

    summary = run_canary(tmp_path, executor)
    record = summary["record"]
    assert record["provider_prompt_tokens"] is None
    assert record["provider_completion_tokens"] is None
    assert record["cache_hit_tokens"] is None
    assert record["cache_split_status"] is None
    assert record["provider_usage_observed"] is False
    assert record["estimated_cost_usd"] is None
    assert record["estimated_cost_usd_state"] == "UNKNOWN"
    assert record["cost_vs_ceiling"] == "UNKNOWN"
    written = json.loads((tmp_path / "results" / "canary-summary.json").read_text("utf-8"))
    assert written["record"]["estimated_cost_usd"] is None
    assert written["record"]["provider_prompt_tokens"] is None


def test_an_invalid_cost_is_not_coerced_to_zero() -> None:
    for value in (float("nan"), float("inf"), -3.0):
        record = normalize_canary_result(make_result(cost=value))
        assert record["estimated_cost_usd"] is None
        assert record["estimated_cost_usd_state"] == "INVALID"


def test_an_execution_timeout_is_recorded_without_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path
) -> None:
    plan = approve(monkeypatch)
    calls: list[int] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        await asyncio.sleep(0.5)
        return make_result()

    summary = run_canary(tmp_path, executor, outer_timeout_seconds=0.01)
    assert len(calls) == 1
    assert summary["status"] == "EXECUTION_TIMEOUT"
    assert summary["error"]["error_class"] == "TimeoutError"
    assert summary["error"]["auto_retry"] is False
    rows = PilotLedger(isolated_ledger).cell_rows(plan.digest())
    assert [row["status"] for row in rows] == ["FAILED"]


def test_a_failed_running_write_stops_the_cell_before_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)
    calls: list[Any] = []
    original = PilotLedger.set_cell_state

    def fail_running(
        self: PilotLedger,
        plan_digest: str,
        cell_id: str,
        repetition_id: int,
        status: str,
        **fields: Any,
    ) -> None:
        if status == "RUNNING":
            raise PilotLedgerLocked("PILOT_LEDGER_LOCKED: forced")
        original(self, plan_digest, cell_id, repetition_id, status, **fields)

    monkeypatch.setattr(PilotLedger, "set_cell_state", fail_running)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return make_result()

    summary = run_canary(tmp_path, executor)
    assert calls == []
    assert summary["status"] == "BLOCKED_LEDGER_PRE_EXECUTION"
    assert summary["execution_integrity_status"] == "BLOCKED_LEDGER_PRE_EXECUTION"
    assert summary["execution_integrity_ok"] is False
    assert summary["ledger_integrity"]["phase"] == "PRE_EXECUTION"
    assert summary["ledger_integrity"]["executed_before_failure"] is False
    assert summary["record"] is None


def test_a_failed_completion_write_keeps_the_real_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)
    original = PilotLedger.set_cell_state

    def fail_completed(
        self: PilotLedger,
        plan_digest: str,
        cell_id: str,
        repetition_id: int,
        status: str,
        **fields: Any,
    ) -> None:
        if status == "COMPLETED":
            raise PilotLedgerLocked("PILOT_LEDGER_LOCKED: forced")
        original(self, plan_digest, cell_id, repetition_id, status, **fields)

    monkeypatch.setattr(PilotLedger, "set_cell_state", fail_completed)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result()

    summary = run_canary(tmp_path, executor)
    assert summary["status"] == "BLOCKED_LEDGER_POST_EXECUTION"
    assert summary["passed"] is False
    assert summary["record"] is not None
    assert summary["record"]["raw_result_path"].endswith("result.json")
    assert summary["ledger_integrity"]["phase"] == "POST_EXECUTION"
    assert summary["ledger_integrity"]["executed_before_failure"] is True


def test_a_normalisation_failure_invents_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result(extra="not-a-mapping")

    summary = run_canary(tmp_path, executor)
    assert summary["status"] == "EVIDENCE_NORMALIZATION_FAILED"
    assert summary["record"] is None
    assert summary["error"]["error_class"] == "AttributeError"


def test_an_unwritable_evidence_path_refuses_to_claim_it_was_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result()

    with pytest.raises(canary.CanaryEvidenceError) as caught:
        asyncio.run(
            run_connectivity_canary(
                runs_root=tmp_path / "runs",
                results_root=blocker / "results",
                live_intent=True,
                executor=executor,
            )
        )
    assert caught.value.reason_code == "CANARY_EVIDENCE_NOT_SAVED"


# ------------------------------------------------------------------------------- D. evidence rules


def test_a_passing_canary_is_never_a_benchmark_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result()

    summary = run_canary(tmp_path, executor)
    assert summary["is_benchmark"] is False
    assert summary["is_official_pilot"] is False
    assert summary["counts_toward_formal_results"] is False
    assert summary["benchmark_id"] == CANARY_BENCHMARK_ID
    assert summary["formal_benchmark_id"] == canary.FORMAL_BENCHMARK_ID
    assert summary["record"]["benchmark_success_claimed"] is False
    assert summary["record"]["counts_toward_formal_results"] is False
    assert "not a Closure-2A sample" in summary["not_benchmark_evidence"]
    # No field exists that a pilot table could sum by mistake.
    assert "estimated_cost_usd_total" not in summary
    assert "task_success_rate" not in summary


def test_the_canary_reports_no_api_key_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-canary-secret-0000000000000000"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    approve(monkeypatch)
    precheck = build_precheck(runs_root=tmp_path / "runs", results_root=tmp_path / "results")

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return make_result()

    summary = run_canary(tmp_path, executor)
    for payload in (precheck, summary):
        serialised = json.dumps(payload)
        assert secret not in serialised
        assert secret[:6] not in serialised
    assert precheck["credentials"]["present"] is True
    assert precheck["credentials"]["value_printed"] is False
    assert precheck["credentials"]["variable"] == "DEEPSEEK_API_KEY"


def test_the_precheck_touches_no_provider_and_no_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasos.sandbox import provider as sandbox_provider

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the precheck must not construct a sandbox provider")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-unit-test")
    monkeypatch.setattr(sandbox_provider, "build_sandbox_provider", forbidden)
    precheck = build_precheck(runs_root=tmp_path / "runs", results_root=tmp_path / "results")
    assert precheck["model_calls"] == 0
    assert precheck["sandbox_runs"] == 0
    assert precheck["live_authorized"] is False
    # The precheck reports the record it found, never a status this test wishes for: an approval
    # that has already been spent must stay visibly unusable here.
    assert precheck["approval"]["status"] != "APPROVED"
    assert "CANARY_APPROVAL_NOT_APPROVED" in precheck["approval"]["reason_codes"]
    assert precheck["precheck_ready"] is True
    assert precheck["blocking_problems"] == []


def test_the_precheck_reports_a_missing_key_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    state = canary._api_key_state("DEEPSEEK_API_KEY")
    assert state["source"] == "missing"
    assert state["present"] is False
    assert state["value_printed"] is False


# ----------------------------------------------------------------------- E. cost preparation table


def test_the_cost_scenarios_are_ordered_and_labelled() -> None:
    payload = cost_scenarios()
    assert payload["status"] == "ESTIMATE_CONDITIONAL_ON_ASSUMPTION"
    assert payload["pricing"]["pricing_status"] == "PRICING_NOT_LIVE_VERIFIED"
    assert payload["pricing"]["live_verified"] is False
    assert payload["pricing"]["snapshot_date"] == "2026-11-06"
    scenarios = payload["scenarios"]
    assert list(scenarios) == ["LOW", "EXPECTED", "HIGH"]
    costs = [scenarios[name]["estimated_cost_usd"] for name in ("LOW", "EXPECTED", "HIGH")]
    assert costs == sorted(costs)
    assert all(
        scenario["status"] == "ESTIMATE_CONDITIONAL_ON_ASSUMPTION"
        for scenario in scenarios.values()
    )
    assert len(payload["cells"]) == 8
    assert payload["upper_bound"]["total_usd"] is None
    assert payload["upper_bound"]["status"] == "UPPER_BOUND_INCOMPLETE"


def test_the_cost_scenarios_price_every_cell_from_the_frozen_pricing() -> None:
    payload = cost_scenarios()
    pricing = payload["pricing"]
    for name, scenario in payload["scenarios"].items():
        recomputed = 0.0
        for cell in scenario["cells"]:
            recomputed += (
                cell["cache_hit_tokens"] * pricing["cache_hit_input_per_million"] / 1_000_000
                + cell["cache_miss_tokens"] * pricing["cache_miss_input_per_million"] / 1_000_000
                + cell["output_tokens"] * pricing["output_per_million"] / 1_000_000
            )
            assert cell["cache_hit_tokens"] + cell["cache_miss_tokens"] == cell["input_tokens"]
        assert round(recomputed, 4) == scenario["estimated_cost_usd"], name
        assert scenario["input_tokens"] == sum(cell["input_tokens"] for cell in scenario["cells"])


def test_the_direct_cell_is_costed_as_a_single_worker() -> None:
    cells = {cell["cell_id"]: cell for cell in cost_scenarios()["cells"]}
    assert cells["closure2-state-machine-refactor::A2-P"]["topology"] == "single"
    assert cells["closure2-pipeline-migration::A2-P"]["topology"] == "multi"


def test_the_contract_upper_bound_is_read_from_the_frozen_budget_table() -> None:
    from experiments.evaluation.closure2.protocol import BUDGET

    payload = cost_scenarios()
    bound = payload["upper_bound"]
    pricing = payload["pricing"]
    worst_rate = max(pricing["cache_miss_input_per_million"], pricing["output_per_million"])
    assert bound["atlas"]["cells"] == 6
    assert bound["atlas"]["per_cell_usd_approximate"] == round(
        BUDGET["single"]["token_budget"] * worst_rate / 1_000_000, 4
    )
    assert bound["native"]["output_tokens_cap"] == (
        BUDGET["single"]["max_turns"] * BUDGET["shared"]["max_tokens_per_request"]
    )
    assert bound["native"]["prompt_tokens"] == "UNBOUNDED_BY_CONTRACT"
    assert bound["total_usd"] is None


def test_model_calls_prefer_an_observed_request_count() -> None:
    observed = normalize_canary_result(
        make_result(
            extra={
                "request_evidence": {"requests_observed": 5},
                "cache_split_status": "COMPLETE",
            }
        )
    )
    assert observed["model_calls"] == 5
    assert observed["model_calls_source"] == "request_evidence.requests_observed"
    assert observed["model_calls_semantics"] == "requests_observed"

    fallback = normalize_canary_result(make_result())
    assert fallback["model_calls_source"] == "metrics.model_turns"
    assert fallback["model_calls_semantics"] == "completed_model_turns"


def test_the_cost_scenarios_run_nothing() -> None:
    from atlasos.sandbox import provider as sandbox_provider

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the cost table must not build a sandbox provider")

    original = sandbox_provider.build_sandbox_provider
    sandbox_provider.build_sandbox_provider = forbidden
    try:
        payload = cost_scenarios()
    finally:
        sandbox_provider.build_sandbox_provider = original
    assert payload["scenarios"]["LOW"]["estimated_cost_usd"] > 0
    assert canary.PROPOSED_COST_CEILING_USD > 0


# ---------------------------------------------------------------------------------- F. dry-run CLI


def test_the_cli_precheck_writes_an_offline_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-unit-test")
    out = tmp_path / "precheck.json"
    exit_code = canary.main(
        [
            "--precheck",
            "--out",
            str(out),
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "OFFLINE_PRECHECK"
    assert payload["model_calls"] == 0
    assert payload["sandbox_runs"] == 0
    assert payload["live_authorized"] is False
    assert str(canary.REPO_ROOT) not in out.read_text(encoding="utf-8")


def test_the_precheck_out_path_cannot_overwrite_the_frozen_manifest(
    tmp_path: Path,
) -> None:
    """``--out`` is operator input, so it gets the same isolation guard as the output roots."""

    frozen = canary.FORMAL_DRY_RUN_ARTIFACT
    before = frozen.read_bytes() if frozen.is_file() else None
    with pytest.raises(CanaryIsolationError):
        canary.write_precheck(
            frozen,
            runs_root=tmp_path / "runs",
            results_root=tmp_path / "results",
        )
    if before is not None:
        assert frozen.read_bytes() == before


def test_the_cli_refuses_a_precheck_out_inside_the_formal_tree(tmp_path: Path) -> None:
    frozen = canary.FORMAL_DRY_RUN_ARTIFACT
    before = frozen.read_bytes() if frozen.is_file() else None
    exit_code = canary.main(
        [
            "--precheck",
            "--out",
            str(frozen),
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 3
    if before is not None:
        assert frozen.read_bytes() == before
    assert not (tmp_path / "results" / "canary-precheck.json").exists()


def test_the_cli_refuses_live_without_the_second_confirmation(tmp_path: Path) -> None:
    exit_code = canary.main(
        [
            "--live",
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 2
    assert not (tmp_path / "results" / "canary-summary.json").exists()


def test_the_cli_refuses_live_while_the_canary_is_pending(
    tmp_path: Path, isolated_ledger: Path
) -> None:
    exit_code = canary.main(
        [
            "--live",
            "--confirm-live",
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 3
    assert not (tmp_path / "results" / "canary-summary.json").exists()
    assert not isolated_ledger.exists()


def test_the_cli_estimate_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = canary.main(["--estimate", "--results-root", str(tmp_path / "results")])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "ESTIMATE_CONDITIONAL_ON_ASSUMPTION"
    assert not (tmp_path / "results").exists()


def test_the_plan_identity_is_stable_across_calls() -> None:
    assert CANARY_PLAN.digest() == canary.CANARY_PLAN.digest()
    assert CANARY_PLAN.digest() == CANARY_PLAN.digest()
    payload = CANARY_PLAN.payload()
    assert payload["is_benchmark"] is False
    assert payload["counts_toward_formal_results"] is False
    assert payload["attempts"] == 1
