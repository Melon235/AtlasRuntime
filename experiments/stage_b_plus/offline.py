"""Stage B+ offline validation and artefact production. No model call, no sandbox.

``python -m experiments.stage_b_plus.offline`` writes:

``protocol.json``      the candidate contract, derived from files
``plan.json``          the 24 cells and their balance report
``inputs/*.json``      the exact provider request per arm, plus the field diff between arms
``probe.json``         the scripted dynamic-path probes (Phase B)
``cost.json``          the cost envelope (Phase K)
``authorization.json`` a **pending** record; it authorizes nothing and says so
``offline.json``       every check below, with the evidence for each

The checks are the fourteen Phase J items, plus the invariants a reviewer would otherwise have to
take on trust. Each records ``ok`` and a ``detail`` that names what was actually read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2 import preflight as closure2_preflight
from experiments.stage_b_plus import capture as capture_module
from experiments.stage_b_plus import contract, cost, probe
from experiments.stage_benchmark import reconstruct

from atlasos.delegation.capability_card import REFERENCE_SURFACE, build_card
from atlasos.workers import WorkerCatalog
from atlasos.workers.factory import DEFAULT_AVAILABLE_TOOLS

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

#: Recorded at the start of this stage. The freeze must hash to these values afterwards.
STAGE_B_FREEZE = REPO_ROOT / "experiments" / "stage_benchmark" / "execution" / "freeze-1.6.json"
STAGE_B_FREEZE_RAW_PREFIX = "004d4b77"

#: Paths this branch must not modify. Closure-2, the Stage B freeze and its executor are frozen.
PROTECTED_PREFIXES: tuple[str, ...] = (
    "experiments/stage_benchmark/",
    "experiments/evaluation/closure2/",
)
#: The branch this stage started from. ``git diff`` against it is the change surface.
BASELINE_COMMIT = "e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1"

#: Closure-2 S07/S08 must remain reachable and untouched.
CLOSURE2_HEADS: tuple[str, ...] = (
    "dfd60cd406b81a6f085d3b1bb4dd05a2384cd373",
    "231b5ca",
)


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _check(check_id: str, ok: bool, detail: str, **evidence: Any) -> dict[str, Any]:
    return {"check_id": check_id, "ok": bool(ok), "detail": detail, **evidence}


def _git(*arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *arguments],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - environment
        return 1, str(error)
    return completed.returncode, completed.stdout


# -- artefacts --------------------------------------------------------------------------------


def _arm_captures() -> list[dict[str, Any]]:
    prompts = {
        variant: capture_module.MainPromptSpec(
            version=contract.prompt_version(variant), text=contract.prompt_text(variant)
        )
        for variant in ("p3-bplus",)
    }
    results = []
    for arm in contract.ARMS:
        task = contract.TASKS[0]
        capture_arm = capture_module.CaptureArm(
            arm_id=arm.arm_id,
            prompt=prompts["p3-bplus"],
            worker_profiles=(arm.profile_id,),
            worker_exposure=arm.exposure,
            child_write_grants=arm.grant_for(task.task_id),
            worker_max_turns=int(contract.BUDGET["worker_max_turns"]),
        )
        results.append(capture_module.capture(capture_arm))
    return results


def _authorization_record(protocol_digest: str) -> dict[str, Any]:
    return {
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "status": "PENDING_OWNER_APPROVAL",
        "cells": len(contract.cells()),
        "bound_head": None,
        "approved_by": None,
        "approval_commit": None,
        "approved_at": None,
        "cost_ceiling_usd": None,
        "cumulative_stop_usd": None,
        "canary_approved": False,
        "authorizes_live_execution": False,
        "reuses_s06_or_s08_balance": False,
        "notes": (
            "This record authorizes nothing. A live Stage B+ cell requires the Owner to fill every "
            "empty field, name this exact protocol digest and a head commit, and set both a cost "
            "ceiling and a cumulative stop."
        ),
    }


def approval_gate(
    record: Mapping[str, Any] | None, *, protocol_digest: str, head: str | None = None
) -> dict[str, Any]:
    """The three conditions a live B+ cell needs. Same shape as the Closure-2 gate, own identity."""

    active: Mapping[str, Any] = record or {}
    problems: list[str] = []
    if active.get("status") != "APPROVED":
        problems.append(f"status is {active.get('status')!r}, not 'APPROVED'")
    if active.get("protocol_digest") != protocol_digest:
        problems.append(
            f"record names {active.get('protocol_digest')!r}, contract is {protocol_digest!r}"
        )
    if not active.get("approved_by"):
        problems.append("record has no approver")
    if not active.get("approval_commit"):
        problems.append("record has no approval commit")
    if not active.get("approved_at"):
        problems.append("record has no approval timestamp")
    if active.get("cost_ceiling_usd") is None:
        problems.append("record has no cost ceiling")
    if head is not None and active.get("bound_head") != head:
        problems.append(f"record is bound to {active.get('bound_head')!r}, head is {head!r}")
    return {
        "execution_ready": not problems,
        "problems": problems,
        "protocol_digest": protocol_digest,
        "record": dict(active),
    }


def require_approval(*, record: Mapping[str, Any] | None, protocol_digest: str) -> dict[str, Any]:
    gate = approval_gate(record, protocol_digest=protocol_digest)
    if not gate["execution_ready"]:
        raise PermissionError("Stage B+ is not approved: " + "; ".join(gate["problems"]))
    return gate


def protocol_digest_of(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return _digest_bytes(canonical.encode("utf-8"))


# -- checks -----------------------------------------------------------------------------------


def run_checks(
    document: dict[str, Any],
    captures: list[dict[str, Any]],
    probe_report: dict[str, Any],
    *,
    scratch: Path,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    digest = protocol_digest_of(document)

    # 1 — the four requests differ exactly where the design says they do.
    compare = capture_module.compare(captures)
    by_arm = {item["arm_id"]: item["fingerprint"] for item in captures}
    prompts = {item["system_prompt"]["digest"] for item in by_arm.values()}
    descriptions = {
        arm_id: value["delegate_tasks"]["description_digest"] for arm_id, value in by_arm.items()
    }
    delegated_pairs = [
        pair for pair, fields in compare["pairwise_changed_fields"].items() if pair.startswith("C")
    ]
    # ``tools.chars`` is the sum of the tool descriptions, so it moves with the delegate_tasks
    # description and with nothing else. Anything beyond these four fields would be a difference the
    # design did not ask for.
    allowed = {
        "tools.digest",
        "tools.chars",
        "delegate_tasks.description_chars",
        "delegate_tasks.description_digest",
    }
    only_description = all(
        set(fields) <= allowed
        for pair, fields in compare["pairwise_changed_fields"].items()
        if pair in delegated_pairs
    )
    checks.append(
        _check(
            "J01_four_arm_requests_match_design",
            len(prompts) == 1 and len(set(descriptions.values())) == 4 and only_description,
            "one system prompt across the arms, four distinct delegate_tasks descriptions, and no "
            "other field differs between arms",
            system_prompt_digests=sorted(prompts),
            description_digests=descriptions,
            changed_fields={
                pair: fields
                for pair, fields in compare["pairwise_changed_fields"].items()
                if pair in delegated_pairs
            },
            unchanged_across_arms=[
                "system_prompt.digest",
                "delegate_tasks.schema_digest",
                "delegate_tasks.profile_default",
                "first_user_turn.digest",
            ],
        )
    )

    # 2 — the disclosure describes the profile that will actually run.
    cards = {
        item["arm_id"]: item["session"]["delegation_controls"]["exposure"] for item in captures
    }
    profiles_of = {
        item["arm_id"]: item["session"]["delegation_controls"]["profiles"] for item in captures
    }
    card_ok = True
    card_detail: list[str] = []
    shipped = WorkerCatalog()
    for arm in contract.ARMS:
        profile = shipped.profiles[arm.profile_id].model_copy(
            update={"max_turns": int(contract.BUDGET["worker_max_turns"])}
        )
        card = build_card(
            profile,
            registered_tools=DEFAULT_AVAILABLE_TOOLS,
            surface_tools=REFERENCE_SURFACE,
            granted_write_paths=contract.TASK_WRITE_SCOPE[contract.TASK_IDS[0]]
            if arm.profile_id == contract.CODING
            else (),
            child_max_turns=int(contract.BUDGET["child_max_turns"]),
            result_preview_chars=int(contract.BUDGET["result_preview_chars"]),
        )
        if set(card.allowed) != set(profile.base_tools):
            card_ok = False
            card_detail.append(f"{arm.arm_id}: allowed set is not the profile's tool list")
        if set(card.not_granted) & set(profile.base_tools):
            card_ok = False
            card_detail.append(f"{arm.arm_id}: a granted tool is reported as not granted")
        if card.read_only != (arm.profile_id == contract.RESEARCH):
            card_ok = False
            card_detail.append(f"{arm.arm_id}: read-only classification disagrees with the profile")
        disclosed = cards[arm.arm_id]["cards"]
        if disclosed and disclosed[0]["digest"] != card.digest():
            card_ok = False
            card_detail.append(f"{arm.arm_id}: the disclosed card is not this profile's card")
        effective = profiles_of[arm.arm_id][arm.profile_id]["effective_max_turns"]
        if effective > int(contract.BUDGET["child_max_turns"]):
            card_ok = False
            card_detail.append(f"{arm.arm_id}: effective turns exceed the delegation limit")
    checks.append(
        _check(
            "J02_worker_capability_matches_its_description",
            card_ok,
            "; ".join(card_detail) or "every disclosed tool set equals the trusted profile's",
            profiles=cards,
        )
    )

    probes = {item["probe_id"]: item for item in probe_report["probes"]}

    # 3 — a research worker has no route to a write.
    research = probes["research_write"]
    checks.append(
        _check(
            "J03_research_worker_has_no_write_route",
            research["ok"],
            "a declared write is refused before a worker starts",
            checks=research["checks"],
        )
    )

    # 4 — a coding worker can complete a granted coding task.
    coding = probes["coding_parallel"]
    checks.append(
        _check(
            "J04_coding_worker_completes_a_granted_task",
            coding["ok"],
            "two admitted children wrote inside their declared paths and completed",
            checks=coding["checks"],
        )
    )

    # 5 — the Main-driven path derives real workers.
    derived = coding["delegation"]["children"]
    checks.append(
        _check(
            "J05_main_driven_path_derives_workers",
            len(derived) == 2 and all(child["status"] == "completed" for child in derived),
            "children were constructed by the real factory and ran their own loops",
            children=derived,
        )
    )

    # 6 — the router and ownership gates still refuse what they must.
    gate_probes = ["research_write", "shared_write", "single_coding"]
    checks.append(
        _check(
            "J06_router_and_ownership_gates_remain_effective",
            all(probes[name]["ok"] for name in gate_probes)
            and probes["research_tool_escape"]["ok"],
            "declared write refused, shared write refused, single write task refused, and an "
            "unoffered tool never executed",
            probes={name: probes[name]["ok"] for name in [*gate_probes, "research_tool_escape"]},
        )
    )

    # 7 — a handoff reaches the Main.
    reads = coding["handoff_reads"] or {}
    checks.append(
        _check(
            "J07_handoff_reaches_the_main",
            bool(reads.get("read_success")),
            "the Main read a child result through delegate.read",
            handoff_reads=reads,
        )
    )

    # 8 — the arms share one budget.
    budgets = {item["arm_id"]: item["session"]["delegation_controls"]["child_max_turns_limit"]
               for item in captures}
    arm_budgets = {arm_record["arm_id"]: arm_record["budget"] for arm_record in document["arms"]}
    checks.append(
        _check(
            "J08_four_arms_share_one_budget",
            len(set(budgets.values())) == 1
            and len({json.dumps(value, sort_keys=True) for value in arm_budgets.values()}) == 1,
            "one child turn limit and one run configuration across the four arms",
            child_max_turns=budgets,
            run_config=document["run_config"],
        )
    )

    # 9 — system usage includes the Main and every child.
    session_metrics = dict(coding.get("metrics") or {})
    checks.append(
        _check(
            "J09_usage_includes_main_and_children",
            _usage_includes_children(session_metrics),
            "system_total_tokens is Main plus child prompt and completion, and is not a Main-only "
            "number",
            metrics={
                key: session_metrics.get(key)
                for key in (
                    "main_cumulative_input_tokens",
                    "main_completion_tokens",
                    "child_total_prompt_tokens",
                    "child_completion_tokens",
                    "system_total_tokens",
                )
            },
        )
    )

    # 10 — L1-L6 evidence is recoverable from a real run directory.
    record = _reconstruct_sample(scratch)
    met = ((record or {}).get("delegation") or {}).get("levels", {}).get("met") or {}
    checks.append(
        _check(
            "J10_l1_l6_evidence_recoverable",
            sorted(met) == sorted(name for name, _ in contract.DELEGATION_LEVELS),
            "the Stage B analyzer reconstructs all six levels from a probe run directory",
            levels=met,
            level_reached=((record or {}).get("delegation") or {})
            .get("levels", {})
            .get("level_reached"),
        )
    )

    # 11 — 24 unique cell identities.
    cells = document["plan"]["cells"]
    checks.append(
        _check(
            "J11_24_unique_cell_identities",
            len(cells) == 24 and len({cell["cell_id"] for cell in cells}) == 24,
            "24 cells, 24 distinct ids, four arms x three tasks x two repetitions",
            first_three=[cell["cell_id"] for cell in cells[:3]],
        )
    )

    # 12 — an unsigned authorization refuses every real execution.
    pending = _authorization_record(digest)
    gate = approval_gate(pending, protocol_digest=digest)
    refused = False
    try:  # noqa: SIM105 - the refusal is the observation
        require_approval(record=pending, protocol_digest=digest)
    except PermissionError:
        refused = True
    forged = dict(pending, status="APPROVED", approved_by="someone", approval_commit="c0ffee",
                  approved_at="2026-01-01T00:00:00Z", cost_ceiling_usd=1.0,
                  protocol_digest="sha256:" + "0" * 64)
    checks.append(
        _check(
            "J12_unsigned_authorization_refuses_execution",
            refused and not gate["execution_ready"] and not approval_gate(
                forged, protocol_digest=digest
            )["execution_ready"],
            "the pending record is refused, and a fully populated record bound to another digest "
            "is refused too",
            problems=gate["problems"],
            forged_problems=approval_gate(forged, protocol_digest=digest)["problems"],
        )
    )

    # 13 — Stage B Freeze 1.6 is unchanged.
    freeze_ok, freeze_detail = _freeze_state()
    checks.append(_check("J13_stage_b_freeze_1_6_unchanged", freeze_ok, freeze_detail))

    # 14 — Closure-2 S07/S08 are untouched.
    isolation_ok, isolation_detail = _closure2_isolation()
    checks.append(
        _check("J14_closure2_s07_s08_unaffected", isolation_ok, isolation_detail)
    )

    # Beyond the fourteen: the contract itself must still validate.
    problems = contract.validate(document)
    checks.append(
        _check(
            "X01_contract_self_validates",
            not problems,
            "the derived protocol document passes its own invariants",
            problems=problems,
        )
    )
    checks.append(
        _check(
            "X02_capture_is_not_a_model_call",
            all(item["session"]["status"] == "succeeded" for item in captures),
            "every capture completed in one turn against a fake provider; no provider was built",
            statuses={item["arm_id"]: item["session"]["status"] for item in captures},
        )
    )

    workload_path = STAGE_DIR / "workload.json"
    if workload_path.is_file():
        workload = json.loads(workload_path.read_text(encoding="utf-8"))
        checks.append(
            _check(
                "X03_reused_tasks_still_satisfiable",
                bool(workload.get("ok")),
                "the frozen Closure-2 oracle self-test still discriminates seed from reference on "
                "all three tasks",
                tasks={
                    task_id: {
                        "seed_hidden_fails": value["seed_hidden_fails"],
                        "reference_hidden_passes": value["reference_hidden_passes"],
                    }
                    for task_id, value in workload["tasks"].items()
                },
            )
        )
    else:
        checks.append(
            _check(
                "X03_reused_tasks_still_satisfiable",
                False,
                "workload.json is absent; run with --selftest to prove the oracles still "
                "discriminate",
            )
        )
    return checks


def _usage_includes_children(metrics: Mapping[str, Any]) -> bool:
    child_prompt = metrics.get("child_total_prompt_tokens")
    child_completion = metrics.get("child_completion_tokens")
    total = metrics.get("system_total_tokens")
    main = metrics.get("main_cumulative_input_tokens")
    main_completion = metrics.get("main_completion_tokens")
    if None in (child_prompt, child_completion, total, main, main_completion):
        return False
    return int(total) == int(main) + int(main_completion) + int(child_prompt) + int(
        child_completion
    )


def _reconstruct_sample(scratch: Path) -> dict[str, Any] | None:
    """Analyse one probe run with the Stage B analyzer, so the ladder is proven recoverable."""

    probe_result = probe.run_probe(probe.probe_table()[0], root=scratch / "evidence")
    run_dir = Path(probe_result.session["run_dir"])
    if not (run_dir / "session.json").is_file():
        return None
    record = reconstruct.reconstruct(run_dir)
    cells = contract.cells()
    return reconstruct.annotate(
        record,
        task_id=contract.TASK_IDS[0],
        arm_id=cells[0]["arm_id"],
        repetition=1,
    )


def workload_report() -> dict[str, Any]:
    """Re-run the frozen Closure-2 oracle self-test on the three tasks B+ reuses.

    Reused, not reimplemented: ``closure2_preflight.task_selftest`` builds four disposable
    workspaces per task and decides whether the seed fails the hidden suite and the reference passes
    it. What this adds is only the question B+ needs answered: the same task data still supports a
    Main-driven run, where the goal is the public requirement and ``docs/INTERFACE.md`` is a
    workspace file rather than part of a composed assignment.
    """

    per_task: dict[str, Any] = {}
    for task in contract.TASKS:
        result = closure2_preflight.task_selftest(task)
        per_task[task.task_id] = {
            "seed_public_fails": result["seed_public_fails"],
            "seed_hidden_fails": result["seed_hidden_fails"],
            "reference_public_passes": result["reference_public_passes"],
            "reference_hidden_passes": result["reference_hidden_passes"],
            "hidden_oracle_executed_tests": result["hidden_oracle_executed_tests"],
            "hidden_oracle_really_executes": result["hidden_oracle_really_executes"],
            "separate_workspaces": result["independence"]["separate_workspaces"],
            "public_and_hidden_targets_disjoint": result["independence"][
                "public_and_hidden_targets_disjoint"
            ],
            "harness_blocked": result["harness_blocked"],
            "main_visible_variables": {
                "goal_is_public_requirement": True,
                "workspace_is_seed_only": True,
                "interface_contract_is_a_repo_file": "docs/INTERFACE.md" in task.seed,
            },
        }
    report = {
        "stage": contract.STAGE_ID,
        "kind": "closure2_oracle_selftest_reused",
        "source": "experiments/evaluation/closure2/preflight.py::task_selftest",
        "tasks": per_task,
        "ok": all(
            value["seed_hidden_fails"]
            and value["reference_hidden_passes"]
            and value["hidden_oracle_really_executes"]
            and value["separate_workspaces"]
            for value in per_task.values()
        ),
    }
    contract.write_document(report, STAGE_DIR / "workload.json")
    return report


def _freeze_state() -> tuple[bool, str]:
    """Is Freeze 1.6 unmodified *in the repository*, not merely re-rendered by this checkout?

    A fresh worktree materialises every file with different line endings, so the raw digest of the
    same bytes-in-git differs from the digest recorded against the original checkout. The
    authoritative question is therefore ``git diff`` against the baseline commit; the digests are
    reported afterwards so the ``004d4b77`` / ``73713095`` pair from the stage report can be
    reconciled rather than assumed.
    """

    if not STAGE_B_FREEZE.is_file():
        return False, f"{STAGE_B_FREEZE} does not exist"
    relative = str(STAGE_B_FREEZE.relative_to(REPO_ROOT)).replace("\\", "/")
    code, output = _git("diff", "--name-only", BASELINE_COMMIT, "--", relative)
    if code != 0:
        return False, f"git diff for {relative} failed: {output.strip()[:200]}"
    unchanged = not output.strip()
    code, dirty = _git("status", "--porcelain", "--", relative)
    if code == 0 and dirty.strip():
        return False, f"{relative} is dirty: {dirty.strip()[:120]}"

    raw = STAGE_B_FREEZE.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    payload = json.loads(raw.decode("utf-8"))
    return unchanged, (
        f"{relative} {'is unchanged' if unchanged else 'CHANGED'} against {BASELINE_COMMIT[:7]}; "
        f"raw {_digest_bytes(raw)}, LF {_digest_bytes(lf)}, CRLF {_digest_bytes(crlf)}; "
        f"declared content digest {payload.get('content_digest')!r}; {len(raw)} bytes"
    )


def _closure2_isolation() -> tuple[bool, str]:
    """This branch must not touch the frozen Closure-2 or Stage B trees, and S07 must survive."""

    problems: list[str] = []
    code, output = _git("diff", "--name-only", BASELINE_COMMIT)
    if code != 0:
        problems.append(f"git diff against the baseline failed: {output.strip()[:200]}")
        changed: list[str] = []
    else:
        changed = [line.strip() for line in output.splitlines() if line.strip()]
    touched = [
        path for path in changed if path.startswith(PROTECTED_PREFIXES) or path in {
            "experiments/stage_benchmark/protocol.json",
            "experiments/stage_benchmark/freeze.json",
        }
    ]
    if touched:
        problems.append(f"protected paths changed: {touched}")
    code, output = _git("status", "--porcelain", "--", *PROTECTED_PREFIXES)
    if code == 0 and output.strip():
        problems.append(f"protected paths are dirty: {output.strip()[:200]}")
    for head in CLOSURE2_HEADS:
        code, output = _git("cat-file", "-t", head)
        if code != 0 or not output.strip():
            problems.append(f"closure-2 commit {head[:7]} is no longer reachable")
    return not problems, (
        "; ".join(problems)
        or f"{len(changed)} files changed since {BASELINE_COMMIT[:7]}, none of them protected"
    )


# -- entry point ------------------------------------------------------------------------------


def build(scratch: Path | None = None, *, selftest: bool = False) -> dict[str, Any]:
    if selftest:
        workload_report()
    document = contract.protocol_document()
    protocol_path = contract.write_document(document, STAGE_DIR / "protocol.json")
    plan_path = contract.write_document(
        {
            "stage": contract.STAGE_ID,
            "protocol_version": contract.PROTOCOL_VERSION,
            "plan": document["plan"],
        },
        STAGE_DIR / "plan.json",
    )

    captures = _arm_captures()
    for item in captures:
        capture_module.write_capture(
            item, STAGE_DIR / "inputs" / f"request-{item['arm_id']}.json"
        )
    diff = capture_module.compare(captures)
    capture_module.write_capture(
        {
            "stage": contract.STAGE_ID,
            "arms": diff["arms"],
            "pairwise_changed_fields": diff["pairwise_changed_fields"],
            "fingerprints": {item["arm_id"]: item["fingerprint"] for item in captures},
        },
        STAGE_DIR / "inputs" / "diff.json",
    )

    probe_report = probe.run_all()
    contract.write_document(probe_report, STAGE_DIR / "probe.json")

    cost_report = cost.estimate()
    contract.write_document(cost_report, STAGE_DIR / "cost.json")

    digest = protocol_digest_of(document)
    record = _authorization_record(digest)
    contract.write_document(record, STAGE_DIR / "authorization.json")

    work_root = Path(scratch) if scratch is not None else Path.cwd()
    checks = run_checks(document, captures, probe_report, scratch=work_root)
    report = {
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": digest,
        "protocol_path": str(protocol_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "plan_path": str(plan_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for check in checks if check["ok"]),
            "failed": [check["check_id"] for check in checks if not check["ok"]],
        },
        "authorization": record,
        "live_execution_authorized": False,
    }
    contract.write_document(report, STAGE_DIR / "offline.json")
    return report


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Stage B+ offline validation")
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="re-run the frozen Closure-2 oracle self-test on the three reused tasks",
    )
    arguments = parser.parse_args(argv)
    report = build(arguments.scratch, selftest=arguments.selftest)
    if not arguments.quiet:
        for check in report["checks"]:
            flag = "PASS" if check["ok"] else "FAIL"
            print(f"{flag} {check['check_id']}: {check['detail'][:160]}")
        print(json.dumps(report["summary"], indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
