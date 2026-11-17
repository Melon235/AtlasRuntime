"""Closure-2A-R1/R2: the executable preflight and its negative controls.

This is the only test module that spawns pytest subprocesses. It proves the benchmark before any
model may run — seed fails, oracle executes, reference passes, the DAG is legal, the router decision
is exactly the registered one, the workspace carries no answer — and it proves that each check can
actually *fail*, because a check that cannot fail is not evidence.

R2 adds one more kind of evidence: the pytest *session classification* is exercised against real
subprocesses for every failure shape the review named, so "the oracle found a defect" can only ever
mean "a test in the oracle failed", never "the fixture broke" or "the environment was unwell".

Every negative control here mutates a copy in memory or a temporary directory. No frozen task, no
router policy and no oracle is edited.

No model call, no sandbox.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from experiments.evaluation.closure2 import preflight
from experiments.evaluation.closure2.catalog import TASKS
from experiments.evaluation.closure2.preflight import (
    C2A_R1_PREREGISTRATION_DIGEST,
    REPO_ROOT,
    ROUTING_POLICY_DIGEST,
    _run_pytest,
    _write,
    _write_files,
    answer_leak_guard,
    classify_pytest,
    collect_cause_class,
    dag_audit,
    frozen_identity,
    harness_blocked,
    layout_executable,
    router_audit,
    router_gate,
    run_preflight,
    session_is_valid_defect,
    task_selftest,
    workspace_isolation,
)

from atlasos.workers.routing import RoutingPolicy

BY_ID = {task.task_id: task for task in TASKS}
P1 = next(task for task in TASKS if task.family == "P1")
N1 = next(task for task in TASKS if task.family == "N1")

PUBLIC_FAILS = "def test_public_contract():\n    assert False, 'public contract unmet'\n"
HIDDEN_PASSES = "def test_hidden_contract():\n    assert True\n"


@pytest.fixture(scope="module")
def preflight_payload() -> dict:
    return run_preflight()


def answer_bearing_reference(task) -> tuple[str, str]:
    """A reference module whose body is not the seed body: the only kind that can leak an answer."""

    for name, content in task.reference.items():
        if content.strip() and content != task.seed.get(name):
            return name, content
    raise AssertionError(f"{task.task_id} has no reference module that differs from its seed")


# ------------------------------------------------------------------ the registered outcome


def test_the_preflight_passes_every_check(preflight_payload: dict) -> None:
    assert preflight_payload["status"] == "PASSED", sorted(
        name for name, passed in preflight_payload["checks"].items() if not passed
    )
    assert preflight_payload["checks"]
    assert all(preflight_payload["checks"].values())
    assert len(preflight_payload["checks"]) >= 55
    assert preflight_payload["model_calls"] == 0
    assert preflight_payload["sandbox_runs"] == 0
    assert preflight_payload["stage"] == "closure-2a-r1"
    assert preflight_payload["protocol_revision"] == "closure-2a-r1"


def test_an_offline_pass_does_not_unlock_a_live_run(preflight_payload: dict) -> None:
    """R1-01: ``status`` and ``execution_ready`` are two different facts."""

    assert preflight_payload["status"] == "PASSED"
    assert preflight_payload["execution_ready"] is False
    assert preflight_payload["approval"]["status"] == "PENDING_OWNER_APPROVAL"
    assert preflight_payload["approval"]["problems"]
    assert preflight_payload["checks"]["execution_blocked_while_not_approved"] is True


def test_the_registration_identity_is_the_pinned_r1_one(preflight_payload: dict) -> None:
    assert preflight_payload["preregistration_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert preflight_payload["frozen_preregistration_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert (
        preflight_payload["c2a_preregistration_digest"]
        == "f7bbdfb8350589a959d78be00a9dc150c3b12fd6d277cf0fe2cee8a8b536b058"
    )
    assert preflight_payload["supersedes"]["head_commit"] == (
        "ac7af16b4506afafe6e932e2cc351b04354b9f57"
    )
    assert preflight_payload["checks"]["preregistration_digest_matches_frozen_r1"] is True
    assert preflight_payload["checks"]["preregistration_digest_differs_from_c2a"] is True


def test_the_approval_gate_binds_the_frozen_digest(preflight_payload: dict) -> None:
    """R2-01: a drifted checkout approved with its own digest is still refused."""

    binding = preflight_payload["approval_binding"]
    assert binding["passed"] is True
    assert binding["frozen_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert binding["faithful_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert binding["drifted_digest"] != C2A_R1_PREREGISTRATION_DIGEST
    facts = binding["facts"]
    assert facts["faithful_copy_can_be_approved"] is True
    assert facts["drifted_copy_cannot_be_approved_even_with_its_own_digest"] is True
    assert facts["frozen_approval_does_not_cover_a_drifted_checkout"] is True
    assert preflight_payload["checks"]["approval_gate_binds_frozen_digest"] is True


def test_the_approval_gate_exposes_no_digest_override(preflight_payload: dict) -> None:
    assert preflight_payload["checks"]["approval_gate_has_no_digest_override"] is True


def test_the_config_binding_probe_passed(preflight_payload: dict) -> None:
    binding = preflight_payload["config_binding"]
    assert binding["passed"] is True
    assert binding["facts"]["value_change_moves_arm_digest"] is True
    assert binding["facts"]["comment_only_keeps_arm_digest"] is True
    assert binding["facts"]["same_content_two_roots_same_registration_digest"] is True
    assert all(binding["path_rejections"].values())


# ------------------------------------------------------------------ R1-04: oracle independence


def test_every_task_has_four_independent_sessions(preflight_payload: dict) -> None:
    for task in TASKS:
        record = preflight_payload["tasks"][task.task_id]["selftest"]
        assert "seed_oracle_fails" not in record, "the conflated Closure-2A field must be gone"
        assert record["seed_public_fails"] is True, f"{task.task_id}: seed passes public"
        assert record["seed_hidden_fails"] is True, f"{task.task_id}: seed passes the oracle"
        assert record["reference_public_passes"] is True, f"{task.task_id}: ref fails public"
        assert record["reference_hidden_passes"] is True, f"{task.task_id}: ref fails oracle"
        assert all(record["independence"].values()), record["independence"]
        for session in ("seed_public", "seed_hidden", "reference_public", "reference_hidden"):
            assert record[session]["targets"], f"{task.task_id}/{session} ran no explicit target"


def test_the_hidden_oracle_really_executes_on_the_reference(preflight_payload: dict) -> None:
    for task in TASKS:
        record = preflight_payload["tasks"][task.task_id]["selftest"]
        assert record["hidden_oracle_really_executes"] is True, task.task_id
        assert record["hidden_oracle_executed_tests"] > 0, task.task_id


def test_the_n1_seed_failure_is_classified_honestly(preflight_payload: dict) -> None:
    """N1's seed cannot even import the v2 interface. That is a collection error, not an assertion.

    Recording it as "the oracle caught a defect" would be fake evidence; recording it as a harness
    problem would hide a real unimplemented contract. Both facts are reported separately.
    """

    independence = preflight_payload["oracle_independence"][N1.task_id]
    assert independence["seed_hidden"] == "COLLECT_ERROR"
    assert independence["seed_hidden_failed_without_executing_assertions"] is True
    assert independence["seed_hidden_cause"]["cause"].startswith("ImportError")
    assert independence["seed_hidden_cause_class"] == "INTERFACE_MISMATCH"
    assert independence["reference_hidden"] == "PASSED"
    assert independence["hidden_oracle_executed_tests"] > 0
    checks = preflight_payload["checks"]
    assert checks[f"{N1.task_id}:no_harness_failure"] is True
    assert independence["harness_blocked"]["seed_hidden"] is False


def test_a_failing_public_test_does_not_turn_into_a_failing_oracle() -> None:
    """R1-04 negative control: public FAIL + hidden PASS must not read as "the oracle fails"."""

    synthetic = dataclasses.replace(
        P1,
        seed={**P1.seed, "test_public.py": PUBLIC_FAILS},
        public_tests={"test_public.py": PUBLIC_FAILS},
        oracle_tests={"test_hidden_probe.py": HIDDEN_PASSES},
    )
    record = task_selftest(synthetic)

    assert record["seed_public_fails"] is True
    assert record["seed_public"]["kind"] == "TEST_FAILURE"
    assert record["seed_hidden_fails"] is False
    assert record["seed_hidden"]["kind"] == "PASSED"
    assert record["independence"]["seed_hidden_ran_only_hidden_targets"] is True
    assert record["independence"]["separate_workspaces"] is True


def test_an_empty_target_list_is_refused(tmp_path: Path) -> None:
    """``pytest`` with no argument would collect the whole workspace: that must fail closed."""

    record = _run_pytest(tmp_path, [], timeout=5)
    assert record["kind"] == "NO_TARGETS"
    assert record["passed"] is False
    assert record["targets"] == []


# ------------------------------------------------------------------ R1-05: leakage detection


def test_the_workspace_carries_no_hidden_test_and_no_answer(preflight_payload: dict) -> None:
    for task in TASKS:
        record = preflight_payload["tasks"][task.task_id]["workspace_isolation"]
        assert record["passed"] is True, record["leaks"]
        assert record["leaks"] == []
        assert preflight_payload["tasks"][task.task_id]["frozen_identity"]["matches"] is True


def test_a_reference_body_copied_into_the_workspace_is_caught() -> None:
    """R1-05 negative control: the answer under a different name must be detected."""

    name, content = answer_bearing_reference(P1)
    leaked = dataclasses.replace(P1, seed={**P1.seed, "pipeline/leaked_answer.py": content})
    record = workspace_isolation(leaked)

    assert record["passed"] is False
    assert any("reference body" in leak for leak in record["leaks"]), record["leaks"]
    assert any(name in leak for leak in record["leaks"]), record["leaks"]


def test_a_renamed_hidden_oracle_copied_into_the_workspace_is_caught() -> None:
    """R1-05 negative control: renaming the oracle defeats a name check, not a body check."""

    _, body = next(iter(P1.oracle_tests.items()))
    leaked = dataclasses.replace(P1, seed={**P1.seed, "test_hidden_renamed.py": body})
    record = workspace_isolation(leaked)

    assert record["passed"] is False
    assert any("hidden test body staged" in leak for leak in record["leaks"]), record["leaks"]


def test_a_seed_edit_breaks_the_identity_against_the_frozen_baseline() -> None:
    """R1-05: the comparison target is the C2-A table, not the object being checked."""

    victim = "pipeline/parsing.py"
    assert victim in P1.seed
    tampered = dataclasses.replace(
        P1, seed={**P1.seed, victim: P1.seed[victim] + "\n# tampered after the freeze\n"}
    )

    identity = frozen_identity(tampered)
    assert identity["matches"] is False
    assert "seed_digest" in identity["mismatches"]
    assert answer_leak_guard(tampered)["passed"] is False


def test_a_reference_module_that_equals_its_seed_is_not_a_leak() -> None:
    """Same file name and same content is not leakage: identity, not a name, decides.

    Sharing a *name* with the seed is by design — every reference module is a migration of a seeded
    file. Only a body that actually differs from the seed can carry an answer.
    """

    unchanged = "pipeline/parsing.py"
    assert unchanged in P1.seed
    not_answer_bearing = dataclasses.replace(
        P1, reference={**P1.reference, unchanged: P1.seed[unchanged]}
    )
    record = workspace_isolation(not_answer_bearing)
    assert record["passed"] is True, record["leaks"]


# ------------------------------------------------------------------ R1-06: router decision gate


def test_the_router_decisions_are_the_registered_ones(preflight_payload: dict) -> None:
    by_family = {task.family: preflight_payload["tasks"][task.task_id] for task in TASKS}

    for family in ("P1", "P2"):
        record = by_family[family]
        assert record["router"]["route"] == "delegate_parallel", record["router"]
        assert "DELEGATION_ADMITTED" in record["router"]["reason_codes"]
        assert record["router"]["blocked_gates"] == []
        assert record["dag"]["max_wave_width"] == 2
        assert record["dag"]["first_wave_disjoint"] is True
        assert record["router_gate"]["passed"] is True
        assert len(record["router_gate"]["wave0"]) == 2

    negative = by_family["N1"]
    assert negative["router"]["route"] == "direct"
    assert "SHARED_WRITE_RISK" in negative["router"]["reason_codes"]
    assert "UNSTABLE_INTERFACE" in negative["router"]["reason_codes"]
    assert "GATE_2_DEPENDENCY_COUPLING" in negative["router"]["blocked_gates"]
    assert negative["dag"]["max_wave_width"] == 1
    assert negative["router_gate"]["passed"] is True
    assert len(negative["router_gate"]["wave0"]) == 1


def test_the_router_policy_is_the_frozen_one(preflight_payload: dict) -> None:
    assert preflight_payload["router_policy"]["digest"] == ROUTING_POLICY_DIGEST
    assert RoutingPolicy(version="r0.1.0").digest() == ROUTING_POLICY_DIGEST
    assert preflight_payload["router_policy"]["version"] == "r0.1.0"


def test_a_drifted_route_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1-06 negative control: the policy digest is unchanged, but the decision is not."""

    drifted = SimpleNamespace(
        decision=SimpleNamespace(
            route="direct",
            reason_codes=("SHARED_WRITE_RISK",),
            blocked_gates=("GATE_2_DEPENDENCY_COUPLING",),
            gates=(),
            max_parallel_wave_width=1,
            diagnostics=(),
        ),
        execution_mode="single_worker",
    )
    monkeypatch.setattr(preflight, "plan_route", lambda *args, **kwargs: drifted)

    audit = router_audit(P1)
    assert audit["policy_digest"] == ROUTING_POLICY_DIGEST
    gate = router_gate(P1, audit, dag_audit(P1))

    assert gate["passed"] is False
    assert any("route" in problem for problem in gate["problems"]), gate["problems"]
    assert gate["observed_route"] == "direct"


def test_a_drifted_reason_code_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    admitted_without_reason = SimpleNamespace(
        decision=SimpleNamespace(
            route="delegate_parallel",
            reason_codes=(),
            blocked_gates=(),
            gates=(),
            max_parallel_wave_width=2,
            diagnostics=(),
        ),
        execution_mode="parallel_workers",
    )
    monkeypatch.setattr(preflight, "plan_route", lambda *args, **kwargs: admitted_without_reason)

    gate = router_gate(P1, router_audit(P1), dag_audit(P1))

    assert gate["passed"] is False
    assert any("reason codes" in problem for problem in gate["problems"]), gate["problems"]


def test_a_collapsed_first_wave_fails_the_gate() -> None:
    """The gate checks wave-0 width, not only the route name."""

    audit = router_audit(P1)
    dag = dict(dag_audit(P1))
    dag["first_wave"] = dag["first_wave"][:1]

    gate = router_gate(P1, audit, dag)

    assert gate["passed"] is False
    assert any("wave-0" in problem for problem in gate["problems"]), gate["problems"]


def test_overlapping_wave_zero_write_sets_fail_the_gate() -> None:
    audit = router_audit(P1)
    dag = dict(dag_audit(P1))
    dag["first_wave_disjoint"] = False
    dag["first_wave_write_overlap"] = ["parse & aggregate: ['pipeline/parsing.py']"]

    gate = router_gate(P1, audit, dag)

    assert gate["passed"] is False
    assert any("overlap" in problem for problem in gate["problems"]), gate["problems"]


def test_the_declared_write_sets_are_the_owned_write_sets(preflight_payload: dict) -> None:
    for task in TASKS:
        dag = preflight_payload["tasks"][task.task_id]["dag"]
        assert dag["declared_write_paths_match_ownership"] is True, dag["declared_write_paths"]
        for step_id, paths in dag["declared_write_paths"].items():
            assert paths, f"{task.task_id}/{step_id} declares no write path"


def test_the_dag_audit_reports_disjoint_waves() -> None:
    for task in TASKS:
        dag = dag_audit(task)
        assert dag["dependency_complete"] is True
        assert dag["integration_dependencies_are_declared_steps"] is True
        if task.family in ("P1", "P2"):
            assert dag["wave_zero_width_is_two"] is True


# ------------------------------------------------------------------ layout and parity


def test_both_layouts_resolve_and_an_unknown_one_is_refused(preflight_payload: dict) -> None:
    layout = preflight_payload["layout"]
    assert layout["legacy_resolved_layout"] == "legacy"
    assert layout["shared_prefix_resolved_layout"] == "shared_prefix"
    assert layout["unknown_layout_rejected"] is True
    assert layout["ablation_single_variable"] is True
    record = layout_executable()
    assert record["legacy_ok"] is True
    assert record["shared_prefix_ok"] is True
    assert record["transform_identity"] is True


def test_same_topology_digests_are_equal(preflight_payload: dict) -> None:
    for group in preflight_payload["parity"]["groups"]:
        assert group["equal"] is True, group
    assert preflight_payload["parity"]["all_task_facts_equal"] is True


# ------------------------------------------------------------------ R2-03: session classification

CLASSIFICATION_CASES: dict[str, dict] = {
    "assertion": {
        "files": {"test_a.py": "def test_failure():\n    assert False\n"},
        "kind": "TEST_FAILURE",
        "cause_class": None,
        "valid_defect": True,
        "harness_blocked": False,
    },
    "fixture_setup_error": {
        "files": {
            "test_a.py": (
                "import pytest\n\n\n"
                "@pytest.fixture\n"
                "def broken_fixture():\n"
                "    raise RuntimeError('setup failed')\n\n\n"
                "def test_example(broken_fixture):\n"
                "    assert True\n"
            )
        },
        "kind": "SETUP_ERROR",
        "cause_class": None,
        "valid_defect": False,
        "harness_blocked": True,
    },
    "fixture_teardown_error": {
        "files": {
            "test_a.py": (
                "import pytest\n\n\n"
                "@pytest.fixture\n"
                "def broken_teardown():\n"
                "    yield 1\n"
                "    raise RuntimeError('teardown failed')\n\n\n"
                "def test_example(broken_teardown):\n"
                "    assert True\n"
            )
        },
        "kind": "TEARDOWN_ERROR",
        "cause_class": None,
        "valid_defect": False,
        "harness_blocked": True,
    },
    "collection_interface_mismatch": {
        "files": {
            "lifecycle.py": "def approve():\n    return True\n",
            "test_a.py": "from lifecycle import reject\n\n\ndef test_x():\n    assert True\n",
        },
        "kind": "COLLECT_ERROR",
        "cause_class": "INTERFACE_MISMATCH",
        "valid_defect": True,
        "harness_blocked": False,
    },
    "collection_environment": {
        "files": {"test_a.py": "import third_party_missing\n\n\ndef test_x():\n    assert True\n"},
        "kind": "COLLECT_ERROR",
        "cause_class": "ENVIRONMENT",
        "valid_defect": False,
        "harness_blocked": True,
    },
    "collection_unparsable": {
        "files": {"test_a.py": "def test_ok(:\n    assert True\n"},
        "kind": "COLLECT_ERROR",
        "cause_class": "ENVIRONMENT",
        "valid_defect": False,
        "harness_blocked": True,
    },
    "mixed_failure_and_setup_error": {
        "files": {
            "test_a.py": (
                "import pytest\n\n\n"
                "@pytest.fixture\n"
                "def broken_fixture():\n"
                "    raise RuntimeError('setup failed')\n\n\n"
                "def test_fails():\n"
                "    assert False\n\n\n"
                "def test_setup_error(broken_fixture):\n"
                "    assert True\n"
            )
        },
        "kind": "MIXED_ERROR",
        "cause_class": None,
        "valid_defect": False,
        "harness_blocked": True,
    },
    "passing": {
        "files": {"test_a.py": "def test_ok():\n    assert True\n"},
        "kind": "PASSED",
        "cause_class": None,
        "valid_defect": False,
        "harness_blocked": False,
    },
}


@pytest.mark.parametrize("case", sorted(CLASSIFICATION_CASES), ids=str)
def test_r2_03_each_pytest_shape_is_classified_and_gated(case: str, tmp_path: Path) -> None:
    """Exit code 1 is not one fact. Every shape below is a real pytest session, not a guess."""

    expected = CLASSIFICATION_CASES[case]
    workspace = tmp_path / case
    _write_files(workspace, expected["files"])

    record = _run_pytest(workspace, ["test_a.py"], timeout=120)

    assert record["kind"] == expected["kind"], (record["kind"], record["tail"])
    assert record["cause_class"] == expected["cause_class"], (
        record["cause_class"],
        record["cause"],
    )
    assert session_is_valid_defect(record) is expected["valid_defect"], record["tail"]
    assert harness_blocked(record) is expected["harness_blocked"], record["tail"]


def test_r2_03_a_broken_fixture_never_reads_as_a_seed_defect() -> None:
    """The gate itself must refuse a setup error, not only the classifier."""

    synthetic = dataclasses.replace(
        P1,
        oracle_tests={
            "test_hidden_broken.py": (
                "import pytest\n\n\n"
                "@pytest.fixture\n"
                "def broken_fixture():\n"
                "    raise RuntimeError('setup failed')\n\n\n"
                "def test_hidden_contract(broken_fixture):\n"
                "    assert True\n"
            )
        },
    )
    record = task_selftest(synthetic)

    assert record["seed_hidden"]["kind"] == "SETUP_ERROR"
    assert record["seed_hidden_fails"] is False
    assert record["harness_blocked"]["seed_hidden"] is True
    assert record["failure_classification"]["seed_hidden"] == "SETUP_ERROR"


def test_r2_03_a_third_party_collection_error_does_not_count_as_an_interface_gap() -> None:
    """A missing dependency is not the seed failing to implement the contract."""

    synthetic = dataclasses.replace(
        P1,
        oracle_tests={
            "test_hidden_dependency.py": (
                "import a_package_that_is_not_installed\n\n\n"
                "def test_hidden_contract():\n"
                "    assert True\n"
            )
        },
    )
    record = task_selftest(synthetic)

    assert record["seed_hidden"]["kind"] == "COLLECT_ERROR"
    assert record["seed_hidden"]["cause_class"] == "ENVIRONMENT"
    assert record["seed_hidden_fails"] is False
    assert record["harness_blocked"]["seed_hidden"] is True


def test_r2_03_the_classifier_reads_phases_not_just_the_exit_code() -> None:
    """Recorded pytest output, asserted directly: the exit code carries no phase information."""

    setup = (
        "_______________________ ERROR at setup of test_example ________________________\n"
        "E       RuntimeError: setup failed\n"
        "=========================== short test summary info ===========================\n"
        "ERROR test_a.py::test_example - RuntimeError: setup failed\n"
        "1 error in 0.05s\n"
    )
    teardown = (
        "______________________ ERROR at teardown of test_example ______________________\n"
        "E       RuntimeError: teardown failed\n"
        "1 passed, 1 error in 0.04s\n"
    )
    mixed = (
        "_____________________ ERROR at setup of test_setup_error ______________________\n"
        "FAILED test_a.py::test_fails - assert False\n"
        "1 failed, 1 error in 0.05s\n"
    )
    assertion = "FAILED test_a.py::test_failure - assert False\n1 failed in 0.05s\n"

    assert classify_pytest(1, setup) == "SETUP_ERROR"
    assert classify_pytest(1, teardown) == "TEARDOWN_ERROR"
    assert classify_pytest(1, mixed) == "MIXED_ERROR"
    assert classify_pytest(1, assertion) == "TEST_FAILURE"
    # An exit code 1 with nothing readable in it is not a defect either.
    assert classify_pytest(1, "something went wrong\n") == "UNKNOWN_ERROR"
    assert classify_pytest(1, "1 error in 0.05s\n") == "UNKNOWN_ERROR"
    assert classify_pytest(7, "weird\n") == "UNKNOWN_ERROR"
    assert classify_pytest(0, "") == "PASSED"


def test_r2_03_a_collection_error_names_its_cause_class(tmp_path: Path) -> None:
    local = "E   ImportError: cannot import name 'reject' from 'lifecycle.approval'\n"
    missing_local = "E   ModuleNotFoundError: No module named 'lifecycle_helper'\n"
    missing_third_party = "E   ModuleNotFoundError: No module named 'pytest_asyncio'\n"
    attribute = "E   AttributeError: module 'lifecycle' has no attribute 'reject'\n"

    (tmp_path / "lifecycle.py").write_text("def approve():\n    return True\n", encoding="utf-8")

    assert collect_cause_class(local, tmp_path) == "INTERFACE_MISMATCH"
    assert collect_cause_class(attribute, tmp_path) == "INTERFACE_MISMATCH"
    assert collect_cause_class(missing_third_party, tmp_path) == "ENVIRONMENT"
    assert collect_cause_class(missing_local, tmp_path) == "ENVIRONMENT"
    assert collect_cause_class("E   SyntaxError: invalid syntax\n", tmp_path) == "ENVIRONMENT"
    # Without a workspace there is nothing to compare against, so nothing may be called a mismatch.
    assert collect_cause_class(local, None) == "ENVIRONMENT"


# ------------------------------------------------------------------ artifact


def test_the_r1_artifact_is_written_with_its_own_identity(
    preflight_payload: dict, tmp_path: Path
) -> None:
    target = tmp_path / "preflight-r1.json"
    _write(target, preflight_payload)
    reloaded = json.loads(target.read_text(encoding="utf-8"))

    assert reloaded["stage"] == "closure-2a-r1"
    assert reloaded["repair_revision"] == "closure-2a-r2"
    assert reloaded["preregistration_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert reloaded["status"] == "PASSED"
    assert reloaded["execution_ready"] is False
    assert reloaded["model_calls"] == 0
    assert reloaded["sandbox_runs"] == 0
    assert reloaded["approval_binding"]["passed"] is True
    assert target.parent == tmp_path


def test_the_artifact_never_contains_a_host_path(preflight_payload: dict) -> None:
    text = json.dumps(preflight_payload)
    assert str(REPO_ROOT) not in text
