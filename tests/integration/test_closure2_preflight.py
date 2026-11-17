"""Closure-2A: the executable preflight.

This is the only test that spawns pytest subprocesses, and it is the one that proves the benchmark
before any model may run: seed fails, reference passes, the DAG is legal, the router decision is
real, and the two topologies describe the same task.

No model call, no sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.evaluation.closure2.catalog import TASKS
from experiments.evaluation.closure2.preflight import (
    ROUTING_POLICY_DIGEST,
    _write,
    dag_audit,
    layout_executable,
    router_audit,
    run_preflight,
    task_selftest,
    workspace_isolation,
)

from atlasos.workers.routing import RoutingPolicy


@pytest.fixture(scope="module")
def preflight() -> dict:
    return run_preflight()


def test_the_preflight_passes_every_check(preflight: dict) -> None:
    assert preflight["status"] == "PASSED", sorted(
        name for name, passed in preflight["checks"].items() if not passed
    )
    assert preflight["checks"]
    assert all(preflight["checks"].values())
    assert preflight["model_calls"] == 0
    assert preflight["sandbox_runs"] == 0


def test_every_task_fails_on_the_seed_and_passes_on_the_reference(preflight: dict) -> None:
    for task in TASKS:
        selftest = preflight["tasks"][task.task_id]["selftest"]
        assert selftest["seed_public_fails"] is True, f"{task.task_id}: seed passes public"
        assert selftest["seed_oracle_fails"] is True, f"{task.task_id}: seed passes the oracle"
        assert selftest["reference_public_passes"] is True, f"{task.task_id}: ref fails public"
        assert selftest["reference_oracle_passes"] is True, f"{task.task_id}: ref fails oracle"


def test_the_workspace_carries_no_hidden_test_and_no_answer(preflight: dict) -> None:
    for task in TASKS:
        record = preflight["tasks"][task.task_id]["workspace_isolation"]
        assert record["passed"] is True, record["leaks"]
        assert record["leaks"] == []


def test_p1_and_p2_are_admitted_and_n1_is_refused(preflight: dict) -> None:
    by_family = {task.family: preflight["tasks"][task.task_id] for task in TASKS}

    for family in ("P1", "P2"):
        router = by_family[family]["router"]
        assert router["route"] == "delegate_parallel", router
        assert "DELEGATION_ADMITTED" in router["reason_codes"]
        assert router["blocked_gates"] == []
        assert by_family[family]["dag"]["max_wave_width"] == 2
        assert by_family[family]["dag"]["first_wave_disjoint"] is True

    negative = by_family["N1"]["router"]
    assert negative["route"] == "direct"
    # The refusal is the structural one: both candidate steps must edit the same state module.
    assert "SHARED_WRITE_RISK" in negative["reason_codes"]
    assert "GATE_2_DEPENDENCY_COUPLING" in negative["blocked_gates"]
    assert by_family["N1"]["dag"]["max_wave_width"] == 1


def test_the_router_policy_is_the_frozen_one(preflight: dict) -> None:
    assert preflight["router_policy"]["digest"] == ROUTING_POLICY_DIGEST
    assert RoutingPolicy(version="r0.1.0").digest() == ROUTING_POLICY_DIGEST
    assert preflight["router_policy"]["version"] == "r0.1.0"


def test_the_declared_write_sets_are_the_owned_write_sets(preflight: dict) -> None:
    """The router reads paths out of the text; the catalog claims a tuple. They must agree."""

    for task in TASKS:
        dag = preflight["tasks"][task.task_id]["dag"]
        assert dag["declared_write_paths_match_ownership"] is True, dag["declared_write_paths"]
        for step_id, paths in dag["declared_write_paths"].items():
            assert paths, f"{task.task_id}/{step_id} declares no write path"


def test_both_layouts_resolve_and_an_unknown_one_is_refused(preflight: dict) -> None:
    layout = preflight["layout"]
    assert layout["legacy_resolved_layout"] == "legacy"
    assert layout["shared_prefix_resolved_layout"] == "shared_prefix"
    assert layout["unknown_layout_rejected"] is True


def test_same_topology_digests_are_equal(preflight: dict) -> None:
    for group in preflight["parity"]["groups"]:
        assert group["equal"] is True, group
    assert preflight["parity"]["all_task_facts_equal"] is True


def test_the_negative_control_does_not_depend_on_a_missing_contract_alone() -> None:
    """N1 must be refused for the shared write, not only for the interface."""

    n1 = next(task for task in TASKS if task.family == "N1")
    router = router_audit(n1)
    assert "SHARED_WRITE_RISK" in router["reason_codes"]


def test_each_task_gets_its_own_disposable_workspace() -> None:
    task = TASKS[0]
    first = task_selftest(task)
    second = task_selftest(task)
    assert first["reference_oracle_passes"] is True
    assert second["reference_oracle_passes"] is True
    # No leftover directories from the preflight.
    assert "cleanup_dirs" not in first


def test_the_dag_audit_reports_disjoint_waves() -> None:
    for task in TASKS:
        dag = dag_audit(task)
        assert dag["dependency_complete"] is True
        assert dag["integration_dependencies_are_declared_steps"] is True
        if task.family in ("P1", "P2"):
            assert dag["wave_zero_width_is_two"] is True


def test_the_isolation_check_catches_a_deliberate_leak() -> None:
    """The leak detector must be able to fail, otherwise its pass means nothing."""

    task = TASKS[0]
    leaked = type(task)(
        task_id=task.task_id,
        family=task.family,
        title=task.title,
        prompt=task.prompt,
        seed={**task.seed, "test_hidden_pipeline.py": next(iter(task.oracle_tests.values()))},
        reference=task.reference,
        public_tests=task.public_tests,
        oracle_tests=task.oracle_tests,
        workers=task.workers,
        deliverables=task.deliverables,
        allowed_paths=task.allowed_paths,
        forbidden_paths=task.forbidden_paths,
        shared_context=task.shared_context,
        frozen_contracts=task.frozen_contracts,
    )
    record = workspace_isolation(leaked)
    assert record["passed"] is False
    assert any("hidden test file staged" in leak for leak in record["leaks"])


def test_the_preflight_artifact_is_valid_json(tmp_path: Path) -> None:
    payload = {
        "status": "PASSED",
        "checks": {"example": True},
        "tasks": {},
        "benchmark_version": "x",
    }
    target = tmp_path / "preflight.json"
    _write(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_the_layout_check_reads_real_config_files() -> None:
    record = layout_executable()
    assert record["legacy_ok"] is True
    assert record["shared_prefix_ok"] is True
    assert record["transform_identity"] is True
