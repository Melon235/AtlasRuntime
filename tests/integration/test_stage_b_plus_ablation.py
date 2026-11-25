"""Stage B+ contract, capture and dynamic-path probes.

What is asserted here, and why each assertion is the one that matters:

* the candidate contract validates, and it *refuses* to look like a frozen document;
* the B+ prompt is exactly P3 with one section removed, so the ablation's prompt change is a
  countable deletion rather than a rewrite;
* the arms differ in exactly one place on the wire (the ``delegate_tasks`` description), which
  is what makes the exposure factor separable from the prompt;
* the two control arms keep the shipped description byte for byte, and are identical on the wire;
* the formal matrix is byte-identical to its previous identity, and the canary is its own object;
* a scripted coding delegation actually reaches admission, writes inside the grant, is refused
  outside it, hands a result back, and lets the Main continue;
* a proposal for a profile the run does not offer is refused by the router, not silently run.
"""

from __future__ import annotations

import pytest
from experiments.stage_b_plus import authority, capture, contract, executor, probe

from atlasos.delegation.capability_card import REFERENCE_SURFACE, build_card
from atlasos.delegation.tool import DEFAULT_DESCRIPTION
from atlasos.workers import WorkerCatalog
from atlasos.workers.factory import DEFAULT_AVAILABLE_TOOLS


def _arm_capture(arm: contract.Arm) -> dict:
    task = contract.TASKS[0]
    return capture.capture(
        capture.CaptureArm(
            arm_id=arm.arm_id,
            prompt=capture_module_prompt(),
            worker_profiles=(arm.profile_id,),
            worker_exposure=arm.exposure,
            child_write_grants=arm.grant_for(task.task_id),
            worker_max_turns=int(contract.BUDGET["worker_max_turns"]),
        )
    )


def capture_module_prompt():
    return capture.MainPromptSpec(
        version=contract.prompt_version("p3-bplus"),
        text=contract.prompt_text("p3-bplus"),
    )


# -- contract -----------------------------------------------------------------------------------


def test_candidate_contract_validates() -> None:
    document = contract.protocol_document()
    assert contract.validate(document) == []


def test_candidate_contract_refuses_to_claim_a_superseded_document() -> None:
    document = contract.protocol_document()
    document["supersedes"] = "1.6"
    assert "a candidate contract must not claim to supersede a frozen document" in (
        contract.validate(document)
    )


def test_contract_authorizes_nothing() -> None:
    authorization = contract.protocol_document()["authorization"]
    assert authorization["live_execution_authorized"] is False
    assert authorization["cells_allowed"] == 0
    assert authorization["deepseek_calls_allowed"] == 0
    assert authorization["sandboxes_allowed"] == 0
    assert authorization["reuses_s06_or_s08_balance"] is False


def test_plan_is_twenty_four_unique_balanced_cells() -> None:
    cells = contract.cells()
    assert len(cells) == 24
    assert len({cell["cell_id"] for cell in cells}) == 24
    report = contract.balance(cells)
    assert report["each_task_meets_each_arm"] is True
    assert report["every_arm_run_equally_often"] is True
    assert report["first_arm_differs_across_repetitions"] is True


def test_every_arm_shares_one_budget() -> None:
    arms = contract.protocol_document()["arms"]
    assert {arm["arm_id"] for arm in arms} == set(contract.ARM_IDS)
    assert all(arm["budget"] == contract.BUDGET for arm in arms)
    pairs = {(arm["capability"], arm["exposure"]) for arm in arms}
    assert pairs == {
        ("research", "native"),
        ("research", "card"),
        ("coding", "native"),
        ("coding", "card"),
        ("research", "none"),
        ("coding", "none"),
    }
    # The formal matrix is the crossed four; the two no-disclosure arms are controls.
    assert set(contract.FORMAL_ARM_IDS) == {"C0", "C1", "C2", "C3"}
    assert set(contract.CONTROL_ARM_IDS) == {"C4", "C5"}
    assert all(
        arm["is_control"] is (arm["arm_id"] in contract.CONTROL_ARM_IDS) for arm in arms
    )


# -- prompt derivation --------------------------------------------------------------------------


def test_bplus_prompt_is_p3_with_one_section_removed() -> None:
    p3 = contract.p3_text()
    bplus, removed = contract.remove_capability_section(p3)
    assert len(bplus) < len(p3)
    assert removed.startswith("## WORKER CAPABILITY CONTRACT")
    assert "WORKER CAPABILITY CONTRACT" not in bplus
    assert "HANDOFF-FIRST INTEGRATION" in bplus
    # The rest of the document survives byte for byte, in order.
    head, tail = p3[: p3.index(removed)], p3[p3.index(removed) + len(removed) :]
    assert bplus == head + tail
    assert bplus.startswith(head[:200])


def test_p3_literal_is_still_available_and_unchanged() -> None:
    assert contract.prompt_text("p3-literal") == contract.p3_text()
    literal = contract.protocol_document()["prompts"]["p3-literal"]
    assert literal["digest"] == contract.P3_DIGEST
    assert literal["digest"] == capture.digest_text(contract.p3_text())


def test_removal_refuses_a_prompt_without_the_markers() -> None:
    with pytest.raises(ValueError):
        contract.remove_capability_section("# nothing here\n")


def test_goals_carry_no_delegation_hint() -> None:
    for task in contract.TASKS:
        goal = contract.GOAL_OF[task.task_id].lower()
        for phrase in contract.FORBIDDEN_GOAL_PHRASES:
            assert phrase not in goal, f"{task.task_id} goal mentions {phrase!r}"


# -- capture ------------------------------------------------------------------------------------


def test_six_arm_requests_differ_only_in_the_delegate_description() -> None:
    results = [_arm_capture(arm) for arm in contract.ARMS]
    compare = capture.compare(results)
    assert len({item["fingerprint"]["system_prompt"]["digest"] for item in results}) == 1
    # Five, not six: the two control arms carry the same shipped text, which is the point.
    assert len(
        {item["fingerprint"]["delegate_tasks"]["description_digest"] for item in results}
    ) == 5
    allowed = {
        "tools.digest",
        "tools.chars",
        "delegate_tasks.description_chars",
        "delegate_tasks.description_digest",
    }
    for fields in compare["pairwise_changed_fields"].values():
        assert set(fields) <= allowed, fields


def test_control_arms_are_indistinguishable_on_the_wire() -> None:
    """C4 and C5 differ in what the run accepts, not in what the Main reads.

    That is a limitation of the control pair, not a bug: with no disclosure at all the two arms send
    byte-identical requests, so ``C5 - C4`` can only reflect what the runtime does *after* the Main
    calls the tool. Recorded as coupling disclosure ``X7``.
    """

    c4 = _arm_capture(contract.ARMS_BY_ID["C4"])["fingerprint"]["delegate_tasks"]
    c5 = _arm_capture(contract.ARMS_BY_ID["C5"])["fingerprint"]["delegate_tasks"]
    assert c4["description_digest"] == c5["description_digest"]
    assert c4["description_chars"] == c5["description_chars"] == len(DEFAULT_DESCRIPTION)


def test_the_formal_four_differ_only_in_the_delegate_description() -> None:
    """The formal matrix is a subset, so its four arms keep the single-channel property."""

    results = [_arm_capture(contract.ARMS_BY_ID[arm]) for arm in contract.FORMAL_ARM_IDS]
    compare = capture.compare(results)
    assert len(
        {item["fingerprint"]["delegate_tasks"]["description_digest"] for item in results}
    ) == 4
    for fields in compare["pairwise_changed_fields"].values():
        assert set(fields) <= {
            "tools.digest",
            "tools.chars",
            "delegate_tasks.description_chars",
            "delegate_tasks.description_digest",
        }


def test_capture_reports_the_schema_default_profile() -> None:
    """The wire schema defaults ``profile_id`` to one profile: the default is itself a statement."""

    fingerprint = _arm_capture(contract.ARMS_BY_ID["C0"])["fingerprint"]
    assert fingerprint["delegate_tasks"]["profile_default"] == contract.RESEARCH
    assert fingerprint["delegate_tasks"]["profile_enum"] is None


def test_native_arm_exposure_is_not_the_shipped_description() -> None:
    arm = contract.ARMS_BY_ID["C0"]
    result = _arm_capture(arm)
    assert result["fingerprint"]["delegate_tasks"]["description_digest"] != capture.digest_text(
        DEFAULT_DESCRIPTION
    )
    assert result["session"]["delegation_controls"]["exposure"]["level"] == arm.exposure


@pytest.mark.parametrize("arm_id", ["C4", "C5"])
def test_control_arm_keeps_the_shipped_description(arm_id: str) -> None:
    """A ``none`` arm must be the shipped bytes, so it cannot smuggle a capability hint in."""

    arm = contract.ARMS_BY_ID[arm_id]
    result = _arm_capture(arm)
    block = result["fingerprint"]["delegate_tasks"]
    assert block["description_digest"] == capture.digest_text(DEFAULT_DESCRIPTION)
    assert block["description_chars"] == len(DEFAULT_DESCRIPTION)
    assert result["session"]["delegation_controls"]["exposure"]["level"] == "none"
    assert result["session"]["delegation_controls"]["exposure"]["cards"] == []
    # The capability is still real for C5: only the disclosure is absent.
    assert arm.profile_id == (contract.CODING if arm_id == "C5" else contract.RESEARCH)


def test_coding_control_arm_still_grants_a_write_scope() -> None:
    arm = contract.ARMS_BY_ID["C5"]
    assert arm.grant_for(contract.TASKS[0].task_id) == {
        contract.CODING: contract.TASK_WRITE_SCOPE[contract.TASKS[0].task_id]
    }
    assert contract.ARMS_BY_ID["C4"].grant_for(contract.TASKS[0].task_id) == {}


def test_exposure_evidence_records_the_effective_profile_shape() -> None:
    controls = _arm_capture(contract.ARMS_BY_ID["C3"])["session"]["delegation_controls"]
    profile = controls["profiles"][contract.CODING]
    assert profile["effective_max_turns"] == int(contract.BUDGET["worker_max_turns"])
    assert profile["granted_write_paths"] == list(
        contract.TASK_WRITE_SCOPE[contract.TASKS[0].task_id]
    )
    assert controls["authority"] == "disclosure_only"


def test_reference_surface_and_factory_gate_agree() -> None:
    shipped = WorkerCatalog()
    for profile_id in (contract.RESEARCH, contract.CODING):
        profile = shipped.profiles[profile_id]
        built = build_card(
            profile,
            registered_tools=DEFAULT_AVAILABLE_TOOLS,
            surface_tools=REFERENCE_SURFACE,
            child_max_turns=int(contract.BUDGET["child_max_turns"]),
            result_preview_chars=900,
        )
        assert set(built.allowed) == set(profile.base_tools)


# -- dynamic path -------------------------------------------------------------------------------


def _probe(probe_id: str) -> probe.ProbeResult:
    table = {item.probe_id: item for item in probe.probe_table()}
    return probe.run_probe(table[probe_id])


def test_coding_delegation_reaches_admission_and_keeps_the_write_scope() -> None:
    result = _probe("coding_parallel")
    assert result.ok, [check for check in result.checks if not check["ok"]]
    names = {check["check"] for check in result.checks}
    assert {
        "route_is_parallel",
        "two_children_admitted",
        "every_child_completed",
        "granted_write_landed",
        "outside_write_blocked",
        "ownership_denied_the_escape",
        "interpreter_shell_blocked",
        "main_continued_after_handoff",
        "handoff_read_succeeded",
    } <= names


def test_research_write_is_refused_before_a_worker_starts() -> None:
    result = _probe("research_write")
    assert result.ok
    reasons = probe.batch_reasons(probe.first_batch(result.session))
    assert "WRITE_SCOPE_NOT_GRANTED" in reasons


def test_shared_write_is_refused() -> None:
    result = _probe("shared_write")
    assert result.ok
    assert "SHARED_WRITE_RISK" in probe.batch_reasons(probe.first_batch(result.session))


def test_single_coding_task_is_refused_as_trivial() -> None:
    result = _probe("single_coding")
    assert result.ok
    assert "TRIVIAL_DIRECT_TASK" in probe.batch_reasons(probe.first_batch(result.session))


def test_unoffered_profile_is_refused_by_the_router() -> None:
    """Restricting ``worker_profiles`` must make a foreign proposal a refusal, not a grey run."""

    prober = probe.Probe(
        probe_id="unoffered",
        profile_id=contract.RESEARCH,
        tasks=(
            probe.task("alpha", "MARK-ALPHA: do it", profile_id=contract.CODING),
            probe.task("beta", "MARK-BETA: do it", profile_id=contract.CODING),
        ),
        main_script=(
            lambda _messages: [
                probe.ToolUseBlock(
                    id="call-delegate-1",
                    name="delegate_tasks",
                    input={
                        "tasks": [
                            {"key": "alpha", "objective": "MARK-ALPHA", "background": "bg",
                             "profile_id": contract.CODING},
                            {"key": "beta", "objective": "MARK-BETA", "background": "bg",
                             "profile_id": contract.CODING},
                        ],
                        "reason": "unoffered",
                    },
                )
            ],
        ),
        child_scripts={},
    )
    result = probe.run_probe(prober)
    assert probe.batch_reasons(probe.first_batch(result.session)) == ["UNKNOWN_PROFILE"]
    assert result.session["delegation"]["batches"][0]["children"] == []


# -- authorization ------------------------------------------------------------------------------


def _plan_and_identity() -> tuple[dict, dict, str]:
    plan = contract.scope_plan(contract.SCOPE_CANARY)
    identity = executor.executor_identity()
    return plan, identity, authority.protocol_digest(contract.protocol_document())


def _signed(
    plan: dict,
    identity: dict,
    digest: str,
    *,
    head: str,
    cost_stop: float = 1.0,
    per_cell: float = 0.5,
) -> dict:
    pending = authority.pending_record(plan, protocol_digest_value=digest, executor=identity)
    body = {"statement": "integration test", "scope": plan["scope"]}
    return authority.sign(
        pending,
        approved_by="test",
        approved_at="2026-11-25T00:00:00Z",
        owner_statement="integration test, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=cost_stop,
        per_cell_cost_stop_usd=per_cell,
        provenance={
            "kind": "external_file",
            "path": "/nonexistent/owner.json",
            "sha256": authority.digest_of(body),
            "content_digest": authority.digest_of(body),
            "body": body,
        },
    )


def test_pending_authorization_refuses_execution() -> None:
    plan, identity, digest = _plan_and_identity()
    head = executor.git_head()
    record = authority.pending_record(plan, protocol_digest_value=digest, executor=identity)
    verdict = authority.verify(
        record, plan=plan, head=head, executor=identity, protocol_digest_value=digest
    )
    assert verdict["ready"] is False
    with pytest.raises(PermissionError):
        authority.require(
            record, plan=plan, head=head, executor=identity, protocol_digest_value=digest
        )


def test_a_signed_record_for_another_digest_refuses_execution() -> None:
    plan, identity, digest = _plan_and_identity()
    head = executor.git_head()
    record = _signed(plan, identity, digest, head=head)
    other = dict(record, plan_digest="sha256:" + "1" * 64)
    verdict = authority.verify(
        other, plan=plan, head=head, executor=identity, protocol_digest_value=digest
    )
    assert verdict["ready"] is False
    assert any("plan_digest" in item for item in verdict["problems"])
