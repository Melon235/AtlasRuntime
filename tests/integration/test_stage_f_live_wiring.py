"""Stage F F2-C correction: the default live route, one price formula, one success definition.

Every test names the defect it would have caught. Nothing here calls a model, opens a sandbox or
consumes a production claim: the runner selection is asserted through spies, the price formula on
synthetic counters, and the dispatch proof uses the offline synthetic cell.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest
from experiments.stage_b_plus import authority, executor
from experiments.stage_f import arms, normalize, plan, routing, smoke
from experiments.stage_f import runners as stage_f_runners

REPO = Path(__file__).resolve().parents[2]


def _cell(arm_id: str) -> dict:
    return next(cell for cell in plan.batch_cells(1) if cell["arm_id"] == arm_id)


def _prices() -> dict:
    return {
        "cache_hit_input_per_million": 0.006,
        "cache_miss_input_per_million": 0.3,
        "output_per_million": 1.2,
    }


# -- the default live route ---------------------------------------------------------------------


def test_a_declared_mode_routes_to_its_stage_f_runner_even_with_a_fallback() -> None:
    """The defect: ``cell_runner_for`` returned the fallback for A1/A3, so the live default ran the
    legacy goal runner and the cell was never normalised into Unified Evidence."""

    legacy = executor.live_cell_runner()
    expected = {
        "A0": "native",
        "A1": "main_driven",
        "A2": "atlas_static_dag",
        "A3": "main_driven",
    }
    for arm_id, mode in expected.items():
        resolved = executor.cell_runner_for(_cell(arm_id), fallback=legacy)
        assert resolved is not legacy
        assert getattr(resolved, "is_stage_f_runner", False) is True
        assert getattr(resolved, "execution_mode", None) == mode


def test_a_main_driven_wrapper_keeps_the_production_configuration() -> None:
    """The defect: the wrapper required an injected provider, so it was a test-only path."""

    runner = stage_f_runners.main_driven_cell_runner()
    assert getattr(runner, "keeps_session_record", False) is True
    assert getattr(runner, "is_live", False) is True


def test_a_cell_without_execution_mode_keeps_the_legacy_runner() -> None:
    """Every Stage B+ and C-series cell declares no mode and must not be re-routed."""

    legacy = executor.live_cell_runner()
    cell = {"cell_id": "bplus-x", "execution_mode": None}
    assert executor.cell_runner_for(cell, fallback=legacy) is legacy
    assert executor.cell_runner_for({}, fallback=legacy) is legacy


def test_dispatch_selects_the_runner_without_a_cell_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect the F2-C smoke had: it injected a composed ``cell_runner``, which bypassed the
    production selection entirely. Here the dispatch is given none and every arm must still reach
    the factory of its own execution mode."""

    from experiments.stage_b_plus import contract
    from experiments.stage_b_plus.offline import synthetic_cell_run
    from experiments.stage_c1 import checks as c1_checks

    calls: list[str] = []

    def factory_for(mode: str):
        def build(*_args: object, **_kwargs: object):
            calls.append(mode)

            async def run(cell: dict, context: Mapping) -> executor.CellRun:
                return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

            run.execution_mode = mode  # type: ignore[attr-defined]
            run.is_stage_f_runner = True  # type: ignore[attr-defined]
            return run

        return build

    monkeypatch.setattr(stage_f_runners, "native_cell_runner", factory_for("native"))
    monkeypatch.setattr(stage_f_runners, "static_dag_cell_runner", factory_for("atlas_static_dag"))
    monkeypatch.setattr(stage_f_runners, "main_driven_cell_runner", factory_for("main_driven"))

    cells = smoke.cells_for()
    record = smoke.plan_for(cells)
    scratch = Path(tempfile.mkdtemp(prefix="stage-f-wiring-"))
    signed = c1_checks._offline_signed(
        record,
        scratch=scratch / "sign",
        head=executor.git_head(REPO),
        identity=executor.executor_identity(REPO),
        protocol_digest=authority.protocol_digest(contract.protocol_document()),
        cost_stop_usd=float(record["cost_stop_usd"]),
        per_cell_cost_stop_usd=float(record["per_cell_cost_stop_usd"]),
    )
    authority_path = scratch / "authority.json"
    authority_path.write_text(json.dumps(signed, indent=2) + "\n", encoding="utf-8")

    with executor.claim_root_override(scratch / "claims"):
        report = asyncio.run(
            executor.dispatch(
                smoke.SMOKE_PLAN_ID,
                runs_root=scratch / "runs",
                evidence_root=scratch / "evidence",
                authority_path=authority_path,
                repo_root=REPO,
                mode=executor.MODE_OFFLINE_PROOF,
                plan=record,
            )
        )

    assert report["cells_started"] == 4
    # One build per cell, in cell order, each reaching the factory of its own declared mode.
    assert calls == ["native", "main_driven", "atlas_static_dag", "main_driven"]
    # The analysis reads every started cell; the sample flag is a label, not a filter.
    assert report["analysis_scope"]["excluded_by_sample_flag"] == 0
    assert report["analysis_scope"]["cells_in_report"] == report["cells_started"]
    assert report["success_counts"]["cells_started"] == 4


# -- one price formula --------------------------------------------------------------------------


def test_the_same_composition_costs_the_same_in_every_topology() -> None:
    """The defect: ``_usage_of`` never passed the cache split, so every Main was priced as a miss
    and a child's prompt was always a miss, whatever the provider reported."""

    input_tokens, completion, hit = 100_000, 20_000, 60_000
    miss = input_tokens - hit
    total = input_tokens + completion
    single = {
        "main_cumulative_input_tokens": input_tokens,
        "main_completion_tokens": completion,
        "child_total_prompt_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": total,
        "main_cache_hit_tokens": hit,
        "main_cache_miss_tokens": miss,
        "child_cache_hit_tokens": 0,
        "child_cache_miss_tokens": 0,
    }
    dag = {
        "main_cumulative_input_tokens": 0,
        "main_completion_tokens": 0,
        "child_total_prompt_tokens": input_tokens,
        "child_completion_tokens": completion,
        "system_total_tokens": total,
        "main_cache_hit_tokens": 0,
        "main_cache_miss_tokens": 0,
        "child_cache_hit_tokens": hit,
        "child_cache_miss_tokens": miss,
    }
    main_child = {
        "main_cumulative_input_tokens": 40_000,
        "main_completion_tokens": 8_000,
        "child_total_prompt_tokens": 60_000,
        "child_completion_tokens": 12_000,
        "system_total_tokens": total,
        "main_cache_hit_tokens": 30_000,
        "main_cache_miss_tokens": 10_000,
        "child_cache_hit_tokens": 30_000,
        "child_cache_miss_tokens": 30_000,
    }

    costs = {
        name: executor.cell_cost(metrics, _prices())
        for name, metrics in (
            ("single", single),
            ("dag", dag),
            ("main_child", main_child),
        )
    }
    assert len(set(costs.values())) == 1, costs
    expected = (hit * 0.006 + miss * 0.3 + completion * 1.2) / 1_000_000
    assert costs["single"] == pytest.approx(expected, rel=1e-9)
    # A hit is cheaper than a miss and is not counted twice: the same input priced as all-miss is
    # strictly more expensive.
    assert costs["single"] < executor.cell_cost(
        {key: value for key, value in single.items() if "cache" not in key}, _prices()
    )


def test_a_missing_split_is_priced_as_a_miss_and_never_as_a_hit() -> None:
    metrics = {
        "main_cumulative_input_tokens": 100_000,
        "main_completion_tokens": 20_000,
        "child_total_prompt_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": 120_000,
    }
    assert executor.cell_cost(metrics, _prices()) == pytest.approx(
        (100_000 * 0.3 + 20_000 * 1.2) / 1_000_000
    )
    assert executor.validate_usage(metrics)["cache_split"] == "UNKNOWN"


def test_a_partial_split_is_refused_rather_than_re_priced() -> None:
    metrics = {
        "main_cumulative_input_tokens": 100_000,
        "main_completion_tokens": 20_000,
        "child_total_prompt_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": 120_000,
        "main_cache_hit_tokens": 10_000,
        "main_cache_miss_tokens": 10_000,
    }
    assert executor.validate_usage(metrics)["ok"] is False
    assert executor.cell_cost(metrics, _prices()) is None


def test_the_usage_mapper_carries_every_reported_split() -> None:
    evidence = {
        "usage": {
            "main": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "cache_hit_tokens": 900,
                "cache_miss_tokens": 100,
            },
            "children": {
                "prompt_tokens": 500,
                "completion_tokens": 5,
                "cache_hit_tokens": 200,
                "cache_miss_tokens": 300,
            },
            "primary_agent": {},
            "system_total_tokens": 1515,
        },
        "usage_attribution": "main_child",
    }
    metrics = stage_f_runners._usage_of(evidence)  # noqa: SLF001 - the seam under test
    assert metrics["main_cache_hit_tokens"] == 900
    assert metrics["main_cache_miss_tokens"] == 100
    assert metrics["child_cache_hit_tokens"] == 200
    assert metrics["child_cache_miss_tokens"] == 300
    assert executor.validate_usage(metrics)["ok"] is True


# -- one success definition ---------------------------------------------------------------------


def test_delivered_success_ignores_the_public_suite() -> None:
    """The defect: delivery was a four-way conjunction, so a failed or absent public suite could
    turn a correct, normally completed run into a non-delivery — an arm's tool surface deciding the
    primary metric."""

    delivered = normalize._delivered  # noqa: SLF001 - the definition under test
    assert delivered(oracle_status="PASS", execution_integrity="PASS", agent_success=True) is True
    assert (
        delivered(oracle_status="PASS", execution_integrity="UNKNOWN", agent_success=True) is False
    )
    assert delivered(oracle_status="FAIL", execution_integrity="PASS", agent_success=True) is False
    assert delivered(oracle_status="PASS", execution_integrity="PASS", agent_success=False) is False


def test_the_public_suite_is_recorded_apart_from_delivery() -> None:
    evidence = normalize.blank_evidence(
        {
            "cell_id": "c",
            "plan_id": "p",
            "arm_id": "A1",
            "task_id": "f-p1-connector-migration",
            "execution_mode": "atlas_direct",
            "usage_attribution": "main_child",
        }
    )
    evidence["correctness"] = normalize._correctness(  # noqa: SLF001
        agent_success=True,
        primary_completion=None,
        static_acceptance="NOT_APPLICABLE",
        public_test="FAIL",
        oracle_status="PASS",
        oracle_passed=True,
        execution_integrity="PASS",
        failure_kind=None,
    )
    assert evidence["correctness"]["public_test"] == "FAIL"
    assert evidence["correctness"]["delivered_success"] is True


def test_the_matrix_counts_each_axis_separately() -> None:
    """The defect: ``cells_passed`` was the only success number in the report, so a reader had no
    way to tell a delivered cell from a merely audited one."""

    rows = [
        {
            "verdict": "PASS",
            "agent_success": True,
            "oracle_passed": True,
            "execution_integrity": "PASS",
            "public_test_status": "PASS",
            "delivered_success": True,
        },
        {
            "verdict": "MEASURED_FAILURE",
            "agent_success": False,
            "oracle_passed": True,
            "execution_integrity": "PASS",
            "public_test_status": "NOT_RUN",
            "delivered_success": False,
        },
        {
            "verdict": "STOPPED",
            "agent_success": True,
            "oracle_passed": False,
            "execution_integrity": "FAIL",
            "public_test_status": "FAIL",
            "delivered_success": False,
        },
    ]
    counts = executor.success_counts(rows)
    assert counts["cells_started"] == 3
    assert counts["executor_verdict_pass"] == 1
    assert counts["oracle_pass"] == 2
    assert counts["agent_completed"] == 2
    assert counts["execution_integrity_pass"] == 2
    assert counts["public_test_pass"] == 1
    assert counts["delivered_success"] == 1
    assert counts["delivered_success_measured"] is True
    # The one cell that is audited PASS is a delivery; the oracle-pass cell whose agent did not
    # finish is not, and the report says so instead of counting it as one.
    assert counts["delivered_success"] != counts["cells_started"]


# -- the frozen identity ------------------------------------------------------------------------


def test_the_manifest_binds_every_task_content_identity() -> None:
    record = plan.manifest()
    assert len(record["task_facts"]) == 8
    for task_id, facts in record["task_facts"].items():
        for key in (
            "goal_digest",
            "grant_digest",
            "static_dag_digest",
            "seed_digest",
            "public_interface_digest",
            "public_test_digest",
            "oracle_digest",
            "reference_digest",
            "paths_digest",
        ):
            assert str(facts[key]).startswith("sha256:"), (task_id, key, facts[key])
        assert facts["public_interface_paths"], task_id
        assert facts["allowed_paths"] and facts["forbidden_paths"], task_id
        assert facts["reference_files"], task_id
        assert all(node.get("assignment") for node in facts["static_dag"]), task_id
        assert facts["static_dag_digest"] == arms.static_dag_digest(task_id)


def test_the_static_dag_binds_the_cap_the_scheduler_is_given() -> None:
    for task_id in plan.tasks.TASK_IDS:
        nodes = arms.static_dag(task_id)
        depended_on = {dep for node in nodes for dep in node["depends_on"]}
        for node in nodes:
            expected = 40 if node["task_id"] not in depended_on else 20
            assert node["max_turns"] == expected, (task_id, node["task_id"])


def test_the_default_routing_smoke_names_the_arms_it_covers() -> None:
    assert routing.ARMS == ("A1", "A3")
    assert routing.PLAN_ID == "stage-f-default-routing-smoke"
