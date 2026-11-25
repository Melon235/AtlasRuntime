# ruff: noqa: E501
"""S08 canary revision 2 — the same seven cells, under a runner that gates dispatch on integrity.

Revision 1 (`closure2-s08-canary-1`, digest `f3d09a85…`) was prepared, reviewed and never executed:
the Phase A.5 audit found that its runner revision would dispatch the next cell after a sandbox
cleanup failure. The scope itself was fine — the same seven cells, order, repetition, ceiling and
budget profile are unchanged here. What changes is the runner revision inside the plan payload, and
with it the plan digest, because a digest that names a runner must not survive a change to what that
runner does.

What this module refuses to do:

* it does not edit revision 1 — that plan object, its digest and its approval template stay byte
  identical, so the blocked attempt remains auditable;
* it does not migrate the authorization. The owner approved `f3d09a85…`; that approval names a digest,
  not a stage, and revision 2 has a different one. The new template ships *pending*;
* it does not move the budget approval either. The same candidate numbers (20 turns, 500 000 tokens,
  USD 0.45) appear in the new scope, but they are a proposal for a new digest, not a continuation of
  the old one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.candidate_budget import CANDIDATE_PROFILE
from experiments.evaluation.closure2.pilot_runner import PilotPlan
from experiments.evaluation.closure2.protocol import C2A_R1_PREREGISTRATION_DIGEST
from experiments.evaluation.closure2.s08_canary import (
    CANARY_ORDER,
    canary_cells,
    canary_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "s08-canary-revision.json"

#: Revision 1, frozen. Never edited, never re-derived from this module.
CANARY_V1_PLAN_ID = "closure2-s08-canary-1"
CANARY_V1_PLAN_REVISION = "closure-2-s08-canary-1"
CANARY_V1_PLAN_DIGEST = "f3d09a8523ae3312872be31dfcc1c1544104de1afa3c9db3b6a5f10108589fbd"

#: Revision 2. Only the identity changes; the experiment does not.
CANARY_V2_PLAN_ID = "closure2-s08-canary-2"
CANARY_V2_PLAN_REVISION = "closure-2-s08-canary-2"

#: The candidate cost stop, unchanged, and explicitly not a migrated approval.
CANARY_COST_STOP_USD = 0.45


class CanaryRevisionError(RuntimeError):
    """The revision is not a faithful copy of revision 1."""


def canary_v1_plan() -> PilotPlan:
    """Revision 1 exactly as it shipped."""

    return canary_plan()


def canary_v2_plan() -> PilotPlan:
    """Revision 2: same scope, new runner revision, and therefore a new digest."""

    return dataclasses.replace(
        canary_plan(),
        plan_id=CANARY_V2_PLAN_ID,
        runner_revision=CANARY_V2_PLAN_REVISION,
    )


def v2_authorization_template() -> dict[str, Any]:
    """The only record that could authorize revision 2. Pending by construction."""

    plan = canary_v2_plan()
    return {
        "status": "PENDING_OWNER_APPROVAL",
        "approved_plan_digest": plan.digest(),
        "approved_by": None,
        "approval_commit": None,
        "approved_at": None,
        "scope": {
            "plan_id": plan.plan_id,
            "cells": list(plan.cells),
            "repetitions": list(plan.repetitions),
            "max_cell_executions": plan.max_cell_executions,
            "estimated_cost_stop_usd": plan.estimated_cost_ceiling_usd,
            "budget_profile_digest": plan.budget_profile_digest,
            "worker_max_turns": CANDIDATE_PROFILE.multi_worker_max_turns,
            "per_cell_token_budget": CANDIDATE_PROFILE.cell_token_budget,
            "attempts": 1,
        },
        "cannot_be_satisfied_by": [
            "the revision 1 approval: it names f3d09a85…, not this digest",
            "protocol.APPROVAL_RECORD: it approves the benchmark definition, not an execution scope",
            "the S06 authorization: a different stage, a different digest, already spent",
            "closure2-pilot-1 / closure2-routing-pair-1 / closure2-connectivity-canary-v1: consumed",
            "any of the three formal plans: a different digest and a different cell list",
        ],
        "granting_requires": (
            "a new commit that sets status=APPROVED with an approver, that commit's hash, a timestamp "
            "and exactly the digest above"
        ),
    }


#: The record the dispatcher reads for revision 2. Pending, so the shipped gate refuses it.
CANARY_V2_APPROVAL: dict[str, Any] = v2_authorization_template()


def supersession_record() -> dict[str, Any]:
    """What changed, what did not, and what does not carry over."""

    v1, v2 = canary_v1_plan(), canary_v2_plan()
    return {
        "kind": "closure2_s08_canary_supersession",
        "supersedes": {
            "plan_id": v1.plan_id,
            "runner_revision": v1.runner_revision,
            "plan_digest": v1.digest(),
            "executed": False,
            "claim_taken": False,
            "approval_status": "SUPERSEDED_BY_REVISION",
        },
        "superseded_by": {
            "plan_id": v2.plan_id,
            "runner_revision": v2.runner_revision,
            "plan_digest": v2.digest(),
            "approval_status": "PENDING_OWNER_APPROVAL",
            "claim_taken": False,
        },
        "reason": (
            "the S08 dispatch integrity gate changes what may follow a completed cell; revision 1's "
            "runner would have dispatched the next cell after a sandbox cleanup failure (audited "
            "offline on 2026-11-25, zero model calls, zero sandboxes)"
        ),
        "changed_fields": ["plan_id", "runner_revision", "plan_digest"],
        "unchanged_fields": [
            "cells",
            "cell_order",
            "repetitions",
            "max_cell_executions",
            "estimated_cost_ceiling_usd",
            "budget_profile_digest",
            "preregistration_digest",
            "benchmark_id",
        ],
        "authorization_migrates": False,
        "cost_stop_is": (
            "the same candidate number (USD 0.45) proposed under a new digest; it is not the "
            "continuation of an approval"
        ),
    }


def check_revision() -> list[str]:
    """Everything the revision must satisfy before it may be proposed for authorization."""

    problems: list[str] = []
    v1 = canary_v1_plan()
    v2 = canary_v2_plan()
    if v1.plan_id != CANARY_V1_PLAN_ID:
        problems.append(f"revision 1 plan id changed: {v1.plan_id!r}")
    if v1.runner_revision != CANARY_V1_PLAN_REVISION:
        problems.append(f"revision 1 runner revision changed: {v1.runner_revision!r}")
    if v1.digest() != CANARY_V1_PLAN_DIGEST:
        problems.append(f"revision 1 digest moved: {v1.digest()}")
    if v2.plan_id != CANARY_V2_PLAN_ID or v2.runner_revision != CANARY_V2_PLAN_REVISION:
        problems.append("revision 2 does not carry its own identity")
    if v2.digest() == v1.digest():
        problems.append("revision 2 has the same digest as revision 1")
    if v2.cells != v1.cells or tuple(v2.cells) != CANARY_ORDER:
        problems.append("revision 2 changed the cell list or its order")
    if v2.repetitions != v1.repetitions:
        problems.append("revision 2 changed the repetition")
    if v2.estimated_cost_ceiling_usd != CANARY_COST_STOP_USD:
        problems.append("revision 2 changed the cost stop")
    if v2.budget_profile_digest != CANDIDATE_PROFILE.digest():
        problems.append("revision 2 does not bind the candidate budget profile")
    if v2.preregistration_digest != C2A_R1_PREREGISTRATION_DIGEST:
        problems.append("revision 2 does not pin the frozen preregistration digest")
    if v2.benchmark_id != v1.benchmark_id:
        problems.append("revision 2 claims a different benchmark identity")
    if len(canary_cells()) != len(CANARY_ORDER) or v2.max_cell_executions != len(CANARY_ORDER):
        problems.append("revision 2 does not authorize exactly the seven cells")
    if CANARY_V2_APPROVAL["status"] == "APPROVED":
        problems.append("revision 2 must ship pending")
    if CANARY_V2_APPROVAL["approved_plan_digest"] != v2.digest():
        problems.append("revision 2 template names a different digest")
    for key in ("approved_by", "approval_commit", "approved_at"):
        if CANARY_V2_APPROVAL.get(key) is not None:
            problems.append(f"revision 2 template must leave {key} empty")
    return problems


def revision_payload() -> dict[str, Any]:
    """The offline artifact a revision-2 approval would be written against."""

    v1, v2 = canary_v1_plan(), canary_v2_plan()
    return {
        "kind": "closure2_s08_canary_revision",
        "stage": "closure-2-s08",
        "status": "REVISION_PENDING_OWNER_APPROVAL",
        "authorized": False,
        "revision": {
            "v1": {
                "plan_id": v1.plan_id,
                "runner_revision": v1.runner_revision,
                "plan_digest": v1.digest(),
                "approval_status": "SUPERSEDED_BY_REVISION",
            },
            "v2": {
                "plan_id": v2.plan_id,
                "runner_revision": v2.runner_revision,
                "plan_digest": v2.digest(),
                "approval_status": "PENDING_OWNER_APPROVAL",
            },
        },
        "supersession": supersession_record(),
        "authorization_template": v2_authorization_template(),
        "budget_profile": {
            "digest": v2.budget_profile_digest,
            "multi_worker_max_turns": CANDIDATE_PROFILE.multi_worker_max_turns,
            "single_max_turns": CANDIDATE_PROFILE.single_max_turns,
            "cell_token_budget": CANDIDATE_PROFILE.cell_token_budget,
        },
        "preregistration_digest": v2.preregistration_digest,
        "contract_problems": check_revision(),
        "model_calls": 0,
        "sandbox_runs": 0,
    }


def write_payload(path: Path) -> dict[str, Any]:
    payload = revision_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write the S08 canary revision record (offline)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    payload = write_payload(Path(args.out))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "v1_digest": payload["revision"]["v1"]["plan_digest"],
                "v2_digest": payload["revision"]["v2"]["plan_digest"],
                "contract_problems": payload["contract_problems"],
                "model_calls": 0,
                "out": str(args.out),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not payload["contract_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
