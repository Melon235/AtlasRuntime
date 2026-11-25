"""Stage C1-C: the resource headroom control and the budget seam that carries it.

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
from experiments.stage_c1b import checks as c1b_checks
from experiments.stage_c1b import plan as c1b
from experiments.stage_c1c import checks, plan

REPO_ROOT = Path(__file__).resolve().parents[2]

FROZEN_BUDGET_DIGEST = "sha256:a6ea43679649cb39c6c4f55f3bbb915b8a0287abcddce511a725ec6abee23eb9"
C1B_PLAN_DIGEST = "sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c"


def _options(tmp_path: Path):
    return checks._production_options(plan.plan_record()["cells"][0], tmp_path)


def test_the_plan_is_one_cell_on_the_c1b_task_and_arm() -> None:
    record = plan.plan_record()

    assert record["plan_id"] == "stage-c1c-completion-headroom-control-v1"
    assert record["scope"] == "C1C_HEADROOM_CONTROL_ONLY"
    assert record["stage"] == "C1C"
    assert record["cells_count"] == 1
    assert record["is_benchmark_sample"] is False
    assert record["is_diagnostic"] is True
    assert record["attempt_limit"] == 1

    cell = record["cells"][0]
    assert cell["cell_id"] == "c1c.closure2-plugin-migration.C3.r1"
    assert cell["task_id"] == c1.TASK_ID
    assert cell["arm_id"] == c1.ARM_ID


def test_the_only_intervention_is_the_shared_token_budget() -> None:
    record = plan.plan_record()
    intervention = record["intervention"]

    assert intervention["kind"] == "resource_headroom_control"
    assert intervention["shared_token_budget_before"] == 500_000
    assert intervention["shared_token_budget_after"] == 750_000
    assert intervention["single_variable_ablation"] is True
    assert intervention["statistical_claim"] is False
    for flag in (
        "main_prompt_changed",
        "worker_contract_changed",
        "goal_changed",
        "task_changed",
        "turns_changed",
        "router_changed",
        "grant_changed",
        "oracle_changed",
    ):
        assert intervention[flag] is False
    assert intervention["cost_stop"]["approved"] is False

    # The budget record differs from the frozen one in exactly one field.
    differences = {
        key: value for key, value in plan.budget().items() if contract.BUDGET.get(key) != value
    }
    assert differences == {"token_budget": 750_000}


def test_the_goal_prompt_worker_contract_grant_and_task_are_c1b_bytes() -> None:
    record = plan.plan_record()
    cell = record["cells"][0]
    c1b_plan = c1b.plan_record()
    c1b_cell = c1b_plan["cells"][0]

    assert cell["goal"] == c1b_cell["goal"]
    assert cell["goal_digest"] == (
        "sha256:1496055dfef64496f2efb570b835cbb0936a32bf67ba2df5b97a1e1e1d508e07"
    )
    assert cell["main_prompt"] == c1b_cell["main_prompt"]
    assert cell["main_prompt_digest"] == (
        "sha256:4f742d08c6ed04c6ecd07ac446825e17bc8cdf78ec04e956845d64c3092093af"
    )
    assert cell["worker_contract_digest"] == (
        "sha256:5b0fa74e9ccdeadbe3a61e41d96ef0608bdc961606397070f25043cb42c45b4a"
    )
    assert cell["child_write_grants"] == c1b_cell["child_write_grants"]
    assert cell["grant_normalization"] == c1b_cell["grant_normalization"]
    assert record["task"]["public_tests"] == c1b_plan["task"]["public_tests"]
    assert record["task"]["oracle_tests"] == sorted(c1.task().oracle_tests)
    assert record["execution_contract_digest"] == c1b_plan["execution_contract_digest"]


def test_the_plan_and_the_effective_budget_are_bound_and_distinct() -> None:
    record = plan.plan_record()
    cell = record["cells"][0]

    assert record["budget"]["token_budget"] == 750_000
    assert record["budget_digest"] == authority.digest_of(dict(record["budget"]))
    assert record["budget_digest"] != FROZEN_BUDGET_DIGEST
    assert cell["budget"] == record["budget"]
    assert cell["budget_digest"] == record["budget_digest"]
    assert cell["token_budget"] == 750_000
    assert record["nominal_token_capacity"] == 750_000
    assert authority.plan_facts(record)["budget_digest"] == record["budget_digest"]
    assert authority.plan_facts(record)["token_budget"] == 750_000
    assert record["digest"] != C1B_PLAN_DIGEST


def test_the_frozen_budget_and_the_older_plans_do_not_move() -> None:
    assert contract.BUDGET["token_budget"] == 500_000
    assert contract.token_budget_for({}) == 500_000

    for older in (
        contract.scope_plan(contract.SCOPE_CANARY),
        contract.scope_plan(contract.SCOPE_FORMAL),
        c1.plan_record(),
        c1b.plan_record(),
    ):
        facts = authority.plan_facts(older)
        assert facts["budget_digest"] == FROZEN_BUDGET_DIGEST
        assert facts["token_budget"] == 500_000

    assert c1b.plan_record()["digest"] == C1B_PLAN_DIGEST
    assert contract.scope_plan(contract.SCOPE_CANARY)["digest"] == (
        c1_checks.BPLUS_CANARY_PLAN_DIGEST
    )
    assert contract.scope_plan(contract.SCOPE_FORMAL)["digest"] == (
        c1_checks.BPLUS_FORMAL_PLAN_DIGEST
    )


def test_production_options_carry_the_new_budget_and_nothing_else(tmp_path: Path) -> None:
    options = _options(tmp_path)
    c1b_options = c1b_checks._production_options(c1b.plan_record()["cells"][0], tmp_path / "c1b")

    assert options.token_budget == 750_000
    assert c1b_options.token_budget == 500_000
    assert options.max_turns == c1b_options.max_turns == 20
    assert options.worker_max_turns == c1b_options.worker_max_turns == 20
    assert options.main_prompt.digest == c1b_options.main_prompt.digest
    assert options.main_prompt.text == c1b_options.main_prompt.text
    assert options.goal == c1b_options.goal
    assert options.child_write_grants == c1b_options.child_write_grants
    assert options.main_write_paths == c1b_options.main_write_paths
    assert options.worker_profiles == c1b_options.worker_profiles
    assert options.worker_exposure == c1b_options.worker_exposure
    assert options.delegation == c1b_options.delegation


def test_a_real_run_goal_builds_a_750k_budget_state(tmp_path: Path) -> None:
    """The number is checked where it is consumed, not only where it is declared."""

    proof = checks.run_budget_proof(plan.plan_record(), tmp_path / "budget")

    assert proof["options_token_budget"] == 750_000
    assert proof["run_budget_limit_tokens"] == 750_000
    assert proof["session_status"] == "succeeded"


def test_the_plan_validates() -> None:
    assert plan.validate() == []


def test_the_plan_id_and_scope_resolve_through_the_dispatch_entry() -> None:
    record = plan.plan_record()
    by_id = contract.scope_plan(plan.PLAN_ID)
    by_scope = contract.scope_plan(plan.SCOPE)

    assert by_id["plan_id"] == plan.PLAN_ID
    assert by_id["digest"] == record["digest"]
    assert by_scope == by_id


def test_the_entry_rejects_unknown_ids_and_keeps_the_older_entries() -> None:
    for rejected in checks.REJECTED_PLAN_IDS:
        with pytest.raises(ValueError):
            contract.scope_plan(rejected)

    assert contract.scope_plan(c1.PLAN_ID)["digest"] == c1.plan_record()["digest"]
    assert contract.scope_plan(c1b.PLAN_ID)["digest"] == C1B_PLAN_DIGEST
    assert contract.scope_plan(contract.SCOPE_CANARY)["digest"] == (
        c1_checks.BPLUS_CANARY_PLAN_DIGEST
    )
    assert contract.scope_plan(contract.SCOPE_FORMAL)["digest"] == (
        c1_checks.BPLUS_FORMAL_PLAN_DIGEST
    )


def test_the_router_still_admits_the_pair(tmp_path: Path) -> None:
    report = c1_checks.admission_report(_options(tmp_path))
    pair = report["production"][c1_checks.POSITIVE_SHAPE]

    assert pair["route"] == "delegate_parallel"
    assert pair["admitted_children"] == ["csv", "json"]
    assert not pair["blocked_gates"]
    assert report["raw_representation"]["reason_codes"] == ["WRITE_SCOPE_NOT_GRANTED"]


def test_pending_authorization_authorizes_nothing_and_binds_the_c1c_budget() -> None:
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
    assert pending["nominal_token_capacity"] == 750_000
    assert pending["token_stop"] == 750_000
    assert pending["attempt_limit"] == 1
    assert pending["proposed_envelope"]["approved"] is False


def test_an_old_budget_digest_or_old_executor_cannot_authorize_the_cell() -> None:
    record = plan.plan_record()
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    pending = checks.authorization_record(record, protocol_digest=protocol, identity=identity)
    head = executor.git_head(REPO_ROOT)

    old_budget = authority.verify(
        dict(pending, budget_digest=FROZEN_BUDGET_DIGEST),
        plan=record,
        head=head,
        executor=identity,
        protocol_digest_value=protocol,
        repo_root=REPO_ROOT,
    )
    old_executor = authority.verify(
        dict(
            pending,
            executor_digest="sha256:145f6c83822c3d38e21f86d4999057250518b2587d273aaa5451b6f5b2bccba4",
        ),
        plan=record,
        head=head,
        executor=identity,
        protocol_digest_value=protocol,
        repo_root=REPO_ROOT,
    )

    assert any("budget_digest" in item for item in old_budget["problems"])
    assert any("executor_digest" in item for item in old_executor["problems"])


def test_the_checked_in_artefacts_match_the_current_code() -> None:
    stage = REPO_ROOT / "experiments" / "stage_c1c"
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
    assert offline["nominal_token_capacity"] == 750_000
    assert offline["summary"]["failed"] == []
    assert offline["status"] == "STAGE_C1C_HEADROOM_CONTROL_READY_FOR_LIVE_AUTHORIZATION"
    assert authorization["plan_digest"] == plan_file["digest"]
    assert authorization["budget_digest"] == plan_file["budget_digest"]
    assert authorization["token_stop"] == 750_000


def test_five_historical_claims_are_present_and_no_c1c_claim_exists() -> None:
    record = plan.plan_record()

    labels = [label for label, _ in checks.CONSUMED_CLAIMS]
    assert labels == ["v1", "v2", "v3", "c1", "c1b"]
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
        plan=record,
        pending=pending,
    )

    failed = [item["check_id"] for item in report if not item["ok"]]
    assert failed == []
    assert len(report) == 10
