"""Stage C1: the positive control's preparation invariants.

One real model call is not made here and no sandbox is created. These tests assert the frozen
identity, the one intervention, the reused execution path, and the admission finding the stage
exists to measure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_c1 import checks
from experiments.stage_c1 import plan as c1

REPO_ROOT = Path(__file__).resolve().parents[2]


# -- identity ------------------------------------------------------------------------------------


def test_the_plan_is_one_cell_on_p2_with_its_own_identity() -> None:
    plan = c1.plan_record()
    assert plan["plan_id"] == "stage-c1-coding-delegation-positive-control-v1"
    assert plan["scope"] == "C1_POSITIVE_CONTROL_ONLY"
    assert plan["cells_count"] == 1
    assert plan["is_benchmark_sample"] is False
    assert plan["authorization_migrates"] is False
    assert plan["attempt_limit"] == 1

    cell = plan["cells"][0]
    assert cell["task_id"] == "closure2-plugin-migration"
    assert cell["arm_id"] == "C3"
    assert cell["worker_profile"] == contract.CODING
    assert cell["worker_exposure"] == "card"
    assert cell["is_benchmark_sample"] is False
    assert cell["cell_id"].startswith("c1.")
    assert cell["execution_contract"] == contract.EXECUTION_CONTRACT_DIGEST

    from experiments.stage_benchmark import matrix

    assert plan["digest"] == matrix.plan_digest(plan["cells"])
    assert plan["nominal_token_capacity"] == 500_000


def test_scope_plan_resolves_the_plan_by_id_and_by_scope() -> None:
    by_id = contract.scope_plan(c1.PLAN_ID)
    by_scope = contract.scope_plan(c1.SCOPE)
    assert by_id["digest"] == by_scope["digest"] == c1.plan_record()["digest"]
    assert by_id["is_benchmark_sample"] is False


def test_the_plan_validates_and_a_moved_appendix_is_rejected() -> None:
    assert c1.validate() == []

    tampered = dict(c1.plan_record())
    cell = dict(tampered["cells"][0])
    cell["goal"] = cell["goal"].replace("at least two", "at least three")
    tampered["cells"] = [cell]
    problems = c1.validate(tampered)
    assert any("activation instruction" in item or "goal" in item for item in problems)


# -- the one intervention ------------------------------------------------------------------------


def test_the_goal_is_the_frozen_requirement_plus_the_appendix() -> None:
    requirement = contract.GOAL_OF[c1.TASK_ID]
    goal = c1.goal_text()
    assert goal == requirement + "\n\n" + c1.ACTIVATION_INSTRUCTION
    assert goal.startswith(requirement)
    assert goal.count(c1.ACTIVATION_INSTRUCTION) == 1

    cell = c1.plan_record()["cells"][0]
    assert cell["goal"] == goal
    assert cell["goal_digest"] == c1.goal_digest()


def test_no_frozen_assignment_dag_reference_or_hidden_test_reaches_the_goal() -> None:
    goal = c1.goal_text()
    task = c1.task()
    for worker in task.workers:
        assert worker["assignment"] not in goal
    for hidden in task.oracle_tests:
        assert hidden not in goal
    for reference in task.reference:
        assert reference not in goal
    assert "[YOUR ASSIGNMENT" not in goal
    assert "owned_paths" not in goal


def test_the_main_prompt_is_the_bplus_prompt_unchanged() -> None:
    document = contract.protocol_document()
    declared = document["prompts"]["p3-bplus"]["digest"]
    assert c1.prompt_digest() == declared
    assert c1.main_prompt_spec().text == contract.prompt_text("p3-bplus")
    assert document["prompts"]["p3-literal"]["digest"] == contract.P3_DIGEST
    assert c1.plan_record()["intervention"]["main_prompt_modified"] is False


# -- the reused execution path -------------------------------------------------------------------


def _options(tmp_path: Path):
    cell = c1.plan_record()["cells"][0]
    return checks._production_options(cell, tmp_path)


def test_arm_c3_offers_a_write_capable_coding_worker_with_the_card(tmp_path: Path) -> None:
    report = checks._worker_report(_options(tmp_path))
    assert report["profiles_offered"] == [contract.CODING]
    assert report["exposure"] == "card"
    # the disclosure states the grant the runtime enforces, not the unnormalised catalog glob
    assert report["grant"] == ["plugins/"]
    assert report["description_mentions_grant"] is True
    assert report["description_mentions_raw_grant"] is False
    assert report["max_turns_effective"] == 20
    assert report["card_read_only"] is False
    assert {"file_write", "file_edit"} <= set(report["write_tools_present"])
    assert not report["description_mentions_assignment"]


def test_the_cell_binds_both_representations_of_one_scope(tmp_path: Path) -> None:
    """One scope, two strings: the frozen original and the form the matcher accepts."""

    assert c1.raw_grant() == {contract.CODING: ("plugins/**",)}
    assert c1.effective_grant() == {contract.CODING: ("plugins/",)}
    record = c1.grant_record()
    assert record["raw"] == {contract.CODING: ["plugins/**"]}
    assert record["effective"] == {contract.CODING: ["plugins/"]}
    assert record["changes"] == [{"raw": "plugins/**", "effective": "plugins/"}]
    assert record["normalizer"] == "atlasos.ownership.paths.normalize_declared_path"
    assert record["scope_changed"] is False and record["profile_changed"] is False
    assert record["matches_frozen_task_scope"] is True
    cell = c1.plan_record()["cells"][0]
    assert cell["child_write_grants"] == c1.effective_grant()
    assert cell["grant_normalization"] == record


def test_the_grant_is_normalised_against_the_real_sandbox_workspace_root() -> None:
    from atlasos.app.main_driven import SANDBOX_WORKSPACE_ROOT

    assert c1.WORKSPACE_ROOT == SANDBOX_WORKSPACE_ROOT


def test_production_options_carry_the_normalised_grant(tmp_path: Path) -> None:
    """The preflight and the live runner build one ``GoalRunOptions``; it must carry the fix."""

    options = _options(tmp_path)
    assert {profile: tuple(paths) for profile, paths in options.child_write_grants.items()} == {
        contract.CODING: ("plugins/",)
    }
    # the Main's own write scope is the task's, untouched by the child-grant normalisation
    assert tuple(options.main_write_paths) == c1.task().allowed_paths


def test_p2_is_admitted_with_the_normalised_grant_and_the_boundary_holds(tmp_path: Path) -> None:
    """The correction, in both directions.

    The pair must now be admitted *through the production configuration*, and every refusal the
    boundary is supposed to produce must still be produced. The raw-glob replay stays on the record:
    the router is unchanged, so the unnormalised form is still refused — the grant moved, not the
    gate.
    """

    report = checks.admission_report(_options(tmp_path))
    pair = report["production"][checks.POSITIVE_SHAPE]
    raw = report["raw_representation"]

    assert report["production_grant"] == {contract.CODING: ["plugins/"]}
    assert pair["route"] == "delegate_parallel"
    assert pair["admitted_children"] == ["csv", "json"]
    assert "DELEGATION_ADMITTED" in pair["reason_codes"]
    assert "WRITE_SCOPE_NOT_GRANTED" not in pair["reason_codes"]
    assert pair["blocked_gates"] == []
    assert pair["max_wave_width"] == 2

    assert report["raw_grant"] == {contract.CODING: ["plugins/**"]}
    assert raw["route"] == "direct"
    assert raw["reason_codes"] == ["WRITE_SCOPE_NOT_GRANTED"]

    for shape, code in checks.EXPECTED_NEGATIVE_CODES.items():
        produced = report["production"][shape]
        assert produced["route"] == "direct", shape
        assert code in produced["reason_codes"], shape
        assert produced["admitted_children"] == [], shape


def test_the_dispatch_loop_and_audit_accept_one_synthetic_cell(tmp_path: Path) -> None:
    plan = c1.plan_record()
    head = executor.git_head(REPO_ROOT)
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    report = checks.dispatch_report(
        plan,
        scratch=tmp_path / "dispatch",
        head=head,
        identity=identity,
        protocol_digest=protocol,
    )
    assert report["report"]["complete"] is True
    assert report["report"]["cells_started"] == 1
    assert report["report"]["aggregate_includes_sample"] is False
    assert report["cell"]["verdict"] == "PASS"
    assert report["cell"]["execution_integrity"] == "PASS"
    assert report["cell"]["goal_correctness"] is True
    assert report["cell"]["stops"] == []
    assert all(report["evidence_exists"].values())


# -- history, claim and authorization ------------------------------------------------------------


def test_stage_b_plus_identity_did_not_move() -> None:
    assert contract.scope_plan(contract.SCOPE_CANARY)["digest"] == checks.BPLUS_CANARY_PLAN_DIGEST
    assert contract.scope_plan(contract.SCOPE_FORMAL)["digest"] == checks.BPLUS_FORMAL_PLAN_DIGEST
    assert authority.protocol_digest(contract.protocol_document()) == checks.BPLUS_PROTOCOL_DIGEST
    assert contract.validate(contract.protocol_document()) == []
    for plan_id in (contract.CANARY_PLAN_ID_V1, contract.CANARY_PLAN_ID_V2):
        with pytest.raises(ValueError):
            contract.scope_plan(plan_id)


def test_the_c1_claim_was_consumed_exactly_once() -> None:
    """The one-shot claim exists, names this plan and head, and cannot be recreated."""

    plan = c1.plan_record()
    for _, digest in checks.CONSUMED_CLAIMS:
        assert digest != plan["digest"]
    path = executor.claim_path(plan["digest"])
    assert path.is_file()
    claim = json.loads(path.read_text(encoding="utf-8"))
    assert claim["plan_digest"] == plan["digest"]
    assert claim["plan_id"] == plan["plan_id"]
    assert claim["execution_head"] == "1101b6689bf968997f0ec037109ac821d1de24b9"
    assert claim["mode"] == "live"
    assert claim["claim_root_is_default"] is True


def test_pending_authorization_authorizes_nothing() -> None:
    plan = c1.plan_record()
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    record = checks.authorization_record(plan, protocol_digest=protocol, identity=identity)
    verdict = authority.verify(
        record,
        plan=plan,
        head=executor.git_head(REPO_ROOT),
        executor=identity,
        protocol_digest_value=protocol,
        repo_root=REPO_ROOT,
    )
    assert verdict["ready"] is False
    assert record["status"] == "PENDING_OWNER_APPROVAL"
    assert record["authorizes_live_execution"] is False
    assert record["cost_stop_usd"] is None
    assert record["per_cell_cost_stop_usd"] is None
    assert record["token_stop"] == 500_000
    assert record["proposed_envelope"]["approved"] is False


def test_the_checked_in_artefacts_are_the_frozen_history() -> None:
    """The C1 artefacts record the run that happened; later stages must not rewrite them.

    This used to assert that the artefacts matched the *current* code, which was true while the
    stage was only prepared. After the live run the plan digest must still be the C1 digest and the
    executor digest must still be the 1.3 identity that ran, whatever later stages build.
    """

    stage = REPO_ROOT / "experiments" / "stage_c1"
    plan_file = json.loads((stage / "plan.json").read_text(encoding="utf-8"))
    executor_file = json.loads((stage / "executor.json").read_text(encoding="utf-8"))
    offline = json.loads((stage / "offline.json").read_text(encoding="utf-8"))

    assert plan_file["digest"] == (
        "sha256:537508bf0fc10056987ef9342d9899008609aa9ba9c1734f7de53a3d3bc7ecd2"
    )
    assert plan_file["digest"] == c1.plan_record()["digest"]
    assert executor_file["executor_version"] == "bplus-executor-1.3"
    assert executor_file["executor_digest"] == (
        "sha256:c7f3c4c88a86c91be82792fb5da640f8a62e0922840fd9d492606ea8a279937d"
    )
    assert offline["plan_digest"] == plan_file["digest"]
    assert offline["executor_digest"] == executor_file["executor_digest"]
    assert offline["live_execution_authorized"] is False
    assert offline["summary"]["failed"] == []


def test_only_the_claim_check_fails_after_the_live_run(tmp_path: Path) -> None:
    """The preparation gate still holds everywhere except where the run changed the world."""

    plan = c1.plan_record()
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    pending = checks.authorization_record(plan, protocol_digest=protocol, identity=identity)
    report = checks.run_checks(
        scratch=tmp_path,
        head=executor.git_head(REPO_ROOT),
        identity=identity,
        protocol_digest=protocol,
        plan=plan,
        pending=pending,
    )
    failed = [item["check_id"] for item in report if not item["ok"]]

    assert failed == ["C09_new_plan_claim_is_not_created"]
    assert len(report) == 12
