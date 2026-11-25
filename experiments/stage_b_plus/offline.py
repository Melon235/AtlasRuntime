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


def _live_path_proof(scratch: Path) -> tuple[bool, str, dict[str, Any]]:
    """One cell through the production runner: fixture, sandbox, export, sandboxed oracle.

    The model and the sandbox are fakes; every step between them is the production code, including
    the git-staged per-cell fixture, the real ``run_goal``, the export identity check and the oracle
    that runs in its own sandbox. The fake sandbox executes on the Host, so this proves control flow
    and file plumbing, never isolation.
    """

    import asyncio

    from experiments.stage_b_plus import probe as probe_module
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    cell = contract.canary_cells()[2]  # C2: coding worker, native exposure
    arm = contract.ARMS_BY_ID[cell["arm_id"]]
    scripted = probe_module.ScriptedProvider(
        main_script=[lambda _messages: [probe_module.TextBlock(text="REPORT: nothing to do.")]],
        child_scripts={},
    )
    config = executor.RunnerConfig(
        llm_provider="fake",
        llm_factory=lambda: scripted,
        sandbox_provider_name="fake",
        sandbox_factory=lambda root: FakeSandboxProvider(root),
        require_real_sandbox=False,
        main_max_turns=2,
    )
    runner = executor.live_cell_runner(config)
    plan = contract.scope_plan(contract.SCOPE_CANARY)
    context = {
        "plan": plan,
        "runs_root": scratch / "live-proof" / "runs",
        "evidence_root": scratch / "live-proof" / "evidence",
    }

    async def _await_run() -> executor.CellRun:
        return await runner(cell, context)

    run = asyncio.run(_await_run())
    session = run.session or {}
    controls = session.get("delegation_controls") or {}
    exposure = (controls.get("exposure") or {}).get("level")
    profiles = list(controls.get("profiles") or {})
    usage = executor.validate_usage(session.get("metrics"))
    fixture = run.fixture or {}
    oracle = run.oracle or {}
    export = run.export or {}

    # The producer's CellRun goes straight into the real audit. This is the seam v2 was missing:
    # the proof checked the runner's output field by field and never asked the gate to read it.
    outcome = executor.audit_cell(
        cell=cell,
        run=run,
        plan=plan,
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
    )
    ok = bool(
        run.error is None
        and exposure == arm.exposure
        and profiles == [arm.profile_id]
        and usage["ok"]
        and fixture.get("matches_frozen_seed") is True
        and fixture.get("commit")
        and export.get("workspace_path")
        and export.get("workspace_digest")
        and export.get("directory_digest")
        and export.get("workspace_digest") != export.get("directory_digest")
        and export.get("archive_digest_verifiable") is False
        and oracle.get("execution_mode") == "sandbox"
        and oracle.get("status") in {"PASS", "FAIL"}
        and oracle.get("sandbox_id")
        and not oracle.get("cleanup_failure")
        # The fake sandbox runs on the Host, so the isolation gate must refuse it — and that is the
        # only stop it may produce. Export and public-test checks must stay silent.
        and set(outcome["stops"]) == {executor.STOP_HOST_ISOLATION}
        and outcome["public_test_status"] == "NOT_RUN"
        and outcome["goal_correctness"] is not True
    )
    detail = (
        f"one cell through the live runner and the real audit for {cell['cell_id']}: "
        f"fixture_commit={str(fixture.get('commit'))[:8]} "
        f"export_files={export.get('workspace_files')} "
        f"oracle={oracle.get('status')} in {oracle.get('execution_mode')} "
        f"(sandbox {oracle.get('sandbox_id')}), "
        f"audit_stops={outcome['stops'] or 'none'} (isolation only: the fake sandbox is not "
        f"isolated), public_test={outcome['public_test_status']} "
        f"goal={outcome['goal_correctness']}, usage_ok={usage['ok']}"
    )
    evidence = {
        "cell_id": cell["cell_id"],
        "arm": {"profile": arm.profile_id, "exposure": arm.exposure},
        "applied_exposure": exposure,
        "applied_profiles": profiles,
        "effective_max_turns": (controls.get("profiles") or {}).get(arm.profile_id, {}).get(
            "effective_max_turns"
        ),
        "fixture": {
            "path_is_per_cell": cell["cell_id"] in str(fixture.get("path")),
            "commit": fixture.get("commit"),
            "seed_digest": fixture.get("seed_digest"),
            "matches_frozen_seed": fixture.get("matches_frozen_seed"),
            "files": fixture.get("files"),
        },
        "export": {
            "workspace_files": export.get("workspace_files"),
            "archive_digest": export.get("workspace_digest"),
            "directory_digest": export.get("directory_digest"),
            "directory_files": export.get("directory_files"),
            "archive_digest_verifiable": export.get("archive_digest_verifiable"),
            "patch_digest": export.get("patch_digest"),
        },
        "oracle": {
            "status": oracle.get("status"),
            "kind": oracle.get("kind"),
            "execution_mode": oracle.get("execution_mode"),
            "sandbox_id": oracle.get("sandbox_id"),
            "cleanup_failure": oracle.get("cleanup_failure"),
        },
        "audit": {
            "verdict": outcome["verdict"],
            "stops": outcome["stops"],
            "execution_integrity": outcome["execution_integrity"],
            "public_test_status": outcome["public_test_status"],
            "public_test_runs": outcome["public_test_runs"],
            "goal_correctness": outcome["goal_correctness"],
            "oracle_passed": outcome["oracle_passed"],
            "agent_success": outcome["agent_success"],
        },
        "usage": usage["values"] if usage["ok"] else usage["problems"],
        "run_dir": str(run.run_dir) if run.run_dir is not None else None,
        "note": "fake model + fake sandbox; control flow is production, isolation is not claimed",
    }
    return ok, detail, evidence


def _ladder_from_live_evidence(run_dir: str | None) -> tuple[bool, str, dict[str, Any]]:
    """Rebuild L1-L6 from a real run directory, with the Stage B analyzer, unmodified."""

    from experiments.stage_benchmark import reconstruct

    if not run_dir or not (Path(run_dir) / "session.json").is_file():
        return False, "the live-proof run directory is missing", {}
    record = reconstruct.reconstruct(Path(run_dir))
    levels = (record.get("delegation") or {}).get("levels") or {}
    met = levels.get("met") or {}
    expected = {name for name, _ in contract.DELEGATION_LEVELS}
    ok = set(met) == expected and levels.get("level_reached") is not None
    return ok, (
        f"the Stage B analyzer rebuilds {len(met)} levels from a live-runner run directory "
        f"(level_reached={levels.get('level_reached')})"
    ), {
        "levels": sorted(met),
        "level_reached": levels.get("level_reached"),
        "proposals": (record.get("delegation") or {}).get("admission", {}).get("proposals"),
    }


def _context_metrics_from_live_evidence(
    run_dir: str | None, *, independent_oracle: str | None = None
) -> tuple[bool, str, dict[str, Any]]:
    """Every Phase D counter, rebuilt from a live-runner run directory and re-validated here.

    The numbers come from the Stage B analyzer, not from this module. This check asserts they are
    *present* (an unknown counter must surface as null, so a null is a failed check and never a
    zero), that the usage identity still holds under the executor's own validator, that a cost
    follows from them, and that the delegation outcome is named rather than collapsed into
    "nothing happened": ``NO_PROPOSAL`` is not a Router rejection and must not read as one.

    The session's own verdict is the *in-run* verification. It is never reported as the independent
    oracle: in Stage B+ the oracle is a second sandbox that scores the exported workspace, and its
    verdict is passed in separately so the two cannot be read as one number.
    """

    from experiments.stage_benchmark import reconstruct

    if not run_dir or not (Path(run_dir) / "session.json").is_file():
        return False, "the live-proof run directory is missing", {}
    record = reconstruct.reconstruct(Path(run_dir))
    context = record.get("context") or {}
    usage = record.get("usage") or {}
    verification = record.get("verification") or {}
    in_run_verification = verification.get("oracle")
    admission = (record.get("delegation") or {}).get("admission") or {}
    required = (
        "main_peak_context_tokens",
        "main_cumulative_input_tokens",
        "main_cache_hit_tokens",
        "main_cache_miss_tokens",
        "child_total_prompt_tokens",
        "completion_tokens",
        "system_total_tokens",
        "handoff_payload_chars",
    )
    missing = [name for name in required if context.get(name) is None]
    metrics = {
        "main_cumulative_input_tokens": usage.get("main_input_tokens"),
        "main_completion_tokens": usage.get("main_completion_tokens"),
        "child_total_prompt_tokens": usage.get("child_prompt_tokens"),
        "child_completion_tokens": usage.get("child_completion_tokens"),
        "system_total_tokens": context.get("system_total_tokens"),
    }
    usage_report = executor.validate_usage(metrics)
    modeled_cost = executor.cell_cost(metrics) if usage_report["ok"] else None
    state = str(admission.get("state") or "")
    named_states = {"NO_PROPOSAL", "ADMITTED", "REJECTED", "ROUTER_RESULT_UNKNOWN"}
    per_proposal = set(admission.get("by_state") or {}) - set(reconstruct.PROPOSAL_STATES)
    ok = bool(
        not missing
        and usage_report["ok"]
        and usage.get("sum_matches_system_total") is True
        and modeled_cost is not None
        and in_run_verification in {"PASS", "FAIL"}
        and independent_oracle in {"PASS", "FAIL"}
        and state in named_states
        and not per_proposal
        and (state == "NO_PROPOSAL") == (int(admission.get("proposals") or 0) == 0)
    )
    detail = (
        f"the analyzer exposes every Phase D counter from the live-runner evidence "
        f"(peak={context.get('main_peak_context_tokens')}, "
        f"cache={context.get('main_cache_hit_tokens')}/{context.get('main_cache_miss_tokens')}, "
        f"handoff={context.get('handoff_payload_chars')} chars, "
        f"system={context.get('system_total_tokens')}, "
        f"cost={'unknown' if modeled_cost is None else round(modeled_cost, 6)}, "
        f"in-run verification={in_run_verification}, independent oracle={independent_oracle}, "
        f"delegation={state})"
    )
    evidence = {
        "context": dict(context),
        "usage": dict(usage),
        "modeled_cost_usd": None if modeled_cost is None else round(modeled_cost, 6),
        "in_run_verification": in_run_verification,
        "independent_oracle": independent_oracle,
        "delegation_state": state,
        "admission": dict(admission),
        "missing": missing,
        "usage_problems": usage_report["problems"],
    }
    return ok, detail, evidence


def _public_test_agreement(run_dir: str | None) -> tuple[bool, str, dict[str, Any]]:
    """The gate's public-test reader and the Stage B analyzer must agree on the same session.

    ``experiments/stage_benchmark`` is protected, so the rule cannot be shared by import. This check
    is the substitute: both readers are applied to one real session and must reach the same verdict.
    """

    from experiments.stage_benchmark import reconstruct

    if not run_dir or not (Path(run_dir) / "session.json").is_file():
        return False, "the live-proof run directory is missing", {}
    record = reconstruct.reconstruct(Path(run_dir))
    session = json.loads((Path(run_dir) / "session.json").read_text(encoding="utf-8"))
    mine = executor.public_test_verdict(session.get("public_tests"))
    theirs = record["verification"]
    same_statuses = list(theirs.get("public_test_statuses") or []) == mine["statuses"]
    same_pass = (theirs.get("public_tests_passed") is True) == (mine["passed"] is True)
    ok = same_statuses and same_pass
    return ok, (
        f"the gate and the Stage B analyzer read {mine['runs']} public run(s) the same way "
        f"(state={mine['state']}, statuses={mine['statuses']})"
    ), {
        "gate": mine,
        "analyzer": {
            "statuses": theirs.get("public_test_statuses"),
            "passed": theirs.get("public_tests_passed"),
        },
        "same_statuses": same_statuses,
        "same_pass": same_pass,
    }


def _admission_checks(scratch: Path) -> list[dict[str, Any]]:
    """Task A/B: the admission contract, proved on the real preflight and the real options.

    Nothing here calls a model or creates a sandbox. ``run_live_preflight`` reads configuration and
    the filesystem; the fixtures are Host-side git repositories in a throwaway directory.
    """

    import dataclasses

    from atlasos.app.goal_verification import ORACLE_MODE_EXTERNAL, GoalAcceptance
    from atlasos.app.live_preflight import run_live_preflight

    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    work = scratch / "admission"
    executor.remove_tree(work)
    work.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    report = executor.run_admission_preflight(plan, scratch_root=work / "arms")
    contract_failures = list(report["contract_blocking_failures"])
    per_arm = [
        {
            "cell_id": item["cell_id"],
            "status": item["status"],
            "checks": item["checks"],
            "blocking": item["blocking_failures"],
            "profile": item["worker_profile"],
            "exposure": item["worker_exposure"],
            "provider_factory_injected": item["provider_factory_injected"],
        }
        for item in report["cells"]
    ]
    ready = sum(1 for item in per_arm if item["status"] == "READY")
    checks.append(
        _check(
            "L26_six_arm_production_admission",
            not contract_failures
            and len(per_arm) == len(plan["cells"])
            and all(item["provider_factory_injected"] is False for item in per_arm),
            f"{ready}/6 arms READY against the real run_live_preflight; contract failures="
            f"{contract_failures or 'none'}; credential_present={report['credential_present']}",
            cells=per_arm,
            credential_present=report["credential_present"],
            status=report["status"],
        )
    )

    cell = plan["cells"][2]
    fixture = executor.prepare_fixture(
        contract.TASKS_BY_ID[cell["task_id"]], work / "repro-fixture"
    )
    options = executor.production_goal_options(
        cell,
        evidence_root=work / "repro-evidence",
        fixture_dir=work / "repro-fixture",
        config=executor.RunnerConfig(),
        run_id="repro",
    )
    acceptance = options.acceptance

    # The exact shape v1 shipped: a static acceptance with no dynamic command, declared as in-run.
    v1_acceptance = GoalAcceptance(
        spec=acceptance.spec, dynamic_command=None, require_dynamic=False
    )
    v1_report = run_live_preflight(dataclasses.replace(options, acceptance=v1_acceptance))
    checks.append(
        _check(
            "L27_v1_admission_refusal_reproduced",
            v1_report["status"] == "BLOCKED"
            and set(v1_report["blocking_failures"])
            == {"public_tests_separate_from_oracle", "acceptance_configured"},
            "the v1 contract shape is still refused on exactly the two checks that stopped the "
            "real canary",
            blocking_failures=v1_report["blocking_failures"],
        )
    )

    external = acceptance.external_oracle
    hidden = set(getattr(external, "hidden_paths", ()) or ())
    public = set(getattr(external, "public_paths", ()) or ())
    staged = sorted(name for name in hidden if (Path(str(fixture["path"])) / name).exists())
    checks.append(
        _check(
            "L28_external_oracle_contract_is_bound",
            acceptance.oracle_mode == ORACLE_MODE_EXTERNAL
            and external is not None
            and not external.problems()
            and external.declared_digest == external.contract_digest
            and bool(hidden)
            and not (hidden & public)
            and not staged,
            f"oracle={getattr(external, 'oracle_id', None)} "
            f"digest={str(getattr(external, 'declared_digest', ''))[:23]}…; hidden and public "
            f"artifacts are disjoint and no hidden file is staged in the fixture",
            oracle_mode=acceptance.oracle_mode,
            hidden=sorted(hidden),
            public=sorted(public),
            staged=staged,
        )
    )

    def _blocked(candidate: Any) -> set[str]:
        outcome = run_live_preflight(dataclasses.replace(options, acceptance=candidate))
        return set(outcome["blocking_failures"]) if outcome["status"] == "BLOCKED" else set()

    assert external is not None  # the production contract declares one; L28 asserts it

    missing_contract = _blocked(
        GoalAcceptance(
            spec=acceptance.spec, oracle_mode=ORACLE_MODE_EXTERNAL, external_oracle=None
        )
    )
    overlap = _blocked(
        GoalAcceptance(
            spec=acceptance.spec,
            oracle_mode=ORACLE_MODE_EXTERNAL,
            external_oracle=dataclasses.replace(
                external, hidden_paths=tuple(sorted(public)) or ("test_public.py",)
            ),
        )
    )
    tampered = _blocked(
        GoalAcceptance(
            spec=acceptance.spec,
            oracle_mode=ORACLE_MODE_EXTERNAL,
            external_oracle=dataclasses.replace(external, declared_digest="sha256:" + "0" * 64),
        )
    )
    no_digest = _blocked(
        GoalAcceptance(
            spec=dataclasses.replace(acceptance.spec, declared_digest=None),
            oracle_mode=ORACLE_MODE_EXTERNAL,
            external_oracle=external,
        )
    )
    checks.append(
        _check(
            "L29_external_oracle_negative_controls",
            "acceptance_configured" in missing_contract
            and "public_tests_separate_from_oracle" in overlap
            and "acceptance_configured" in tampered
            and "acceptance_digest_declared" in no_digest,
            "a missing external contract, an overlapping hidden suite, a tampered oracle digest "
            "and an undeclared verification spec are each refused",
            missing=sorted(missing_contract),
            overlap=sorted(overlap),
            tampered=sorted(tampered),
            no_digest=sorted(no_digest),
        )
    )

    in_run = run_live_preflight(
        dataclasses.replace(
            options,
            acceptance=GoalAcceptance(
                spec=acceptance.spec,
                dynamic_command="python3 -m pytest -q " + " ".join(sorted(public)),
            ),
        )
    )
    in_run_missing = _blocked(
        GoalAcceptance(spec=acceptance.spec, dynamic_command=None, require_dynamic=False)
    )
    checks.append(
        _check(
            "L30_in_run_oracle_contract_unchanged",
            in_run["status"] == "READY"
            and "public_tests_separate_from_oracle" in in_run_missing
            and "acceptance_configured" in in_run_missing,
            "the stage-P in-run contract still passes when its command is configured, and is still "
            "refused when it is absent",
            in_run_blocking=in_run["blocking_failures"],
        )
    )

    with executor.claim_root_override(work / "claims"):
        claim = executor.claim_path(plan["digest"])
        before = claim.exists()
        executor.run_admission_preflight(plan, scratch_root=work / "gate")
        after = claim.exists()
    checks.append(
        _check(
            "L31_admission_consumes_no_claim",
            not before and not after,
            "a full six-arm admission pass writes no claim, so a refused configuration still "
            "leaves the single attempt intact",
        )
    )
    return checks


def _evidence_contract_checks(scratch: Path) -> list[dict[str, Any]]:
    """The producer/consumer contract that v2 got wrong: export identity and public-test evidence.

    Each case feeds a real ``CellRun`` into the real ``audit_cell`` and reads the gate's verdict. No
    model, no sandbox, no oracle: the fixture, the export and the session are real bytes on disk.
    """

    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    work = scratch / "evidence-contract"
    executor.remove_tree(work)
    work.mkdir(parents=True, exist_ok=True)
    cell = plan["cells"][0]
    checks: list[dict[str, Any]] = []

    def _audit(candidate: executor.CellRun) -> dict[str, Any]:
        return executor.audit_cell(
            cell=cell,
            run=candidate,
            plan=plan,
            usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
            cost_before=0.0,
        )

    healthy = synthetic_cell_run(cell, work / "healthy")
    export = healthy.export or {}
    checks.append(
        _check(
            "L32_export_identity_is_two_records",
            bool(export.get("workspace_digest"))
            and bool(export.get("directory_digest"))
            and export.get("workspace_digest") != export.get("directory_digest")
            and export.get("archive_digest_verifiable") is False,
            "the producer's archive digest and the host's directory digest are recorded "
            "separately and are never compared",
            archive_digest=export.get("workspace_digest"),
            directory_digest=export.get("directory_digest"),
        )
    )

    healthy_outcome = _audit(healthy)
    checks.append(
        _check(
            "L33_healthy_cell_passes_the_evidence_checks",
            healthy_outcome["stops"] == []
            and healthy_outcome["execution_integrity"] == "PASS"
            and healthy_outcome["public_test_status"] == "PASS"
            and healthy_outcome["goal_correctness"] is True,
            "a production-shaped cell passes export, public-test and oracle checks together",
            stops=healthy_outcome["stops"],
            public_test=healthy_outcome["public_test_status"],
            goal=healthy_outcome["goal_correctness"],
        )
    )

    mutated = synthetic_cell_run(cell, work / "mutated")
    target = Path(str((mutated.export or {})["workspace_path"])) / "pipeline" / "parsing.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# changed after export\n", "utf-8")
    mutated_outcome = _audit(mutated)
    checks.append(
        _check(
            "L34_changed_export_directory_stops",
            executor.STOP_EXPORT in mutated_outcome["stops"],
            "a workspace that changed after the host recorded its identity is refused",
            stops=mutated_outcome["stops"],
        )
    )

    miscounted = synthetic_cell_run(cell, work / "miscounted")
    assert miscounted.session is not None
    miscounted.session["artifacts"]["workspace_files"] = 999
    miscounted_outcome = _audit(miscounted)
    checks.append(
        _check(
            "L35_export_file_count_mismatch_stops",
            executor.STOP_EXPORT in miscounted_outcome["stops"],
            "a recorded file count that the directory does not hold is refused",
            stops=miscounted_outcome["stops"],
        )
    )

    suite: dict[str, Any] = {}
    for behaviour in (
        "public_fail_then_pass",
        "public_pass_then_fail",
        "public_not_run",
        "public_error",
    ):
        outcome = _audit(synthetic_cell_run(cell, work / behaviour, behaviour=behaviour))
        suite[behaviour] = {
            "state": outcome["public_test_status"],
            "goal": outcome["goal_correctness"],
            "verdict": outcome["verdict"],
        }
    checks.append(
        _check(
            "L36_public_test_verdict_reads_the_runs",
            suite["public_fail_then_pass"]["state"] == "PASS"
            and suite["public_fail_then_pass"]["goal"] is True
            and suite["public_pass_then_fail"]["state"] == "FAIL"
            and suite["public_pass_then_fail"]["goal"] is False
            and suite["public_not_run"]["state"] == "NOT_RUN"
            and suite["public_not_run"]["goal"] is None
            and suite["public_error"]["state"] == "ERROR"
            and suite["public_error"]["goal"] is None,
            "the verdict is the last recorded run: an earlier pass cannot hide a later fail, and "
            "NOT_RUN / ERROR are never a pass",
            outcomes=suite,
        )
    )

    negative: dict[str, Any] = {}
    for behaviour in (
        "public_missing_last_status",
        "public_corrupt_status",
        "public_identity_mismatch",
        "public_exit_contradiction",
    ):
        outcome = _audit(synthetic_cell_run(cell, work / behaviour, behaviour=behaviour))
        negative[behaviour] = {
            "state": outcome["public_test_status"],
            "stops": outcome["stops"],
            "goal": outcome["goal_correctness"],
        }
    checks.append(
        _check(
            "L38_public_test_evidence_negative_controls",
            all(item["state"] in {"INCOMPLETE", "INVALID"} for item in negative.values())
            and all(executor.STOP_MISSING_EVIDENCE in item["stops"] for item in negative.values())
            and all(item["goal"] is not True for item in negative.values()),
            "a counted call with no record, a corrupt status, a foreign suite identity and a PASS "
            "contradicted by its exit code are each refused and never scored",
            outcomes=negative,
        )
    )

    diverged = synthetic_cell_run(cell, work / "diverged")
    assert diverged.export is not None
    diverged.export["workspace_digest"] = "sha256:" + "e" * 64
    diverged_outcome = _audit(diverged)
    moved = synthetic_cell_run(cell, work / "moved")
    assert moved.export is not None
    moved.export["workspace_path"] = str(work / "elsewhere")
    moved_outcome = _audit(moved)
    checks.append(
        _check(
            "L39_runner_and_session_export_must_agree",
            executor.STOP_EXPORT in diverged_outcome["stops"]
            and executor.STOP_EXPORT in moved_outcome["stops"],
            "an archive digest or a workspace path that differs between the runner and the "
            "session is refused",
            diverged=diverged_outcome["stops"],
            moved=moved_outcome["stops"],
        )
    )

    report = _read_json(scratch / "history" / "clean" / "matrix-report.json") or {}
    cells = report.get("cells") or []
    delta_floor = round(
        sum(item["delta_cost_usd"] for item in cells if item.get("delta_cost_usd") is not None), 6
    )
    cumulative_floor = round(
        sum(item["cost_usd"] for item in cells if item.get("cost_usd") is not None), 6
    )
    checks.append(
        _check(
            "L37_observed_cost_floor_sums_deltas",
            bool(cells)
            and report.get("observed_cost_floor_usd") == delta_floor
            and delta_floor != cumulative_floor,
            f"the observed floor is the sum of per-cell costs ({delta_floor}), not of the running "
            f"totals ({cumulative_floor})",
            reported=report.get("observed_cost_floor_usd"),
            delta_sum=delta_floor,
            cumulative_sum=cumulative_floor,
        )
    )
    return checks


# -- synthetic cells for the gate ---------------------------------------------------------------


_FIXTURE_CACHE: dict[str, dict[str, Any]] = {}


def _synthetic_fixture(task: Any, destination: Path) -> dict[str, Any]:
    """A real per-cell fixture, prepared once per task and copied, so the gate reads real bytes."""

    cached = _FIXTURE_CACHE.get(task.task_id)
    if cached is None:
        cache_dir = destination.parent / f".seed-{task.task_id}"
        cached = executor.prepare_fixture(task, cache_dir)
        _FIXTURE_CACHE[task.task_id] = cached
    executor.remove_tree(destination)
    shutil.copytree(cached["path"], destination)
    return dict(cached, path=str(destination))


def _synthetic_session(
    *, cell: Mapping[str, Any], root: Path, overrides: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
    """A session shaped like a real one, with a real export and a real per-cell fixture."""

    from experiments.evaluation.fixtures import workspace_digest

    arm = contract.ARMS_BY_ID[cell["arm_id"]]
    task = contract.TASKS_BY_ID[cell["task_id"]]
    suite_contract = contract.public_test_spec(task)
    key = f"c{int(cell['order']):02d}"
    cell_root = root / key
    run_dir = cell_root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    fixture = _synthetic_fixture(task, cell_root / "fixtures" / cell["cell_id"])
    export_dir = cell_root / "export"
    executor.remove_tree(export_dir)
    shutil.copytree(fixture["path"], export_dir, ignore=shutil.ignore_patterns(".git"))
    directory = "sha256:" + workspace_digest(export_dir)
    # The producer records an *archive* digest, which is a different object from the directory
    # hash. The synthetic value stands in for it and is deliberately not the directory digest, so a
    # regression that compares the two fails here instead of on a live cell.
    archive = "sha256:" + hashlib.sha256(
        ("synthetic-archive:" + directory).encode("utf-8")
    ).hexdigest()
    artifacts = {
        "workspace_path": str(export_dir),
        "workspace_digest": archive,
        "workspace_files": len(executor.workspace_hashes(export_dir)),
        "patch_digest": "sha256:" + "0" * 64,
        "patch_changed_files": 0,
        "patch_path": str(cell_root / "workspace.patch"),
        "workspace_size_bytes": 1024,
        "archive_digest_source": "synthetic stand-in for the producer's archive digest",
    }
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
        "run_id": f"bplus-{key}",
        "status": "succeeded",
        "execution_succeeded": True,
        "failure": None,
        "metrics": metrics,
        "artifacts": artifacts,
        "sandbox": {
            "provider": "fake",
            "provider_is_real": False,
            "created": True,
            "destroyed": True,
            "retained": False,
            "error": None,
            "network_probe": "PASS",
            "host_separation": {"ok": True},
            "export_digest": artifacts["workspace_digest"],
            "export_files": artifacts["workspace_files"],
        },
        "delegation_controls": {
            "exposure": {"level": arm.exposure},
            "profiles": {
                arm.profile_id: {
                    "profile_id": arm.profile_id,
                    "effective_max_turns": int(contract.BUDGET["worker_max_turns"]),
                }
            },
        },
        "delegation": {"batches": [], "routes": []},
        "ownership": {"denied_calls": []},
        "verification": {"verdict": "PASS", "dynamic": {"status": "PASS"}},
        "acceptance": "PASS",
        # The producer's real shape: no top-level ``status``. Only ``contract``, ``command_digest``,
        # ``calls`` and one ``runs`` entry per call — carrying the frozen suite's identity.
        "public_tests": {
            "contract": suite_contract.payload(),
            "command_digest": suite_contract.command_digest(),
            "calls": 1,
            "runs": [
                {
                    "test_id": suite_contract.test_id,
                    "command_digest": suite_contract.command_digest(),
                    "call_index": 1,
                    "status": "PASS",
                    "exit_code": 0,
                    "duration_ms": 120,
                    "output_digest": "sha256:" + "2" * 64,
                    "output_chars": 40,
                    "output_truncated": False,
                    "reason": None,
                }
            ],
            "oracle_is_separate": True,
        },
    }
    for key, value in (overrides or {}).items():
        if isinstance(value, Mapping) and isinstance(session.get(key), Mapping):
            merged = dict(session[key])
            merged.update(value)
            session[key] = merged
        else:
            session[key] = value
    (run_dir / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    return session, run_dir, fixture, artifacts


class ProviderTransportError(RuntimeError):
    """Named so the gate classifies it as a provider failure rather than an unknown exception."""


def key_of(cell: Mapping[str, Any]) -> str:
    return f"c{int(cell['order']):02d}"


def synthetic_cell_run(
    cell: Mapping[str, Any],
    root: Path,
    *,
    overrides: Mapping[str, Any] | None = None,
    behaviour: str | None = None,
) -> executor.CellRun:
    """A complete cell result, built from real bytes, for exercising the gate."""

    session, run_dir, fixture, artifacts = _synthetic_session(
        cell=cell, root=root, overrides=overrides
    )
    oracle: dict[str, Any] = {
        "status": "PASS",
        "kind": "HIDDEN_TESTS_PASSED",
        "passed": True,
        "environment_failure": False,
        "cleanup_failure": None,
        "execution_mode": "sandbox",
        "sandbox_id": f"oracle-{key_of(cell)}",
    }
    if behaviour == "oracle_error":
        oracle = dict(oracle, status="ERROR", kind="ORACLE_TIMEOUT", passed=False,
                      environment_failure=True)
    if behaviour == "oracle_cleanup_failure":
        oracle = dict(oracle, cleanup_failure="destroy refused")
    if behaviour == "oracle_fail":
        oracle = dict(oracle, status="FAIL", kind="HIDDEN_TESTS_FAILED", passed=False)

    suite = session["public_tests"]
    if behaviour in {"public_fail", "public_fail_then_pass", "public_pass_then_fail"}:
        statuses = {
            "public_fail": ["FAIL"],
            "public_fail_then_pass": ["FAIL", "PASS"],
            "public_pass_then_fail": ["PASS", "FAIL"],
        }[behaviour]
        suite["runs"] = [
            dict(suite["runs"][0], call_index=index, status=status,
                 exit_code=0 if status == "PASS" else 1)
            for index, status in enumerate(statuses, start=1)
        ]
        suite["calls"] = len(statuses)
    if behaviour == "public_not_run":
        suite["runs"] = []
        suite["calls"] = 0
    if behaviour == "public_error":
        suite["runs"] = [dict(suite["runs"][0], status="ERROR", exit_code=None)]
        suite["calls"] = 1
    if behaviour == "public_missing_last_status":
        # The reachable producer shape after a GoalSandboxError: the call was counted, no record was
        # appended, so the earlier PASS would be inherited by a reader that skips empty statuses.
        suite["runs"] = [dict(suite["runs"][0])]
        suite["calls"] = 2
    if behaviour == "public_corrupt_status":
        suite["runs"] = [dict(suite["runs"][0], status=None, exit_code=None)]
        suite["calls"] = 1
    if behaviour == "public_identity_mismatch":
        suite["runs"] = [dict(suite["runs"][0], test_id="some-other-suite")]
        suite["calls"] = 1
    if behaviour == "public_exit_contradiction":
        suite["runs"] = [dict(suite["runs"][0], status="PASS", exit_code=1)]
        suite["calls"] = 1
    (run_dir / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    if behaviour == "corrupt_evidence":
        (run_dir / "session.json").write_text("{ this is not json", encoding="utf-8")

    # What the live runner adds on top of the producer's fields: the Host identity of the exported
    # directory, recorded after the export and before the oracle. The producer's archive digest is
    # left exactly as it came.
    export = dict(artifacts)
    export.update(executor.workspace_identity(Path(str(artifacts["workspace_path"]))))
    export["archive_digest_verifiable"] = False
    return executor.CellRun(
        session=session,
        run_dir=None if behaviour == "no_evidence" else run_dir,
        fixture_root=Path(fixture["path"]),
        oracle=oracle,
        duration_s=0.01,
        fixture=fixture,
        export=export,
        sandbox_plan=executor.sandbox_plan(require_real=False, provider_name="fake").as_dict(),
        expected_run_id=f"bplus-{key_of(cell)}",
    )


def _synthetic_runner(spec: Mapping[str, Any]) -> executor.CellRunner:
    """A runner whose output the *next* cell in the plan triggers, so one dispatch can stop late."""

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        entry = spec.get(cell["cell_id"]) or {}
        behaviour = entry.get("behaviour")
        if behaviour == "raise":
            raise ProviderTransportError("scripted transport failure")
        return synthetic_cell_run(
            cell,
            Path(context["evidence_root"]) / "cells",
            overrides=entry.get("overrides"),
            behaviour=behaviour,
        )

    return run


def _live_mode_guard(history: Path) -> tuple[bool, str]:
    """A live dispatch must refuse an injected head, an injected runner and a moved claim root."""

    import asyncio

    evidence = history / "live-guard"
    evidence.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    with executor.claim_root_override(history / "claims-live-guard"):
        head_refusal = asyncio.run(
            executor.dispatch(
                contract.SCOPE_CANARY,
                runs_root=evidence / "runs",
                evidence_root=evidence / "a",
                head_override="0" * 40,
                mode=executor.MODE_LIVE,
                clock=lambda: "2026-11-25T00:00:00Z",
            )
        )
        results["head"] = {
            "stop": head_refusal.get("stop_reason"),
            "problems": head_refusal.get("problems"),
            "cells_started": head_refusal.get("cells_started"),
        }
        runner_refusal = asyncio.run(
            executor.dispatch(
                contract.SCOPE_CANARY,
                runs_root=evidence / "runs",
                evidence_root=evidence / "b",
                cell_runner=_synthetic_runner({}),
                mode=executor.MODE_LIVE,
                clock=lambda: "2026-11-25T00:00:00Z",
            )
        )
        results["runner"] = {
            "stop": runner_refusal.get("stop_reason"),
            "problems": runner_refusal.get("problems"),
        }
    ok = (
        results["head"]["stop"] == executor.STOP_MODE_VIOLATION
        and results["head"]["cells_started"] == 0
        and any("head" in str(item) for item in results["head"]["problems"] or [])
        and any("claim" in str(item) for item in results["head"]["problems"] or [])
        and results["runner"]["stop"] == executor.STOP_MODE_VIOLATION
        and any("runner" in str(item) for item in results["runner"]["problems"] or [])
    )
    detail = (
        "a live dispatch refuses an injected head and refuses to run an injected runner; the claim "
        "root override is also reported as a violation"
    )
    return ok, detail


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

    # L11 — one cell through the production runner: fixture, export, sandboxed oracle, usage
    ok, detail, evidence = _live_path_proof(scratch)
    checks.append(_check("L11_live_path_fixture_export_oracle", ok, detail, **evidence))
    ladder_ok, ladder_detail, ladder_evidence = _ladder_from_live_evidence(
        evidence.get("run_dir")
    )
    checks.append(
        _check("X06_l1_l6_rebuilt_from_live_evidence", ladder_ok, ladder_detail, **ladder_evidence)
    )
    agreement_ok, agreement_detail, agreement_evidence = _public_test_agreement(
        evidence.get("run_dir")
    )
    checks.append(
        _check(
            "X08_public_test_readers_agree",
            agreement_ok,
            agreement_detail,
            **agreement_evidence,
        )
    )
    metrics_ok, metrics_detail, metrics_evidence = _context_metrics_from_live_evidence(
        evidence.get("run_dir"),
        independent_oracle=(evidence.get("oracle") or {}).get("status"),
    )
    checks.append(
        _check(
            "X07_context_metrics_rebuilt_from_live_evidence",
            metrics_ok,
            metrics_detail,
            **metrics_evidence,
        )
    )

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
            bool(signed_canary)
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

    def _run(
        root: str,
        spec: Mapping[str, Any],
        *,
        stop: float = 100.0,
        per_cell: float | None = None,
    ) -> dict[str, Any]:
        record = dict(
            canary_signed,
            cost_stop_usd=stop,
            per_cell_cost_stop_usd=min(0.5, stop) if per_cell is None else per_cell,
        )
        path = history / f"{root}-authority.json"
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return _dispatch(
            contract.SCOPE_CANARY, path, history / root, spec, claim_dir=history / f"claims-{root}"
        )

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

    # the claim is global: a different evidence root and a different runs root still refuse
    again = _dispatch(
        contract.SCOPE_CANARY,
        authority_path,
        history / "clean-elsewhere",
        {},
        claim_dir=history / "claims-clean",
    )
    checks.append(
        _check(
            "L17_consumed_claim_refuses",
            again.get("refused") is True
            and any("claim" in item for item in again.get("problems", []))
            and again["cells_started"] == 0,
            "a second dispatch on a different evidence root is refused: the claim is per plan",
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

    # the per-cell stop is inclusive: exactly one cell's cost already stops the matrix
    one_cell_cost = next(
        cell["delta_cost_usd"] for cell in clean["cells"] if cell["delta_cost_usd"] is not None
    )
    exact = _run("cost-exact", {}, stop=100.0, per_cell=one_cell_cost)
    checks.append(
        _check(
            "L23_per_cell_cost_stop_is_inclusive",
            one_cell_cost > 0
            and exact["cells_started"] == 1
            and exact["stop_reason"] == executor.STOP_COST_REACHED,
            f"a per-cell stop of exactly {one_cell_cost:.6f} ends the matrix after one cell",
            one_cell_cost=one_cell_cost,
            stop_reason=exact["stop_reason"],
        )
    )

    # oracle cleanup failure stops
    oracle_cleanup = _run(
        "oracle-cleanup", {canary["cells"][0]["cell_id"]: {"behaviour": "oracle_cleanup_failure"}}
    )
    checks.append(
        _check(
            "L23b_oracle_cleanup_failure_stops",
            oracle_cleanup["complete"] is False
            and oracle_cleanup["stop_reason"] == executor.STOP_ORACLE_CLEANUP,
            "a retained oracle sandbox stops dispatch",
            stop_reason=oracle_cleanup["stop_reason"],
        )
    )

    # oracle environment failure stops
    oracle_env = _run(
        "oracle-env", {canary["cells"][0]["cell_id"]: {"behaviour": "oracle_error"}}
    )
    checks.append(
        _check(
            "L23c_oracle_environment_failure_stops",
            oracle_env["complete"] is False
            and oracle_env["stop_reason"] == executor.STOP_ORACLE_ENVIRONMENT,
            "an oracle that cannot produce a verdict stops dispatch",
            stop_reason=oracle_env["stop_reason"],
        )
    )

    # agent claims success, oracle says FAIL: a measured failure, not a pass and not a stop
    oracle_fail = _run(
        "oracle-fail", {canary["cells"][0]["cell_id"]: {"behaviour": "oracle_fail"}}
    )
    checks.append(
        _check(
            "L23d_agent_success_oracle_fail_is_measured",
            oracle_fail["complete"] is True
            and oracle_fail["cells"][0]["agent_success"] is True
            and oracle_fail["cells"][0]["oracle_passed"] is False
            and oracle_fail["cells"][0]["goal_correctness"] is False
            and oracle_fail["cells"][0]["executor_verdict"] == "MEASURED_FAILURE",
            "the independent oracle decides: an agent that claims success is not scored PASS",
            cell=oracle_fail["cells"][0]["executor_verdict"],
        )
    )

    # a worker turn cap that does not match the arm stops immediately
    cap = _run(
        "cap",
        {
            canary["cells"][0]["cell_id"]: {
                "overrides": {
                    "delegation_controls": {
                        "profiles": {contract.RESEARCH: {"effective_max_turns": 6}}
                    }
                }
            }
        },
    )
    checks.append(
        _check(
            "L23e_worker_cap_mismatch_stops",
            cap["complete"] is False
            and cap["stop_reason"] == executor.STOP_IDENTITY_MISMATCH
            and cap["cells_started"] == 1,
            "an effective worker cap of 6 instead of 20 stops the matrix",
            stop_reason=cap["stop_reason"],
        )
    )

    # cumulative cost stop prevents the next cell
    throttled = _run("cost", {}, stop=0.000001)
    checks.append(
        _check(
            "L24_cost_stop_stops_dispatch",
            throttled["complete"] is False
            and throttled["stop_reason"] == executor.STOP_COST_REACHED
            and throttled["cells_started"] == 1,
            "a cumulative stop below one cell's cost ends the matrix after that cell",
            stop_reason=throttled["stop_reason"],
            started=throttled["cells_started"],
        )
    )

    # unknown consumption is never filled with zero
    unknown_cost = _run(
        "unknown-cost",
        {
            canary["cells"][0]["cell_id"]: {
                "overrides": {"metrics": {"child_completion_tokens": None}}
            }
        },
    )
    checks.append(
        _check(
            "L24b_unknown_cost_is_not_zero",
            unknown_cost["cells"][0]["cost_usd"] is None
            and unknown_cost["cells"][0]["delta_cost_usd"] is None
            and unknown_cost["total_cost_usd"] is None
            and unknown_cost["observed_cost_floor_usd"] == 0.0
            and unknown_cost["usage_completeness"] == "UNKNOWN",
            "an unmeasurable cell leaves the cost unknown and reports only an observed floor",
            cell=unknown_cost["cells"][0],
        )
    )

    # a corrupted session.json stops rather than scoring a pass
    corrupt = _run("corrupt", {canary["cells"][0]["cell_id"]: {"behaviour": "corrupt_evidence"}})
    checks.append(
        _check(
            "L24c_corrupt_session_stops",
            corrupt["complete"] is False
            and corrupt["stop_reason"] == executor.STOP_MISSING_EVIDENCE,
            "an empty or unparseable session.json stops dispatch",
            stop_reason=corrupt["stop_reason"],
        )
    )

    # a live dispatch cannot be given a head, a claim root or an injected runner
    live_guard = _live_mode_guard(history)
    checks.append(_check("L24d_live_mode_refuses_injection", live_guard[0], live_guard[1]))

    # L25 — a canary cell never enters a formal aggregate
    canary_report = _read_json(history / "clean" / "matrix-report.json") or {}
    checks.append(
        _check(
            "L25_canary_excluded_from_formal_aggregate",
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

    # Task A/B — the admission contract, on the real preflight and the real options
    checks.extend(_admission_checks(scratch))

    # The producer/consumer contract that v2 got wrong: export identity and public-test evidence.
    checks.extend(_evidence_contract_checks(scratch))

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
    plan_id: str,
    authority_path: Path,
    evidence: Path,
    spec: Mapping[str, Any],
    *,
    claim_dir: Path,
) -> dict[str, Any]:
    """Run one offline proof. A per-check claim root is what makes the proofs repeatable."""

    import asyncio

    with executor.claim_root_override(claim_dir):
        return asyncio.run(
            executor.dispatch(
                plan_id,
                runs_root=evidence / "runs",
                evidence_root=evidence,
                authority_path=authority_path,
                cell_runner=_synthetic_runner(spec),
                repo_root=REPO_ROOT,
                mode=executor.MODE_OFFLINE_PROOF,
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
    executor.remove_tree(work_root)
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
