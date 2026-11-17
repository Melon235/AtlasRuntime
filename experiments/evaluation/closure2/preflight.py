# ruff: noqa: E501
"""Closure-2A deterministic preflight: prove the frozen benchmark before any model may run.

    python -m experiments.evaluation.closure2.preflight

No model, no sandbox, no network. Every check is one of: run the task's own tests locally, read the
frozen definition, or ask the shipped router for a decision.

Exit codes: ``0`` all checks passed, ``1`` a check failed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.catalog import (
    BENCHMARK_ID,
    BENCHMARK_VERSION,
    DATASET_VERSION,
    TASKS,
    Task,
    build_task,
)
from experiments.evaluation.closure2.protocol import (
    ARMS,
    BUDGET,
    BUDGET_SEMANTICS,
    EXECUTION_ORDER,
    LEGACY_CONTEXT_CONFIG,
    METRICS,
    ROUTING_POLICY_VERSION,
    SAME_DIGEST_GROUP,
    SHARED_PREFIX_CONFIG,
    UNSUPPORTED,
    arm_config_digest,
    preregistration_digest,
    preregistration_payload,
    task_taskspec_parity,
)
from experiments.evaluation.models import canonical_digest
from experiments.evaluation.stage_r0_routing import (
    build_proposal,
    plan_route,
    routing_policy,
)

from atlasos.config import load_config
from atlasos.workers import WorkerCatalog
from atlasos.workers.routing import (
    CandidateTask,
    capabilities_from_catalog,
    extract_declared_paths,
    find_cycle,
    max_wave_width,
    ready_waves,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "preflight.json"

#: Frozen at Closure-2A. A change here means the router policy moved and the preregistration is void.
ROUTING_POLICY_DIGEST = "5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244"

_PYTEST_ARGS = [
    "-m",
    "pytest",
    "-q",
    "--no-header",
    "-p",
    "no:cacheprovider",
    "-W",
    "ignore::DeprecationWarning",
]


# ---------------------------------------------------------------------------------- task materialisation


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in sorted(files.items()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _run_pytest(root: Path, *, timeout: int = 180) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, *_PYTEST_ARGS],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    lines = [
        line
        for line in (completed.stdout + completed.stderr).splitlines()
        if line.strip() and "warnings.warn" not in line and "PytestDeprecationWarning" not in line
    ]
    return completed.returncode, "\n".join(lines[-4:])


def _materialise(task: Task, *, with_reference: bool, with_oracle: bool) -> Path:
    """One disposable workspace per check, so no arm or phase can leak into another."""

    root = Path(tempfile.mkdtemp(prefix=f"closure2-{task.task_id}-"))
    _write_files(root, task.seed)
    if with_reference:
        _write_files(root, task.reference)
    if with_oracle:
        _write_files(root, task.oracle_tests)
    return root


def task_selftest(task: Task) -> dict[str, Any]:
    """Seed must fail, the reference must pass, and the two test sets must agree."""

    roots: list[Path] = []
    try:
        seed_root = _materialise(task, with_reference=False, with_oracle=False)
        roots.append(seed_root)
        seed_public_code, seed_public_tail = _run_pytest(seed_root)

        seed_oracle_root = _materialise(task, with_reference=False, with_oracle=True)
        roots.append(seed_oracle_root)
        seed_oracle_code, seed_oracle_tail = _run_pytest(seed_oracle_root)

        ref_public_root = _materialise(task, with_reference=True, with_oracle=False)
        roots.append(ref_public_root)
        ref_public_code, ref_public_tail = _run_pytest(ref_public_root)

        ref_oracle_root = _materialise(task, with_reference=True, with_oracle=True)
        roots.append(ref_oracle_root)
        ref_oracle_code, ref_oracle_tail = _run_pytest(ref_oracle_root)
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    return {
        "seed_public_fails": seed_public_code != 0,
        "seed_oracle_fails": seed_oracle_code != 0,
        "reference_public_passes": ref_public_code == 0,
        "reference_oracle_passes": ref_oracle_code == 0,
        "seed_public_tail": seed_public_tail,
        "seed_oracle_tail": seed_oracle_tail,
        "reference_public_tail": ref_public_tail,
        "reference_oracle_tail": ref_oracle_tail,
    }


def workspace_isolation(task: Task) -> dict[str, Any]:
    """The agent workspace must contain no oracle content and no reference-only content."""

    visible = build_task(task, topology="multi").visible_files
    leaks: list[str] = []
    for name, content in task.oracle_tests.items():
        if name in visible:
            leaks.append(f"hidden test file staged: {name}")
        # A whole hidden file being copied under another name is the same leak.
        for staged_name, staged in visible.items():
            if content.strip() and content.strip() in staged:
                leaks.append(f"hidden test body staged in {staged_name}")
    for name in task.reference:
        if name not in visible:
            leaks.append(f"reference-only file staged: {name}")
    return {"passed": not leaks, "leaks": leaks, "visible_files": sorted(visible)}


# ---------------------------------------------------------------------------------- DAG and router


def dag_audit(task: Task) -> dict[str, Any]:
    steps = task.workers
    ids = [str(step["task_id"]) for step in steps]
    candidates = [
        CandidateTask(
            task_id=str(step["task_id"]),
            description=str(step["assignment"]),
            depends_on=tuple(step["depends_on"]),
            proposed_profile_id="coding_worker",
        )
        for step in steps
    ]
    waves = ready_waves(candidates)
    width = max_wave_width(candidates)
    wave_zero = set(waves[0]) if waves else set()

    write_sets = {str(step["task_id"]): set(step["owned_paths"]) for step in steps}
    first_wave_ids = [task_id for task_id in ids if task_id in wave_zero]
    overlaps: list[str] = []
    for index, left in enumerate(first_wave_ids):
        for right in first_wave_ids[index + 1 :]:
            shared = write_sets[left] & write_sets[right]
            if shared:
                overlaps.append(f"{left} & {right}: {sorted(shared)}")

    dependency_complete = all(
        set(step["depends_on"]) <= set(ids) for step in steps
    ) and not find_cycle(candidates)

    # Every dependency must be a declared step that actually owns a file: a dependency on a step
    # with no write set would mean the consumer waits for nothing.
    integration_ok = all(
        all(
            dependency in write_sets and write_sets[dependency] for dependency in step["depends_on"]
        )
        for step in steps
    )

    # The router reads write targets out of the public assignment text, never out of the tuple we
    # *say* is owned. If the two disagree, the routing evidence is describing a different task.
    declared = {
        str(step["task_id"]): list(extract_declared_paths(str(step["assignment"]))[0])
        for step in steps
    }
    ownership_matches = all(set(declared[task_id]) == write_sets[task_id] for task_id in ids)

    return {
        "declared_write_paths": declared,
        "declared_write_paths_match_ownership": ownership_matches,
        "task_ids": ids,
        "waves": [list(wave) for wave in waves],
        "max_wave_width": width,
        "first_wave": sorted(wave_zero),
        "first_wave_write_overlap": overlaps,
        "first_wave_disjoint": not overlaps,
        "dependency_complete": dependency_complete,
        "integration_dependencies_are_declared_steps": integration_ok,
        "wave_zero_width_is_two": len(first_wave_ids) == 2,
    }


def router_audit(task: Task) -> dict[str, Any]:
    """Ask the shipped router, unmodified, and record exactly what it says."""

    multi = build_task(task, topology="multi")
    policy = routing_policy(ROUTING_POLICY_VERSION)
    capabilities = capabilities_from_catalog(WorkerCatalog())
    routed = plan_route(multi, capabilities, policy)
    decision = routed.decision
    proposal = build_proposal(multi, multi.workers, proposal_id=f"{task.task_id}:audit")
    return {
        "policy_version": policy.version,
        "policy_digest": policy.digest(),
        "max_parallel_workers": policy.max_parallel_workers,
        "route": str(decision.route),
        "execution_mode": routed.execution_mode,
        "reason_codes": list(decision.reason_codes),
        "blocked_gates": list(decision.blocked_gates),
        "gates": [
            {
                "gate": gate.gate,
                "status": str(gate.status),
                "reason_codes": list(gate.reason_codes),
            }
            for gate in decision.gates
        ],
        "max_parallel_wave_width": decision.max_parallel_wave_width,
        "diagnostics": list(decision.diagnostics),
        "proposal_digest": canonical_digest(
            {
                "proposal_id": proposal.proposal_id,
                "tasks": [
                    {"task_id": item.task_id, "depends_on": list(item.depends_on)}
                    for item in proposal.candidate_tasks
                ],
                "evidence": sorted(item.evidence_id for item in proposal.evidence),
            }
        ),
    }


# ---------------------------------------------------------------------------------- parity and config


def digest_parity() -> dict[str, Any]:
    by_arm_task: dict[str, dict[str, str]] = {}
    for arm, entry in ARMS.items():
        for task in TASKS:
            built = build_task(task, topology=entry["topology"])
            by_arm_task.setdefault(task.task_id, {})[arm] = built.effective_public_task_digest

    groups: list[dict[str, Any]] = []
    for group in SAME_DIGEST_GROUP:
        for task in TASKS:
            digests = {arm: by_arm_task[task.task_id][arm] for arm in group}
            groups.append(
                {
                    "task_id": task.task_id,
                    "arms": list(group),
                    "digests": digests,
                    "equal": len(set(digests.values())) == 1,
                }
            )
    cross_topology = {
        task.task_id: {
            "single": by_arm_task[task.task_id]["N0"],
            "multi": by_arm_task[task.task_id]["N1"],
            "differ": by_arm_task[task.task_id]["N0"] != by_arm_task[task.task_id]["N1"],
        }
        for task in TASKS
    }
    return {
        "groups": groups,
        "all_groups_equal": all(entry["equal"] for entry in groups),
        "cross_topology": cross_topology,
        "task_facts": task_taskspec_parity(),
        "all_task_facts_equal": all(
            all(
                value is True
                for key, value in record.items()
                if key.startswith("same_") or key.endswith("_carry_shared_context")
            )
            for record in task_taskspec_parity().values()
        ),
    }


def layout_executable() -> dict[str, Any]:
    legacy = load_config(LEGACY_CONTEXT_CONFIG)
    shared = load_config(SHARED_PREFIX_CONFIG)
    unknown_rejected = False
    try:
        from atlasos.config import ContextConfig

        ContextConfig(
            policy="task_scoped",
            renderer="stable_deterministic",
            prompt_layout="not-a-layout",  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001 - any refusal is the point
        unknown_rejected = True
    return {
        "legacy_config": LEGACY_CONTEXT_CONFIG.name,
        "legacy_resolved_layout": legacy.context.prompt_layout,
        "legacy_ok": legacy.context.prompt_layout == "legacy",
        "shared_prefix_config": SHARED_PREFIX_CONFIG.name,
        "shared_prefix_resolved_layout": shared.context.prompt_layout,
        "shared_prefix_ok": shared.context.prompt_layout == "shared_prefix",
        "policy_task_scoped": shared.context.policy == "task_scoped",
        "renderer_stable": shared.context.renderer == "stable_deterministic",
        "transform_identity": shared.context.transform.name == "identity",
        "unknown_layout_rejected": unknown_rejected,
    }


def budget_protocol() -> dict[str, Any]:
    single = BUDGET["single"]
    multi = BUDGET["multi"]
    problems: list[str] = []
    if multi["max_turns_per_worker"] * multi["workers"] != single["max_turns"]:
        problems.append("multi per-worker turns x workers must equal the single-worker allowance")
    if single["token_budget"] != multi["token_budget"]:
        problems.append("both topologies must share one token budget")
    if not 240_000 <= single["token_budget"] <= 300_000:
        problems.append("token budget outside the range the owner approved for discussion")
    if single["max_tool_calls"] != multi["max_tool_calls"]:
        problems.append("both topologies must share one tool-call limit")
    if BUDGET["approval"] != "PENDING_OWNER_APPROVAL":
        problems.append("this stage must not claim an approved budget")
    return {
        "single": single,
        "multi": multi,
        "shared": BUDGET["shared"],
        "semantics": BUDGET_SEMANTICS,
        "problems": problems,
        "consistent": not problems,
    }


# ---------------------------------------------------------------------------------- entry point


def run_preflight() -> dict[str, Any]:
    per_task: dict[str, Any] = {}
    for task in TASKS:
        selftest = task_selftest(task)
        per_task[task.task_id] = {
            "family": task.family,
            "selftest": selftest,
            "workspace_isolation": workspace_isolation(task),
            "dag": dag_audit(task),
            "router": router_audit(task),
        }

    parity = digest_parity()
    layout = layout_executable()
    budget = budget_protocol()
    router_policy_digest = routing_policy(ROUTING_POLICY_VERSION).digest()

    checks: dict[str, bool] = {}
    for task_id, record in per_task.items():
        selftest = record["selftest"]
        checks[f"{task_id}:seed_public_fails"] = selftest["seed_public_fails"]
        checks[f"{task_id}:seed_oracle_fails"] = selftest["seed_oracle_fails"]
        checks[f"{task_id}:reference_public_passes"] = selftest["reference_public_passes"]
        checks[f"{task_id}:reference_oracle_passes"] = selftest["reference_oracle_passes"]
        checks[f"{task_id}:workspace_isolated"] = record["workspace_isolation"]["passed"]
        checks[f"{task_id}:dag_legal"] = record["dag"]["dependency_complete"]
        checks[f"{task_id}:dag_integration_dependencies"] = record["dag"][
            "integration_dependencies_are_declared_steps"
        ]
        checks[f"{task_id}:declared_write_paths_match_ownership"] = record["dag"][
            "declared_write_paths_match_ownership"
        ]
        checks[f"{task_id}:same_task_facts"] = bool(
            parity["task_facts"][task_id]["same_visible_files"]
            and parity["task_facts"][task_id]["same_hidden_files"]
            and parity["task_facts"][task_id]["same_required_artifacts"]
            and parity["task_facts"][task_id]["single_prompt_contains_shared_context"]
            and parity["task_facts"][task_id]["multi_steps_carry_shared_context"]
        )
    checks["digest_groups_equal"] = parity["all_groups_equal"]
    checks["task_facts_equal_across_topologies"] = parity["all_task_facts_equal"]
    checks["layout_configs_executable"] = all(
        layout[key]
        for key in (
            "legacy_ok",
            "shared_prefix_ok",
            "policy_task_scoped",
            "renderer_stable",
            "transform_identity",
            "unknown_layout_rejected",
        )
    )
    checks["budget_protocol_consistent"] = budget["consistent"]
    checks["router_policy_digest_frozen"] = router_policy_digest == ROUTING_POLICY_DIGEST
    checks["arms_are_distinct"] = len(set(arm_config_digest(arm) for arm in ARMS)) == len(ARMS)
    checks["preregistration_complete"] = all(
        key in preregistration_payload()
        for key in (
            "tasks",
            "arms",
            "budget",
            "comparisons",
            "execution_order",
            "metrics",
            "exclusions",
        )
    )

    return {
        "stage": "closure-2a",
        "kind": "deterministic_benchmark_preflight",
        "generated_at": datetime.now(UTC).isoformat(),
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset_version": DATASET_VERSION,
        "model_calls": 0,
        "sandbox_runs": 0,
        "tasks": per_task,
        "parity": parity,
        "layout": layout,
        "budget": budget,
        "router_policy": {
            "version": ROUTING_POLICY_VERSION,
            "digest": router_policy_digest,
            "frozen_digest": ROUTING_POLICY_DIGEST,
        },
        "preregistration_digest": preregistration_digest(),
        "execution_order": EXECUTION_ORDER,
        "metrics": METRICS,
        "unsupported": UNSUPPORTED,
        "checks": checks,
        "status": "PASSED" if all(checks.values()) else "FAILED",
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="closure-2a deterministic benchmark preflight")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    payload = run_preflight()
    _write(Path(args.out), payload)
    failed = sorted(name for name, passed in payload["checks"].items() if not passed)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "checks": len(payload["checks"]),
                "failed": failed,
                "preregistration_digest": payload["preregistration_digest"],
            },
            indent=2,
        )
    )
    return 0 if payload["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
