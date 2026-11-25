"""Stage C2: the coding-aware natural delegation decision design.

No model call, no sandbox service, no production claim. The checks that need the production builder
use the same ``production_goal_options`` the live runner and the pre-claim admission both call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_c1 import checks as c1_checks
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import plan as c1b
from experiments.stage_c1c import checks as c1c_checks
from experiments.stage_c1c import plan as c1c
from experiments.stage_c2 import checks, plan, prompts

REPO_ROOT = Path(__file__).resolve().parents[2]

FROZEN_BUDGET_DIGEST = "sha256:a6ea43679649cb39c6c4f55f3bbb915b8a0287abcddce511a725ec6abee23eb9"
C1B_PLAN_DIGEST = "sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c"
C1C_PLAN_DIGEST = "sha256:4481bba2d3a0a9e0e3b7b04c5de3c851c70f1d4d03892043e23046782ab727bb"
C1B_PROMPT_DIGEST = "sha256:4f742d08c6ed04c6ecd07ac446825e17bc8cdf78ec04e956845d64c3092093af"


def _options(tmp_path: Path, cell: dict):
    return checks._production_options(cell, tmp_path / str(cell["order"]))


def test_the_plan_is_four_cells_in_the_frozen_order() -> None:
    record = plan.plan_record()

    assert record["plan_id"] == "stage-c2-coding-aware-natural-delegation-v1"
    assert record["scope"] == "C2_NATURAL_DELEGATION_DIAGNOSTIC_ONLY"
    assert record["stage"] == "C2"
    assert record["cells_count"] == 4
    assert record["is_benchmark_sample"] is False
    assert record["is_diagnostic"] is True
    assert record["attempt_limit"] == 1
    assert record["tasks"] == [plan.P_PLUS_TASK_ID, plan.N_MINUS_TASK_ID]
    assert record["arms"] == ["C3"]
    assert record["prompt_arms"] == ["B0", "B1"]
    assert [cell["cell_id"] for cell in record["cells"]] == [
        "c2.closure2-pipeline-migration.C3.B0.r1",
        "c2.closure2-state-machine-refactor.C3.B0.r1",
        "c2.closure2-pipeline-migration.C3.B1.r1",
        "c2.closure2-state-machine-refactor.C3.B1.r1",
    ]
    assert [(cell["task_id"], cell["prompt_arm"]) for cell in record["cells"]] == list(
        plan.CELL_ORDER
    )


def test_the_baseline_prompt_is_the_c1b_bytes() -> None:
    assert prompts.B0_TEXT == c1b.prompts.V22_TEXT
    assert prompts.B0_DIGEST == c1b.prompts.V22_DIGEST == C1B_PROMPT_DIGEST
    assert prompts.B0_VERSION == "main-v2.2-rc1-c1b"

    cell = next(cell for cell in plan.plan_record()["cells"] if cell["prompt_arm"] == "B0")
    assert cell["main_prompt"]["text"] == c1b.prompts.V22_TEXT


def test_the_treatment_prompt_changes_three_sections_and_nothing_else() -> None:
    assert prompts.V30_VERSION == "main-v3.0-rc1-coding-aware"
    assert prompts.V30_DIGEST != prompts.B0_DIGEST
    assert prompts.validate() == []

    changed = prompts.changed_sections()
    assert changed == (
        "## EARLY PLANNING CHECKPOINT",
        "## DELEGATION CONTRACT",
        "## HANDOFF-FIRST INTEGRATION",
    )
    assert all(prompts.unchanged_sections().values())
    assert prompts.section_headings(prompts.V30_TEXT) == prompts.section_headings(prompts.B0_TEXT)

    # C1-B's verification discipline survives byte for byte.
    start = prompts.V30_TEXT.index("## TESTING AND RECOVERY")
    end = prompts.V30_TEXT.index("## FINAL DELIVERY")
    assert prompts.V30_TEXT[start:end] == c1b.prompts.changed_sections()["replacement"]

    # No mandate and no task-specific text.
    lowered = prompts.V30_TEXT.lower()
    for phrase in prompts.MANDATE_PHRASES:
        assert phrase not in lowered
    for token in prompts.LEAK_TOKENS:
        assert token not in lowered


def test_within_a_task_only_the_prompt_and_the_position_differ() -> None:
    cells = plan.plan_record()["cells"]

    for task_id in plan.TASK_IDS:
        left, right = [cell for cell in cells if cell["task_id"] == task_id]
        differing = {key for key in set(left) | set(right) if left.get(key) != right.get(key)}
        assert differing <= set(plan.PROMPT_ONLY_FIELDS)
        assert left["main_prompt_digest"] != right["main_prompt_digest"]
        assert left["goal_digest"] == right["goal_digest"]
        assert left["child_write_grants"] == right["child_write_grants"]
        assert left["budget_digest"] == right["budget_digest"]


def test_the_goals_are_the_published_requirements_without_an_appendix() -> None:
    for cell in plan.plan_record()["cells"]:
        goal = cell["goal"]
        assert goal == contract.GOAL_OF[cell["task_id"]]
        assert c1.ACTIVATION_INSTRUCTION not in goal
        assert "[YOUR ASSIGNMENT" not in goal
        for oracle_file in contract.TASKS_BY_ID[cell["task_id"]].oracle_tests:
            assert oracle_file not in goal


def test_the_selection_is_structural_not_a_file_count() -> None:
    report = plan.suitability_report()
    positive = report["tasks"][plan.P_PLUS_TASK_ID]
    negative = report["tasks"][plan.N_MINUS_TASK_ID]

    assert positive["independent_pairs"]
    assert positive["shared_write_paths"] == []
    assert positive["integration_seam_after_the_independent_pair"]
    assert positive["criteria"]["delegated_mode_feasible"] is True

    assert negative["independent_pairs"] == []
    assert negative["shared_write_paths"] == ["lifecycle/state.py"]
    assert negative["criteria"]["delegated_mode_feasible"] is False
    for task in (positive, negative):
        assert task["grant_admits_every_deliverable"] is True
        assert task["criteria"]["public_test_and_oracle_available"] is True
        assert task["reference_implementation_available"] is True


def test_the_plan_and_the_effective_budget_are_bound_and_distinct() -> None:
    record = plan.plan_record()

    assert record["budget"]["token_budget"] == 750_000
    assert record["budget_digest"] == authority.digest_of(dict(record["budget"]))
    assert record["budget_digest"] != FROZEN_BUDGET_DIGEST
    assert record["nominal_token_capacity"] == 3_000_000
    assert authority.plan_facts(record)["token_budget"] == 750_000
    assert authority.plan_facts(record)["budget_digest"] == record["budget_digest"]

    for cell in record["cells"]:
        assert cell["budget"] == record["budget"]
        assert cell["token_budget"] == 750_000
        assert contract.token_budget_for(cell) == 750_000
        assert cell["attempt_limit"] == 1
        assert cell["is_benchmark_sample"] is False

    assert contract.token_budget_for({}) == 500_000
    assert record["digest"] != C1B_PLAN_DIGEST
    assert record["digest"] != C1C_PLAN_DIGEST
    assert record["envelope_candidate"]["approved"] is False
    assert record["envelope_candidate"]["cost_stop_usd"] == 1.00
    assert record["envelope_candidate"]["per_cell_cost_stop_usd"] == 0.25


def test_production_options_carry_the_arm_the_task_and_the_budget(tmp_path: Path) -> None:
    cells = plan.plan_record()["cells"]
    baseline = next(cell for cell in cells if cell["prompt_arm"] == "B0")
    treatment = next(cell for cell in cells if cell["prompt_arm"] == "B1")

    b0 = _options(tmp_path, baseline)
    b1 = _options(tmp_path, treatment)

    assert b0.token_budget == b1.token_budget == 750_000
    assert b0.max_turns == b1.max_turns == 20
    assert b0.worker_max_turns == b1.worker_max_turns == 20
    assert b0.main_prompt.text == prompts.B0_TEXT
    assert b1.main_prompt.text == prompts.V30_TEXT
    assert b0.main_prompt.digest != b1.main_prompt.digest
    assert b0.goal == b1.goal == plan.goal_text(baseline["task_id"])
    assert b0.child_write_grants == b1.child_write_grants
    assert list(b0.main_write_paths) == list(
        contract.TASKS_BY_ID[baseline["task_id"]].allowed_paths
    )
    assert b0.worker_profiles == b1.worker_profiles == (contract.CODING,)
    assert b0.worker_exposure == b1.worker_exposure == "card"
    assert b0.delegation.max_children_per_call == 2
    assert b0.delegation.max_parallel_children == 2
    assert b0.delegation.max_delegation_rounds == 3
    assert b0.delegation.child_max_turns == 20
    assert b0.public_tests.max_calls == 4

    negative = next(cell for cell in cells if cell["task_id"] == plan.N_MINUS_TASK_ID)
    n_minus = _options(tmp_path, negative)
    assert n_minus.child_write_grants == plan.effective_grant(plan.N_MINUS_TASK_ID)


def test_a_real_run_goal_builds_a_750k_budget_state(tmp_path: Path) -> None:
    """The number is checked where it is consumed, not only where it is declared."""

    cell = next(cell for cell in plan.plan_record()["cells"] if cell["prompt_arm"] == "B1")
    proof = c1c_checks.run_budget_proof({"cells": [cell]}, tmp_path / "budget")

    assert proof["options_token_budget"] == 750_000
    assert proof["run_budget_limit_tokens"] == 750_000
    assert proof["session_status"] == "succeeded"


def test_the_router_admits_the_independent_pair_and_refuses_the_shared_write(
    tmp_path: Path,
) -> None:
    report = checks._check_router(plan.plan_record(), tmp_path)

    assert report["ok"], report["detail"]
    assert report["independent"]["route"] == "delegate_parallel"
    assert report["independent"]["admitted_children"] == ["parse", "aggregate"]
    assert report["shared_write"]["route"] == "direct"
    assert "SHARED_WRITE_RISK" in report["shared_write"]["reason_codes"]
    assert report["frozen_dag"]["route"] == "direct"
    assert "DEPENDENCY_NOT_SUPPORTED" in report["frozen_dag"]["reason_codes"]


def test_the_plan_id_and_scope_resolve_through_the_dispatch_entry() -> None:
    record = plan.plan_record()
    by_id = contract.scope_plan(plan.PLAN_ID)
    by_scope = contract.scope_plan(plan.SCOPE)

    assert by_id["plan_id"] == plan.PLAN_ID
    assert by_id["digest"] == record["digest"]
    assert by_scope == by_id
    for rejected in checks.REJECTED_PLAN_IDS:
        with pytest.raises(ValueError):
            contract.scope_plan(rejected)


def test_the_older_entries_keep_their_identities() -> None:
    assert contract.scope_plan(c1.PLAN_ID)["digest"] == c1.plan_record()["digest"]
    assert contract.scope_plan(c1b.PLAN_ID)["digest"] == C1B_PLAN_DIGEST
    assert contract.scope_plan(c1c.PLAN_ID)["digest"] == C1C_PLAN_DIGEST
    assert contract.scope_plan(contract.SCOPE_CANARY)["digest"] == (
        c1_checks.BPLUS_CANARY_PLAN_DIGEST
    )
    assert contract.scope_plan(contract.SCOPE_FORMAL)["digest"] == (
        c1_checks.BPLUS_FORMAL_PLAN_DIGEST
    )
    assert contract.BUDGET["token_budget"] == 500_000


def test_pending_authorization_authorizes_nothing() -> None:
    record = plan.plan_record()
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    pending = checks.authorization_record(record, protocol_digest=protocol, identity=identity)

    verdict = authority.verify(
        pending,
        plan=record,
        head=executor.git_head(REPO_ROOT),
        executor=identity,
        protocol_digest_value=protocol,
        repo_root=REPO_ROOT,
    )

    assert verdict["ready"] is False
    assert pending["status"] == "PENDING_OWNER_APPROVAL"
    assert pending["authorizes_live_execution"] is False
    assert pending["cost_stop_usd"] is None
    assert pending["per_cell_cost_stop_usd"] is None
    assert pending["budget_digest"] == record["budget_digest"]
    assert pending["nominal_token_capacity"] == 3_000_000
    assert pending["token_stop"] == 3_000_000
    assert pending["attempt_limit"] == 1
    assert pending["cell_ids"] == [cell["cell_id"] for cell in record["cells"]]
    assert pending["proposed_envelope"]["approved"] is False
    assert pending["review_surface"]["reporting_contract"]["final_correctness"]["source"]


def test_the_plan_validates() -> None:
    assert plan.validate() == []


def test_the_checked_in_artefacts_match_the_current_code() -> None:
    stage = REPO_ROOT / "experiments" / "stage_c2"
    plan_file = json.loads((stage / "plan.json").read_text(encoding="utf-8"))
    identity = executor.executor_identity(REPO_ROOT)
    executor_file = json.loads((stage / "executor.json").read_text(encoding="utf-8"))
    offline = json.loads((stage / "offline.json").read_text(encoding="utf-8"))
    authorization = json.loads((stage / "authorization.json").read_text(encoding="utf-8"))

    assert plan_file["digest"] == plan.plan_record()["digest"]
    assert plan_file["budget_digest"] == plan.plan_record()["budget_digest"]
    assert executor_file["executor_digest"] == identity["executor_digest"]
    assert executor_file["files"] == identity["files"]
    assert offline["plan_digest"] == plan_file["digest"]
    assert offline["budget_digest"] == plan_file["budget_digest"]
    assert offline["executor_digest"] == identity["executor_digest"]
    assert offline["nominal_token_capacity"] == 3_000_000
    assert offline["summary"]["failed"] == []
    assert offline["status"] == "STAGE_C2_NATURAL_DELEGATION_READY_FOR_LIVE_AUTHORIZATION"
    assert authorization["plan_digest"] == plan_file["digest"]
    assert authorization["budget_digest"] == plan_file["budget_digest"]
    assert authorization["token_stop"] == 3_000_000


def test_six_historical_claims_are_present_and_no_c2_claim_exists() -> None:
    record = plan.plan_record()

    labels = [label for label, _ in checks.CONSUMED_CLAIMS]
    assert labels == ["v1", "v2", "v3", "c1", "c1b", "c1c"]
    for _, digest in checks.CONSUMED_CLAIMS:
        assert executor.claim_path(digest).is_file()
    assert not executor.claim_path(record["digest"]).exists()


def test_every_check_passes_on_this_repository(tmp_path: Path) -> None:
    record = plan.plan_record()
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    pending = checks.authorization_record(record, protocol_digest=protocol, identity=identity)

    report = checks.run_checks(
        scratch=tmp_path,
        head=executor.git_head(REPO_ROOT),
        identity=identity,
        protocol_digest=protocol,
        plan_record=record,
        pending=pending,
    )

    failed = [item["check_id"] for item in report if not item["ok"]]
    assert failed == []
    assert len(report) == 13
