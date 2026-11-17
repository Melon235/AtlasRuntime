"""Closure-2A: the frozen benchmark definition.

Structural checks only — no subprocess, no router, no model. The executable checks (seed fails,
reference passes, router decisions) live in ``tests/integration/test_closure2_preflight.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from experiments.evaluation.closure2.catalog import (
    BENCHMARK_VERSION,
    TASKS,
    build_task,
    task_digests,
)
from experiments.evaluation.closure2.protocol import (
    ARMS,
    BUDGET,
    COMPARISONS,
    EXCLUSIONS,
    METRICS,
    SAME_DIGEST_GROUP,
    UNSUPPORTED,
    arm_config_digest,
    preregistration_digest,
    preregistration_payload,
    task_taskspec_parity,
)
from experiments.evaluation.models import canonical_digest

TASK_IDS = {task.task_id for task in TASKS}
FAMILIES = {task.family for task in TASKS}


def test_the_benchmark_has_exactly_three_tasks_in_three_families() -> None:
    expected = {
        "closure2-pipeline-migration",
        "closure2-plugin-migration",
        "closure2-state-machine-refactor",
    }
    assert expected == TASK_IDS
    assert {"P1", "P2", "N1"} == FAMILIES


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_every_task_is_a_migration_of_a_working_seed(task) -> None:
    assert task.seed, "a task needs a v1 seed"
    assert task.reference, "a task needs a reference implementation"
    assert task.oracle_tests, "a task needs a hidden oracle"
    assert task.public_tests, "a task needs public tests"
    # The reference only replaces files that already exist in the seed: a migration, not a scaffold.
    assert set(task.reference) <= set(task.seed)
    # Every deliverable is a real seeded file.
    assert set(task.deliverables) <= set(task.seed)


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_hidden_oracle_never_ships_in_the_workspace(task) -> None:
    visible = build_task(task, topology="multi").visible_files
    for name in task.oracle_tests:
        assert name not in visible
    # And the seed does not carry the answer under another name.
    for name in task.reference:
        assert name in visible


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_two_topologies_describe_the_same_task(task) -> None:
    single = build_task(task, topology="single")
    multi = build_task(task, topology="multi")

    assert single.visible_files == multi.visible_files
    assert single.hidden_files == multi.hidden_files
    assert single.required_artifacts == multi.required_artifacts
    assert single.allowed_paths == multi.allowed_paths
    assert single.forbidden_paths == multi.forbidden_paths
    # Every multi step carries the same public block the single worker receives.
    assert all(step.shared_context == task.shared_context for step in multi.workers)
    assert task.shared_context.splitlines()[0] in single.prompt
    assert len(single.workers) == 1
    assert len(multi.workers) >= 2


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_digest_is_stable_and_versioned(task) -> None:
    assert (
        build_task(task, topology="multi").effective_public_task_digest
        == build_task(task, topology="multi").effective_public_task_digest
    )
    digests = task_digests(task)
    assert set(digests) >= {
        "seed_digest",
        "reference_digest",
        "oracle_digest",
        "public_requirement_digest",
        "candidate_dag_digest",
        "effective_public_task_digest_single",
        "effective_public_task_digest_multi",
    }
    # Single and multi are different public inputs: the topology is part of the digest.
    assert (
        digests["effective_public_task_digest_single"]
        != digests["effective_public_task_digest_multi"]
    )


def test_same_topology_arms_agree_on_the_digest() -> None:
    parity = task_taskspec_parity()
    for task in TASKS:
        single = build_task(task, topology="single").effective_public_task_digest
        for arm, entry in ARMS.items():
            if entry["topology"] != "single":
                continue
            assert build_task(task, topology="single").effective_public_task_digest == single, (
                f"{arm} disagrees with another single arm on {task.task_id}"
            )
        for group in SAME_DIGEST_GROUP:
            digests = {
                build_task(task, topology=ARMS[arm]["topology"]).effective_public_task_digest
                for arm in group
            }
            assert len(digests) == 1, f"{group} disagree on {task.task_id}"
        assert parity[task.task_id]["same_visible_files"] is True


def test_the_arm_matrix_matches_the_preregistered_protocol() -> None:
    assert set(ARMS) == {"N0", "N1", "A0", "A1-L", "A1-P", "A2-P"}
    assert ARMS["N0"]["topology"] == "single"
    assert ARMS["A0"]["topology"] == "single"
    for arm in ("N1", "A1-L", "A1-P", "A2-P"):
        assert ARMS[arm]["topology"] == "multi"
    # The layout ablation differs in exactly two fields.
    left, right = ARMS["A1-L"], ARMS["A1-P"]
    differing = {key for key in left if left[key] != right[key]}
    assert differing == {"prompt_layout", "config"}
    assert (left["prompt_layout"], right["prompt_layout"]) == ("legacy", "shared_prefix")
    # The router arm differs from the static multi arm only in the routing mode.
    assert ARMS["A2-P"]["route"] == "selective"
    assert ARMS["A1-P"]["route"] == "off"


def test_every_arm_config_digest_is_unique() -> None:
    digests = {arm_config_digest(arm) for arm in ARMS}
    assert len(digests) == len(ARMS)


def test_the_budget_protocol_is_arithmetically_consistent() -> None:
    single, multi = BUDGET["single"], BUDGET["multi"]
    assert multi["max_turns_per_worker"] * multi["workers"] == single["max_turns"] == 36
    assert single["token_budget"] == multi["token_budget"]
    assert 240_000 <= single["token_budget"] <= 300_000
    assert single["max_tool_calls"] == multi["max_tool_calls"]
    # This stage proposes; it does not approve.
    assert BUDGET["approval"] == "PENDING_OWNER_APPROVAL"


def test_the_protocol_names_what_it_cannot_compare() -> None:
    assert UNSUPPORTED
    assert any("Router" in item for item in UNSUPPORTED)
    assert any("Context Plane" in item for item in UNSUPPORTED)
    assert {item["id"] for item in COMPARISONS} >= {
        "native-vs-atlas-single",
        "native-vs-atlas-multi",
        "layout-ablation",
        "router-admission",
    }
    assert EXCLUSIONS["never_allowed"]
    assert "oracle" in METRICS["primary"]


def test_the_preregistration_payload_is_complete_and_digestible() -> None:
    payload = preregistration_payload()
    assert payload["benchmark_version"] == BENCHMARK_VERSION
    assert len(payload["tasks"]) == len(TASKS)
    assert payload["release_boundary"]["rule"]
    assert "NOT" in payload["release_boundary"]["repository_contains"].upper()
    assert preregistration_digest() == canonical_digest(preregistration_payload())
    # Deterministic across two builds.
    assert preregistration_digest() == preregistration_digest()


def test_the_two_configs_are_distinct_files() -> None:
    from experiments.evaluation.closure2.protocol import (
        LEGACY_CONTEXT_CONFIG,
        SHARED_PREFIX_CONFIG,
    )

    assert LEGACY_CONTEXT_CONFIG.is_file()
    assert SHARED_PREFIX_CONFIG.is_file()
    assert LEGACY_CONTEXT_CONFIG != SHARED_PREFIX_CONFIG
    legacy = Path(LEGACY_CONTEXT_CONFIG).read_text(encoding="utf-8")
    shared = Path(SHARED_PREFIX_CONFIG).read_text(encoding="utf-8")
    assert "prompt_layout: shared_prefix" in shared
    assert "prompt_layout: shared_prefix" not in legacy
