"""Closure-2C-1: the routing pair plan, its refusals, and the offline analysis readers.

Everything here is offline. No sandbox is built, no provider is created, no API key is read: the
executor is always a stub, the ledger is always a temporary file, and the approval records are
either the real (pending) ones or dictionaries a test installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from experiments.evaluation.closure2 import pilot_runner, protocol, routing_pair_analysis
from experiments.evaluation.closure2.pilot_ledger import PilotLedger
from experiments.evaluation.closure2.pilot_runner import (
    ROUTING_PAIR_APPROVAL,
    ROUTING_PAIR_CELLS,
    ROUTING_PAIR_PLAN,
    ROUTING_PAIR_PLAN_ID,
    Cell,
    PilotNotAuthorized,
    pilot_authorization_gate,
    run_live_pilot,
)
from experiments.evaluation.closure2.routing_pair_analysis import (
    NOT_APPLICABLE,
    UNKNOWN,
    compare_pair,
    deliverable_correctness,
    efficiency,
    pairing_report,
    summarize_cell,
    worker_contract_compliance,
)

TASK = "closure2-state-machine-refactor"
PIPELINE = "closure2-pipeline-migration"

#: Frozen upstream identities this stage must not move.
FROZEN_PREREGISTRATION = "3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765"
FROZEN_ROUTER = "5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244"

#: The pilot's executed plan, and the routing pair's preparation identity (ceiling unset, pending).
PILOT_PLAN_DIGEST = "0f61f84ad195fbfb2e27f4c80f7906530066f7128da84595b0293dfd0b2e32bb"
ROUTING_PAIR_PLAN_DIGEST = "ef8e65e788cfb898e703411128b946c57345c88b9970829039885046e8ff97a3"

APPROVED_BENCHMARK = {
    "status": "APPROVED",
    "approved_digest": FROZEN_PREREGISTRATION,
    "approved_by": "unit-test",
    "approval_commit": "0" * 40,
    "approved_at": "2026-11-18T00:00:00+00:00",
}


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The production ledger is per-user and permanent, so a test never touches it."""

    path = tmp_path / "pilot-ledger.sqlite"
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: PilotLedger(path))
    return path


@pytest.fixture(autouse=True)
def no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused run must never reach a real engine, even if a gate were to fail open."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an offline check must never build an engine or a sandbox")

    monkeypatch.setattr(pilot_runner, "_default_executor", forbidden, raising=False)


def approve_pair(monkeypatch: pytest.MonkeyPatch, *, ceiling: float | None = 5.0) -> Any:
    """Install an approved pair plan and its record, exactly as the owner would.

    ``approve_pilot`` in the pilot tests does the same for the eight-cell plan: the record is the
    only thing a test may substitute, and the plan digest must follow the substituted plan.
    """

    plan = dataclasses.replace(ROUTING_PAIR_PLAN, estimated_cost_ceiling_usd=ceiling)
    monkeypatch.setattr(pilot_runner, "ROUTING_PAIR_PLAN", plan)
    monkeypatch.setattr(
        pilot_runner,
        "ROUTING_PAIR_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": "unit-test",
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-18T00:00:00+00:00",
        },
    )
    return plan


def _fake_result(**overrides: Any) -> SimpleNamespace:
    metrics = SimpleNamespace(
        prompt_tokens=overrides.pop("prompt_tokens", 1000),
        completion_tokens=overrides.pop("completion_tokens", 200),
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        estimated_cost_usd=overrides.pop("estimated_cost_usd", 0.01),
        model_turns=3,
        tool_calls=5,
        latency_ms=1234.0,
        sandbox_created=True,
        sandbox_destroyed=True,
        worker_selection=list(overrides.pop("workers", ("w1",))),
        provider_calls=None,
        routing=None,
        worker_contract={"mode": "legacy", "workers": []},
        extra=overrides.pop("extra", {}),
    )
    identity = SimpleNamespace(
        run_id="eval-pair",
        repetition_id=0,
        model="deepseek-chat",
        git_commit="deadbeef",
        git_dirty=False,
    )
    oracle = SimpleNamespace(
        passed=overrides.pop("oracle_passed", True),
        environment_failure=overrides.pop("environment_failure", False),
        failure_kind=overrides.pop("failure_kind", "HIDDEN_TESTS_PASSED"),
    )
    environment = {
        "sandbox_image": "atlas-uat-python:3.12",
        "workspace_isolation": "remote_opensandbox_container",
        "agent_latency_ms": 900.0,
    }
    return SimpleNamespace(
        identity=identity,
        metrics=metrics,
        oracle=oracle,
        environment=environment,
        run_dir="experiments/runs/closure2-routing-pair/x",
        agent_success=overrides.pop("agent_success", True),
        success=overrides.pop("success", True),
        failure_class=overrides.pop("failure_class", None),
        failure_reason=overrides.pop("failure_reason", None),
    )


def _oracle_payload(checks: dict[str, Any], *, passed: bool, workers: int) -> dict[str, Any]:
    return {
        "oracle": {
            "checks": checks,
            "passed": passed,
            "failure_kind": "HIDDEN_TESTS_PASSED" if passed else "HIDDEN_TESTS_FAILED",
            "environment_failure": False,
        },
        "metrics": {
            "provider_calls": 4,
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "cache_hit_tokens": 800,
            "cache_miss_tokens": 200,
            "estimated_cost_usd": 0.02,
            "tool_calls": 3,
            "latency_ms": 5000.0,
            "cache_split_status": "COMPLETE",
            "cache_hit_ratio": 0.8,
            "worker_selection": [f"w{index}" for index in range(workers)],
            "worker_contract": {"mode": "legacy", "workers": []},
        },
        "environment": {"agent_latency_ms": 3000.0},
        "worker_results": {
            f"w{index}": {
                "success": True,
                "stop_reason": {"code": "success"},
                "output": "done",
                "token_usage": {},
            }
            for index in range(workers)
        },
    }


# ------------------------------------------------------------------ 1. the fixed two-cell pair


def test_the_pair_is_the_two_frozen_cells() -> None:
    assert tuple(ROUTING_PAIR_CELLS) == (f"{TASK}::A1-P", f"{TASK}::A2-P")
    assert [cell.cell_id for cell in pilot_runner.routing_pair_cells()] == list(ROUTING_PAIR_CELLS)
    assert [cell.cell_id for cell in pilot_runner.plan_cells_for(ROUTING_PAIR_PLAN_ID)] == list(
        ROUTING_PAIR_CELLS
    )


def test_the_pair_plan_is_pinned_to_the_frozen_identity() -> None:
    assert ROUTING_PAIR_PLAN.plan_id == ROUTING_PAIR_PLAN_ID
    assert ROUTING_PAIR_PLAN.benchmark_id == pilot_runner.BENCHMARK_ID
    assert ROUTING_PAIR_PLAN.preregistration_digest == FROZEN_PREREGISTRATION
    assert ROUTING_PAIR_PLAN.runner_revision == "closure-2c1-r1"
    assert ROUTING_PAIR_PLAN.cells == ROUTING_PAIR_CELLS
    assert ROUTING_PAIR_PLAN.repetitions == (0,)
    assert ROUTING_PAIR_PLAN.max_cell_executions == 2
    # No owner authorization exists for a routing run, so the ceiling stays unset on purpose.
    assert ROUTING_PAIR_PLAN.estimated_cost_ceiling_usd is None
    assert pilot_runner.cost_policy(ROUTING_PAIR_PLAN)["valid"] is False


# ------------------------------------------------------------------------------ 2. the digest


def test_the_pair_plan_digest_is_reproducible() -> None:
    rebuilt = dataclasses.replace(ROUTING_PAIR_PLAN)
    assert rebuilt.digest() == ROUTING_PAIR_PLAN.digest()
    assert rebuilt.payload() == ROUTING_PAIR_PLAN.payload()
    # Preparation identity: pending approval, ceiling unset. Setting a ceiling moves this digest and
    # the pin must move with the owner's authorization, not before it.
    assert ROUTING_PAIR_PLAN.digest() == ROUTING_PAIR_PLAN_DIGEST


def test_the_pair_digest_covers_cells_order_repetitions_and_ceiling() -> None:
    before = ROUTING_PAIR_PLAN.digest()
    variants = (
        dataclasses.replace(ROUTING_PAIR_PLAN, cells=tuple(reversed(ROUTING_PAIR_CELLS))),
        dataclasses.replace(ROUTING_PAIR_PLAN, cells=ROUTING_PAIR_CELLS[:1]),
        dataclasses.replace(ROUTING_PAIR_PLAN, repetitions=(0, 1)),
        dataclasses.replace(ROUTING_PAIR_PLAN, max_cell_executions=3),
        dataclasses.replace(ROUTING_PAIR_PLAN, estimated_cost_ceiling_usd=2.00),
    )
    for variant in variants:
        assert variant.digest() != before
    # The pilot's digest is a different plan, not the same plan with another name.
    assert before != pilot_runner.PILOT_PLAN.digest()


# -------------------------------------------------------------------- 3/4/5. authorization crossing


def test_a_pending_pair_approval_refuses_the_pair() -> None:
    gate = pilot_authorization_gate(
        cells=pilot_runner.routing_pair_cells(),
        repetition_id=0,
        plan=ROUTING_PAIR_PLAN,
        record=ROUTING_PAIR_APPROVAL,
    )
    assert gate["execution_ready"] is False
    assert any("not 'APPROVED'" in problem for problem in gate["problems"])
    assert any("cost policy is not usable" in problem for problem in gate["problems"])


def test_the_consumed_pilot_approval_cannot_authorize_the_pair() -> None:
    gate = pilot_authorization_gate(
        cells=pilot_runner.routing_pair_cells(),
        repetition_id=0,
        plan=ROUTING_PAIR_PLAN,
        record=pilot_runner.PILOT_APPROVAL,
    )
    assert gate["execution_ready"] is False
    assert pilot_runner.PILOT_APPROVAL["status"] == "CONSUMED_BY_EXECUTION"
    assert any("pilot approval status is 'CONSUMED_BY_EXECUTION'" in p for p in gate["problems"])
    # Even a status edit would not be enough: the digest names the eight-cell plan.
    assert any("is not the approved" in problem for problem in gate["problems"])


def test_the_pair_approval_cannot_authorize_the_pilot() -> None:
    gate = pilot_authorization_gate(
        cells=pilot_runner.pilot_cells(),
        repetition_id=0,
        plan=pilot_runner.PILOT_PLAN,
        record=ROUTING_PAIR_APPROVAL,
    )
    assert gate["execution_ready"] is False
    assert any("is not the approved" in problem for problem in gate["problems"])


def test_a_live_run_without_the_pair_approval_never_reaches_the_executor(tmp_path: Path) -> None:
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _fake_result()

    with pytest.raises(PilotNotAuthorized):
        asyncio.run(
            run_live_pilot(
                pilot_runner.routing_pair_cells(),
                runs_root=tmp_path,
                executor=executor,
                live_intent=True,
                plan_id=ROUTING_PAIR_PLAN_ID,
            )
        )
    assert calls == []
    assert not (tmp_path / "pilot-ledger.sqlite").exists()


def test_a_cross_plan_cell_list_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED_BENCHMARK))
    approve_pair(monkeypatch)
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _fake_result()

    with pytest.raises(PilotNotAuthorized, match="not the authorized cells"):
        asyncio.run(
            run_live_pilot(
                (Cell(PIPELINE, "A1-P"),),
                runs_root=tmp_path,
                executor=executor,
                live_intent=True,
                plan_id=ROUTING_PAIR_PLAN_ID,
            )
        )
    assert calls == []


# ---------------------------------------------------------------- 7. offline preparation is inert


def test_offline_preparation_builds_no_sandbox_and_no_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasos.sandbox import provider as sandbox_provider

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline preparation must not construct a sandbox provider")

    monkeypatch.setattr(sandbox_provider, "build_sandbox_provider", forbidden)
    ledger = tmp_path / "untouched" / "pilot-ledger.sqlite"
    monkeypatch.setattr(pilot_runner, "_ledger_factory", lambda: PilotLedger(ledger))

    report = pairing_report(runs_root=tmp_path)
    manifest = pilot_runner.build_dry_run_manifest()

    assert report["parity"]["same_pre_route_public_task_digest"] is True
    assert report["parity"]["different_route"] is True
    assert manifest["model_calls"] == 0
    assert manifest["agent_sandbox_runs_total"] == 0
    assert not ledger.exists()


def test_the_cli_dry_run_ignores_the_plan_and_stays_the_frozen_contract(tmp_path: Path) -> None:
    out = tmp_path / "manifest.json"
    exit_code = pilot_runner.main(
        [
            "--dry-run",
            "--plan",
            ROUTING_PAIR_PLAN_ID,
            "--out",
            str(out),
            "--runs-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "DRY_RUN"
    assert payload["cell_count"] == 18
    assert payload["pilot_plan"]["plan_id"] == pilot_runner.PILOT_PLAN_ID


def test_the_two_plans_cannot_share_a_summary_or_a_run_tree(tmp_path: Path) -> None:
    out = tmp_path / "pilot-dry-run.json"
    assert pilot_runner.live_summary_path(out, pilot_runner.PILOT_PLAN_ID).name == (
        "pilot-live-summary.json"
    )
    pair = pilot_runner.live_summary_path(out, ROUTING_PAIR_PLAN_ID)
    assert pair.name == "routing-pair-live-summary.json"
    assert pair.parent == out.parent
    assert pilot_runner.default_runs_root(pilot_runner.PILOT_PLAN_ID) != (
        pilot_runner.default_runs_root(ROUTING_PAIR_PLAN_ID)
    )
    with pytest.raises(pilot_runner.PilotContractError):
        pilot_runner.live_summary_path(out, "closure2-unknown-plan")


# ------------------------------------------------------------- 9. serial execution under one claim


def test_the_pair_runs_both_cells_serially_under_one_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED_BENCHMARK))
    plan = approve_pair(monkeypatch)
    order: list[str] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        order.append(cell.cell_id)
        return _fake_result(prompt_tokens=100, workers=("w1", "w2"))

    summary = asyncio.run(
        run_live_pilot(
            pilot_runner.routing_pair_cells(),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
            plan_id=ROUTING_PAIR_PLAN_ID,
        )
    )

    assert order == list(ROUTING_PAIR_CELLS)
    assert summary["cell_count"] == 2
    assert [record["status"] for record in summary["cells"]] == ["COMPLETED", "COMPLETED"]
    assert summary["pilot_plan_id"] == ROUTING_PAIR_PLAN_ID
    assert summary["pilot_plan_digest"] == plan.digest()
    assert summary["execution_claim"]["plan_digest"] == plan.digest()
    assert summary["execution_integrity_status"] == "COMPLETE"

    ledger = PilotLedger(tmp_path / "pilot-ledger.sqlite")
    claim = ledger.get_claim(plan.digest())
    assert claim is not None
    assert claim["plan_id"] == ROUTING_PAIR_PLAN_ID
    assert claim["runner_revision"] == "closure-2c1-r1"
    assert claim["attempt_id"] == summary["execution_claim"]["attempt_id"]
    rows = ledger.cell_rows(plan.digest())
    assert [row["cell_id"] for row in rows] == list(ROUTING_PAIR_CELLS)
    assert [row["status"] for row in rows] == ["COMPLETED", "COMPLETED"]
    # The pilot's plan was never claimed by this run.
    assert ledger.get_claim(pilot_runner.PILOT_PLAN.digest()) is None


def test_the_pair_claim_is_one_shot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from experiments.evaluation.closure2.pilot_ledger import PilotAlreadyClaimed

    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED_BENCHMARK))
    approve_pair(monkeypatch)

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        return _fake_result(workers=("w1", "w2"))

    asyncio.run(
        run_live_pilot(
            pilot_runner.routing_pair_cells(),
            runs_root=tmp_path,
            executor=executor,
            live_intent=True,
            plan_id=ROUTING_PAIR_PLAN_ID,
        )
    )
    calls: list[Any] = []

    async def second(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        return _fake_result()

    with pytest.raises(PilotAlreadyClaimed):
        asyncio.run(
            run_live_pilot(
                pilot_runner.routing_pair_cells(),
                runs_root=tmp_path / "other",
                executor=second,
                live_intent=True,
                plan_id=ROUTING_PAIR_PLAN_ID,
            )
        )
    assert calls == []


# ----------------------------------------------------------------- 10. analysis readers, no rewrite


def test_a_direct_worker_has_no_fanout_checks_to_pass() -> None:
    payload = _oracle_payload(
        {
            "hidden_tests_passed": True,
            "required_artifacts_present": True,
            "forbidden_paths_unchanged": True,
            "only_allowed_paths_changed": True,
            "host_fixture_unchanged": True,
        },
        passed=True,
        workers=1,
    )
    contract = worker_contract_compliance(payload)
    assert contract["checks"] == {
        "all_workers_produced_output": NOT_APPLICABLE,
        "distinct_workers_executed": NOT_APPLICABLE,
    }
    assert contract["actual_worker_count"] == 1
    assert deliverable_correctness(payload)["verdict"] == "PASSED"


def test_a_missing_fanout_check_on_a_fanout_run_is_unknown() -> None:
    payload = _oracle_payload(
        {
            "hidden_tests_passed": True,
            "required_artifacts_present": True,
            "forbidden_paths_unchanged": True,
            "only_allowed_paths_changed": True,
            "host_fixture_unchanged": True,
        },
        passed=True,
        workers=3,
    )
    contract = worker_contract_compliance(payload)
    assert contract["checks"]["all_workers_produced_output"] == UNKNOWN
    assert contract["actual_worker_count"] == 3


def test_the_readers_never_rewrite_the_oracle_verdict() -> None:
    payload = _oracle_payload(
        {
            "hidden_tests_passed": False,
            "required_artifacts_present": True,
            "forbidden_paths_unchanged": True,
            "only_allowed_paths_changed": True,
            "host_fixture_unchanged": True,
            "all_workers_produced_output": True,
            "distinct_workers_executed": True,
        },
        passed=False,
        workers=2,
    )
    deliverable = deliverable_correctness(payload)
    assert deliverable["verdict"] == "FAILED"
    # The runner's own value is carried through untouched.
    assert deliverable["oracle_passed_observed"] is False
    assert payload["oracle"]["passed"] is False

    contract = worker_contract_compliance(payload)
    assert contract["checks"]["all_workers_produced_output"] is True
    assert contract["stop_reasons"] == {"w0": "success", "w1": "success"}


def test_deltas_are_withheld_while_correctness_is_unknown() -> None:
    multi_side = {
        "deliverable": {"verdict": "PASSED"},
        "efficiency": {"model_requests": 10, "prompt_tokens": 1000},
    }
    direct_side = {
        "deliverable": {"verdict": UNKNOWN},
        "efficiency": {"model_requests": 4, "prompt_tokens": 400},
    }
    result = compare_pair(multi=multi_side, direct=direct_side)
    assert result["correctness_comparable"] is False
    assert result["efficiency_comparable"] is False
    assert result["efficiency_delta_direct_minus_multi"]["model_requests"] == -6


def test_the_analysis_reads_a_real_result_shape(tmp_path: Path) -> None:
    payload = _oracle_payload(
        {
            "hidden_tests_passed": True,
            "required_artifacts_present": True,
            "forbidden_paths_unchanged": True,
            "only_allowed_paths_changed": True,
            "host_fixture_unchanged": True,
        },
        passed=True,
        workers=1,
    )
    payload["identity"] = {"run_id": "eval-pair"}
    payload["sample_id"] = TASK
    payload["mode"] = "coding"
    payload["metrics"]["routing"] = {
        "mode": "selective",
        "decision": {"route": "direct"},
        "executed_worker_count": 1,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    summarised = summarize_cell(path, cell_id=f"{TASK}::A2-P")
    assert summarised["deliverable"]["verdict"] == "PASSED"
    assert summarised["worker_contract"]["actual_worker_count"] == 1
    assert summarised["efficiency"]["actual_route"] == "direct"
    assert summarised["efficiency"]["model_requests"] == 4
    assert summarised["efficiency"]["agent_latency_ms"] == 3000.0
    assert summarised["efficiency"]["end_to_end_latency_ms"] == 5000.0


# ---------------------------------------------------------------- 8. the pilot stays untouched


def test_the_finished_pilot_is_unchanged() -> None:
    summary_path = Path("experiments/results/closure2/pilot-live-summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["pilot_plan_digest"] == PILOT_PLAN_DIGEST
    assert summary["pilot_plan_id"] == "closure2-pilot-1"
    assert summary["cell_count"] == 8
    assert summary["execution_integrity_status"] == "COMPLETE"
    assert summary["estimated_cost_usd_total"] == pytest.approx(0.08404231)

    assert pilot_runner.PILOT_PLAN.digest() == PILOT_PLAN_DIGEST
    assert pilot_runner.PILOT_APPROVAL["status"] == "CONSUMED_BY_EXECUTION"
    assert pilot_runner.PILOT_APPROVAL["approved_plan_digest"] == PILOT_PLAN_DIGEST
    assert pilot_runner.PILOT_APPROVAL["consumed_attempt_id"] == (
        "f862981c20a24fdf881884b9fbd891cc"
    )
    assert protocol.APPROVAL_RECORD["approved_digest"] == FROZEN_PREREGISTRATION
    assert protocol.APPROVAL_RECORD["status"] == "APPROVED"


def test_the_pairing_report_keeps_the_frozen_identities() -> None:
    report = pairing_report()
    assert report["preregistration_digest"] == FROZEN_PREREGISTRATION
    assert report["preregistration_digest_matches_frozen"] is True
    assert report["router"]["computed_digest"] == FROZEN_ROUTER
    assert report["router"]["matches_frozen"] is True
    assert report["parity"] == {
        "same_task": True,
        "same_seed_digest": True,
        "same_oracle_digest": True,
        "same_candidate_dag": True,
        "same_pre_route_public_task_digest": True,
        "same_model": True,
        "same_layout": True,
        "same_token_budget": True,
        "different_route": True,
    }
    assert report["differences"]["expected_executed_workers"] == {"A1-P": 2, "A2-P": 1}
    assert report["differences"]["expected_route"] == {"A1-P": None, "A2-P": "direct"}
    assert report["worker_turns"]["max_turns_per_worker"] == 12
    assert report["worker_turns"]["max_total_worker_turns_if_all_candidates_run"] == 24
    # No host path leaks into a report.
    assert "\\\\Users\\\\" not in json.dumps(report)


def test_the_pairing_report_is_deterministic() -> None:
    assert pairing_report() == pairing_report()


def test_efficiency_prefers_request_evidence_over_turns() -> None:
    payload = _oracle_payload({"hidden_tests_passed": True}, passed=True, workers=1)
    payload["metrics"]["model_turns"] = 99
    reading = efficiency(payload)
    assert reading["model_requests"] == 4
    assert reading["model_requests_source"] == "metrics.provider_calls"

    native = _oracle_payload({"hidden_tests_passed": True}, passed=True, workers=1)
    native["metrics"]["provider_calls"] = None
    native["metrics"]["model_turns"] = 7
    assert efficiency(native)["model_requests"] == 7
    assert efficiency(native)["model_requests_source"] == "metrics.model_turns"
    assert efficiency(native)["actual_route"] is None
    assert efficiency(native)["route_applicable"] is False

    # Neither counter present is UNKNOWN, never zero.
    silent = _oracle_payload({"hidden_tests_passed": True}, passed=True, workers=1)
    silent["metrics"]["provider_calls"] = None
    assert efficiency(silent)["model_requests"] is None
    assert efficiency(silent)["model_requests_source"] == UNKNOWN


def test_the_analysis_module_exports_are_complete() -> None:
    assert routing_pair_analysis.NOT_APPLICABLE == "NOT_APPLICABLE"
    assert routing_pair_analysis.UNKNOWN == "UNKNOWN"
    assert set(routing_pair_analysis.DELIVERABLE_CHECKS).isdisjoint(
        routing_pair_analysis.MULTI_WORKER_CHECKS
    )
