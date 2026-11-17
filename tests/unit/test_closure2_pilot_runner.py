"""Closure-2B-0: the pilot runner's arm adapter, contract checks, gate and evidence mapping.

Everything here is offline. No sandbox is created, no provider is built, no API key is read: the
executor is always a stub, and the only "approval" that is ever granted is a dictionary passed in by
a test.
"""

from __future__ import annotations

import asyncio
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
from experiments.evaluation.closure2.pilot_runner import (
    ARM_ORDER,
    ARM_SPECS,
    FROZEN_MAX_TOKENS_PER_REQUEST,
    PROPOSED_PILOT_CELLS,
    ArmSpec,
    Cell,
    PilotContractError,
    build_dry_run_manifest,
    cell_descriptor,
    check_frozen_contract,
    effective_model,
    frozen_budget,
    normalize_result,
    pilot_cells,
    plan_cells,
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
    assert manifest["sandbox_runs"] == 0
    assert manifest["contains_live_outcomes"] is False
    assert manifest["route_expectation_is_prediction"] is True
    assert manifest["cell_count"] == 18
    assert manifest["contract_ok"] is True
    assert manifest["contract_problems"] == []
    assert manifest["proposed_live_pilot"]["approved"] is False


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
    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED))
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
    assert [record["status"] for record in summary["cells"]] == ["COMPLETED", "COMPLETED"]


def test_a_failing_cell_is_recorded_and_does_not_stop_the_pilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "APPROVAL_RECORD", dict(APPROVED))

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        if cell.arm_id == "N0":
            raise RuntimeError("sandbox never came up")
        return _fake_result()

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
    assert summary["cells"][1]["status"] == "COMPLETED"
    # One unmeasured cell makes the pilot total unknown; it is not a sum of the known ones.
    assert summary["model_calls"] is None
    # Only the completed stub claims a sandbox; the failed cell contributes no such claim.
    assert summary["sandbox_runs"] == 1
    assert "sandbox_created" not in broken


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(PilotContractError, match="unknown arm_id"):
        assert Cell(P1, "A9").spec is not None
    with pytest.raises(PilotContractError, match="unknown task_id"):
        Cell("closure2-not-a-task", "A0").source_task()


def test_the_default_approval_is_still_pending() -> None:
    gate = approval_gate()
    assert gate["execution_ready"] is False
    assert gate["status"] == "PENDING_OWNER_APPROVAL"


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
        sandbox_created=True,
        sandbox_destroyed=True,
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
    assert record["provider_usage_source"] == "not_exposed_by_runner"


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
