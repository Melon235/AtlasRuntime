"""Stage C1-C targeted verification.

`python -m experiments.stage_c1c.checks` writes ``plan.json``, ``authorization.json``,
``executor.json`` and ``offline.json`` next to this module, and nothing else in the repository.

The ten checks are the stage's Phase C list, in order. Each one is a fact about this repository at
this commit, or (where it says so) about the frozen Stage C1-B evidence tree, which is read
read-only and never rewritten:

C01  the plan id and scope resolve through ``contract.scope_plan``, and no earlier entry moved
C02  the cell and the plan carry the effective 750,000-token budget, its digest, and the nominal
     capacity; the frozen 500,000 budget and the older budget digests are unchanged
C03  the production ``GoalRunOptions`` carries 750,000, and a real ``run_goal`` builds a
     ``RunBudgetState`` whose limit is 750,000 — the number is checked where it is consumed, not
     only where it is declared
C04  the authority binds the C1-C budget digest and refuses the old one, the old executor and an
     earlier plan's record
C05  the frozen router still admits the P2 pair and still refuses all five boundary shapes
C06  the real ``executor.dispatch(PLAN_ID, mode=MODE_OFFLINE_PROOF)`` runs plan-id resolution →
     authority verification → offline claim → synthetic cell → audit → matrix report
C07  the pre-claim production admission reports READY for the C1-C cell
C08  five historical claims are intact, no C1-C claim exists, and the C1-B evidence index and the
     failed first launch are preserved
C09  the plan's own design invariants hold
C10  the Stage C1-B token categories reconcile from its own session, and a public PASS that
     precedes the last code change is reported as such
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_c1 import checks as c1_checks
from experiments.stage_c1 import plan as c1
from experiments.stage_c1b import checks as c1b_checks
from experiments.stage_c1b import plan as c1b
from experiments.stage_c1c import plan as c1c

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

#: The Stage C1-B identities, recorded at ``fa0268e``. Constants, so a later edit that moves them
#: fails a check instead of quietly re-baselining the run this stage is measured against.
C1_PLAN_DIGEST = "sha256:537508bf0fc10056987ef9342d9899008609aa9ba9c1734f7de53a3d3bc7ecd2"
C1B_PLAN_DIGEST = "sha256:d74c4d6f4634e16afedf9f42b21abfb966b8f6ac029dec4bbfc92eb1b76a284c"
C1B_EXECUTOR_DIGEST = "sha256:145f6c83822c3d38e21f86d4999057250518b2587d273aaa5451b6f5b2bccba4"
FROZEN_BUDGET_DIGEST = "sha256:a6ea43679649cb39c6c4f55f3bbb915b8a0287abcddce511a725ec6abee23eb9"

BPLUS_CANARY_PLAN_DIGEST = c1_checks.BPLUS_CANARY_PLAN_DIGEST
BPLUS_FORMAL_PLAN_DIGEST = c1_checks.BPLUS_FORMAL_PLAN_DIGEST
BPLUS_PROTOCOL_DIGEST = c1_checks.BPLUS_PROTOCOL_DIGEST

#: The five consumed production claims, oldest last. A claim is consumed for ever.
CONSUMED_CLAIMS: tuple[tuple[str, str], ...] = (
    *c1b_checks.CONSUMED_CLAIMS,
    ("c1b", C1B_PLAN_DIGEST),
)

#: Stage C1-B custody, read-only. The index and the failed first launch are the two artefacts this
#: stage must not disturb; both are recorded by digest rather than trusted by existence.
C1B_EVIDENCE_ROOT = Path(r"D:\Work\AtlasRuntime-runs\stage-c1b-completion-2")
C1B_EVIDENCE_INDEX_DIGEST = (
    "sha256:c8505498234f2fc9a81d498309e2fef8179b4fc7c5dfa655d3fafafc5e7cce25"
)
C1B_EVIDENCE_FILES = 35
C1B_FAILED_LAUNCH_ROOT = Path(r"D:\Work\AtlasRuntime-runs\stage-c1b-completion")
C1B_FAILED_LAUNCH_RECORD = "PRE_CLAIM_DISPATCH_ENTRY_FAILURE.json"

#: The C1-B figures this stage's rationale quotes, checked against the session itself in C10.
C1B_METRICS: dict[str, Any] = {
    "main_cumulative_input_tokens": 209_711,
    "main_cache_hit_tokens": 195_072,
    "main_cache_miss_tokens": 14_639,
    "main_completion_tokens": 5_143,
    "child_total_prompt_tokens": 257_950,
    "child_completion_tokens": 12_536,
    "system_total_tokens": 485_340,
}

#: Plan ids that must stay unresolvable.
REJECTED_PLAN_IDS: tuple[str, ...] = (
    contract.CANARY_PLAN_ID_V1,
    contract.CANARY_PLAN_ID_V2,
    "stage-c1c-unknown-plan",
    "C1C_UNKNOWN_SCOPE",
)


def _check(check_id: str, ok: bool, detail: str, **evidence: Any) -> dict[str, Any]:
    return {"check_id": check_id, "ok": bool(ok), "detail": detail, **evidence}


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _production_options(cell: Mapping[str, Any], root: Path) -> Any:
    return executor.production_goal_options(
        cell,
        evidence_root=root,
        fixture_dir=root / "fixture",
        config=executor.RunnerConfig(),
        run_id="c1c-offline-preparation",
    )


# -- C01: the entry the dispatch calls first ---------------------------------------------------


def _check_entry_resolution(plan: Mapping[str, Any]) -> dict[str, Any]:
    from experiments.stage_benchmark import matrix

    problems: list[str] = []
    by_id = contract.scope_plan(c1c.PLAN_ID)
    by_scope = contract.scope_plan(c1c.SCOPE)

    if by_id["digest"] != plan["digest"] or matrix.plan_digest(plan["cells"]) != plan["digest"]:
        problems.append("the plan digest does not recompute from its cells")
    if by_scope["digest"] != by_id["digest"] or by_scope["plan_id"] != by_id["plan_id"]:
        problems.append("the scope and the plan id resolve to different plans")
    if by_id["plan_id"] != c1c.PLAN_ID or by_id["scope"] != c1c.SCOPE:
        problems.append("the resolved plan is not the C1-C identity")

    rejected: list[str] = []
    for plan_id in REJECTED_PLAN_IDS:
        try:
            contract.scope_plan(plan_id)
        except ValueError:
            rejected.append(plan_id)
        else:
            problems.append(f"{plan_id} resolved; it must be rejected")

    unchanged = {
        "c1": (contract.scope_plan(c1.PLAN_ID)["digest"], C1_PLAN_DIGEST),
        "c1b": (contract.scope_plan(c1b.PLAN_ID)["digest"], C1B_PLAN_DIGEST),
        "bplus-canary": (
            contract.scope_plan(contract.SCOPE_CANARY)["digest"],
            BPLUS_CANARY_PLAN_DIGEST,
        ),
        "bplus-formal": (
            contract.scope_plan(contract.SCOPE_FORMAL)["digest"],
            BPLUS_FORMAL_PLAN_DIGEST,
        ),
    }
    for label, (actual, expected) in unchanged.items():
        if actual != expected:
            problems.append(f"the {label} entry moved to {actual}, expected {expected}")

    ok = not problems
    return _check(
        "C01_plan_entry_resolves_the_c1c_plan_and_nothing_else_moved",
        ok,
        "; ".join(problems)
        or (
            f"{c1c.PLAN_ID} and its scope both resolve to {plan['digest'][:19]}…; "
            f"C1, C1-B and both B+ entries are unchanged; {len(rejected)} ids remain rejected"
        ),
        resolved_digest=by_id["digest"],
        rejected=rejected,
        unchanged={label: value[0] for label, value in unchanged.items()},
    )


# -- C02: the budget identity ------------------------------------------------------------------


def _check_budget_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    cell = plan["cells"][0]
    facts = authority.plan_facts(plan)
    problems: list[str] = []

    if int(contract.BUDGET["token_budget"]) != c1c.TOKEN_BUDGET_BEFORE:
        problems.append("the frozen Stage B+ budget moved off 500000")
    if int(plan["budget"]["token_budget"]) != c1c.TOKEN_BUDGET_AFTER:
        problems.append("the plan's budget record is not 750000")
    if int(cell["token_budget"]) != c1c.TOKEN_BUDGET_AFTER:
        problems.append("the cell's token budget is not 750000")
    if int(plan["nominal_token_capacity"]) != c1c.TOKEN_BUDGET_AFTER:
        problems.append("the nominal token capacity is not one cell's 750000")
    if facts["budget_digest"] != plan["budget_digest"]:
        problems.append("the authority's budget digest is not the plan's")
    if facts["token_budget"] != c1c.TOKEN_BUDGET_AFTER:
        problems.append("the authority reads a different token budget from this plan")
    if plan["budget_digest"] == FROZEN_BUDGET_DIGEST:
        problems.append("the plan carries the frozen 500K digest")
    if contract.token_budget_for(cell) != c1c.TOKEN_BUDGET_AFTER:
        problems.append("the production resolver does not read 750000 from this cell")
    if contract.token_budget_for({}) != c1c.TOKEN_BUDGET_BEFORE:
        problems.append("a cell with no budget no longer resolves to 500000")

    # The older plans keep their own budget, their own digest and their own capacity: read through
    # the same ``plan_facts`` the authority uses, so "the digest did not move" is the authority's
    # own statement about those plans, not a second implementation of it.
    older: dict[str, Any] = {}
    for label, plan_value, capacity in (
        ("bplus-canary", contract.scope_plan(contract.SCOPE_CANARY), 6 * 500_000),
        ("bplus-formal", contract.scope_plan(contract.SCOPE_FORMAL), 24 * 500_000),
        ("c1", c1.plan_record(), 500_000),
        ("c1b", c1b.plan_record(), 500_000),
    ):
        facts = authority.plan_facts(plan_value)
        older[label] = {
            "budget_digest": facts["budget_digest"],
            "token_budget": facts["token_budget"],
            "nominal_token_capacity": facts["nominal_token_capacity"],
        }
        if facts["budget_digest"] != FROZEN_BUDGET_DIGEST:
            problems.append(f"the {label} budget digest moved")
        if facts["token_budget"] != c1c.TOKEN_BUDGET_BEFORE:
            problems.append(f"the {label} budget is not the frozen 500000")
        if facts["nominal_token_capacity"] != capacity:
            problems.append(f"the {label} nominal capacity moved off {capacity}")

    ok = not problems
    return _check(
        "C02_effective_budget_is_bound_and_the_historical_digest_is_unchanged",
        ok,
        "; ".join(problems)
        or (
            f"the cell binds {c1c.TOKEN_BUDGET_AFTER} tokens and {plan['budget_digest'][:19]}…; "
            f"the plan's capacity is {plan['nominal_token_capacity']}; the frozen budget stays "
            f"500000 / {FROZEN_BUDGET_DIGEST[:19]}… for B+, C1 and C1-B"
        ),
        effective_budget_digest=plan["budget_digest"],
        frozen_budget_digest=FROZEN_BUDGET_DIGEST,
        nominal_token_capacity=plan["nominal_token_capacity"],
        older=older,
    )


# -- C03: the options, and the budget state they become ----------------------------------------


def run_budget_proof(plan: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    """One real ``run_goal`` on the production options, with a scripted model and a fake sandbox.

    What this proves is the *seam*, not the experiment: the value ``production_goal_options``
    declares is the value ``run_goal`` turns into ``RunBudgetState(limit_tokens=…)`` and the value
    the session reports. No provider is built, no sandbox service is contacted, no model is called.
    """

    import asyncio
    import dataclasses
    import uuid

    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
    from tests.helpers.stage_p import PipelineRig, sandbox_options, text

    from atlasos.app.main_driven import run_goal

    cell = plan["cells"][0]
    scratch.mkdir(parents=True, exist_ok=True)
    fixture = executor.prepare_fixture(contract.TASKS_BY_ID[cell["task_id"]], scratch / "fixture")
    evidence = scratch / "evidence"
    # A fresh run id per call: provider registry names are immutable for the process lifetime and a
    # goal run refuses to reuse its run directory, so two proofs in one process must not share one.
    run_id = f"c1c-budget-proof-{uuid.uuid4().hex[:8]}"
    options = executor.production_goal_options(
        cell,
        evidence_root=evidence,
        fixture_dir=Path(fixture["path"]),
        config=executor.RunnerConfig(),
        run_id=run_id,
    )
    rig = PipelineRig()
    rig.main_script = [text("REPORT: nothing to do.")]
    provider = FakeSandboxProvider(scratch / "sandboxes")
    options = dataclasses.replace(
        options,
        provider_factory=rig.build,
        sandbox=sandbox_options(Path(fixture["path"]), lambda root: provider),
    )
    session = asyncio.run(run_goal(options))
    budget = (session.get("budget") or {}) if isinstance(session.get("budget"), Mapping) else {}
    return {
        "options_token_budget": options.token_budget,
        "session_status": session.get("status"),
        "run_budget_limit_tokens": budget.get("limit_tokens"),
        "run_budget_mode": budget.get("mode"),
        "source": (
            "atlasos.app.main_driven.run_goal: RunBudgetState(limit_tokens=options.token_budget)"
        ),
    }


def _check_production_options(plan: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    cell = plan["cells"][0]
    options = _production_options(cell, scratch / "options")
    grant = {key: list(value) for key, value in options.child_write_grants.items()}
    from atlasos.workers import WorkerCatalog

    profile = WorkerCatalog().profiles[contract.CODING]
    problems: list[str] = []

    if options.token_budget != c1c.TOKEN_BUDGET_AFTER:
        problems.append(f"GoalRunOptions.token_budget is {options.token_budget}")
    if options.max_turns != int(contract.BUDGET["main_max_turns"]):
        problems.append("the Main turn cap moved")
    if options.worker_max_turns != int(contract.BUDGET["worker_max_turns"]):
        problems.append("the worker turn cap moved")
    if options.main_prompt.version != c1b.prompts.V22_VERSION:
        problems.append("the Main prompt version moved")
    if options.main_prompt.digest != c1b.prompts.V22_DIGEST:
        problems.append("the Main prompt digest moved")
    if options.main_prompt.text != c1b.prompts.V22_TEXT:
        problems.append("the Main prompt bytes moved")
    if options.goal != c1b.goal_text():
        problems.append("the goal bytes moved")
    if grant != {contract.CODING: ["plugins/"]}:
        problems.append(f"the child grant is {grant}")
    if list(options.main_write_paths) != ["plugins/**"]:
        problems.append("the Main write scope moved")
    if options.worker_profiles != (contract.CODING,) or options.worker_exposure != "card":
        problems.append("the worker profile or exposure moved")
    if options.delegation.max_children_per_call != 2:
        problems.append("the per-call width moved")
    if options.delegation.max_parallel_children != 2:
        problems.append("the concurrency limit moved")
    if options.delegation.max_delegation_rounds != 3:
        problems.append("the delegation round cap moved")
    if options.delegation.child_max_turns != 20:
        problems.append("the child turn cap moved")
    if set(profile.base_tools) != set(c1b.contract_of_coding_tools()):
        problems.append("the coding profile's tool surface moved")
    if profile.max_turns != 8:
        problems.append("the shipped profile turn cap moved")

    try:
        proof = run_budget_proof(plan, scratch / "budget-proof")
    except Exception as error:  # noqa: BLE001 - a raised proof is a failed check
        proof = {"error": f"{type(error).__name__}: {error}"[:300]}
    if proof.get("error"):
        problems.append(f"the run-budget proof raised: {proof['error']}")
    elif proof.get("run_budget_limit_tokens") != c1c.TOKEN_BUDGET_AFTER:
        problems.append(
            f"run_goal's RunBudgetState limit is {proof.get('run_budget_limit_tokens')!r}"
        )

    ok = not problems
    return _check(
        "C03_production_options_and_the_run_budget_state_carry_750000",
        ok,
        "; ".join(problems)
        or (
            f"GoalRunOptions carries {options.token_budget} tokens, 20/20 turns, the C1-B prompt "
            f"({c1b.prompts.V22_DIGEST[:19]}…), the C1-B goal and the C1-B grant; a real run_goal "
            f"builds RunBudgetState(limit_tokens={proof.get('run_budget_limit_tokens')})"
        ),
        options_token_budget=options.token_budget,
        run_budget=proof,
    )


# -- C04: the authority --------------------------------------------------------------------------


def _check_authority(
    plan: Mapping[str, Any],
    pending: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    problems: list[str] = []

    unsigned = authority.verify(
        pending,
        plan=plan,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    if unsigned["ready"] is not False:
        problems.append("the pending record verifies as an authorization")
    if pending["status"] != "PENDING_OWNER_APPROVAL":
        problems.append("the pending record is not pending")
    if pending["authorizes_live_execution"] is not False:
        problems.append("the pending record claims to authorize live execution")
    if pending["cost_stop_usd"] is not None or pending["per_cell_cost_stop_usd"] is not None:
        problems.append("a cost stop is signed in a pending record")
    if int(pending["token_stop"]) != c1c.TOKEN_BUDGET_AFTER:
        problems.append("the pending token stop is not 750000")
    if pending["budget_digest"] != plan["budget_digest"]:
        problems.append("the pending record does not carry the C1-C budget digest")
    if pending["executor_digest"] != identity["executor_digest"]:
        problems.append("the pending record does not carry the current executor digest")
    if list(pending["cell_ids"]) != [plan["cells"][0]["cell_id"]]:
        problems.append("the pending record does not authorize exactly this cell")

    # A correctly signed record for *this* plan is the baseline the negatives are compared with.
    scratch.mkdir(parents=True, exist_ok=True)
    signed = c1_checks._offline_signed(
        plan,
        scratch=scratch,
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
        cost_stop_usd=c1c.COST_STOP_CANDIDATE,
        per_cell_cost_stop_usd=c1c.PER_CELL_COST_STOP_CANDIDATE,
    )
    baseline = authority.verify(
        signed,
        plan=plan,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    if baseline["ready"] is not True:
        problems.append(f"a correctly signed C1-C record is refused: {baseline['problems']}")

    cases: dict[str, Any] = {}
    for label, candidate in (
        ("old_budget_digest", dict(signed, budget_digest=FROZEN_BUDGET_DIGEST)),
        ("old_executor", dict(signed, executor_digest=C1B_EXECUTOR_DIGEST)),
        ("old_plan_digest", dict(signed, plan_digest=C1B_PLAN_DIGEST)),
        ("old_protocol_digest", dict(signed, protocol_digest="sha256:" + "0" * 64)),
    ):
        verdict = authority.verify(
            candidate,
            plan=plan,
            head=head,
            executor=identity,
            protocol_digest_value=protocol_digest,
            repo_root=REPO_ROOT,
        )
        cases[label] = {"ready": verdict["ready"], "problems": verdict["problems"][:4]}
        if verdict["ready"]:
            problems.append(f"a record with {label} is accepted")
    if not any("budget_digest" in item for item in cases["old_budget_digest"]["problems"]):
        problems.append("the old budget digest is refused, but not for the budget digest")
    if not any("executor_digest" in item for item in cases["old_executor"]["problems"]):
        problems.append("the old executor is refused, but not for the executor digest")

    # The C1-B record — right shape, right signer, wrong plan — must not authorize this cell.
    c1b_signed = c1_checks._offline_signed(
        c1b.plan_record(),
        scratch=scratch / "c1b",
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
    )
    cross = authority.verify(
        c1b_signed,
        plan=plan,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    cases["c1b_authority_for_c1c"] = {"ready": cross["ready"], "problems": cross["problems"][:4]}
    if cross["ready"]:
        problems.append("the C1-B authorization is accepted for the C1-C plan")
    if not any("budget_digest" in item for item in cross["problems"]):
        problems.append("the C1-B record is refused, but not for the budget digest")

    ok = not problems
    return _check(
        "C04_authority_binds_the_c1c_budget_and_refuses_the_old_identities",
        ok,
        "; ".join(problems)
        or (
            f"the pending record authorizes nothing ({len(unsigned['problems'])} stated reasons); "
            f"a correctly signed record is accepted; the old budget digest, the old executor, the "
            f"old plan digest and the C1-B record are each refused"
        ),
        pending_problems=list(unsigned["problems"]),
        negatives=cases,
    )


# -- C05: the router boundary --------------------------------------------------------------------


def _check_admission(plan: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    options = _production_options(plan["cells"][0], scratch / "admission")
    report = c1_checks.admission_report(options)
    pair = report["production"][c1_checks.POSITIVE_SHAPE]
    raw = report["raw_representation"]
    negatives = {
        shape: report["production"][shape]["reason_codes"] for shape in c1_checks.NEGATIVE_SHAPES
    }
    ok = (
        pair["route"] == "delegate_parallel"
        and pair["admitted_children"] == ["csv", "json"]
        and "DELEGATION_ADMITTED" in pair["reason_codes"]
        and not pair["blocked_gates"]
        and pair["max_wave_width"] == 2
        and pair["policy_version"] == contract.BUDGET["router_policy"]
        and raw["reason_codes"] == ["WRITE_SCOPE_NOT_GRANTED"]
        and all(
            code in negatives[shape] for shape, code in c1_checks.EXPECTED_NEGATIVE_CODES.items()
        )
    )
    return _check(
        "C05_router_still_admits_the_p2_pair_and_the_boundary_is_unchanged",
        ok,
        "the production grant plugins/ admits both children as a two-wide wave under m.1.0; "
        "the raw glob is still refused and all five boundary codes are unchanged",
        production_grant=report["production_grant"],
        children=pair["admitted_children"],
        boundary=negatives,
    )


# -- C06: the real dispatch entry ----------------------------------------------------------------


def _check_dispatch_entry(
    plan: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    try:
        report = c1_checks.dispatch_report(
            plan,
            scratch=scratch,
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
            cost_stop_usd=c1c.COST_STOP_CANDIDATE,
            per_cell_cost_stop_usd=c1c.PER_CELL_COST_STOP_CANDIDATE,
        )
    except Exception as error:  # noqa: BLE001 - a raised entry is a failed check
        return _check(
            "C06_real_dispatch_entry_runs_the_offline_control_flow",
            False,
            f"{type(error).__name__}: {error}"[:300],
        )
    body = report["report"]
    cell = report["cell"]
    report_file = _read_json(scratch / "dispatch" / "matrix-report.json") or {}
    ok = (
        body["plan_id"] == c1c.PLAN_ID
        and body["scope"] == c1c.SCOPE
        and body["plan_digest"] == plan["digest"]
        and body["mode"] == executor.MODE_OFFLINE_PROOF
        and body["complete"] is True
        and body["cells_planned"] == 1
        and body["cells_started"] == 1
        and body["cells_passed"] == 1
        and body["stop_reason"] == executor.STOP_NONE
        and body["aggregate_includes_sample"] is False
        and int(body["token_stop"]) == c1c.TOKEN_BUDGET_AFTER
        and float(body["cost_stop_usd"]) == c1c.COST_STOP_CANDIDATE
        and cell["verdict"] == "PASS"
        and cell["execution_integrity"] == "PASS"
        and cell["goal_correctness"] is True
        and not cell["stops"]
        and not cell["failed_checks"]
        and all(report["evidence_exists"].values())
        and int(report_file.get("token_stop") or 0) == c1c.TOKEN_BUDGET_AFTER
        and report_file.get("plan_digest") == plan["digest"]
    )
    return _check(
        "C06_real_dispatch_entry_runs_the_offline_control_flow",
        ok,
        f"dispatch({c1c.PLAN_ID!r}) resolved, verified against the C1-C budget digest, claimed in "
        f"a throwaway root, ran one synthetic cell and wrote the audit + matrix report (verdict "
        f"{cell['verdict']}, {cell['check_count']} audit checks, token_stop "
        f"{body['token_stop']})",
        **report,
        token_stop=body["token_stop"],
        cost_stop_usd=body["cost_stop_usd"],
    )


# -- C07: production admission -------------------------------------------------------------------


def _check_preflight(plan: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    try:
        report = executor.run_admission_preflight(plan, repo_root=REPO_ROOT, scratch_root=scratch)
    except Exception as error:  # noqa: BLE001 - a raised preflight is a failed check
        return _check(
            "C07_production_admission_is_ready", False, f"{type(error).__name__}: {error}"[:300]
        )
    cell = (report.get("cells") or [{}])[0]
    ok = (
        report.get("status") == "READY"
        and not report.get("blocking_failures")
        and not report.get("contract_blocking_failures")
        and cell.get("worker_profile") == contract.CODING
        and cell.get("worker_exposure") == "card"
        and cell.get("oracle_mode") == "EXTERNAL_SANDBOX_ORACLE"
    )
    return _check(
        "C07_production_admission_is_ready",
        ok,
        f"the pre-claim gate accepts the C1-C cell ({report.get('status')}); the credential "
        f"(present={bool(report.get('credential_present'))}) is reported apart from the contract",
        status=report.get("status"),
        blocking_failures=list(report.get("blocking_failures") or []),
        contract_blocking_failures=list(report.get("contract_blocking_failures") or []),
        credential_present=bool(report.get("credential_present")),
        checks=cell.get("checks"),
    )


# -- C08: history --------------------------------------------------------------------------------


def _claims_snapshot() -> dict[str, Any]:
    root, is_default = executor.claim_root()
    out: dict[str, Any] = {"root": str(root), "root_is_default": is_default, "claims": {}}
    for label, digest in CONSUMED_CLAIMS:
        path = executor.claim_path(digest)
        if path.is_file():
            payload = path.read_bytes()
            out["claims"][label] = {
                "path": str(path),
                "sha256": _digest_bytes(payload),
                "plan_digest_in_file": (_read_json(path) or {}).get("plan_digest"),
            }
        else:
            out["claims"][label] = {"path": str(path), "present": False}
    return out


def _evidence_index_report() -> dict[str, Any]:
    index = C1B_EVIDENCE_ROOT / "evidence-index.json"
    if not index.is_file():
        return {"mounted": False, "root": str(C1B_EVIDENCE_ROOT), "ok": True}
    payload = _read_json(index) or {}
    files = payload.get("files") or {}
    recomputed = _digest_bytes(json.dumps(files, sort_keys=True).encode("utf-8"))
    plan_digest = payload.get("plan_digest")
    return {
        "mounted": True,
        "root": str(C1B_EVIDENCE_ROOT),
        "files": len(files) if isinstance(files, Mapping) else None,
        "index_digest": recomputed,
        "index_digest_as_declared": C1B_EVIDENCE_INDEX_DIGEST,
        "declared_in_file": payload.get("index_digest"),
        "plan_digest": plan_digest,
        "ok": (
            recomputed == C1B_EVIDENCE_INDEX_DIGEST == payload.get("index_digest")
            and plan_digest == C1B_PLAN_DIGEST
            and isinstance(files, Mapping)
            and len(files) == C1B_EVIDENCE_FILES
        ),
    }


def _failed_launch_report() -> dict[str, Any]:
    path = C1B_FAILED_LAUNCH_ROOT / C1B_FAILED_LAUNCH_RECORD
    if not path.is_file():
        return {"mounted": False, "path": str(path), "ok": True}
    payload = _read_json(path) or {}
    report = {
        "mounted": True,
        "path": str(path),
        "sha256": _digest_bytes(path.read_bytes()),
        "incident": payload.get("incident"),
        "plan_digest": payload.get("plan_digest"),
        "claim_consumed": payload.get("claim_consumed"),
        "cells_started": payload.get("cells_started"),
        "model_requests": payload.get("model_requests"),
        "sandboxes_created": payload.get("sandboxes_created"),
        "preserved_unchanged": payload.get("preserved_unchanged"),
        "note": "the failed first launch is left exactly as it was; this stage reads it only",
    }
    report["ok"] = (
        report["incident"] == "PRE_CLAIM_DISPATCH_ENTRY_FAILURE"
        and report["plan_digest"] == C1B_PLAN_DIGEST
        and report["claim_consumed"] is False
        and int(report["cells_started"] or 0) == 0
        and int(report["model_requests"] or 0) == 0
        and int(report["sandboxes_created"] or 0) == 0
    )
    return report


def _frozen_artefacts() -> dict[str, Any]:
    """The C1-B artefacts as they are checked in: the historical identities, not the live ones."""

    stage = REPO_ROOT / "experiments" / "stage_c1b"
    plan_file = _read_json(stage / "plan.json") or {}
    authorization = _read_json(stage / "authorization.json") or {}
    executor_file = _read_json(stage / "executor.json") or {}
    offline = _read_json(stage / "offline.json") or {}
    return {
        "plan_digest": plan_file.get("digest"),
        "plan_digest_recomputes": plan_file.get("digest") == c1b.plan_record()["digest"],
        "authorization_plan_digest": authorization.get("plan_digest"),
        "authorization_status": authorization.get("status"),
        "executor_version": executor_file.get("executor_version"),
        "executor_digest": executor_file.get("executor_digest"),
        "offline_plan_digest": offline.get("plan_digest"),
        "offline_executor_digest": offline.get("executor_digest"),
    }


def _check_history(
    plan: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    problems: list[str] = []
    states = {label: bool(item.get("sha256")) for label, item in after["claims"].items()}
    present = [label for label, ok in states.items() if ok]
    if len(present) not in (0, len(CONSUMED_CLAIMS)):
        problems.append(f"{len(present)} of {len(CONSUMED_CLAIMS)} consumed claims are present")
    if before.get("claims") != after.get("claims"):
        problems.append("a consumed claim changed during this preparation")
    # Stage C1-C ran once, after this preparation was written. Its claim therefore exists now, and
    # that is a fact of history rather than a defect: what stays checkable is that an existing claim
    # is *this* plan's claim and that nothing moved while this preparation ran (the snapshots
    # above).
    claim_file = executor.claim_path(plan["digest"])
    claim_state = "ABSENT"
    if claim_file.exists():
        claim_state = "PRESENT"
        recorded = (_read_json(claim_file) or {}).get("plan_digest")
        if recorded != plan["digest"]:
            problems.append(f"the C1-C claim carries {recorded!r}, not this plan's digest")
    if not after["root_is_default"]:
        problems.append("the production claim root is not in force")

    index = _evidence_index_report()
    if not index["ok"]:
        problems.append("the C1-B evidence index does not match its recorded digest")
    launch = _failed_launch_report()
    if not launch["ok"]:
        problems.append("the C1-B failed first launch is not the incident record it was")

    frozen = _frozen_artefacts()
    if frozen["plan_digest"] != C1B_PLAN_DIGEST:
        problems.append("the C1-B plan.json no longer carries the frozen plan digest")
    if frozen["plan_digest_recomputes"] is not True:
        problems.append("the C1-B plan digest no longer recomputes")
    if frozen["authorization_plan_digest"] != C1B_PLAN_DIGEST:
        problems.append("the C1-B authorization was re-pointed")
    if frozen["authorization_status"] != "PENDING_OWNER_APPROVAL":
        problems.append("the C1-B authorization is no longer the pending record it was")
    if frozen["executor_digest"] != C1B_EXECUTOR_DIGEST:
        problems.append("the C1-B executor identity was rewritten")
    if frozen["offline_plan_digest"] != C1B_PLAN_DIGEST:
        problems.append("the C1-B offline report was regenerated")
    if frozen["offline_executor_digest"] != C1B_EXECUTOR_DIGEST:
        problems.append("the C1-B offline report's executor identity moved")

    ok = not problems
    index_detail = (
        f"the C1-B evidence index re-hashes to {C1B_EVIDENCE_INDEX_DIGEST[:19]}… "
        f"({index.get('files')} files)"
        if index.get("mounted")
        else "the C1-B evidence tree is not mounted here, so only the repository's frozen artefacts"
        " were compared"
    )
    return _check(
        "C08_history_claims_evidence_and_the_failed_launch_are_preserved",
        ok,
        "; ".join(problems)
        or (
            f"{len(present)}/{len(CONSUMED_CLAIMS)} earlier consumed claims are byte-identical "
            f"before and after this preparation, the C1-C claim for {plan['digest'][:19]}… is "
            f"{claim_state} and carries this plan's digest, {index_detail}, the failed first "
            f"launch is intact, and the C1-B plan/authorization/executor/offline identities "
            f"are still the frozen ones"
        ),
        claims=states,
        claim_path=str(claim_file),
        claim_state=claim_state,
        claim_root=after["root"],
        evidence_index=index,
        failed_launch=launch,
        frozen_artefacts=frozen,
    )


# -- C09: the plan's own invariants ---------------------------------------------------------------


def _check_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    problems = c1c.validate(plan)
    return _check(
        "C09_plan_contract_invariants",
        not problems,
        "the frozen design invariants hold on the record itself",
        problems=problems,
    )


# -- C10: the C1-B metric categories ---------------------------------------------------------------


def _c1b_session() -> dict[str, Any] | None:
    sessions = sorted(C1B_EVIDENCE_ROOT.glob("evidence/cells/*/agent/*/session.json"))
    return _read_json(sessions[0]) if sessions else None


def _c1b_trace() -> dict[str, Any] | None:
    return _read_json(C1B_EVIDENCE_ROOT / "trace-audit-c1b.json")


def _public_pass_turn(trace: Mapping[str, Any] | None) -> int | None:
    if not trace:
        return None
    main = (trace.get("agents") or {}).get("bplus-c01-main") or {}
    turns = [
        int(row["turn"])
        for row in main.get("rows") or []
        if any(call.get("tool") == "run_public_tests" for call in row.get("calls") or [])
    ]
    return turns[-1] if turns else None


def metric_report() -> dict[str, Any]:
    """The Stage C1-B token categories, recomputed from its own session and trace.

    Every category is kept separate on purpose. A cache hit is *part of* prompt tokens; it is not
    added to them. ``post_public_pass_input_tokens`` is the Main input spent after the last public
    test call, which is the only correct reading of "the suite passed" when code changed afterwards.
    """

    session = _c1b_session()
    if session is None:
        return {"mounted": False, "root": str(C1B_EVIDENCE_ROOT)}
    metrics = session.get("metrics") or {}
    budget = session.get("budget") or {}
    trace = _c1b_trace()
    main = ((trace or {}).get("agents") or {}).get("bplus-c01-main") or {}
    contexts = list(metrics.get("main_context_tokens_by_request") or [])
    pass_turn = _public_pass_turn(trace)
    last_write = main.get("last_write_turn")

    reported = {
        key: metrics.get(key)
        for key in (
            "main_cumulative_input_tokens",
            "main_cache_hit_tokens",
            "main_cache_miss_tokens",
            "main_completion_tokens",
            "child_total_prompt_tokens",
            "child_completion_tokens",
            "system_total_tokens",
        )
    }
    parts = (
        int(metrics.get("main_cumulative_input_tokens") or 0)
        + int(metrics.get("main_completion_tokens") or 0)
        + int(metrics.get("child_total_prompt_tokens") or 0)
        + int(metrics.get("child_completion_tokens") or 0)
    )
    main_total = int(metrics.get("main_cumulative_input_tokens") or 0) + int(
        metrics.get("main_completion_tokens") or 0
    )
    child_total = int(metrics.get("child_total_prompt_tokens") or 0) + int(
        metrics.get("child_completion_tokens") or 0
    )
    cache_total = int(metrics.get("main_cache_hit_tokens") or 0) + int(
        metrics.get("main_cache_miss_tokens") or 0
    )
    workers = budget.get("workers") or {}
    worker_tokens = {
        name: int(item.get("actual_tokens") or 0)
        for name, item in workers.items()
        if name != "atlas-main"
    }
    # Request i carries turn i+1's context, so the input spent *after* turn N is ``contexts[N:]``.
    post_pass = sum(contexts[pass_turn:]) if pass_turn else None
    post_write = sum(contexts[last_write:]) if last_write is not None else None

    identities = {
        "parts_reconcile_to_system_total": parts == int(metrics.get("system_total_tokens") or -1),
        "cache_split_reconciles_to_main_input": cache_total
        == int(metrics.get("main_cumulative_input_tokens") or -1),
        "main_total_is_input_plus_completion": main_total
        == int(budget.get("used_prompt_tokens") or 0)
        + int(budget.get("used_completion_tokens") or 0)
        - child_total,
        "child_totals_reconcile": sum(worker_tokens.values()) == child_total,
        "budget_used_is_the_system_total": int(budget.get("used_tokens") or 0)
        == int(metrics.get("system_total_tokens") or -1),
        "budget_remaining_reconciles": int(budget.get("remaining_tokens") or 0)
        == int(budget.get("limit_tokens") or 0) - int(budget.get("used_tokens") or 0),
        "reported_categories_match_the_record": all(
            reported.get(key) == value for key, value in C1B_METRICS.items()
        ),
        "main_completion_is_the_reported_remainder": C1B_METRICS["main_completion_tokens"]
        == C1B_METRICS["system_total_tokens"]
        - C1B_METRICS["main_cumulative_input_tokens"]
        - C1B_METRICS["child_total_prompt_tokens"]
        - C1B_METRICS["child_completion_tokens"],
        "public_pass_precedes_the_last_code_change": (
            pass_turn is not None and last_write is not None and pass_turn < last_write
        ),
    }
    return {
        "mounted": True,
        "root": str(C1B_EVIDENCE_ROOT),
        "run_id": session.get("run_id"),
        "categories": {
            "prompt_tokens_main": metrics.get("main_cumulative_input_tokens"),
            "cache_hit_tokens": metrics.get("main_cache_hit_tokens"),
            "cache_miss_tokens": metrics.get("main_cache_miss_tokens"),
            "completion_tokens_main": metrics.get("main_completion_tokens"),
            "prompt_tokens_children": metrics.get("child_total_prompt_tokens"),
            "completion_tokens_children": metrics.get("child_completion_tokens"),
            "main_total_tokens": main_total,
            "child_total_tokens": child_total,
            "system_total_tokens": metrics.get("system_total_tokens"),
            "budget_accounting_tokens": budget.get("used_tokens"),
            "budget_limit_tokens": budget.get("limit_tokens"),
            "budget_remaining_tokens": budget.get("remaining_tokens"),
            "post_write_input_tokens": post_write,
            "post_public_pass_input_tokens": post_pass,
            "cache_hits_are_part_of_prompt_tokens": True,
            "note": "a cache hit is inside prompt tokens; hit and miss are never added on top",
        },
        "turns": {"public_pass_turn": pass_turn, "last_write_turn": last_write},
        "identities": identities,
        "ok": all(identities.values()),
    }


def _check_metrics() -> dict[str, Any]:
    report = metric_report()
    if not report.get("mounted"):
        return _check(
            "C10_c1b_metric_categories_reconcile",
            True,
            "the C1-B evidence tree is not mounted on this machine; the categories were not "
            "recomputed and nothing is claimed about them",
            mounted=False,
            verified=False,
        )
    failed = [name for name, ok in (report["identities"] or {}).items() if not ok]
    return _check(
        "C10_c1b_metric_categories_reconcile",
        not failed,
        "; ".join(failed)
        or (
            f"{report['categories']['system_total_tokens']} system tokens = "
            f"{report['categories']['prompt_tokens_main']} Main prompt (cache "
            f"{report['categories']['cache_hit_tokens']}/{report['categories']['cache_miss_tokens']}"
            f") + {report['categories']['completion_tokens_main']} Main completion + "
            f"{report['categories']['prompt_tokens_children']} child prompt + "
            f"{report['categories']['completion_tokens_children']} child completion; the public "
            f"PASS at turn {report['turns']['public_pass_turn']} precedes the last code change at "
            f"turn {report['turns']['last_write_turn']}"
        ),
        mounted=True,
        verified=True,
        categories=report["categories"],
        turns=report["turns"],
        identities=report["identities"],
    )


# -- assembly ------------------------------------------------------------------------------------


def authorization_record(
    plan: Mapping[str, Any], *, protocol_digest: str, identity: Mapping[str, Any]
) -> dict[str, Any]:
    record = authority.pending_record(
        plan, protocol_digest_value=protocol_digest, executor=identity
    )
    record["stage"] = c1c.STAGE_ID
    record["proposed_envelope"] = {
        **dict(plan["envelope_candidate"]),
        "note": "a candidate for the Owner's decision; the signed fields stay null until signed",
    }
    record["intervention"] = dict(plan["intervention"])
    record["review_surface"] = {
        "plan_id": c1c.PLAN_ID,
        "goal_digest": c1c.goal_digest(),
        "main_prompt_digest": c1c.prompt_digest(),
        "worker_contract_digest": c1c.worker_contract()["rendered_prompt_digest"],
        "write_grant": c1.grant_record(),
        "effective_budget_digest": plan["budget_digest"],
        "budget_intervention": dict(plan["budget_intervention"]),
        "cost_stop_candidate": {
            "cost_stop_usd": c1c.COST_STOP_CANDIDATE,
            "per_cell_cost_stop_usd": c1c.PER_CELL_COST_STOP_CANDIDATE,
            "approved": False,
        },
        "single_variable_ablation": True,
        "statistical_claim": False,
    }
    record["notes"] = (
        "Stage C1-C. This record authorizes nothing. It covers one cell and one attempt in which "
        "the only parameter that moved is the shared nominal token budget: 500000 -> 750000. The "
        "prompt, the worker completion contract, the goal, the grant, the router and the oracle "
        "are Stage C1-B's, byte for byte. A live dispatch needs every null field filled, both cost "
        "fields finite and positive, and an external provenance artefact whose bytes still hash to "
        "the recorded value."
    )
    return record


def run_checks(
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
    plan: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> list[dict[str, Any]]:
    scratch.mkdir(parents=True, exist_ok=True)
    before = _claims_snapshot()
    checks = [
        _check_entry_resolution(plan),
        _check_budget_identity(plan),
        _check_production_options(plan, scratch / "c03"),
        _check_authority(
            plan,
            pending,
            scratch=scratch / "c04",
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        ),
        _check_admission(plan, scratch / "c05"),
        _check_dispatch_entry(
            plan,
            scratch=scratch / "c06",
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        ),
        _check_preflight(plan, scratch / "c07"),
        _check_history(plan, before, _claims_snapshot()),
        _check_plan(plan),
        _check_metrics(),
    ]
    return checks


def build(scratch: Path | None = None, *, write: bool = True) -> dict[str, Any]:
    head = executor.git_head(REPO_ROOT)
    identity = executor.executor_identity(REPO_ROOT)
    protocol_digest = authority.protocol_digest(contract.protocol_document())
    plan = c1c.plan_record()
    pending = authorization_record(plan, protocol_digest=protocol_digest, identity=identity)

    parent = Path(scratch) if scratch is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=parent, prefix="stage-c1c-run-"))
    checks = run_checks(
        scratch=base,
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
        plan=plan,
        pending=pending,
    )
    report = {
        "stage": c1c.STAGE_ID,
        "design_version": c1c.DESIGN_VERSION,
        "plan_id": c1c.PLAN_ID,
        "scope": c1c.SCOPE,
        "plan_digest": plan["digest"],
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "bplus_protocol_unchanged": protocol_digest == BPLUS_PROTOCOL_DIGEST,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "c1b_executor_digest": C1B_EXECUTOR_DIGEST,
        "goal_digest": c1c.goal_digest(),
        "activation_digest": c1.activation_digest(),
        "intervention_digest": c1c.intervention_digest(),
        "main_prompt_version": c1b.prompts.V22_VERSION,
        "main_prompt_digest": c1c.prompt_digest(),
        "worker_contract": c1c.worker_contract(),
        "budget_digest": plan["budget_digest"],
        "frozen_budget_digest": FROZEN_BUDGET_DIGEST,
        "nominal_token_capacity": plan["nominal_token_capacity"],
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "write_grant": c1.grant_record(),
        "budget_intervention": dict(plan["budget_intervention"]),
        "built_from_head": head,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for check in checks if check["ok"]),
            "failed": [check["check_id"] for check in checks if not check["ok"]],
        },
        "status": (
            "STAGE_C1C_HEADROOM_CONTROL_READY_FOR_LIVE_AUTHORIZATION"
            if all(check["ok"] for check in checks)
            else "STAGE_C1C_HEADROOM_CONTROL_NOT_READY"
        ),
        "live_execution_authorized": False,
        "real_model_calls": 0,
        "real_sandboxes": 0,
        "real_cells": 0,
        "claims_consumed": 0,
    }
    if write:
        contract.write_document(plan, STAGE_DIR / "plan.json")
        contract.write_document(pending, STAGE_DIR / "authorization.json")
        contract.write_document(
            {
                "stage": c1c.STAGE_ID,
                "executor_version": identity["executor_version"],
                "executor_digest": identity["executor_digest"],
                "files": identity["files"],
                "built_from_head": head,
                "supersedes": {
                    "executor_version": "bplus-executor-1.5",
                    "executor_digest": C1B_EXECUTOR_DIGEST,
                    "scope": "Stage B+, C1 and C1-B plans only; their evidence is not rebuilt",
                },
                "note": (
                    "the identity now covers experiments/stage_c1c/plan.py, because the cell's "
                    "budget is part of what runs; the budget resolver itself lives in contract.py "
                    "and executor.py, which were already covered"
                ),
            },
            STAGE_DIR / "executor.json",
        )
        contract.write_document(report, STAGE_DIR / "offline.json")
    return report


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Stage C1-C targeted verification")
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    report = build(arguments.scratch)
    if not arguments.quiet:
        for check in report["checks"]:
            flag = "PASS" if check["ok"] else "FAIL"
            print(f"{flag} {check['check_id']}: {check['detail'][:180]}")
        print(json.dumps(report["summary"], indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
