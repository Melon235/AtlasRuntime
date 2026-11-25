"""Closure-2 S08: the canary revision that replaced the blocked identity.

Offline only. Revision 1 must stay exactly as it shipped; revision 2 must be a faithful copy of the
experiment with a new identity, and neither may be runnable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from experiments.evaluation.closure2 import pilot_runner, s08_canary, s08_canary_revision
from experiments.evaluation.closure2.candidate_budget import CANDIDATE_PROFILE
from experiments.evaluation.closure2.protocol import C2A_R1_PREREGISTRATION_DIGEST
from experiments.evaluation.closure2.s08_canary_revision import (
    CANARY_COST_STOP_USD,
    CANARY_V1_PLAN_DIGEST,
    CANARY_V2_PLAN_ID,
    CANARY_V2_PLAN_REVISION,
    canary_v1_plan,
    canary_v2_plan,
    check_revision,
    revision_payload,
    supersession_record,
)

V2_ID = CANARY_V2_PLAN_ID


def test_the_revision_satisfies_its_own_contract() -> None:
    assert check_revision() == []


def test_revision_1_is_frozen_and_still_the_same_digest() -> None:
    plan = canary_v1_plan()
    assert plan.plan_id == pilot_runner.S08_CANARY_PLAN_ID == "closure2-s08-canary-1"
    assert plan.runner_revision == "closure-2-s08-canary-1"
    assert plan.digest() == CANARY_V1_PLAN_DIGEST
    assert s08_canary.canary_plan().digest() == CANARY_V1_PLAN_DIGEST


def test_revision_2_changes_only_the_identity() -> None:
    v1, v2 = canary_v1_plan(), canary_v2_plan()
    assert v2.plan_id == V2_ID
    assert v2.runner_revision == CANARY_V2_PLAN_REVISION == "closure-2-s08-canary-2"
    assert v2.digest() != v1.digest()
    for field in (
        "benchmark_id",
        "preregistration_digest",
        "cells",
        "repetitions",
        "max_cell_executions",
        "estimated_cost_ceiling_usd",
        "budget_profile_digest",
    ):
        assert getattr(v2, field) == getattr(v1, field), field
    assert v2.estimated_cost_ceiling_usd == CANARY_COST_STOP_USD == 0.45
    assert v2.budget_profile_digest == CANDIDATE_PROFILE.digest()
    assert v2.preregistration_digest == C2A_R1_PREREGISTRATION_DIGEST
    assert len(v2.cells) == 7


def test_revision_1_is_superseded_and_can_no_longer_be_authorized() -> None:
    plan, record = pilot_runner.pilot_scope("closure2-s08-canary-1")
    assert record["status"] == "SUPERSEDED_BY_REVISION"
    gate = pilot_runner.pilot_authorization_gate(
        cells=pilot_runner.plan_cells_for("closure2-s08-canary-1"),
        repetition_id=0,
        plan=plan,
        record=record,
    )
    assert gate["execution_ready"] is False
    assert any("not 'APPROVED'" in problem for problem in gate["problems"])


def test_revision_2_ships_pending_and_is_refused() -> None:
    plan, record = pilot_runner.pilot_scope(V2_ID)
    assert plan.digest() == canary_v2_plan().digest()
    assert record["status"] == "PENDING_OWNER_APPROVAL"
    assert record["approved_plan_digest"] == plan.digest()
    for key in ("approved_by", "approval_commit", "approved_at"):
        assert record[key] is None
    gate = pilot_runner.pilot_authorization_gate(
        cells=pilot_runner.plan_cells_for(V2_ID), repetition_id=0, plan=plan, record=record
    )
    assert gate["execution_ready"] is False
    assert all("approval" in problem or "approver" in problem for problem in gate["problems"])


def test_neither_revision_can_start_a_live_run(tmp_path: Path) -> None:
    for plan_id in ("closure2-s08-canary-1", V2_ID):
        called = False

        async def executor(*_args: object, **_kwargs: object) -> object:  # pragma: no cover
            nonlocal called
            called = True
            raise AssertionError("the executor must never be reached")

        with pytest.raises(pilot_runner.PilotNotAuthorized):
            asyncio.run(
                pilot_runner.run_live_pilot(
                    pilot_runner.plan_cells_for(plan_id),
                    runs_root=tmp_path,
                    repetition_id=0,
                    executor=executor,
                    live_intent=True,
                    plan_id=plan_id,
                )
            )
        assert called is False


def test_the_supersession_record_says_what_moved_and_what_did_not() -> None:
    record = supersession_record()
    assert record["supersedes"]["plan_id"] == "closure2-s08-canary-1"
    assert record["supersedes"]["plan_digest"] == CANARY_V1_PLAN_DIGEST
    assert record["supersedes"]["executed"] is False
    assert record["supersedes"]["claim_taken"] is False
    assert record["superseded_by"]["plan_id"] == V2_ID
    assert record["superseded_by"]["plan_digest"] == canary_v2_plan().digest()
    assert record["authorization_migrates"] is False
    assert "plan_digest" in record["changed_fields"]
    assert "cells" in record["unchanged_fields"]
    assert "cleanup failure" in record["reason"]


def test_both_revisions_are_registered_under_separate_paths() -> None:
    ids = pilot_runner.plan_scope_ids()
    assert "closure2-s08-canary-1" in ids
    assert V2_ID in ids
    assert (
        pilot_runner.LIVE_SUMMARY_FILENAMES[V2_ID]
        != pilot_runner.LIVE_SUMMARY_FILENAMES["closure2-s08-canary-1"]
    )
    assert pilot_runner.default_runs_root(V2_ID) != pilot_runner.default_runs_root(
        "closure2-s08-canary-1"
    )


def test_the_artifact_states_that_nothing_is_authorized() -> None:
    payload = revision_payload()
    assert payload["status"] == "REVISION_PENDING_OWNER_APPROVAL"
    assert payload["authorized"] is False
    assert payload["contract_problems"] == []
    assert payload["revision"]["v1"]["approval_status"] == "SUPERSEDED_BY_REVISION"
    assert payload["revision"]["v2"]["approval_status"] == "PENDING_OWNER_APPROVAL"
    assert payload["authorization_template"]["approved_plan_digest"] == canary_v2_plan().digest()
    assert payload["model_calls"] == 0
    assert payload["sandbox_runs"] == 0


def test_the_revision_is_not_a_formal_plan() -> None:
    """S10 freezes the formal identity; a canary revision must not pre-empt it."""

    from experiments.evaluation.closure2.formal_plan import FORMAL_PLAN_IDS, formal_plan

    digests = {formal_plan(repetition).digest() for repetition in (0, 1, 2)}
    assert canary_v2_plan().digest() not in digests
    assert V2_ID not in FORMAL_PLAN_IDS


def test_the_supersession_artifact_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "revision.json"
    s08_canary_revision.write_payload(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["supersession"]["superseded_by"]["plan_digest"] == canary_v2_plan().digest()
    assert payload["budget_profile"]["cell_token_budget"] == 500_000
    assert payload["budget_profile"]["multi_worker_max_turns"] == 20
