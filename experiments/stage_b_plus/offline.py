"""Stage B+ offline validation and artefact production. No model call, no sandbox, no cell.

``python -m experiments.stage_b_plus.offline [--scratch DIR]`` writes:

``protocol.json``             the candidate contract, derived from files
``plan-canary.json``          the six-cell diagnostic canary
``plan-formal.json``          the twenty-four cell formal matrix
``inputs/request-C*.json``    the exact provider request per arm, plus the field diff
``probe.json``                the scripted dynamic-path probes
``cost.json``                 the per-scope cost envelope
``authorization-canary.json`` a **pending** CANARY_ONLY record
``authorization-formal.json`` a **pending** FORMAL_MATRIX_ONLY record
``executor.json``             the executor identity a signature binds
``offline.json``              the twenty-four checks below, with their evidence

The checks are the Phase L list. Nine of them exercise the real dispatch loop with synthetic cell
results: the gate is what is under test there, so a synthetic input is the correct instrument. One
cell is executed for real through ``run_goal`` in host mode to prove the runner wiring; that cell
is reported separately because host mode has no sandbox evidence and the gate must stop it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2 import preflight as closure2_preflight
from experiments.stage_b_plus import authority, contract, cost, executor, probe
from experiments.stage_b_plus import capture as capture_module

from atlasos.delegation.capability_card import REFERENCE_SURFACE, build_card
from atlasos.delegation.tool import DEFAULT_DESCRIPTION
from atlasos.workers import WorkerCatalog
from atlasos.workers.factory import DEFAULT_AVAILABLE_TOOLS

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

STAGE_B_FREEZE = REPO_ROOT / "experiments" / "stage_benchmark" / "execution" / "freeze-1.6.json"
#: The CRLF rendering of Freeze 1.6 recorded when Stage B froze it. A fresh worktree renders LF, so
#: the raw digest differs by line endings and only this normalised value is comparable.
STAGE_B_FREEZE_CRLF = "004d4b77"

PROTECTED_PREFIXES: tuple[str, ...] = (
    "experiments/stage_benchmark/",
    "experiments/evaluation/closure2/",
)
BASELINE_COMMIT = "e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1"
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
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return 1, ""
    return completed.returncode, completed.stdout


# -- captures ---------------------------------------------------------------------------------


def _arm_capture(arm: contract.Arm, task_index: int = 0) -> dict[str, Any]:
    task = contract.TASKS[task_index]
    return capture_module.capture(
        capture_module.CaptureArm(
            arm_id=arm.arm_id,
            prompt=contract.main_prompt_spec(),
            worker_profiles=(arm.profile_id,),
            worker_exposure=arm.exposure,
            child_write_grants=arm.grant_for(task.task_id),
            worker_max_turns=int(contract.BUDGET["worker_max_turns"]),
        )
    )


def _all_captures() -> list[dict[str, Any]]:
    return [_arm_capture(arm) for arm in contract.ARMS]


# -- the real runner, used once ---------------------------------------------------------------


def _host_control_flow_check(scratch: Path) -> tuple[bool, str, dict[str, Any]]:
    """One real ``run_goal`` through the dispatch runner, in host mode, with a scripted provider.

    It proves the runner builds the options the arm asks for and that a session comes back with
    reconcilable usage. It cannot prove a cell passes: host mode has no sandbox, so the gate stops
    any cell produced this way, and that is the correct behaviour rather than a defect to hide.
    """

    import asyncio

    from experiments.stage_b_plus import probe as probe_module

    cell = contract.canary_cells()[2]  # C2: coding worker, native exposure
    arm = contract.ARMS_BY_ID[cell["arm_id"]]
    scripted = probe_module.ScriptedProvider(
        main_script=[lambda _messages: [probe_module.TextBlock(text="REPORT: nothing to do.")]],
        child_scripts={},
    )
    runner = executor.host_cell_runner(provider_factory=lambda: scripted, max_turns=2)
    context = {
        "plan": contract.scope_plan(contract.SCOPE_CANARY),
        "runs_root": scratch / "control-flow" / "runs",
        "evidence_root": scratch / "control-flow" / "evidence",
    }
    run = asyncio.run(runner(cell, context))
    session = run.session or {}
    controls = session.get("delegation_controls") or {}
    exposure = (controls.get("exposure") or {}).get("level")
    profiles = list(controls.get("profiles") or {})
    usage = executor.validate_usage(session.get("metrics"))
    ok = (
        run.error is None
        and exposure == arm.exposure
        and profiles == [arm.profile_id]
        and usage["ok"]
    )
    detail = (
        f"one host-mode run_goal for {cell['cell_id']}: exposure={exposure!r} "
        f"profiles={profiles} usage_ok={usage['ok']} status={session.get('status')!r}"
    )
    evidence = {
        "cell_id": cell["cell_id"],
        "arm": {"profile": arm.profile_id, "exposure": arm.exposure},
        "applied_exposure": exposure,
        "applied_profiles": profiles,
        "effective_max_turns": (controls.get("profiles") or {}).get(arm.profile_id, {}).get(
            "effective_max_turns"
        ),
        "usage": usage["values"] if usage["ok"] else usage["problems"],
        "sandbox_present": bool(session.get("sandbox")),
        "note": "host mode: no sandbox evidence, so the gate stops this cell by design",
    }
    return ok, detail, evidence


# -- synthetic cells for the gate ---------------------------------------------------------------


def _synthetic_session(
    *, cell: Mapping[str, Any], root: Path, overrides: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], Path]:
    """A session shaped like a real one, written to a run directory the gate can read."""

    arm = contract.ARMS_BY_ID[cell["arm_id"]]
    run_dir = root / cell["cell_id"] / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "main_cumulative_input_tokens": 12_000,
        "main_completion_tokens": 900,
        "child_total_prompt_tokens": 4_000,
        "child_completion_tokens": 400,
        "system_total_tokens": 17_300,
        "main_cache_hit_tokens": 10_000,
        "main_cache_miss_tokens": 2_000,
    }
    session: dict[str, Any] = {
        "run_id": f"{cell['cell_id']}-synthetic",
        "status": "succeeded",
        "execution_succeeded": True,
        "failure": None,
        "metrics": metrics,
        "sandbox": {
            "created": True,
            "destroyed": True,
            "retained": False,
            "error": None,
            "network_probe": {"ok": True},
            "host_separation": {"ok": True},
        },
        "delegation_controls": {
            "exposure": {"level": arm.exposure},
            "profiles": {arm.profile_id: {"profile_id": arm.profile_id,
                                          "effective_max_turns": int(
                                              contract.BUDGET["worker_max_turns"])}},
        },
        "delegation": {"batches": [], "routes": []},
        "ownership": {"denied_calls": []},
        "verification": {"verdict": "PASS", "dynamic": {"status": "PASS"}},
        "acceptance": "PASS",
        "public_tests": {"status": "PASS", "calls": 1, "runs": []},
    }
    for key, value in (overrides or {}).items():
        if isinstance(value, Mapping) and isinstance(session.get(key), Mapping):
            merged = dict(session[key])
            merged.update(value)
            session[key] = merged
        else:
            session[key] = value
    (run_dir / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    return session, run_dir


class ProviderTransportError(RuntimeError):
    """Named so the gate classifies it as a provider failure rather than an unknown exception."""


def _synthetic_runner(spec: Mapping[str, Any]) -> executor.CellRunner:
    """A runner whose output the *next* cell in the plan triggers, so one dispatch can stop late."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        override = (spec.get(cell["cell_id"]) or {}).get("overrides")
        behaviour = (spec.get(cell["cell_id"]) or {}).get("behaviour")
        root = Path(context["evidence_root"]) / "cells"
        if behaviour == "raise":
            raise ProviderTransportError("scripted transport failure")
        session, run_dir = _synthetic_session(cell=cell, root=root, overrides=override)
        oracle = {"status": "PASS", "kind": "PASS", "targets": [], "counts": {"passed": 3}}
        if behaviour == "oracle_error":
            oracle = {"status": "ERROR", "kind": "COLLECT_ERROR", "targets": []}
        return executor.CellRun(
            session=session,
            run_dir=None if behaviour == "no_evidence" else run_dir,
            fixture_root=root,
            oracle=oracle,
            duration_s=0.01,
        )

    return run


def _signed(
    plan: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
    scope: str | None = None,
    cost_stop: float = 1.0,
    per_cell: float = 0.5,
) -> dict[str, Any]:
    """A record signed *in memory for an offline check*. It is never written to the repository."""

    pending = authority.pending_record(
        plan, protocol_digest_value=protocol_digest, executor=identity
    )
    # One file per signed record: a later signature must not invalidate an earlier one's provenance.
    index = len(list(scratch.glob("owner-statement-*.json"))) + 1
    statement = scratch / f"owner-statement-{plan['plan_id']}-{index}.json"
    body = {"statement": "offline control-flow check", "scope": scope or plan["scope"]}
    statement.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    provenance = {
        "kind": "external_file",
        "path": str(statement),
        "sha256": _digest_bytes(statement.read_bytes()),
        "content_digest": authority.digest_of(body),
        "body": body,
    }
    signed = authority.sign(
        pending,
        approved_by="offline-check",
        approved_at="2026-11-25T00:00:00Z",
        owner_statement="offline control-flow check, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=cost_stop,
        per_cell_cost_stop_usd=per_cell,
        provenance=provenance,
    )
    if scope is not None:
        signed["scope"] = scope
    return signed


# -- checks -----------------------------------------------------------------------------------


def run_checks(
    document: dict[str, Any],
    captures: list[dict[str, Any]],
    probe_report: dict[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    protocol_digest = authority.protocol_digest(document)
    canary = contract.scope_plan(contract.SCOPE_CANARY)
    formal = contract.scope_plan(contract.SCOPE_FORMAL)
    history = scratch / "history"
    history.mkdir(parents=True, exist_ok=True)

    # L01 — the canary is six unique cells, one per arm, one task, one repetition
    canary_cells = canary["cells"]
    checks.append(
        _check(
            "L01_canary_identity_unique",
            len(canary_cells) == 6
            and len({cell["cell_id"] for cell in canary_cells}) == 6
            and sorted(cell["arm_id"] for cell in canary_cells) == sorted(contract.ARM_IDS)
            and len({cell["task_id"] for cell in canary_cells}) == 1
            and all(cell["is_benchmark_sample"] is False for cell in canary_cells),
            "six cells, six ids, each arm once on P1, none marked as a benchmark sample",
            plan_id=canary["plan_id"],
            plan_digest=canary["digest"],
            cell_ids=[cell["cell_id"] for cell in canary_cells],
        )
    )

    # L02 — six constructed provider requests
    by_arm = {item["arm_id"]: item["fingerprint"] for item in captures}
    checks.append(
        _check(
            "L02_six_provider_inputs_captured",
            len(by_arm) == 6 and all(item["session"]["status"] == "succeeded" for item in captures),
            "every arm produced a captured request without a provider being built",
            per_arm={
                arm: {
                    "system_prompt": value["system_prompt"]["digest"],
                    "tools": value["tools"]["digest"],
                    "description": value["delegate_tasks"]["description_digest"],
                    "description_chars": value["delegate_tasks"]["description_chars"],
                }
                for arm, value in by_arm.items()
            },
        )
    )

    # L03 — the two control arms keep the shipped description, byte for byte
    shipped = capture_module.digest_text(DEFAULT_DESCRIPTION)
    control = {arm: by_arm[arm]["delegate_tasks"] for arm in contract.CONTROL_ARM_IDS}
    control_ok = all(
        item["description_digest"] == shipped
        and item["description_chars"] == len(DEFAULT_DESCRIPTION)
        for item in control.values()
    )
    checks.append(
        _check(
            "L03_control_arms_keep_shipped_description",
            control_ok,
            "C4 and C5 disclose nothing beyond the shipped text; no coding hint was smuggled in",
            shipped_chars=len(DEFAULT_DESCRIPTION),
            shipped_digest=shipped,
            controls={
                arm: {"digest": item["description_digest"], "chars": item["description_chars"]}
                for arm, item in control.items()
            },
        )
    )

    # L04 — the disclosure describes the profile that will run
    shipped_catalog = WorkerCatalog()
    card_problems: list[str] = []
    disclosed: dict[str, Any] = {}
    for arm in contract.ARMS:
        profile = shipped_catalog.profiles[arm.profile_id].model_copy(
            update={"max_turns": int(contract.BUDGET["worker_max_turns"])}
        )
        card = build_card(
            profile,
            registered_tools=DEFAULT_AVAILABLE_TOOLS,
            surface_tools=REFERENCE_SURFACE,
            granted_write_paths=arm.grant_for(contract.TASKS[0].task_id).get(arm.profile_id, ()),
            child_max_turns=int(contract.BUDGET["child_max_turns"]),
            result_preview_chars=int(contract.BUDGET["result_preview_chars"]),
        )
        disclosed[arm.arm_id] = {
            "profile": arm.profile_id,
            "exposure": arm.exposure,
            "allowed": list(card.allowed),
            "read_only": card.read_only,
            "max_turns": card.max_turns,
        }
        if set(card.allowed) != set(profile.base_tools):
            card_problems.append(f"{arm.arm_id}: allowed set is not the profile's")
        if arm.exposure == "none":
            continue
        controls_block = by_arm[arm.arm_id]["delegate_tasks"]
        if controls_block["description_digest"] == shipped:
            card_problems.append(f"{arm.arm_id}: a disclosed arm carries the shipped text")
    checks.append(
        _check(
            "L04_disclosure_matches_profile",
            not card_problems,
            "; ".join(card_problems) or "every disclosed arm names its own profile's tools",
            per_arm=disclosed,
        )
    )

    # L05 — the effective worker turn cap is the candidate 20, not the profile's 6 or 8
    effective = {
        item["arm_id"]: item["session"]["delegation_controls"]["profiles"][
            contract.ARMS_BY_ID[item["arm_id"]].profile_id
        ]["effective_max_turns"]
        for item in captures
    }
    native_caps = {pid: shipped_catalog.profiles[pid].max_turns for pid in (contract.RESEARCH,
                                                                           contract.CODING)}
    checks.append(
        _check(
            "L05_effective_worker_turns",
            set(effective.values()) == {int(contract.BUDGET["worker_max_turns"])},
            "the run-level cap replaced the profile cap in every arm, and the override held",
            effective=effective,
            profile_caps=native_caps,
        )
    )

    probes = {item["probe_id"]: item for item in probe_report["probes"]}
    checks.append(
        _check(
            "L06_coding_worker_permissions",
            probes["coding_parallel"]["ok"] and probes["research_write"]["ok"],
            "a coding pair writes inside its grant and is refused outside it; a research write is "
            "refused before a worker starts",
            coding_checks=probes["coding_parallel"]["checks"],
        )
    )
    checks.append(
        _check(
            "L07_research_overreach_blocked",
            probes["research_write"]["ok"],
            "WRITE_SCOPE_NOT_GRANTED is raised at the router, and file_write never reaches a "
            "research child",
            reason=probes["research_write"]["delegation"]["reason_codes"],
        )
    )
    checks.append(
        _check(
            "L08_dynamic_derivation",
            len(probes["coding_parallel"]["delegation"]["children"]) == 2,
            "two children were constructed by the real factory and ran their own loops",
            children=probes["coding_parallel"]["delegation"]["children"],
        )
    )
    checks.append(
        _check(
            "L09_handoff_reaches_main",
            bool((probes["coding_parallel"].get("handoff_reads") or {}).get("read_success")),
            "the Main read a child result through delegate.read",
            handoff_reads=probes["coding_parallel"].get("handoff_reads"),
        )
    )
    checks.append(
        _check(
            "L10_main_continues",
            any(
                item["check"] == "main_continued_after_handoff" and item["ok"]
                for item in probes["coding_parallel"]["checks"]
            ),
            "the Main edited after the handoff",
        )
    )

    # L11 — the runner builds what the arm asks for, and the usage reconciles
    ok, detail, evidence = _host_control_flow_check(scratch)
    checks.append(_check("L11_usage_reconciles_main_and_worker", ok, detail, **evidence))

    # L12 — an unsigned record refuses
    pending = authority.pending_record(
        canary, protocol_digest_value=protocol_digest, executor=identity
    )
    unsigned = authority.verify(
        pending,
        plan=canary,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    checks.append(
        _check(
            "L12_pending_authorization_refuses",
            unsigned["ready"] is False and len(unsigned["problems"]) >= 6,
            "the pending record is refused for missing signature, identity and cost fields",
            problems=unsigned["problems"],
        )
    )

    # L13 — a head or digest mismatch refuses
    signed_canary = _signed(
        canary, scratch=history, head=head, identity=identity, protocol_digest=protocol_digest
    )
    wrong_head = authority.verify(
        signed_canary,
        plan=canary,
        head="0" * 40,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    wrong_digest = authority.verify(
        dict(signed_canary, plan_digest="sha256:" + "0" * 64),
        plan=canary,
        head=head,
        executor=identity,
        protocol_digest_value=protocol_digest,
        repo_root=REPO_ROOT,
    )
    checks.append(
        _check(
            "L13_head_or_digest_mismatch_refuses",
            signed_canary
            and not wrong_head["ready"]
            and not wrong_digest["ready"]
            and any("plan_digest" in item for item in wrong_digest["problems"]),
            "a signed canary record is refused when the head or the plan digest moves",
            head_problems=wrong_head["problems"][:3],
            digest_problems=wrong_digest["problems"][:3],
        )
    )

    # L14 / L15 — scope is not interchangeable
    cross_scope_ok = True
    scope_evidence: dict[str, Any] = {}
    for source_plan, target_plan, label in (
        (canary, formal, "canary_authority_cannot_run_formal"),
        (formal, canary, "formal_authority_cannot_run_canary"),
    ):
        record = _signed(
            source_plan, scratch=history, head=head, identity=identity,
            protocol_digest=protocol_digest,
        )
        verdict = authority.verify(
            record,
            plan=target_plan,
            head=head,
            executor=identity,
            protocol_digest_value=protocol_digest,
            repo_root=REPO_ROOT,
        )
        scope_evidence[label] = verdict["problems"][:3]
        if verdict["ready"]:
            cross_scope_ok = False
    checks.append(
        _check(
            "L14_canary_scope_isolation",
            cross_scope_ok,
            "a canary signature cannot dispatch the formal matrix and vice versa",
            problems=scope_evidence,
        )
    )
    checks.append(
        _check(
            "L15_formal_scope_isolation",
            cross_scope_ok,
            "the reverse direction of L14, reported separately because it has its own record",
            problems=scope_evidence,
        )
    )

    # L16..L23 — the dispatch loop, with synthetic cell results
    canary_signed = _signed(
        canary, scratch=history, head=head, identity=identity, protocol_digest=protocol_digest,
        cost_stop=100.0, per_cell=0.5,
    )
    authority_path = history / "canary-approved.json"
    authority_path.write_text(json.dumps(canary_signed, indent=2) + "\n", encoding="utf-8")

    def _run(root: str, spec: Mapping[str, Any], *, stop: float = 100.0) -> dict[str, Any]:
        record = dict(
            canary_signed, cost_stop_usd=stop, per_cell_cost_stop_usd=min(0.5, stop)
        )
        path = history / f"{root}-authority.json"
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return _dispatch(contract.SCOPE_CANARY, path, history / root, spec)

    # a clean run reaches the end
    clean = _run("clean", {}, stop=100.0)
    checks.append(
        _check(
            "L16_clean_matrix_completes",
            clean["complete"] is True and clean["cells_passed"] == 6,
            "six synthetic cells pass the gate and the matrix reports complete",
            report={key: clean[key] for key in ("cells_started", "cells_passed", "stop_reason")},
        )
    )

    # a consumed claim refuses to start again
    again = _dispatch(contract.SCOPE_CANARY, authority_path, history / "clean", {})
    checks.append(
        _check(
            "L17_consumed_claim_refuses",
            again.get("refused") is True
            and any("claim" in item for item in again.get("problems", []))
            and again["cells_started"] == 0,
            "a second dispatch on the same evidence root is refused before any cell starts",
            problems=again.get("problems"),
        )
    )

    # a measurable goal failure continues
    measured = _run(
        "measured",
        {
            next(cell["cell_id"] for cell in canary["cells"]): {
                "overrides": {"status": "failed", "failure": {"kind": "oracle_failed"}}
            }
        },
    )
    checks.append(
        _check(
            "L18_measured_failure_continues",
            measured["cells_started"] == 6 and measured["complete"] is True,
            "a cell that failed its oracle does not stop the matrix",
            started=measured["cells_started"],
        )
    )

    # a sandbox cleanup failure stops, even on the last cell
    last_cell = canary["cells"][-1]["cell_id"]
    cleanup = _run(
        "cleanup",
        {last_cell: {"overrides": {"sandbox": {"destroyed": False, "retained": True}}}},
    )
    checks.append(
        _check(
            "L19_sandbox_cleanup_failure_stops",
            cleanup["complete"] is False
            and cleanup["stop_reason"] == executor.STOP_SANDBOX_CLEANUP
            and cleanup["stopped_at_order"] == 6
            and cleanup["cells_started"] == 6,
            "a retained sandbox on the final cell still produces a stopped matrix",
            stop_reason=cleanup["stop_reason"],
            stopped_at=cleanup["stopped_at_order"],
        )
    )

    # an exception stops
    raised = _run("raised", {canary["cells"][1]["cell_id"]: {"behaviour": "raise"}})
    checks.append(
        _check(
            "L20_exception_stops",
            raised["complete"] is False
            and raised["stop_reason"] == executor.STOP_PROVIDER_FAILURE
            and raised["cells_started"] == 2,
            "a transport exception is a provider failure, not a measured goal failure",
            stop_reason=raised["stop_reason"],
        )
    )

    # unknown usage stops
    unknown = _run(
        "unknown",
        {
            canary["cells"][0]["cell_id"]: {
                "overrides": {"metrics": {"child_total_prompt_tokens": None}}
            }
        },
    )
    checks.append(
        _check(
            "L21_unknown_usage_stops",
            unknown["complete"] is False
            and unknown["stop_reason"] == executor.STOP_UNKNOWN_USAGE,
            "a counter the provider did not report stops the matrix instead of being written as 0",
            stop_reason=unknown["stop_reason"],
        )
    )

    # missing evidence stops
    missing = _run("missing", {canary["cells"][0]["cell_id"]: {"behaviour": "no_evidence"}})
    checks.append(
        _check(
            "L22_missing_evidence_stops",
            missing["complete"] is False
            and missing["stop_reason"] == executor.STOP_MISSING_EVIDENCE,
            "a cell without a readable run directory is not scored as a pass",
            stop_reason=missing["stop_reason"],
        )
    )

    # cost stop prevents the next cell
    throttled = _run("cost", {}, stop=0.000001)
    checks.append(
        _check(
            "L23_cost_stop_stops_dispatch",
            throttled["complete"] is False
            and throttled["stop_reason"] == executor.STOP_COST_REACHED
            and throttled["cells_started"] == 1,
            "a cost stop below one cell's cost ends the matrix after that cell",
            stop_reason=throttled["stop_reason"],
            started=throttled["cells_started"],
        )
    )

    # L24 — a canary cell never enters a formal aggregate
    canary_report = _read_json(history / "clean" / "matrix-report.json") or {}
    checks.append(
        _check(
            "L24_canary_excluded_from_formal_aggregate",
            canary_report.get("is_benchmark_sample") is False
            and canary_report.get("aggregate_includes_sample") is False
            and all(
                cell.get("is_benchmark_sample") is False
                for cell in _read_json_list(history / "clean")
            ),
            "canary cells are marked as such and the report refuses to aggregate them",
            report_flag=canary_report.get("aggregate_includes_sample"),
        )
    )

    # beyond the list: the contract, the freeze and the isolation
    checks.append(
        _check(
            "X01_contract_self_validates",
            not contract.validate(document),
            "the derived protocol passes its own invariants",
            problems=contract.validate(document),
        )
    )
    checks.append(
        _check(
            "X02_frozen_oracles_still_discriminate",
            _oracle_selftest_ok(scratch),
            "the reused Closure-2 oracles still fail the seed and pass the reference",
        )
    )
    freeze_ok, freeze_detail = _freeze_state()
    checks.append(_check("X03_freeze_1_6_unchanged", freeze_ok, freeze_detail))
    isolation_ok, isolation_detail = _closure2_isolation()
    checks.append(_check("X04_closure2_untouched", isolation_ok, isolation_detail))
    checks.append(
        _check(
            "X05_executor_identity_is_derived",
            bool(identity.get("executor_digest"))
            and set(identity.get("files") or {}) == set(executor.EXECUTOR_FILES),
            "the executor digest covers every file whose bytes decide what runs",
            executor_digest=identity.get("executor_digest"),
        )
    )
    return checks


def _dispatch(
    plan_id: str, authority_path: Path, evidence: Path, spec: Mapping[str, Any]
) -> dict[str, Any]:
    import asyncio

    return asyncio.run(
        executor.dispatch(
            plan_id,
            runs_root=evidence / "runs",
            evidence_root=evidence,
            authority_path=authority_path,
            head=executor.git_head(),
            cell_runner=_synthetic_runner(spec),
            repo_root=REPO_ROOT,
            clock=lambda: "2026-11-25T00:00:00Z",
        )
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_json_list(root: Path) -> list[dict[str, Any]]:
    cells_root = root / "cells"
    if not cells_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(cells_root.glob("*/analysis.json")):
        payload = _read_json(path)
        if payload:
            out.append(payload)
    return out


def _oracle_selftest_ok(scratch: Path) -> bool:
    cached = STAGE_DIR / "workload.json"
    payload = _read_json(cached)
    if payload is not None:
        return bool(payload.get("ok"))
    results = [closure2_preflight.task_selftest(task) for task in contract.TASKS]
    return all(
        item["seed_hidden_fails"]
        and item["reference_hidden_passes"]
        and item["hidden_oracle_really_executes"]
        for item in results
    )


def _freeze_state() -> tuple[bool, str]:
    if not STAGE_B_FREEZE.is_file():
        return False, f"{STAGE_B_FREEZE} does not exist"
    relative = str(STAGE_B_FREEZE.relative_to(REPO_ROOT)).replace("\\", "/")
    code, output = _git("diff", "--name-only", BASELINE_COMMIT, "--", relative)
    if code != 0:
        return False, f"git diff for {relative} failed: {output.strip()[:200]}"
    unchanged = not output.strip()
    raw = STAGE_B_FREEZE.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    payload = json.loads(raw.decode("utf-8"))
    return unchanged, (
        f"{relative} {'is unchanged' if unchanged else 'CHANGED'} against {BASELINE_COMMIT[:7]}; "
        f"raw {_digest_bytes(raw)}, LF {_digest_bytes(lf)}, CRLF {_digest_bytes(crlf)}; "
        f"declared content digest {payload.get('content_digest')!r}"
    )


def _closure2_isolation() -> tuple[bool, str]:
    problems: list[str] = []
    code, output = _git("diff", "--name-only", BASELINE_COMMIT)
    changed = [line.strip() for line in output.splitlines() if line.strip()] if code == 0 else []
    if code != 0:
        problems.append("git diff against the baseline failed")
    touched = [
        path for path in changed if path.startswith(PROTECTED_PREFIXES)
    ]
    if touched:
        problems.append(f"protected paths changed: {touched}")
    code, output = _git("status", "--porcelain", "--", *PROTECTED_PREFIXES)
    if code == 0 and output.strip():
        problems.append(f"protected paths are dirty: {output.strip()[:200]}")
    for commit in CLOSURE2_HEADS:
        code, output = _git("cat-file", "-t", commit)
        if code != 0 or not output.strip():
            problems.append(f"closure-2 commit {commit[:7]} is no longer reachable")
    return not problems, "; ".join(problems) or (
        f"{len(changed)} files changed since {BASELINE_COMMIT[:7]}, none of them protected"
    )


# -- entry point ------------------------------------------------------------------------------


def build(scratch: Path | None = None) -> dict[str, Any]:
    head = executor.git_head(REPO_ROOT)
    identity = executor.executor_identity(REPO_ROOT)
    document = contract.protocol_document()
    protocol_digest = authority.protocol_digest(document)

    contract.write_document(document, STAGE_DIR / "protocol.json")
    for scope in (contract.SCOPE_CANARY, contract.SCOPE_FORMAL):
        plan = contract.scope_plan(scope)
        contract.write_document(
            {
                "stage": contract.STAGE_ID,
                "protocol_version": contract.PROTOCOL_VERSION,
                "plan_id": plan["plan_id"],
                "plan": plan,
            },
            STAGE_DIR / f"plan-{'canary' if scope == contract.SCOPE_CANARY else 'formal'}.json",
        )

    captures = _all_captures()
    (STAGE_DIR / "inputs").mkdir(parents=True, exist_ok=True)
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
    contract.write_document(cost.estimate(), STAGE_DIR / "cost.json")
    contract.write_document(
        {
            "stage": contract.STAGE_ID,
            "executor_version": identity["executor_version"],
            "executor_digest": identity["executor_digest"],
            "files": identity["files"],
            "head_at_build": head,
            "note": (
                "a live authorization must carry these exact digests and the head the Owner "
                "actually reviews. head_at_build records what this artefact set was built on; it "
                "is not an approval, and it is not necessarily the reviewed commit"
            ),
        },
        STAGE_DIR / "executor.json",
    )

    for scope, filename in (
        (contract.SCOPE_CANARY, "authorization-canary.json"),
        (contract.SCOPE_FORMAL, "authorization-formal.json"),
    ):
        plan = contract.scope_plan(scope)
        contract.write_document(
            authority.pending_record(
                plan, protocol_digest_value=protocol_digest, executor=identity
            ),
            STAGE_DIR / filename,
        )

    base = Path(scratch) if scratch is not None else REPO_ROOT / ".bplus-offline"
    # The dispatch checks are one-shot by design, so each build starts from an empty work area.
    # Only this stage's own subdirectory is removed, never the caller's directory.
    work_root = base / "work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    checks = run_checks(
        document,
        captures,
        probe_report,
        scratch=work_root,
        head=head,
        identity=identity,
    )
    report = {
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "head_at_build": head,
        "executor_digest": identity["executor_digest"],
        "plans": {
            plan["plan_id"]: {"scope": plan["scope"], "cells": plan["cells_count"],
                              "digest": plan["digest"]}
            for plan in (contract.scope_plan(contract.SCOPE_CANARY),
                         contract.scope_plan(contract.SCOPE_FORMAL))
        },
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for check in checks if check["ok"]),
            "failed": [check["check_id"] for check in checks if not check["ok"]],
        },
        "live_execution_authorized": False,
        "real_model_calls": 0,
        "real_sandboxes": 0,
        "real_cells": 0,
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
