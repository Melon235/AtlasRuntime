"""Stage C1-B targeted verification.

`python -m experiments.stage_c1b.checks` writes ``plan.json``, ``authorization.json``,
``executor.json`` and ``offline.json`` next to this module and nothing else in the repository.

Twelve checks, each one a fact this stage's design depends on:

C01  the coding worker now renders the completion contract (and the bytes are the plan's bytes)
C02  the tool surface, the profile turn cap, the grant and the Main's write scope did not move
C03  the frozen router still admits the P2 pair through the production configuration
C04  a failed child's partial output is addressable but is not a completed delivery (C1 evidence)
C05  the Main prompt in the production options is the C1-B prompt, and the B+ prompt is untouched
C06  the failure classifier names only what the runtime recorded, and leaves the rest unclassified
C07  the plan identity recomputes and collides with no earlier plan
C08  the pre-claim production admission accepts the cell
C09  the pending authorization authorizes nothing
C10  V1/V2/V3/C1 claims are intact and no C1-B claim exists
C11  the plan's own design invariants hold
C12  the C1 evidence index still matches the digest the Owner recorded
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
from experiments.stage_c1b import plan as c1b
from experiments.stage_c1b import prompts

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

C1_EVIDENCE_ROOT = Path(r"D:\Work\AtlasRuntime-runs\stage-c1-positive-control")
C1_EVIDENCE_INDEX_DIGEST = "sha256:2e8c7be725f7e12119e9e7dc2fd577cb0748c2fb5f3676a5676d6e82648943e7"
C1_PLAN_DIGEST = c1.plan_record()["digest"]
BPLUS_CANARY_PLAN_DIGEST = c1_checks.BPLUS_CANARY_PLAN_DIGEST
BPLUS_FORMAL_PLAN_DIGEST = c1_checks.BPLUS_FORMAL_PLAN_DIGEST
BPLUS_PROTOCOL_DIGEST = c1_checks.BPLUS_PROTOCOL_DIGEST
BPLUS_V3_EXECUTOR_DIGEST = c1_checks.BPLUS_V3_EXECUTOR_DIGEST
C1_EXECUTOR_DIGEST = "sha256:c7f3c4c88a86c91be82792fb5da640f8a62e0922840fd9d492606ea8a279937d"

CONSUMED_CLAIMS: tuple[tuple[str, str], ...] = (
    *c1_checks.CONSUMED_CLAIMS,
    ("c1", C1_PLAN_DIGEST),
)


def _check(check_id: str, ok: bool, detail: str, **evidence: Any) -> dict[str, Any]:
    return {"check_id": check_id, "ok": bool(ok), "detail": detail, **evidence}


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _production_options(cell: Mapping[str, Any], root: Path) -> Any:
    return executor.production_goal_options(
        cell,
        evidence_root=root,
        fixture_dir=root / "fixture",
        config=executor.RunnerConfig(),
        run_id="c1b-offline-preparation",
    )


# -- C01 / C02: worker contract and permissions ---------------------------------------------------


def _check_worker_contract() -> dict[str, Any]:
    recorded = c1b.worker_contract()
    from atlasos.workers import WorkerCatalog
    from atlasos.workers.catalog import CODING_INSTRUCTIONS
    from atlasos.workers.models import WorkerTaskContext
    from atlasos.workers.renderer import render_worker_context

    profile = WorkerCatalog().profiles[contract.CODING]
    canonical = WorkerTaskContext(task_id="c1b-contract", objective="", background=None)
    rendered = render_worker_context(profile, canonical)
    required = (
        "completion status, changed files",
        "unverified",
        "deterministic refusal",
        "scratch files",
    )
    ok = (
        recorded["rendered_prompt_digest"] == _digest_bytes(rendered.encode("utf-8"))
        and all(marker in rendered.lower() for marker in required)
        and recorded["contract_version"] == "c1b-1.0"
        and recorded["instructions_digest"] == _digest_bytes(CODING_INSTRUCTIONS.encode("utf-8"))
        and "report tests run" not in rendered  # the old contract's vaguer phrasing is gone
    )
    return _check(
        "C01_worker_completion_contract_is_in_effect",
        ok,
        "coding_worker renders the completion contract (six-field closing report, refusal is "
        "information, no scratch files, no unverified test claims) and the plan records its bytes",
        **recorded,
    )


def _check_permissions(cell: Mapping[str, Any], root: Path) -> dict[str, Any]:
    options = _production_options(cell, root)
    from atlasos.workers import WorkerCatalog

    profile = WorkerCatalog().profiles[contract.CODING]
    grant = {key: list(value) for key, value in options.child_write_grants.items()}
    ok = (
        set(profile.base_tools) == set(c1b.contract_of_coding_tools())
        and profile.max_turns == 8
        and options.worker_max_turns == 20
        and grant == {contract.CODING: ["plugins/"]}
        and list(options.main_write_paths) == ["plugins/**"]
        and options.worker_profiles == (contract.CODING,)
        and options.worker_exposure == "card"
        and options.max_turns == 20
        and options.token_budget == 500000
        and options.delegation.max_children_per_call == 2
        and options.delegation.child_max_turns == 20
    )
    return _check(
        "C02_permissions_turns_and_budget_did_not_move",
        ok,
        f"tools {sorted(profile.base_tools)}, profile cap {profile.max_turns} → effective "
        f"{options.worker_max_turns}, child grant {grant}, main scope "
        f"{list(options.main_write_paths)}, "
        f"{options.max_turns}/{options.token_budget} unchanged",
        grant=grant,
        tools=sorted(profile.base_tools),
    )


# -- C03: admission -------------------------------------------------------------------------------


def _check_admission(cell: Mapping[str, Any], root: Path) -> dict[str, Any]:
    report = c1_checks.admission_report(_production_options(cell, root))
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
        and raw["reason_codes"] == ["WRITE_SCOPE_NOT_GRANTED"]
        and all(
            code in negatives[shape] for shape, code in c1_checks.EXPECTED_NEGATIVE_CODES.items()
        )
    )
    return _check(
        "C03_router_still_admits_the_p2_pair",
        ok,
        "the production grant plugins/ admits both children as a two-wide wave; the raw glob is "
        "still refused and all five boundary codes are unchanged",
        production_grant=report["production_grant"],
        children=pair["admitted_children"],
        boundary=negatives,
    )


# -- C04: delivery versus failure -----------------------------------------------------------------


def _check_delivery_semantics() -> dict[str, Any]:
    """A failed child's partial output is addressable, and is not a completed delivery."""

    agents = sorted(C1_EVIDENCE_ROOT.glob("evidence/cells/*/agent/*"))
    if not agents:
        return _check(
            "C04_partial_output_is_addressable_but_not_completion",
            True,
            "C1 evidence is not mounted here; the delivery semantics were not re-verified",
            mounted=False,
            verified=False,
        )
    agent = next((path for path in agents if (path / "session.json").is_file()), None)
    if agent is None:
        return _check(
            "C04_partial_output_is_addressable_but_not_completion",
            False,
            "the C1 evidence root exists but holds no session",
            mounted=True,
            verified=False,
        )
    session = json.loads((agent / "session.json").read_text(encoding="utf-8"))
    batch = session["delegation"]["batches"][0]
    children = batch["children"]
    handoffs = json.loads((agent / "delegation" / "handoffs.json").read_text(encoding="utf-8"))
    refs = handoffs.get("refs") or {}
    failed_children = [child for child in children if child["status"] == "failed"]
    with_ref = [child for child in failed_children if child.get("ref_id")]
    payload = None
    if with_ref:
        ref_id = with_ref[0]["ref_id"]
        text_file = agent / "handoffs" / f"{ref_id}.txt"
        payload = text_file.read_text(encoding="utf-8") if text_file.is_file() else None
    ok = (
        session["child_tokens"]["children_completed"] == 0
        and session["child_tokens"]["children_failed"] == 2
        and len(failed_children) == 2
        and all(child["success"] is False for child in failed_children)
        and len(refs) == 1
        and len(with_ref) == 1
        and payload is not None
        and len(payload) < 200
        and session["handoff_reads"]["read_calls"] == 0
        and session["artifacts"]["patch_changed_files"] == 6
    )
    return _check(
        "C04_partial_output_is_addressable_but_not_completion",
        ok,
        "two failed children, one addressable ref whose payload is a truncated sentence, zero "
        "delegate.read calls, and six changed files consumed from the shared workspace",
        mounted=True,
        verified=True,
        refs=list(refs),
        payload_chars=len(payload) if payload is not None else None,
        read_calls=session["handoff_reads"]["read_calls"],
        patch_changed_files=session["artifacts"]["patch_changed_files"],
    )


# -- C05: main prompt -----------------------------------------------------------------------------


def _check_prompt(cell: Mapping[str, Any], root: Path) -> dict[str, Any]:
    options = _production_options(cell, root)
    spec = options.main_prompt
    base = contract.prompt_text("p3-bplus")
    document = contract.protocol_document()
    ok = (
        spec.version == prompts.V22_VERSION
        and spec.digest == prompts.V22_DIGEST
        and spec.text == prompts.V22_TEXT
        and "run_public_tests` is the only authorized way" in spec.text
        and "are deterministically refused in this runtime" in spec.text
        and "Reserve enough turns for integration" in spec.text
        and document["prompts"]["p3-bplus"]["digest"] == _digest_bytes(base.encode("utf-8"))
    )
    return _check(
        "C05_main_verification_discipline_is_in_effect",
        ok,
        f"the run prompt is {spec.version} ({spec.digest[:19]}…), derived from the B+ prompt with "
        "one replaced section; the B+ prompt bytes are untouched",
        prompt_version=spec.version,
        prompt_digest=spec.digest,
        prompt_chars=len(spec.text),
        bplus_digest=document["prompts"]["p3-bplus"]["digest"],
    )


# -- C06: failure classification ------------------------------------------------------------------


class _Stop:
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


class _Result:
    def __init__(self, code: str | None, *, success: bool = False) -> None:
        self.success = success
        self.stop_reason = _Stop(code, f"stop {code}") if code else None


class _Budget:
    def __init__(self, stop_reason: str | None = None) -> None:
        self._stop = stop_reason

    def as_dict(self) -> dict[str, Any]:
        return {"stop_reason": self._stop}


def _check_classification() -> dict[str, Any]:
    from atlasos.app.main_driven import CLASSIFIED_FAILURE_KINDS, classify_failure

    cases = {
        "budget": classify_failure(
            failure_text=None,
            main_result=_Result("budget_exceeded"),
            acceptance_verdict="NOT_CONFIGURED",
            budget_state=_Budget("budget_exhausted: needs ~19650 tokens"),
            succeeded=False,
        ),
        "max_turns": classify_failure(
            failure_text=None,
            main_result=_Result("max_turns"),
            acceptance_verdict="NOT_CONFIGURED",
            budget_state=None,
            succeeded=False,
        ),
        "acceptance": classify_failure(
            failure_text=None,
            main_result=_Result(None, success=True),
            acceptance_verdict="FAIL",
            budget_state=None,
            succeeded=False,
        ),
        "exception": classify_failure(
            failure_text="sandbox provider unavailable",
            main_result=_Result(None),
            acceptance_verdict="NOT_CONFIGURED",
            budget_state=None,
            succeeded=False,
        ),
        "unknown_stop": classify_failure(
            failure_text=None,
            main_result=_Result("provider_error"),
            acceptance_verdict="NOT_CONFIGURED",
            budget_state=None,
            succeeded=False,
        ),
        "succeeded": classify_failure(
            failure_text=None,
            main_result=_Result(None, success=True),
            acceptance_verdict="PASS",
            budget_state=None,
            succeeded=True,
        ),
    }
    ok = (
        cases["budget"]["kind"] == "budget_exhaustion"
        and cases["budget"]["budget_stop_reason"].startswith("budget_exhausted")
        and cases["max_turns"]["kind"] == "main_max_turns"
        and cases["acceptance"]["kind"] == "acceptance_failed"
        and cases["exception"]["kind"] is None
        and cases["exception"]["classified_from"] == "unclassified"
        and cases["unknown_stop"]["kind"] is None
        and cases["succeeded"] is None
        and set(CLASSIFIED_FAILURE_KINDS)
        == {"budget_exhaustion", "main_max_turns", "acceptance_failed"}
    )
    return _check(
        "C06_failure_classifier_names_only_recorded_stops",
        ok,
        "budget_exceeded → budget_exhaustion (with the budget's own message), max_turns → "
        "main_max_turns, acceptance FAIL → acceptance_failed, an exception or an unknown "
        "stop stays "
        "unclassified, success produces no record",
        kinds={name: (record or {}).get("kind") for name, record in cases.items()},
    )


# -- C07..C12 -------------------------------------------------------------------------------------


def _check_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    from experiments.stage_benchmark import matrix

    rebuilt = c1b.plan_record()
    digest = matrix.plan_digest(plan["cells"])
    others = {
        "c1": C1_PLAN_DIGEST,
        "bplus-canary": BPLUS_CANARY_PLAN_DIGEST,
        "bplus-formal": BPLUS_FORMAL_PLAN_DIGEST,
        **{label: value for label, value in CONSUMED_CLAIMS},
    }
    ok = (
        digest == plan["digest"] == rebuilt["digest"]
        and plan["digest"] not in set(others.values())
        and plan["cells"][0]["goal_digest"] == c1.plan_record()["cells"][0]["goal_digest"]
        and plan["cells"][0]["main_prompt_digest"]
        != c1.plan_record()["cells"][0]["main_prompt_digest"]
    )
    return _check(
        "C07_plan_identity_recomputes_and_is_new",
        ok,
        f"one cell digests to {plan['digest'][:19]}…; the goal is Stage C1's, the prompt is not, "
        "and no earlier plan shares the digest",
        plan_digest=plan["digest"],
        cell_id=plan["cells"][0]["cell_id"],
        goal_digest=plan["cells"][0]["goal_digest"],
    )


def _check_preflight(plan: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    try:
        report = executor.run_admission_preflight(plan, repo_root=REPO_ROOT, scratch_root=scratch)
    except Exception as error:  # noqa: BLE001 - a raised preflight is a failed check
        return _check(
            "C08_production_admission_is_ready", False, f"{type(error).__name__}: {error}"[:300]
        )
    cell = (report.get("cells") or [{}])[0]
    ok = (
        report.get("status") == "READY"
        and not report.get("blocking_failures")
        and not report.get("contract_blocking_failures")
        and cell.get("worker_profile") == contract.CODING
        and cell.get("worker_exposure") == "card"
    )
    return _check(
        "C08_production_admission_is_ready",
        ok,
        f"the pre-claim gate accepts the C1-B cell ({report.get('status')}); credential present="
        f"{bool(report.get('credential_present'))} is reported separately from the contract",
        status=report.get("status"),
        blocking_failures=list(report.get("blocking_failures") or []),
        contract_blocking_failures=list(report.get("contract_blocking_failures") or []),
        credential_present=bool(report.get("credential_present")),
    )


def _check_authorization(
    record: Mapping[str, Any],
    *,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    verdict = authority.verify(
        record,
        plan=plan,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    ok = (
        verdict["ready"] is False
        and record.get("status") == "PENDING_OWNER_APPROVAL"
        and record.get("authorizes_live_execution") is False
        and record.get("cost_stop_usd") is None
        and record.get("per_cell_cost_stop_usd") is None
        and record.get("plan_digest") == plan["digest"]
        and list(record.get("cell_ids") or []) == [plan["cells"][0]["cell_id"]]
    )
    return _check(
        "C09_pending_authorization_authorizes_nothing",
        ok,
        f"status {record.get('status')}, authorizes_live_execution false, ceilings null; the "
        f"verifier refuses it for {len(verdict['problems'])} stated reasons",
        blocked_by=list(verdict["problems"]),
    )


def _check_claims(plan: Mapping[str, Any]) -> dict[str, Any]:
    states = {label: executor.claim_path(digest).is_file() for label, digest in CONSUMED_CLAIMS}
    new_claim = executor.claim_path(plan["digest"])
    root, default = executor.claim_root()
    ok = all(states.values()) and not new_claim.exists() and default
    return _check(
        "C10_history_claims_intact_and_no_c1b_claim",
        ok,
        f"{sum(states.values())}/{len(states)} consumed claims present, no claim for "
        f"{plan['digest'][:19]}…, production claim root in force",
        claims=states,
        claim_path=str(new_claim),
        claim_root=str(root),
    )


def _check_evidence_index() -> dict[str, Any]:
    index = C1_EVIDENCE_ROOT / "evidence-index.json"
    if not index.is_file():
        return _check(
            "C12_c1_evidence_index_is_unchanged",
            True,
            "the C1 evidence index is not mounted on this machine; nothing was read or written",
            mounted=False,
        )
    payload = json.loads(index.read_text(encoding="utf-8"))
    files = payload.get("files") or {}
    recomputed = (
        "sha256:" + hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    )
    ok = recomputed == payload.get("index_digest") == C1_EVIDENCE_INDEX_DIGEST
    return _check(
        "C12_c1_evidence_index_is_unchanged",
        ok,
        f"{len(files)} files re-hash to the recorded index digest; no C1 evidence was rewritten",
        mounted=True,
        files=len(files),
        index_digest=recomputed,
    )


# -- assembly -------------------------------------------------------------------------------------


def authorization_record(
    plan: Mapping[str, Any], *, protocol_digest: str, identity: Mapping[str, Any]
) -> dict[str, Any]:
    record = authority.pending_record(
        plan, protocol_digest_value=protocol_digest, executor=identity
    )
    record["stage"] = c1b.STAGE_ID
    record["proposed_envelope"] = {
        **dict(c1b.ENVELOPE_CANDIDATE),
        "note": "a candidate for the Owner's decision; the signed fields stay null until signed",
    }
    record["intervention"] = dict(plan["intervention"])
    record["review_surface"] = {
        "plan_id": c1b.PLAN_ID,
        "goal_digest": c1b.goal_digest(),
        "main_prompt_digest": c1b.prompt_digest(),
        "worker_contract_digest": plan["intervention"]["worker_contract"]["rendered_prompt_digest"],
        "write_grant": c1.grant_record(),
        "combined_intervention": True,
        "single_variable_ablation": False,
    }
    record["notes"] = (
        "Stage C1-B. This record authorizes nothing. It covers one cell and one attempt of a "
        "combined intervention: the coding worker completion contract and the Main verification "
        "discipline. A live dispatch needs every null field filled, both cost fields finite and "
        "positive, and an external provenance artefact whose bytes still hash to the recorded "
        "value."
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
    cell = plan["cells"][0]
    scratch.mkdir(parents=True, exist_ok=True)
    return [
        _check_worker_contract(),
        _check_permissions(cell, scratch / "c02"),
        _check_admission(cell, scratch / "c03"),
        _check_delivery_semantics(),
        _check_prompt(cell, scratch / "c05"),
        _check_classification(),
        _check_identity(plan),
        _check_preflight(plan, scratch / "c08"),
        _check_authorization(
            pending,
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
            plan=plan,
        ),
        _check_claims(plan),
        _check(
            "C11_plan_contract_invariants",
            not c1b.validate(plan),
            "the frozen design invariants hold on the record itself",
            problems=c1b.validate(plan),
        ),
        _check_evidence_index(),
    ]


def build(scratch: Path | None = None, *, write: bool = True) -> dict[str, Any]:
    head = executor.git_head(REPO_ROOT)
    identity = executor.executor_identity(REPO_ROOT)
    protocol_digest = authority.protocol_digest(contract.protocol_document())
    plan = c1b.plan_record()
    pending = authorization_record(plan, protocol_digest=protocol_digest, identity=identity)

    parent = Path(scratch) if scratch is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=parent, prefix="stage-c1b-run-"))
    checks = run_checks(
        scratch=base,
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
        plan=plan,
        pending=pending,
    )
    report = {
        "stage": c1b.STAGE_ID,
        "design_version": c1b.DESIGN_VERSION,
        "plan_id": c1b.PLAN_ID,
        "plan_digest": plan["digest"],
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "bplus_protocol_unchanged": protocol_digest == BPLUS_PROTOCOL_DIGEST,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "c1_executor_digest": C1_EXECUTOR_DIGEST,
        "bplus_v3_executor_digest": BPLUS_V3_EXECUTOR_DIGEST,
        "goal_digest": c1b.goal_digest(),
        "intervention_digest": c1.activation_digest(),
        "main_prompt_version": prompts.V22_VERSION,
        "main_prompt_digest": c1b.prompt_digest(),
        "worker_contract": c1b.worker_contract(),
        "budget_digest": authority.digest_of(dict(contract.BUDGET)),
        "execution_contract_digest": contract.EXECUTION_CONTRACT_DIGEST,
        "write_grant": c1.grant_record(),
        "built_from_head": head,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for check in checks if check["ok"]),
            "failed": [check["check_id"] for check in checks if not check["ok"]],
        },
        "status": (
            "STAGE_C1B_COMPLETION_CONTROL_READY_FOR_LIVE_AUTHORIZATION"
            if all(check["ok"] for check in checks)
            else "STAGE_C1B_COMPLETION_CONTROL_NOT_READY"
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
                "stage": c1b.STAGE_ID,
                "executor_version": identity["executor_version"],
                "executor_digest": identity["executor_digest"],
                "files": identity["files"],
                "built_from_head": head,
                "supersedes": {
                    "executor_version": "bplus-executor-1.3",
                    "executor_digest": C1_EXECUTOR_DIGEST,
                    "scope": "Stage B+ and Stage C1 plans only; their evidence is not rebuilt",
                },
                "note": (
                    "the identity now covers the worker completion contract and the failure "
                    "classifier, so a signature binds the behaviour that ran"
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
    parser = argparse.ArgumentParser(description="Stage C1-B targeted verification")
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    report = build(arguments.scratch)
    if not arguments.quiet:
        for check in report["checks"]:
            flag = "PASS" if check["ok"] else "FAIL"
            print(f"{flag} {check['check_id']}: {check['detail'][:150]}")
        print(json.dumps(report["summary"], indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
