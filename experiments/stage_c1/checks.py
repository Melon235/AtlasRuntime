"""Stage C1 offline verification and artefact production. No model call, no sandbox, no claim.

``python -m experiments.stage_c1.checks [--scratch DIR]`` writes ``plan.json``,
``authorization.json``, ``executor.json`` and ``offline.json`` next to this module, and never writes
anything else in the repository.

The ten checks are exactly the Stage C1 Phase C list. Every one of them is a fact about
*this* repository at *this* commit:

C01  the P2 fixture is built from the frozen seed, and the frozen plugin interface is in it
C02  the executed goal is the frozen P2 requirement plus the one activation appendix
C03  the Main prompt is the Stage B+ prompt, byte for byte
C04  arm C3 offers ``coding_worker`` with the full card, 20 effective turns, no task-specific text
C05  P2's two workstreams are admitted by the frozen router, and the gate still refuses the shapes
     it is supposed to refuse (same script, four variants)
C06  the real ``executor.dispatch`` + ``audit_cell`` run one cell end to end on a synthetic result
C07  the plan identity recomputes from the cells, and is independent of the commit
C08  the Stage B+ plans, protocol digest and the three consumed claims did not move
C09  the new plan's claim does not exist
C10  the pending authorization authorizes nothing
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

from experiments.evaluation.closure2 import catalog as closure2
from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_b_plus.offline import synthetic_cell_run
from experiments.stage_c1 import plan as c1

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

#: Recorded values from the Stage B+ V3 artefacts at ``a55e106``. They are constants here so a
#: later edit that moves them fails a check instead of quietly re-baselining.
BPLUS_PROTOCOL_DIGEST = "sha256:65e0b993796191bf0b34ceb1c258ff9cbcd8bdbde98c4733fc39302cc9b05460"
BPLUS_CANARY_PLAN_DIGEST = "sha256:0b58ab2500325d11cf27763dd0d8dde8498d5bca9ca11f602e6e410a37e572f6"
BPLUS_FORMAL_PLAN_DIGEST = "sha256:ab4d0e509962885c9a24d981446c4697f00e794b6b03e5a5d9bf42cfe91aee72"
BPLUS_V3_EXECUTOR_DIGEST = "sha256:9f4c21cc66eb6b082ea9aec5dcbec3a4d4dda53054b155d31e7dc784f7429cda"

#: The consumed claims, by plan digest, in the order they were spent.
CONSUMED_CLAIMS: tuple[tuple[str, str], ...] = (
    ("v1", "sha256:7ae71273eea29f489edc657ada9a26c834e178289ac6744807350d674dc53575"),
    ("v2", "sha256:41fc36da78feb51a4fa8462b7901bd3f41c6573105ad0d93486bafde4ddd5046"),
    ("v3", BPLUS_CANARY_PLAN_DIGEST),
)

#: The Stage B+ V3 evidence root, kept outside the repository. Recorded as a custody reference, not
#: re-verified as a gate: a machine that does not have the run tree must not fail the preparation.
V3_EVIDENCE_ROOT = Path(r"D:\Work\AtlasRuntime-runs\bplus-canary-3")
V3_EVIDENCE_INDEX_DIGEST = "sha256:660157b3f0d194dae4e4002ef4e9d9bcdaf593b0c7b1a231c2679dca97eaec13"
V3_EVIDENCE_FILES = 476

#: Mirrors ``DelegationService._WRITE_TOOLS``: the vocabulary that decides whether a profile can
#: write at all. Stated here only to assemble routing facts for an offline check.
WRITE_TOOLS = frozenset({"file_write", "file_edit", "bash"})


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


# -- C01: fixture ---------------------------------------------------------------------------------


def fixture_report(scratch: Path) -> dict[str, Any]:
    task = c1.task()
    fixture = executor.prepare_fixture(task, scratch / "fixture")
    frozen = closure2.task_digests(task)
    leaked = sorted(set(task.oracle_tests) & set(fixture["files_list"]))
    return {
        "matches_frozen_seed": bool(fixture["matches_frozen_seed"]),
        "seed_digest": fixture["seed_digest"],
        "frozen_seed_digest": frozen["seed_digest"],
        "files": fixture["files"],
        "interface_present": "plugins/base.py" in fixture["files_list"],
        "interface_digest": _digest_bytes(
            (Path(fixture["path"]) / "plugins" / "base.py").read_bytes()
        )
        if "plugins/base.py" in fixture["files_list"]
        else None,
        "public_suite_present": "test_public.py" in fixture["files_list"],
        "hidden_files_in_fixture": leaked,
        "commit": fixture["commit"],
    }


def _check_fixture(scratch: Path) -> dict[str, Any]:
    report = fixture_report(scratch)
    ok = (
        report["matches_frozen_seed"]
        and report["seed_digest"] == report["frozen_seed_digest"]
        and report["interface_present"]
        and report["public_suite_present"]
        and not report["hidden_files_in_fixture"]
    )
    return _check(
        "C01_p2_fixture_matches_frozen_seed",
        ok,
        f"seed {report['seed_digest'][:19]}… over {report['files']} files; frozen plugin interface "
        "and public suite present, no hidden test in the workspace",
        **report,
    )


# -- C02 / C03: goal and prompt -------------------------------------------------------------------


def _check_goal() -> dict[str, Any]:
    requirement = contract.GOAL_OF[c1.TASK_ID]
    goal = c1.goal_text()
    appendix_at = goal.find(c1.ACTIVATION_INSTRUCTION)
    ok = (
        goal.startswith(requirement + c1.GOAL_SEPARATOR)
        and goal.count(c1.ACTIVATION_INSTRUCTION) == 1
        and appendix_at == len(requirement) + len(c1.GOAL_SEPARATOR)
        and goal.endswith(c1.ACTIVATION_INSTRUCTION)
        and c1.goal_digest() == "sha256:" + hashlib.sha256(goal.encode("utf-8")).hexdigest()
    )
    return _check(
        "C02_goal_is_the_frozen_requirement_plus_one_appendix",
        ok,
        f"{len(requirement)} chars of P2 requirement + {len(c1.GOAL_SEPARATOR)} + "
        f"{len(c1.ACTIVATION_INSTRUCTION)} chars of appendix = {len(goal)} chars, in that order",
        requirement_digest="sha256:" + hashlib.sha256(requirement.encode("utf-8")).hexdigest(),
        goal_digest=c1.goal_digest(),
        appendix_offset=appendix_at,
        appendix_digest=c1.activation_digest(),
    )


def _check_prompt() -> dict[str, Any]:
    document = contract.protocol_document()
    prompts = document.get("prompts") or {}
    declared = (prompts.get("p3-bplus") or {}).get("digest")
    literal = (prompts.get("p3-literal") or {}).get("digest")
    text = contract.prompt_text("p3-bplus")
    ok = (
        c1.prompt_digest() == declared
        and declared == "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        and literal == contract.P3_DIGEST
        and literal != declared
    )
    return _check(
        "C03_main_prompt_is_the_bplus_prompt_unchanged",
        ok,
        f"the run prompt hashes to the Stage B+ digest {str(declared)[:19]}…; the literal P3 "
        f"digest {contract.P3_DIGEST[:19]}… is untouched",
        prompt_version=contract.BPLUS_VERSION,
        prompt_digest=declared,
        literal_p3_digest=literal,
        prompt_chars=len(text),
    )


# -- C04: worker profile and disclosure ------------------------------------------------------------


def _production_options(cell: Mapping[str, Any], root: Path) -> Any:
    return executor.production_goal_options(
        cell,
        evidence_root=root,
        fixture_dir=root / "fixture",
        config=executor.RunnerConfig(),
        run_id="c1-offline-preparation",
    )


def _worker_report(options: Any) -> dict[str, Any]:
    from atlasos.delegation.capability_card import REFERENCE_SURFACE, build_card
    from atlasos.delegation.exposure import render_exposure
    from atlasos.delegation.tool import DEFAULT_DESCRIPTION
    from atlasos.workers import WorkerCatalog
    from atlasos.workers.factory import DEFAULT_AVAILABLE_TOOLS

    catalog = WorkerCatalog()
    profile_id = (options.worker_profiles or (None,))[0]
    profile = catalog.profiles[profile_id].model_copy(
        update={"max_turns": int(options.worker_max_turns)}
    )
    card = build_card(
        profile,
        registered_tools=DEFAULT_AVAILABLE_TOOLS,
        surface_tools=REFERENCE_SURFACE,
        granted_write_paths=tuple(options.child_write_grants.get(profile_id, ())),
        child_max_turns=options.delegation.child_max_turns,
        result_preview_chars=options.delegation.result_preview_chars,
    )
    description = render_exposure(
        options.worker_exposure, (card,), base_description=DEFAULT_DESCRIPTION
    )
    task = c1.task()
    task_specific = [
        token
        for token in (
            *sorted(task.deliverables),
            *(str(worker["assignment"]) for worker in task.workers),
            "[YOUR ASSIGNMENT",
        )
        if token and token in description
    ]
    return {
        "profile_id": profile_id,
        "profiles_offered": list(options.worker_profiles),
        "exposure": options.worker_exposure,
        "grant": list(options.child_write_grants.get(profile_id, ())),
        "max_turns_effective": card.max_turns,
        "card_read_only": card.read_only,
        "allowed": list(card.allowed),
        "write_tools_present": sorted(WRITE_TOOLS & set(card.allowed)),
        "card_digest": card.digest(),
        "description_chars": len(description),
        "description_digest": _digest_bytes(description.encode("utf-8")),
        "description_mentions_grant": "plugins/**" in description,
        "description_mentions_assignment": task_specific,
        "task_writes_possible": card.read_only is False and bool(options.child_write_grants),
    }


def _check_worker(root: Path, cell: Mapping[str, Any]) -> dict[str, Any]:
    options = _production_options(cell, root)
    report = _worker_report(options)
    ok = (
        report["profiles_offered"] == [contract.CODING]
        and report["exposure"] == "card"
        and report["grant"] == list(c1.task().allowed_paths)
        and report["max_turns_effective"] == int(contract.BUDGET["worker_max_turns"])
        and report["card_read_only"] is False
        and report["write_tools_present"]
        and report["description_mentions_grant"]
        and not report["description_mentions_assignment"]
        and report["task_writes_possible"]
    )
    return _check(
        "C04_arm_c3_offers_a_write_capable_coding_worker",
        ok,
        f"{report['profile_id']} with the card, {report['max_turns_effective']} effective turns, "
        f"grant {report['grant']}, tools {report['write_tools_present']}; the disclosure names "
        "no deliverable and no worker id",
        **report,
    )


# -- C05: P2 admission ----------------------------------------------------------------------------


def _plan_context(options: Any) -> Any:
    """The routing facts ``DelegationService.plan_context`` would build for this run.

    Mirrored rather than constructed: ``DelegationService`` needs a live tool runtime, and this
    check must stay offline. The three inputs are the Host-owned ones — the offered catalog, the
    per-call width and the child write grant.
    """

    from atlasos.app.main_driven import SANDBOX_WORKSPACE_ROOT
    from atlasos.delegation.router import DelegationPlanContext, ProfileFacts
    from atlasos.workers import WorkerCatalog

    catalog = WorkerCatalog()
    profiles = {
        profile_id: ProfileFacts(
            profile_id=profile_id,
            base_tools=tuple(catalog.profiles[profile_id].base_tools),
            read_only=not (WRITE_TOOLS & set(catalog.profiles[profile_id].base_tools)),
            can_delegate=bool(catalog.profiles[profile_id].can_delegate),
        )
        for profile_id in options.worker_profiles
    }
    return DelegationPlanContext(
        run_id="c1-offline-admission",
        parent_task_id="c1-offline-admission-main",
        max_children=int(options.delegation.max_children_per_call),
        profiles=profiles,
        granted_write_paths={
            profile_id: tuple(paths) for profile_id, paths in options.child_write_grants.items()
        },
        workspace_root=SANDBOX_WORKSPACE_ROOT,
    )


def _spec(key: str, objective: str, paths: tuple[str, ...], *, depends_on: tuple[str, ...] = ()):
    from atlasos.delegation.models import DelegatedTaskSpec

    return DelegatedTaskSpec(
        key=key,
        objective=objective,
        background=(
            "The project is at /workspace/project. The v2 plugin contract is in plugins/base.py "
            "and described in docs/INTERFACE.md, both frozen. test_public.py is present and must "
            "not be edited."
        ),
        profile_id=contract.CODING,
        write_paths=paths,
        depends_on=depends_on,
    )


def _proposal(shape: str) -> Any:
    from atlasos.delegation.models import DelegateTasksInput

    if shape == "p2_coding_pair":
        tasks = (
            _spec(
                "csv",
                "Implement the CSV plugin against the frozen interface in plugins/base.py.",
                ("plugins/csv_plugin.py", "plugins/csv_fields.py"),
            ),
            _spec(
                "json",
                "Implement the JSON plugin against the frozen interface in plugins/base.py.",
                ("plugins/json_plugin.py", "plugins/json_fields.py"),
            ),
        )
    elif shape == "shared_write":
        tasks = (
            _spec(
                "csv",
                "Implement the CSV plugin.",
                ("plugins/registry.py", "plugins/csv_plugin.py"),
            ),
            _spec(
                "json",
                "Implement the JSON plugin.",
                ("plugins/registry.py", "plugins/json_plugin.py"),
            ),
        )
    elif shape == "declared_dependency":
        tasks = (
            _spec("csv", "Implement the CSV plugin.", ("plugins/csv_plugin.py",)),
            _spec(
                "register",
                "Wire the registry and the loader.",
                ("plugins/registry.py", "plugins/loader.py"),
                depends_on=("csv",),
            ),
        )
    elif shape == "outside_grant":
        tasks = (
            _spec("csv", "Implement the CSV plugin.", ("plugins/csv_plugin.py",)),
            _spec("json", "Implement the JSON plugin.", ("test_public.py",)),
        )
    else:  # pragma: no cover - programming error
        raise ValueError(f"unknown proposal shape {shape!r}")
    return DelegateTasksInput(tasks=tasks, reason="Stage C1 offline admission check")


def normalized_grant(
    grants: Mapping[str, tuple[str, ...]], workspace_root: str
) -> dict[str, tuple[str, ...]]:
    """The same scope, written the way the ownership manifest writes it.

    ``normalize_declared_path`` is the repository's own normaliser: ``plugins/**`` and ``plugins/``
    both mean the directory, and only the second one can be matched by ``path_is_within``.
    """

    from atlasos.ownership.paths import normalize_declared_path

    return {
        profile_id: tuple(
            normalize_declared_path(path, workspace_root=workspace_root) for path in paths
        )
        for profile_id, paths in grants.items()
    }


def _decide(
    options: Any, shape: str, *, grants: Mapping[str, tuple[str, ...]] | None = None
) -> dict[str, Any]:
    from atlasos.delegation.router import DelegationPlanContext, DelegationRouter

    context = _plan_context(options)
    if grants is not None:
        context = DelegationPlanContext(
            run_id=context.run_id,
            parent_task_id=context.parent_task_id,
            max_children=context.max_children,
            profiles=context.profiles,
            granted_write_paths=dict(grants),
            workspace_root=context.workspace_root,
        )
    decision = DelegationRouter().decide(_proposal(shape), context=context)
    return {
        "route": decision.route.value,
        "reason_codes": list(decision.reason_codes),
        "blocked_gates": list(decision.blocked_gates),
        "admitted_children": [child.key for child in decision.children if child.admitted],
        "children": [
            {"key": child.key, "admitted": child.admitted, "reasons": list(child.reason_codes)}
            for child in decision.children
        ],
        "max_wave_width": decision.max_wave_width,
        "policy_version": decision.policy_version,
        "policy_digest": decision.policy_digest,
    }


def admission_report(options: Any) -> dict[str, Any]:
    """The frozen configurations and their scope-equivalents through the frozen router."""

    from atlasos.app.main_driven import SANDBOX_WORKSPACE_ROOT

    frozen_grants = {
        profile_id: tuple(paths) for profile_id, paths in options.child_write_grants.items()
    }
    normalized = normalized_grant(frozen_grants, SANDBOX_WORKSPACE_ROOT)
    out: dict[str, Any] = {
        "frozen_grant": dict(frozen_grants),
        "normalized_grant": normalized,
        "workspace_root": SANDBOX_WORKSPACE_ROOT,
        "frozen": {},
        "normalized": {},
    }
    for shape in ("p2_coding_pair", "shared_write", "declared_dependency", "outside_grant"):
        out["frozen"][shape] = _decide(options, shape)
    out["normalized"]["p2_coding_pair"] = _decide(options, "p2_coding_pair", grants=normalized)
    return out


def _check_admission(root: Path, cell: Mapping[str, Any]) -> dict[str, Any]:
    """P2's pair, measured rather than hoped for.

    Two facts are asserted together, because either one alone would be misleading:

    * under the frozen Stage B+ grant (``plugins/**``) *every* declared write is refused with
      ``WRITE_SCOPE_NOT_GRANTED`` — P2 never reaches the coupling or parallel gates;
    * the identical proposal and the identical scope, written as ``plugins/`` (what
      ``normalize_declared_path`` produces), is admitted as a two-wide parallel wave.

    The second fact is what makes the first a defect in the *grant representation* rather than a
    property of the task, the router's logic or the Main. Nothing here changes the router: the
    refusal is reported, not worked around.
    """

    options = _production_options(cell, root)
    report = admission_report(options)
    frozen = report["frozen"]
    pair = frozen["p2_coding_pair"]
    shared = frozen["shared_write"]
    dependency = frozen["declared_dependency"]
    outside = frozen["outside_grant"]
    normalized_pair = report["normalized"]["p2_coding_pair"]
    ok = (
        pair["route"] == "direct"
        and pair["reason_codes"] == ["WRITE_SCOPE_NOT_GRANTED"]
        and pair["admitted_children"] == []
        and pair["policy_version"] == contract.BUDGET["router_policy"]
        and normalized_pair["route"] == "delegate_parallel"
        and normalized_pair["admitted_children"] == ["csv", "json"]
        and "DELEGATION_ADMITTED" in normalized_pair["reason_codes"]
        and not normalized_pair["blocked_gates"]
        and normalized_pair["max_wave_width"] == 2
        and shared["route"] == "direct"
        and "WRITE_SCOPE_NOT_GRANTED" in shared["reason_codes"]
        and dependency["route"] == "direct"
        and "DEPENDENCY_NOT_SUPPORTED" in dependency["reason_codes"]
        and outside["route"] == "direct"
        and "WRITE_SCOPE_NOT_GRANTED" in outside["reason_codes"]
    )
    return _check(
        "C05_frozen_grant_refuses_p2_and_the_same_scope_is_admitted_normalized",
        ok,
        f"with the frozen grant {report['frozen_grant']} the P2 pair is refused "
        f"{pair['reason_codes']}; with the same scope as {report['normalized_grant']} it is "
        "admitted as a two-wide parallel wave; the gate still refuses a shared write, a declared "
        "dependency and a write outside the grant",
        variants=report,
    )


# -- C06: dispatch and audit ----------------------------------------------------------------------


def _offline_signed(
    plan: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    """A record signed in memory for the offline control-flow proof.

    It is never written to the production claim root and never presented as an approval.
    """

    pending = authority.pending_record(
        plan, protocol_digest_value=protocol_digest, executor=identity
    )
    scratch.mkdir(parents=True, exist_ok=True)
    statement = scratch / "offline-owner-statement.json"
    body = {
        "statement": "Stage C1 offline control-flow proof, not an approval",
        "plan_id": plan["plan_id"],
    }
    statement.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    provenance = {
        "kind": "external_file",
        "path": str(statement),
        "sha256": _digest_bytes(statement.read_bytes()),
        "content_digest": authority.digest_of(body),
        "body": body,
    }
    return authority.sign(
        pending,
        approved_by="offline-check",
        approved_at="2026-11-26T00:00:00Z",
        owner_statement="offline control-flow check, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=float(c1.ENVELOPE_CANDIDATE["cost_stop_usd"]),
        per_cell_cost_stop_usd=float(c1.ENVELOPE_CANDIDATE["per_cell_cost_stop_usd"]),
        provenance=provenance,
    )


def _synthetic_runner() -> executor.CellRunner:
    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> executor.CellRun:
        return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

    return run


def dispatch_report(
    plan: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    import asyncio

    record = _offline_signed(
        plan, scratch=scratch, head=head, identity=identity, protocol_digest=protocol_digest
    )
    authority_path = scratch / "offline-authorization.json"
    authority_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    evidence = scratch / "dispatch"
    with executor.claim_root_override(scratch / "claims"):
        report = asyncio.run(
            executor.dispatch(
                plan["plan_id"],
                runs_root=evidence / "runs",
                evidence_root=evidence,
                authority_path=authority_path,
                cell_runner=_synthetic_runner(),
                repo_root=REPO_ROOT,
                mode=executor.MODE_OFFLINE_PROOF,
                clock=lambda: "2026-11-26T00:00:00Z",
            )
        )
    cell = (report.get("cells") or [{}])[0]
    return {
        "report": {
            "plan_id": report.get("plan_id"),
            "scope": report.get("scope"),
            "plan_digest": report.get("plan_digest"),
            "mode": report.get("mode"),
            "complete": report.get("complete"),
            "cells_planned": report.get("cells_planned"),
            "cells_started": report.get("cells_started"),
            "cells_passed": report.get("cells_passed"),
            "stop_reason": report.get("stop_reason"),
            "aggregate_includes_sample": report.get("aggregate_includes_sample"),
        },
        "cell": {
            "cell_id": cell.get("cell_id"),
            "verdict": cell.get("verdict"),
            "execution_integrity": cell.get("execution_integrity"),
            "goal_correctness": cell.get("goal_correctness"),
            "public_test_status": cell.get("public_test_status"),
            "oracle_passed": cell.get("oracle_passed"),
            "stops": cell.get("stops"),
            "failed_checks": [
                item["check"] for item in cell.get("checks") or [] if not item.get("ok")
            ],
            "check_count": len(cell.get("checks") or []),
        },
        "evidence_exists": {
            "matrix_report": (evidence / "matrix-report.json").is_file(),
            "ledger": (evidence / "ledger.jsonl").is_file(),
            "analysis": (evidence / "cells" / "c01" / "analysis.json").is_file(),
            "attempt": (evidence / "cells" / "c01" / "attempt.json").is_file(),
        },
    }


def _check_dispatch(
    plan: Mapping[str, Any],
    *,
    scratch: Path,
    head: str,
    identity: Mapping[str, Any],
    protocol_digest: str,
) -> dict[str, Any]:
    try:
        report = dispatch_report(
            plan,
            scratch=scratch,
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        )
    except Exception as error:  # noqa: BLE001 - a raised proof is a failed check, not a crash
        return _check(
            "C06_real_dispatch_and_audit_accept_one_synthetic_cell",
            False,
            f"{type(error).__name__}: {error}"[:300],
        )
    body = report["report"]
    cell = report["cell"]
    ok = (
        body["plan_id"] == c1.PLAN_ID
        and body["scope"] == c1.SCOPE
        and body["plan_digest"] == plan["digest"]
        and body["mode"] == executor.MODE_OFFLINE_PROOF
        and body["complete"] is True
        and body["cells_planned"] == 1
        and body["cells_started"] == 1
        and body["cells_passed"] == 1
        and body["stop_reason"] == executor.STOP_NONE
        and body["aggregate_includes_sample"] is False
        and cell["verdict"] == "PASS"
        and cell["execution_integrity"] == "PASS"
        and cell["goal_correctness"] is True
        and cell["public_test_status"] == "PASS"
        and cell["oracle_passed"] is True
        and not cell["stops"]
        and not cell["failed_checks"]
        and all(report["evidence_exists"].values())
    )
    return _check(
        "C06_real_dispatch_and_audit_accept_one_synthetic_cell",
        ok,
        f"dispatch ran the production loop in {body['mode']} mode over one cell; audit recorded "
        f"{cell['check_count']} checks with no stop and verdict {cell['verdict']}",
        **report,
    )


# -- C06b: the pre-claim admission gate -----------------------------------------------------------


def preflight_report(plan: Mapping[str, Any], *, scratch: Path) -> dict[str, Any]:
    """The live preflight ``executor.dispatch`` runs before the claim, on the same options.

    No provider is built, no sandbox is created and no model is called: this is the report a live
    dispatch would write, produced early enough that a configuration defect costs no attempt.
    """

    report = executor.run_admission_preflight(plan, repo_root=REPO_ROOT, scratch_root=scratch)
    cell = (report.get("cells") or [{}])[0]
    return {
        "status": report.get("status"),
        "blocking_failures": list(report.get("blocking_failures") or []),
        "contract_blocking_failures": list(report.get("contract_blocking_failures") or []),
        "credential_present": bool(report.get("credential_present")),
        "cell": {
            "cell_id": cell.get("cell_id"),
            "status": cell.get("status"),
            "checks": cell.get("checks"),
            "worker_profile": cell.get("worker_profile"),
            "worker_exposure": cell.get("worker_exposure"),
            "oracle_mode": cell.get("oracle_mode"),
            "fixture_matches_frozen_seed": cell.get("fixture_matches_frozen_seed"),
        },
    }


def _check_preflight(plan: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    try:
        report = preflight_report(plan, scratch=scratch)
    except Exception as error:  # noqa: BLE001 - a raised preflight is a failed check
        return _check(
            "C12_live_preflight_contract_is_ready_no_credential_needed",
            False,
            f"{type(error).__name__}: {error}"[:300],
        )
    ok = (
        not report["contract_blocking_failures"]
        and report["cell"]["worker_profile"] == contract.CODING
        and report["cell"]["worker_exposure"] == "card"
        and report["cell"]["oracle_mode"] == "EXTERNAL_SANDBOX_ORACLE"
        and report["cell"]["fixture_matches_frozen_seed"] is True
    )
    return _check(
        "C12_live_preflight_contract_is_ready_no_credential_needed",
        ok,
        "the pre-claim gate accepts the C1 cell's production configuration; the only tolerated "
        f"blocker is the machine credential (present={report['credential_present']})",
        **report,
    )


# -- C07: plan identity ---------------------------------------------------------------------------


def _check_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    rebuilt = c1.plan_record()
    cells = plan["cells"]
    from experiments.stage_benchmark import matrix

    recomputed = matrix.plan_digest(cells)
    head_dependent = [
        key for cell in cells for key in cell if key in {"head", "execution_head", "generated_at"}
    ]
    ok = (
        recomputed == plan["digest"]
        and rebuilt["digest"] == plan["digest"]
        and not head_dependent
        and plan["digest"] != BPLUS_CANARY_PLAN_DIGEST
        and plan["digest"] != BPLUS_FORMAL_PLAN_DIGEST
        and {digest for _, digest in CONSUMED_CLAIMS}.isdisjoint({plan["digest"]})
    )
    return _check(
        "C07_plan_identity_recomputes_and_is_independent_of_the_commit",
        ok,
        f"one cell digests to {plan['digest'][:19]}…; rebuilding the record reproduces it, no cell "
        "carries a commit and it collides with no Stage B+ plan",
        plan_digest=plan["digest"],
        cell_ids=[cell["cell_id"] for cell in cells],
        head_dependent_fields=head_dependent,
    )


# -- C08: history ---------------------------------------------------------------------------------


def _claims_snapshot() -> dict[str, Any]:
    """The three consumed claims, read-only. The production claim root, not the proof override."""

    root, is_default = executor.claim_root()
    out: dict[str, Any] = {"root": str(root), "root_is_default": is_default, "claims": {}}
    for label, digest in CONSUMED_CLAIMS:
        path = executor.claim_path(digest)
        if path.is_file():
            payload = path.read_bytes()
            out["claims"][label] = {
                "path": str(path),
                "sha256": _digest_bytes(payload),
                "bytes": len(payload),
                "plan_digest_in_file": (_read_json(path) or {}).get("plan_digest"),
            }
        else:
            out["claims"][label] = {"path": str(path), "present": False}
    return out


def _check_history(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    plans_ok = True
    problems: list[str] = []
    try:
        canary = contract.scope_plan(contract.SCOPE_CANARY)
        formal = contract.scope_plan(contract.SCOPE_FORMAL)
    except ValueError as error:  # pragma: no cover - a removed plan must fail, not crash
        return _check("C08_stage_b_plus_plans_protocol_and_claims_did_not_move", False, str(error))
    for name, actual, expected in (
        ("canary", canary["digest"], BPLUS_CANARY_PLAN_DIGEST),
        ("formal", formal["digest"], BPLUS_FORMAL_PLAN_DIGEST),
    ):
        if actual != expected:
            plans_ok = False
            problems.append(f"{name} plan digest is {actual}, expected {expected}")
    protocol = authority.protocol_digest(contract.protocol_document())
    if protocol != BPLUS_PROTOCOL_DIGEST:
        problems.append(f"the B+ protocol digest moved to {protocol}")
    refused: list[str] = []
    for plan_id in (contract.CANARY_PLAN_ID_V1, contract.CANARY_PLAN_ID_V2):
        try:
            contract.scope_plan(plan_id)
        except ValueError:
            refused.append(plan_id)
            continue
        problems.append(f"{plan_id} can be rebuilt into a dispatchable plan again")
    v1_v2_refused = len(refused) == 2
    claims_unchanged = before.get("claims") == after.get("claims")
    if not claims_unchanged:
        problems.append("a consumed claim changed during this preparation")
    present = [label for label, item in after["claims"].items() if item.get("sha256")]
    # A machine that does not hold the claim directory cannot prove them present, but it can still
    # prove nothing changed; a partial set is a real problem either way.
    ok = (
        plans_ok and not problems and claims_unchanged and len(present) in (0, len(CONSUMED_CLAIMS))
    )
    return _check(
        "C08_stage_b_plus_plans_protocol_and_claims_did_not_move",
        ok,
        "; ".join(problems)
        or (
            f"canary {BPLUS_CANARY_PLAN_DIGEST[:15]}…, formal {BPLUS_FORMAL_PLAN_DIGEST[:15]}…, "
            f"protocol {BPLUS_PROTOCOL_DIGEST[:15]}… unchanged; {len(present)} of "
            f"{len(CONSUMED_CLAIMS)} consumed claims are byte-identical before and after this "
            "preparation"
        ),
        protocol_digest=protocol,
        canary_plan_digest=canary["digest"],
        formal_plan_digest=formal["digest"],
        claims_before=before.get("claims"),
        claims_after=after.get("claims"),
        v1_v2_refused=v1_v2_refused,
    )


def _custody_reference() -> dict[str, Any]:
    """The V3 evidence root, outside the repository. Informational, never a gate."""

    index = V3_EVIDENCE_ROOT / "evidence-index.json"
    if not index.is_file():
        return {
            "present": False,
            "root": str(V3_EVIDENCE_ROOT),
            "note": "not mounted on this machine; the Stage B+ V3 run tree is external custody",
        }
    payload = _read_json(index) or {}
    digest = _digest_bytes(index.read_bytes())
    return {
        "present": True,
        "root": str(V3_EVIDENCE_ROOT),
        "index": str(index),
        "index_digest": digest,
        "index_digest_as_declared": V3_EVIDENCE_INDEX_DIGEST,
        "matches_declared_digest": digest == V3_EVIDENCE_INDEX_DIGEST,
        "files": len(payload.get("files") or []),
        "files_as_declared": V3_EVIDENCE_FILES,
        "plan_id": payload.get("plan_id"),
        "note": "read-only custody reference; this preparation copies nothing out of it",
    }


# -- C09 / C10: claim and authorization -----------------------------------------------------------


def _check_claim(plan: Mapping[str, Any]) -> dict[str, Any]:
    path = executor.claim_path(plan["digest"])
    exists = path.exists()
    root, is_default = executor.claim_root()
    return _check(
        "C09_new_plan_claim_is_not_created",
        not exists and is_default,
        f"no claim file exists at {path} and the production claim root is in force",
        claim_path=str(path),
        claim_exists=exists,
        claim_root=str(root),
        claim_root_is_default=is_default,
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
    fields = (
        "status",
        "authorizes_live_execution",
        "cost_stop_usd",
        "per_cell_cost_stop_usd",
        "token_stop",
        "attempt_limit",
        "approved_by",
        "approved_at",
        "owner_statement",
        "approval_provenance",
        "reviewed_head",
        "execution_head",
    )
    ok = (
        verdict["ready"] is False
        and record.get("status") == "PENDING_OWNER_APPROVAL"
        and record.get("authorizes_live_execution") is False
        and record.get("cost_stop_usd") is None
        and record.get("per_cell_cost_stop_usd") is None
        and record.get("token_stop") == int(c1.ENVELOPE_CANDIDATE["token_stop"])
        and record.get("attempt_limit") == 1
        and all(
            record.get(field) is None
            for field in (
                "approved_by",
                "approved_at",
                "owner_statement",
                "approval_provenance",
                "reviewed_head",
                "execution_head",
            )
        )
        and record.get("plan_digest") == plan["digest"]
        and list(record.get("cell_ids") or []) == [plan["cells"][0]["cell_id"]]
        and list(record.get("arms") or []) == list(plan["arms"])
        and list(record.get("tasks") or []) == list(plan["tasks"])
    )
    return _check(
        "C10_pending_authorization_authorizes_nothing",
        ok,
        f"status {record.get('status')}, authorizes_live_execution false, both cost ceilings null; "
        f"the verifier refuses it for {len(verdict['problems'])} stated reasons",
        blocked_by=list(verdict["problems"]),
        fields={field: record.get(field) for field in fields},
        envelope_candidate=dict(c1.ENVELOPE_CANDIDATE),
    )


# -- records ---------------------------------------------------------------------------------------


def admission_blocker(options: Any) -> dict[str, Any]:
    """What the frozen router does with P2's pair, stated as the Owner's decision input.

    The blocker is a fact about the *grant representation*, not about P2 and not about the router's
    logic: the same proposal, with the same scope written as ``plugins/``, is admitted.
    """

    report = admission_report(options)
    pair = report["frozen"]["p2_coding_pair"]
    normalized_pair = report["normalized"]["p2_coding_pair"]
    blocked = pair["route"] == "direct"
    return {
        "status": "BLOCKED_ADMISSION" if blocked else "ADMITTED",
        "gate": "GATE_M_PROPOSAL" if blocked else None,
        "reason_codes": list(pair["reason_codes"]),
        "frozen_grant": report["frozen_grant"],
        "normalized_grant": report["normalized_grant"],
        "normalized_outcome": {
            "route": normalized_pair["route"],
            "reason_codes": list(normalized_pair["reason_codes"]),
            "admitted_children": normalized_pair["admitted_children"],
        },
        "cause": (
            "the Host grant is the task's glob (plugins/**) and reaches the router unchanged; the "
            "router matches a declared target with path_is_within, which only matches a normalised "
            "file or a directory declaration ending in '/', so every declared write path is "
            "refused before the coupling, parallel, risk and context gates are reached. The "
            "enforcement "
            "layer normalises the same grant with normalize_declared_path (plugins/ -> directory), "
            "so the two representations of one scope disagree at the admission gate only"
        ),
        "source": [
            "experiments/stage_b_plus/contract.py:236-238 (TASK_WRITE_SCOPE = task.allowed_paths)",
            "experiments/stage_b_plus/contract.py:332-342 (Arm.grant_for)",
            "src/atlasos/app/main_driven.py:711 "
            "(granted_write_paths=dict(options.child_write_grants))",
            "src/atlasos/delegation/router.py:193-216 (_write_scope_problem, raw grant)",
            "src/atlasos/ownership/models.py:145-156 (path_is_within)",
            "src/atlasos/ownership/paths.py:66-84 (normalize_declared_path)",
            "src/atlasos/ownership/manifest.py:114-118 (the enforcer does normalise the grant)",
        ],
        "scope_of_the_defect": (
            "not P2-specific: any Closure-2 task's allowed_paths is a glob, so no coding child "
            "could be admitted under this grant form; the six Stage B+ V3 cells produced no "
            "proposal, so the path was never exercised"
        ),
        "not_done_here": (
            "the router was not modified, no grant was widened, and no special admission was "
            "added; expressing plugins/** as plugins/ would be the repository's own normalisation "
            "of the same scope, and that choice belongs to the Owner"
        ),
        "owner_decisions": [
            "A: authorize expressing the frozen write scope with normalize_declared_path "
            "(plugins/** -> plugins/) for the C1 cell only, then re-run this preparation",
            "B: authorize a router fix that normalises the grant before matching, as its own "
            "reviewed change with regression tests, then re-run this preparation",
            "C: sign this plan as-is, accepting that a write-declaring proposal is refused and the "
            "run can only reach A0 or A1 (predicted, not a positive control)",
        ],
    }


def authorization_record(
    plan: Mapping[str, Any],
    *,
    protocol_digest: str,
    identity: Mapping[str, Any],
    blocker: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The pending record, plus the candidate envelope as a *proposal*, never as a signature."""

    record = authority.pending_record(
        plan, protocol_digest_value=protocol_digest, executor=identity
    )
    record["proposed_envelope"] = {
        **dict(c1.ENVELOPE_CANDIDATE),
        "note": (
            "a candidate for the Owner's decision; the signed fields above stay null until signed"
        ),
    }
    record["stage"] = c1.STAGE_ID
    record["notes"] = (
        "Stage C1 positive control. This record authorizes nothing. A live dispatch requires every "
        "null field filled, both cost fields to be finite and positive, and the provenance to name "
        "an external artefact whose bytes still hash to the values recorded here."
    )
    record["review_surface"] = {
        "plan_id": c1.PLAN_ID,
        "goal_digest": c1.goal_digest(),
        "intervention_digest": c1.activation_digest(),
        "main_prompt_digest": c1.prompt_digest(),
        "main_prompt_modified": False,
    }
    if blocker is not None:
        record["prelive_admission"] = dict(blocker)
    return record


# -- assembly --------------------------------------------------------------------------------------


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
    checks: list[dict[str, Any]] = []
    checks.append(_check_fixture(scratch / "c01"))
    checks.append(_check_goal())
    checks.append(_check_prompt())
    cell = plan["cells"][0]
    checks.append(_check_worker(scratch / "c04", cell))
    checks.append(_check_admission(scratch / "c05", cell))
    checks.append(
        _check_dispatch(
            plan,
            scratch=scratch / "c06",
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
        )
    )
    checks.append(_check_preflight(plan, scratch / "c12"))
    checks.append(_check_identity(plan))
    after = _claims_snapshot()
    checks.append(_check_history(before, after))
    checks.append(_check_claim(plan))
    checks.append(
        _check_authorization(
            pending,
            head=head,
            identity=identity,
            protocol_digest=protocol_digest,
            plan=plan,
        )
    )
    checks.append(
        _check(
            "C11_plan_contract_invariants",
            not c1.validate(plan),
            "the frozen design invariants hold on the record itself",
            problems=c1.validate(plan),
        )
    )
    return checks


def build(scratch: Path | None = None, *, write: bool = True) -> dict[str, Any]:
    head = executor.git_head(REPO_ROOT)
    identity = executor.executor_identity(REPO_ROOT)
    document = contract.protocol_document()
    protocol_digest = authority.protocol_digest(document)
    plan = c1.plan_record()

    base = (
        Path(scratch) if scratch is not None else Path(tempfile.mkdtemp(prefix="stage-c1-checks-"))
    )
    blocker = admission_blocker(_production_options(plan["cells"][0], base / "blocker"))
    pending = authorization_record(
        plan, protocol_digest=protocol_digest, identity=identity, blocker=blocker
    )
    checks = run_checks(
        scratch=base,
        head=head,
        identity=identity,
        protocol_digest=protocol_digest,
        plan=plan,
        pending=pending,
    )
    custody = _custody_reference()
    report = {
        "stage": c1.STAGE_ID,
        "design_version": c1.DESIGN_VERSION,
        "plan_id": c1.PLAN_ID,
        "plan_digest": plan["digest"],
        "protocol_version": contract.PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "bplus_protocol_digest": BPLUS_PROTOCOL_DIGEST,
        "bplus_protocol_unchanged": protocol_digest == BPLUS_PROTOCOL_DIGEST,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "bplus_v3_executor_digest": BPLUS_V3_EXECUTOR_DIGEST,
        "executor_identity_moved": identity["executor_digest"] != BPLUS_V3_EXECUTOR_DIGEST,
        "goal_digest": c1.goal_digest(),
        "intervention_digest": c1.activation_digest(),
        "main_prompt_digest": c1.prompt_digest(),
        "budget_digest": authority.digest_of(dict(contract.BUDGET)),
        "built_from_head": head,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for check in checks if check["ok"]),
            "failed": [check["check_id"] for check in checks if not check["ok"]],
        },
        "custody": custody,
        "admission": dict(blocker),
        "status": (
            "STAGE_C1_POSITIVE_CONTROL_BLOCKED_ADMISSION"
            if blocker["status"] == "BLOCKED_ADMISSION"
            else "STAGE_C1_POSITIVE_CONTROL_READY_FOR_LIVE_AUTHORIZATION"
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
                "stage": c1.STAGE_ID,
                "executor_version": identity["executor_version"],
                "executor_digest": identity["executor_digest"],
                "files": identity["files"],
                "built_from_head": head,
                "supersedes": {
                    "executor_version": "bplus-executor-1.2",
                    "executor_digest": BPLUS_V3_EXECUTOR_DIGEST,
                    "recorded_in": "the Stage B+ V3 dispatch claim",
                    "scope": "Stage B+ plans only; no B+ artefact is rebuilt by this stage",
                },
                "note": (
                    "a live authorization must carry these exact digests and the head the Owner "
                    "actually reviews; built_from_head is not an approval"
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
    parser = argparse.ArgumentParser(description="Stage C1 offline verification")
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
