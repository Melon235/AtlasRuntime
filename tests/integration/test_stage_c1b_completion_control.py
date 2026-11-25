"""Stage C1-B: the completion-control plan and its two interventions.

No model call, no sandbox, no claim. The checks that need the production builder use the same
``production_goal_options`` the live runner uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_c1 import checks as c1_checks
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import checks, plan, prompts

REPO_ROOT = Path(__file__).resolve().parents[2]


def _options(tmp_path: Path):
    return checks._production_options(plan.plan_record()["cells"][0], tmp_path)


def test_the_plan_is_one_cell_that_reuses_the_c1_goal() -> None:
    record = plan.plan_record()

    assert record["plan_id"] == "stage-c1b-coding-delegation-completion-v1"
    assert record["scope"] == "C1B_COMPLETION_CONTROL_ONLY"
    assert record["cells_count"] == 1
    assert record["is_benchmark_sample"] is False
    assert record["attempt_limit"] == 1

    cell = record["cells"][0]
    assert cell["cell_id"].startswith("c1b.")
    assert cell["goal"] == c1.goal_text()
    assert cell["goal_digest"] == c1.plan_record()["cells"][0]["goal_digest"]
    # the intervention is the prompt and the worker contract, not the goal
    assert cell["main_prompt_digest"] != c1.plan_record()["cells"][0]["main_prompt_digest"]
    assert cell["child_write_grants"] == c1.effective_grant()


def test_the_prompt_replaces_exactly_one_section() -> None:
    base = contract.prompt_text("p3-bplus")
    start = base.index(prompts.SECTION_START)
    end = base.index(prompts.SECTION_END)

    assert prompts.V22_TEXT.startswith(base[:start])
    assert prompts.V22_TEXT.endswith(base[end:])
    assert (
        base[start:end] != prompts.V22_TEXT[start : start + len(prompts.V22_TEXT) - len(base) + end]
    )
    assert (
        "sha256:" + __import__("hashlib").sha256(prompts.V22_TEXT.encode("utf-8")).hexdigest()
        == prompts.V22_DIGEST
    )
    assert prompts.V22_TEXT.count(prompts.SECTION_START) == 1


def test_the_new_section_states_the_runtime_facts() -> None:
    for marker in (
        "`run_public_tests` is the only authorized way",
        "are deterministically refused in this runtime",
        "do not retry it or an equivalent form",
        "necessary, not sufficient",
        "Reserve enough turns for integration",
    ):
        assert marker in prompts.V22_TEXT


def test_the_worker_completion_contract_is_rendered() -> None:
    from atlasos.workers import WorkerCatalog
    from atlasos.workers.models import WorkerTaskContext
    from atlasos.workers.renderer import render_worker_context

    profile = WorkerCatalog().profiles[contract.CODING]
    rendered = render_worker_context(
        profile, WorkerTaskContext(task_id="t", objective="o", background=None)
    )

    for marker in (
        "deterministic refusal",
        "record the requirement as unverified",
        "leave project-level test execution to the Main",
        "scratch files",
        "completion status, changed files",
        "unverified requirements or known limitations",
    ):
        assert marker in rendered
    # the surface is untouched: same tools, same shipped cap
    assert set(profile.base_tools) == set(plan.contract_of_coding_tools())
    assert profile.max_turns == 8


def test_production_options_carry_both_interventions(tmp_path: Path) -> None:
    options = _options(tmp_path)

    assert options.main_prompt.version == prompts.V22_VERSION
    assert options.main_prompt.digest == prompts.V22_DIGEST
    assert options.main_prompt.text == prompts.V22_TEXT
    assert options.child_write_grants == {contract.CODING: ("plugins/",)}
    assert options.max_turns == 20
    assert options.worker_max_turns == 20
    assert options.token_budget == 500000
    assert options.delegation.child_max_turns == 20


def test_the_router_still_admits_the_pair(tmp_path: Path) -> None:
    report = c1_checks.admission_report(_options(tmp_path))
    pair = report["production"][c1_checks.POSITIVE_SHAPE]

    assert pair["route"] == "delegate_parallel"
    assert pair["admitted_children"] == ["csv", "json"]
    assert not pair["blocked_gates"]


def test_the_plan_validates() -> None:
    assert plan.validate() == []


def test_the_plan_id_and_scope_resolve_through_the_dispatch_entry() -> None:
    """The first statement ``dispatch`` executes; C1-B died here before the fix."""

    record = plan.plan_record()
    by_id = contract.scope_plan(plan.PLAN_ID)
    by_scope = contract.scope_plan(plan.SCOPE)

    assert by_id["plan_id"] == plan.PLAN_ID
    assert by_id["digest"] == record["digest"] == checks.FROZEN_PLAN_DIGEST
    assert by_scope == by_id


def test_the_entry_still_rejects_what_it_should_and_keeps_the_older_entries() -> None:
    for rejected in checks.REJECTED_PLAN_IDS:
        with pytest.raises(ValueError):
            contract.scope_plan(rejected)

    assert contract.scope_plan(c1.PLAN_ID)["digest"] == c1.plan_record()["digest"]
    assert (
        contract.scope_plan(contract.SCOPE_CANARY)["digest"] == c1_checks.BPLUS_CANARY_PLAN_DIGEST
    )
    assert (
        contract.scope_plan(contract.SCOPE_FORMAL)["digest"] == c1_checks.BPLUS_FORMAL_PLAN_DIGEST
    )


def test_the_real_dispatch_entry_runs_the_offline_control_flow(tmp_path: Path) -> None:
    """Real entry, real authority check, synthetic cell, throwaway claim root."""

    record = plan.plan_record()
    identity = executor.executor_identity(REPO_ROOT)
    protocol = authority.protocol_digest(contract.protocol_document())
    report = c1_checks.dispatch_report(
        record,
        scratch=tmp_path / "entry",
        head=executor.git_head(REPO_ROOT),
        identity=identity,
        protocol_digest=protocol,
    )

    assert report["report"]["plan_id"] == plan.PLAN_ID
    assert report["report"]["scope"] == plan.SCOPE
    assert report["report"]["plan_digest"] == record["digest"]
    assert report["report"]["complete"] is True
    assert report["report"]["cells_started"] == 1
    assert report["report"]["stop_reason"] == executor.STOP_NONE
    assert report["cell"]["verdict"] == "PASS"
    assert all(report["evidence_exists"].values())


def test_the_checked_in_artefacts_are_the_frozen_history() -> None:
    """The C1-B artefacts record the run that happened; later stages must not rewrite them.

    This used to assert that the artefacts matched the *current* code. That is only true while the
    stage is the newest one: from Stage C1-C onwards the shared executor (`contract.py`,
    `authority.py`, `executor.py`) legitimately moves, so the recorded identity has to be frozen at
    the value the C1-B live run executed under. Stage C1's artefacts are checked the same way.
    """

    stage = REPO_ROOT / "experiments" / "stage_c1b"
    plan_file = json.loads((stage / "plan.json").read_text(encoding="utf-8"))
    executor_file = json.loads((stage / "executor.json").read_text(encoding="utf-8"))
    offline = json.loads((stage / "offline.json").read_text(encoding="utf-8"))

    assert plan_file["digest"] == (
        "sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c"
    )
    assert plan_file["digest"] == plan.plan_record()["digest"]
    assert executor_file["executor_version"] == "bplus-executor-1.5"
    assert executor_file["executor_digest"] == (
        "sha256:145f6c83822c3d38e21f86d4999057250518b2587d273aaa5451b6f5b2bccba4"
    )
    assert offline["plan_digest"] == plan_file["digest"]
    assert offline["executor_digest"] == executor_file["executor_digest"]
    assert offline["summary"]["failed"] == []
    assert offline["status"] == "STAGE_C1B_COMPLETION_CONTROL_READY_FOR_LIVE_AUTHORIZATION"
    assert offline["live_execution_authorized"] is False


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
    assert pending["proposed_envelope"]["approved"] is False
    assert pending["review_surface"]["combined_intervention"] is True


def test_history_is_intact_and_the_c1b_claim_is_consumed() -> None:
    """After the live run the C1-B claim exists and is spent; before it, it must not exist.

    The stage was prepared with `assert not claim.exists()`. That is a statement about the world
    *before* the one attempt, and the run then spent it — exactly like Stage C1. What has to hold
    afterwards is that the claim is this plan's, that it was a live claim, and that no earlier claim
    moved.
    """

    record = plan.plan_record()

    assert (
        contract.scope_plan(contract.SCOPE_CANARY)["digest"] == c1_checks.BPLUS_CANARY_PLAN_DIGEST
    )
    assert (
        authority.protocol_digest(contract.protocol_document()) == c1_checks.BPLUS_PROTOCOL_DIGEST
    )
    for _, digest in checks.CONSUMED_CLAIMS:
        assert executor.claim_path(digest).is_file()
    for previous in (c1.plan_record()["digest"], c1_checks.BPLUS_CANARY_PLAN_DIGEST):
        assert previous != record["digest"]

    claim = executor.claim_path(record["digest"])
    assert claim.is_file()
    payload = json.loads(claim.read_text(encoding="utf-8"))
    assert payload["plan_digest"] == record["digest"]
    assert payload["plan_id"] == plan.PLAN_ID
    assert payload["mode"] == "live"
    assert len(str(payload["execution_head"])) == 40


def test_only_the_claim_check_fails_after_the_live_run(tmp_path: Path) -> None:
    """The preparation gate still holds everywhere except where the run changed the world."""

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
    assert failed == ["C10_history_claims_intact_and_no_c1b_claim"]
    assert len(report) == 14


@pytest.mark.parametrize("label", ["v1", "v2", "v3", "c1"])
def test_consumed_claims_are_named_in_order(label: str) -> None:
    labels = [name for name, _ in checks.CONSUMED_CLAIMS]

    assert label in labels


# -- the classification, end to end through the goal path ----------------------------------------


async def _session(tmp_path: Path, *, script: list[Any], token_budget: int) -> dict[str, Any]:
    """One goal session with the fake providers the reliability suite already uses."""

    import uuid

    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
    from tests.helpers.stage_p import PipelineRig, config, git_repo, sandbox_options

    from atlasos.app.main_driven import GoalRunOptions, run_goal
    from atlasos.delegation import DelegationConfig

    rig = PipelineRig()
    rig.main_script = script
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    source = git_repo(tmp_path / "source")
    options = GoalRunOptions(
        goal="Report what the workspace contains.",
        runs_root=tmp_path / "runs",
        workspace_root=tmp_path / "host-workspace",
        config=config(),
        delegation=DelegationConfig(enabled=False),
        provider_factory=rig.build,
        run_id=f"c1b-{uuid.uuid4().hex[:8]}",
        main_write_paths=("app/",),
        sandbox=sandbox_options(source, lambda _root: provider),
        token_budget=token_budget,
    )
    return await run_goal(options)


async def test_a_real_budget_rejection_is_classified(tmp_path: Path) -> None:
    from tests.helpers.stage_p import text, tool_call

    session = await _session(
        tmp_path,
        script=[tool_call("list_files", {"path": "."}, "c1"), text("done")],
        token_budget=700,
    )

    assert session["status"] == "failed"
    assert session["failure"]["kind"] == "budget_exhaustion"
    assert session["failure"]["classified_from"] == "recorded_stop_evidence"
    assert session["failure"]["main_stop_reason"] == "budget_exceeded"
    assert "budget_exhausted" in session["failure"]["budget_stop_reason"]


async def test_an_unrecognised_stop_stays_unclassified(tmp_path: Path) -> None:
    from tests.helpers.stage_p import text, tool_call

    # The same call three times trips the runtime's doom-loop guard, which is a stop this producer
    # has no recorded kind for: it must stay unclassified so the audit still fails closed.
    repeated = [
        tool_call("list_files", {"path": "."}, "c1"),
        tool_call("list_files", {"path": "."}, "c2"),
        tool_call("list_files", {"path": "."}, "c3"),
        text("done"),
    ]
    session = await _session(tmp_path, script=repeated, token_budget=200_000)

    assert session["status"] == "failed"
    assert session["failure"]["kind"] is None
    assert session["failure"]["classified_from"] == "unclassified"
    assert session["failure"]["main_stop_reason"] == "doom_loop"
