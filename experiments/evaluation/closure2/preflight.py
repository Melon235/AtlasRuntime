# ruff: noqa: E501
"""Closure-2A-R1 deterministic preflight: prove the frozen benchmark before any model may run.

    python -m experiments.evaluation.closure2.preflight --out experiments/results/closure2/preflight-r1.json

No model, no sandbox, no network. Every check is one of: run the task's own tests locally (public and
hidden separately), read the frozen definition, or ask the shipped router for a decision.

Two outcomes are reported separately and must not be conflated:

* ``status``          — the offline design verification (this file);
* ``execution_ready`` — whether Closure-2B may start. It is false until an approval record names the
  exact frozen digest, so a passing preflight never unlocks a live pilot.

Exit codes: ``0`` all checks passed, ``1`` a check failed.
"""

from __future__ import annotations

import argparse
import json
import re
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
    task_digests,
)
from experiments.evaluation.closure2.protocol import (
    APPROVAL_RECORD,
    ARMS,
    BUDGET,
    BUDGET_SEMANTICS,
    C2A_FROZEN_TASK_DIGESTS,
    C2A_PREREGISTRATION_DIGEST,
    C2A_R1_PREREGISTRATION_DIGEST,
    EXECUTION_ORDER,
    LEGACY_CONTEXT_CONFIG,
    METRICS,
    PROTOCOL_REVISION,
    ROUTING_POLICY_VERSION,
    SAME_DIGEST_GROUP,
    SHARED_PREFIX_CONFIG,
    UNSUPPORTED,
    approval_gate,
    arm_config_digest,
    config_semantic_digest,
    layout_ablation_delta,
    preregistration_digest,
    preregistration_payload,
    require_execution_approval,
    resolve_repo_path,
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
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "preflight-r1.json"

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

#: What the router must decide for each family, and why. Recorded here so a silent routing change
#: fails the preflight instead of passing as "some decision was recorded".
ROUTER_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "P1": {
        "route": "delegate_parallel",
        "required_reasons": ["DELEGATION_ADMITTED"],
        "forbidden_reasons": ["SHARED_WRITE_RISK", "UNSTABLE_INTERFACE"],
        "forbidden_blocked_gates": ["GATE_2_DEPENDENCY_COUPLING"],
        "required_blocked_gates": [],
        "wave0_workers": 2,
        "wave0_disjoint": True,
    },
    "P2": {
        "route": "delegate_parallel",
        "required_reasons": ["DELEGATION_ADMITTED"],
        "forbidden_reasons": ["SHARED_WRITE_RISK", "UNSTABLE_INTERFACE"],
        "forbidden_blocked_gates": ["GATE_2_DEPENDENCY_COUPLING"],
        "required_blocked_gates": [],
        "wave0_workers": 2,
        "wave0_disjoint": True,
    },
    "N1": {
        "route": "direct",
        "required_reasons": ["SHARED_WRITE_RISK", "UNSTABLE_INTERFACE"],
        "forbidden_reasons": ["DELEGATION_ADMITTED"],
        "forbidden_blocked_gates": [],
        "required_blocked_gates": ["GATE_2_DEPENDENCY_COUPLING"],
        "wave0_workers": 1,
        "wave0_disjoint": True,
    },
}

#: Failure kinds that mean "the seed does not satisfy the contract" rather than "the harness broke".
_DELIVERABLE_SEED_FAILURES = frozenset({"TEST_FAILURE", "COLLECT_ERROR"})
#: Kinds that are only ever a harness problem. They never stand in for a legitimate defect.
_HARNESS_FAILURES = frozenset(
    {
        "NO_TESTS_COLLECTED",
        "NO_TARGETS",
        "USAGE_ERROR",
        "INTERRUPTED",
        "INTERNAL_ERROR",
        "TIMEOUT",
        "ERROR",
    }
)


# ---------------------------------------------------------------------------------- test execution


def classify_pytest(returncode: int, output: str) -> str:
    """Map one pytest session onto what actually happened.

    A collection error is not a test failure, and neither is an unusable test session. For a *seed*
    run both of the first two mean the seed does not satisfy the contract; for a *reference* run only
    ``PASSED`` is acceptable.
    """

    text = output
    if returncode == 0:
        return "PASSED"
    if "ERROR collecting" in text or "error during collection" in text.lower():
        return "COLLECT_ERROR"
    if returncode == 5 or "no tests ran" in text:
        return "NO_TESTS_COLLECTED"
    if returncode == 4:
        return "USAGE_ERROR"
    if returncode == 3:
        return "INTERNAL_ERROR"
    if returncode == 2:
        return "INTERRUPTED"
    if returncode == 1:
        return "TEST_FAILURE"
    return "ERROR"


_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def _parse_counts(output: str) -> dict[str, int]:
    """How many tests actually ran, so "the oracle executed" is a measurement and not a claim.

    Only the final summary line is read: pytest repeats counts in the collection-error banner, and
    summing both would double-count.
    """

    for line in reversed(output.splitlines()):
        if not line.strip():
            continue
        found = _COUNT_RE.findall(line)
        if not found:
            continue
        counts: dict[str, int] = {}
        for value, label in found:
            key = "error" if label.startswith("error") else label
            counts[key] = counts.get(key, 0) + int(value)
        return counts
    return {}


def _failure_cause(output: str) -> str | None:
    """The first exception line pytest prints, so a collection error names its real cause."""

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("E "):
            return stripped[1:].strip()[:400]
    return None


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in sorted(files.items()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _run_pytest(root: Path, targets: list[str], *, timeout: int = 180) -> dict[str, Any]:
    """Run exactly ``targets`` — never "whatever pytest finds".

    Running the whole workspace would let a failing ``test_public.py`` stand in for a failing hidden
    test, which is precisely the false positive this stage has to rule out. An empty target list is
    refused for the same reason: ``pytest`` with no argument would collect the whole workspace.
    """

    if not targets:
        return {
            "targets": [],
            "exit_code": None,
            "kind": "NO_TARGETS",
            "tail": "no test target was given; refusing to collect the whole workspace",
            "counts": {},
            "cause": None,
            "passed": False,
        }

    try:
        completed = subprocess.run(
            [sys.executable, *_PYTEST_ARGS, *targets],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "targets": list(targets),
            "exit_code": None,
            "kind": "TIMEOUT",
            "tail": f"pytest exceeded {timeout}s",
            "counts": {},
            "cause": f"pytest exceeded {timeout}s",
            "passed": False,
        }

    raw = completed.stdout + completed.stderr
    lines = [
        line
        for line in raw.splitlines()
        if line.strip() and "warnings.warn" not in line and "PytestDeprecationWarning" not in line
    ]
    kind = classify_pytest(completed.returncode, raw)
    workspace = str(root)
    return {
        "targets": list(targets),
        "exit_code": completed.returncode,
        "kind": kind,
        # The artifact is published: no host path may survive in it.
        "tail": "\n".join(lines[-4:]).replace(workspace, "<workspace>"),
        "counts": _parse_counts(raw),
        "cause": (_failure_cause(raw) or "").replace(workspace, "<workspace>") or None,
        "passed": completed.returncode == 0 and kind == "PASSED",
    }


def _materialise(task: Task, *, with_reference: bool, with_oracle: bool) -> Path:
    """One disposable workspace per session, so no phase can leak into another."""

    root = Path(tempfile.mkdtemp(prefix=f"closure2-r1-{task.task_id}-"))
    _write_files(root, task.seed)
    if with_reference:
        _write_files(root, task.reference)
    if with_oracle:
        _write_files(root, task.oracle_tests)
    return root


# ---------------------------------------------------------------------------------- task self-test


def seed_hidden_gate(session: dict[str, Any]) -> bool:
    """True when the seed genuinely fails the hidden oracle.

    A missing target interface shows up as ``COLLECT_ERROR`` (the hidden test cannot import the v2
    API) and counts: the deliverable does not satisfy the contract. ``NO_TESTS_COLLECTED``,
    ``USAGE_ERROR`` and ``INTERRUPTED`` do **not** count — those are harness problems, and reporting
    them as "the oracle found a defect" would be fake evidence.
    """

    return session["kind"] in _DELIVERABLE_SEED_FAILURES


def task_selftest(task: Task) -> dict[str, Any]:
    """Four independent sessions: seed public, seed hidden, reference public, reference hidden.

    Each session gets its own disposable workspace and its own explicit target list, so a failing
    public test can never be read as "the hidden oracle caught the defect" — which is exactly what
    the whole-workspace collection in Closure-2A did for N1.
    """

    public_targets = sorted(task.public_tests)
    hidden_targets = sorted(task.oracle_tests)
    roots: list[Path] = []
    distinct = False
    try:
        seed_root = _materialise(task, with_reference=False, with_oracle=False)
        roots.append(seed_root)
        seed_public = _run_pytest(seed_root, public_targets)

        seed_hidden_root = _materialise(task, with_reference=False, with_oracle=True)
        roots.append(seed_hidden_root)
        seed_hidden = _run_pytest(seed_hidden_root, hidden_targets)

        ref_public_root = _materialise(task, with_reference=True, with_oracle=False)
        roots.append(ref_public_root)
        reference_public = _run_pytest(ref_public_root, public_targets)

        ref_hidden_root = _materialise(task, with_reference=True, with_oracle=True)
        roots.append(ref_hidden_root)
        reference_hidden = _run_pytest(ref_hidden_root, hidden_targets)

        distinct = len({str(root) for root in roots}) == 4
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    hidden_executed = reference_hidden["counts"].get("passed", 0)
    return {
        "seed_public": seed_public,
        "seed_hidden": seed_hidden,
        "reference_public": reference_public,
        "reference_hidden": reference_hidden,
        "seed_public_fails": seed_public["kind"] in _DELIVERABLE_SEED_FAILURES,
        "seed_hidden_fails": seed_hidden_gate(seed_hidden),
        "seed_hidden_kind": seed_hidden["kind"],
        "reference_public_passes": reference_public["passed"],
        "reference_hidden_passes": reference_hidden["passed"],
        "hidden_oracle_executed_tests": hidden_executed,
        "hidden_oracle_really_executes": hidden_executed > 0,
        "seed_hidden_executed_assertions": (
            seed_hidden["counts"].get("passed", 0) + seed_hidden["counts"].get("failed", 0) > 0
        ),
        "hidden_collection_diagnostics": (
            {"kind": seed_hidden["kind"], "cause": seed_hidden["cause"]}
            if seed_hidden["kind"] in {"COLLECT_ERROR", "TIMEOUT", "NO_TESTS_COLLECTED"}
            else None
        ),
        "independence": {
            "separate_workspaces": distinct,
            "public_and_hidden_targets_disjoint": not (set(public_targets) & set(hidden_targets)),
            "seed_public_ran_only_public_targets": seed_public["targets"] == public_targets,
            "seed_hidden_ran_only_hidden_targets": seed_hidden["targets"] == hidden_targets,
            "reference_public_ran_only_public_targets": reference_public["targets"]
            == public_targets,
            "reference_hidden_ran_only_hidden_targets": reference_hidden["targets"]
            == hidden_targets,
        },
        "failure_classification": {
            "seed_public": seed_public["kind"],
            "seed_hidden": seed_hidden["kind"],
            "reference_public": reference_public["kind"],
            "reference_hidden": reference_hidden["kind"],
            "harness_problem_kinds_never_count_as_a_defect": sorted(_HARNESS_FAILURES),
        },
    }


# ---------------------------------------------------------------------------------- integrity


def frozen_identity(task: Task) -> dict[str, Any]:
    """Compare the live task against the **original** C2-A registration constants.

    Comparing ``task_digests(task)`` with itself would prove nothing; the expected values here come
    from the ac7af16 preregistration table and are immutable in this module.
    """

    expected = C2A_FROZEN_TASK_DIGESTS[task.task_id]
    actual = task_digests(task)
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    return {
        "task_id": task.task_id,
        "matches": not mismatches,
        "mismatches": mismatches,
        "compared": sorted(expected),
    }


def workspace_isolation(task: Task) -> dict[str, Any]:
    """The agent workspace must be exactly the seed, and must not carry the answer.

    Two detectors, with an honest boundary:

    1. **exact workspace** — the visible file set is exactly the seed key set, and every visible body
       equals the seed body (no extra file, no substituted content);
    2. **whole-body leak** — no hidden test body and no *answer-bearing* reference body appears
       anywhere in the visible files, under any name.

    A reference file whose body legitimately equals its seed counterpart is not answer-bearing and is
    skipped. Snippet-level leakage is **not** detected; only whole bodies are.
    """

    visible = build_task(task, topology="multi").visible_files
    leaks: list[str] = []

    if set(visible) != set(task.seed):
        leaks.append(
            f"visible file set differs from the seed: {sorted(set(visible) ^ set(task.seed))}"
        )
    for name, body in visible.items():
        if name in task.seed and body != task.seed[name]:
            leaks.append(f"visible file {name} differs from the frozen seed content")

    for name, content in task.oracle_tests.items():
        if name in visible:
            leaks.append(f"hidden test file staged: {name}")
        if content.strip():
            for staged_name, staged in visible.items():
                if content.strip() in staged:
                    leaks.append(f"hidden test body staged in {staged_name}")

    for name, content in task.reference.items():
        if not content.strip() or content == task.seed.get(name):
            continue  # legitimately identical: nothing to leak
        for staged_name, staged in visible.items():
            if content.strip() in staged:
                leaks.append(f"reference body for {name} staged in {staged_name}")

    return {
        "passed": not leaks,
        "leaks": leaks,
        "visible_files": sorted(visible),
        "detection_boundary": (
            "whole-file and whole-body only: a reference file is answer-bearing when it differs from "
            "its seed counterpart, and snippet-level leakage is not detected"
        ),
    }


def answer_leak_guard(task: Task) -> dict[str, Any]:
    """The two detectors together. Either one failing means the task ships an answer."""

    isolation = workspace_isolation(task)
    frozen = frozen_identity(task)
    return {
        "passed": isolation["passed"] and frozen["matches"],
        "workspace_isolation": isolation,
        "frozen_identity": frozen,
    }


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

    integration_ok = all(
        all(
            dependency in write_sets and write_sets[dependency] for dependency in step["depends_on"]
        )
        for step in steps
    )

    # The router reads write targets out of the public assignment text, never out of the tuple we
    # *say* is owned. If the two disagree, the routing evidence describes a different task.
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


def router_gate(task: Task, audit: dict[str, Any], dag: dict[str, Any]) -> dict[str, Any]:
    """The expected decision, as a boolean gate rather than a recorded observation.

    The policy *digest* being unchanged only proves the policy is the same object; it says nothing
    about what the policy decides for these three tasks. Every expected decision fact is therefore
    asserted here, and a deviation is a hard failure that names the offending field.
    """

    expected = ROUTER_EXPECTATIONS[task.family]
    problems: list[str] = []
    if audit["route"] != expected["route"]:
        problems.append(f"route {audit['route']!r} != expected {expected['route']!r}")
    missing_reasons = [
        reason for reason in expected["required_reasons"] if reason not in audit["reason_codes"]
    ]
    if missing_reasons:
        problems.append(f"missing reason codes {missing_reasons}")
    unexpected_reasons = [
        reason for reason in expected["forbidden_reasons"] if reason in audit["reason_codes"]
    ]
    if unexpected_reasons:
        problems.append(f"unexpected reason codes {unexpected_reasons}")
    for gate in expected["forbidden_blocked_gates"]:
        if gate in audit["blocked_gates"]:
            problems.append(f"unexpectedly blocked at {gate}")
    for gate in expected["required_blocked_gates"]:
        if gate not in audit["blocked_gates"]:
            problems.append(f"expected a block at {gate}, got {audit['blocked_gates']}")
    if len(dag["first_wave"]) != expected["wave0_workers"]:
        problems.append(
            f"wave-0 has {len(dag['first_wave'])} workers "
            f"({dag['first_wave']}), expected {expected['wave0_workers']}"
        )
    if expected["wave0_disjoint"] and not dag["first_wave_disjoint"]:
        problems.append(f"wave-0 write sets overlap: {dag['first_wave_write_overlap']}")
    if not dag["declared_write_paths_match_ownership"]:
        problems.append("declared write paths do not match the owned paths")
    return {
        "family": task.family,
        "expected": expected,
        "observed_route": audit["route"],
        "observed_reason_codes": audit["reason_codes"],
        "observed_blocked_gates": audit["blocked_gates"],
        "wave0": dag["first_wave"],
        "passed": not problems,
        "problems": problems,
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
    facts = task_taskspec_parity()
    return {
        "groups": groups,
        "all_groups_equal": all(entry["equal"] for entry in groups),
        "cross_topology": cross_topology,
        "task_facts": facts,
        "all_task_facts_equal": all(
            all(
                value is True
                for key, value in record.items()
                if key.startswith("same_") or key.endswith("_carry_shared_context")
            )
            for record in facts.values()
        ),
    }


def config_binding_probe() -> dict[str, Any]:
    """Prove three config facts in throwaway copies, never in the frozen files.

    1. the arm digest is bound to the config's **content**, not its file name;
    2. a comment-only edit keeps the identity;
    3. any effective difference other than ``context.prompt_layout`` breaks layout parity;
    4. the registration digest is identical from two different checkouts of the same content.
    """

    roots = [
        Path(tempfile.mkdtemp(prefix=f"closure2-r1-config-{name}-"))
        for name in ("base", "copy", "moved", "comment", "skewed")
    ]
    base, copy_root, moved, commented, skewed = roots
    try:
        legacy_text = (REPO_ROOT / LEGACY_CONTEXT_CONFIG).read_text(encoding="utf-8")
        shared_text = (REPO_ROOT / SHARED_PREFIX_CONFIG).read_text(encoding="utf-8")
        faithful = {LEGACY_CONTEXT_CONFIG: legacy_text, SHARED_PREFIX_CONFIG: shared_text}
        for root in (base, copy_root, moved, commented, skewed):
            _write_files(root, faithful)

        moved_model = shared_text.replace("model: deepseek-chat", "model: deepseek-reasoner")
        if moved_model == shared_text:
            raise ValueError("the frozen shared-prefix config no longer names provider.model")
        _write_files(moved, {SHARED_PREFIX_CONFIG: moved_model})

        # Comments and trailing whitespace are not effective configuration.
        _write_files(
            commented,
            {SHARED_PREFIX_CONFIG: "# probe: identity must ignore this\n" + shared_text + "\n\n"},
        )

        skewed_tools = shared_text.replace("max_results: 5", "max_results: 6")
        if skewed_tools == shared_text:
            raise ValueError("the frozen shared-prefix config no longer sets tools.max_results")
        _write_files(skewed, {SHARED_PREFIX_CONFIG: skewed_tools})

        path_rejections: dict[str, bool] = {}
        for label, candidate in (
            ("absolute_posix", "/etc/passwd"),
            ("absolute_windows", "C:\\Windows\\win.ini"),
            ("parent_escape", "../outside.yaml"),
            ("nested_parent_escape", "experiments/../../outside.yaml"),
            ("empty", ""),
        ):
            try:
                resolve_repo_path(candidate, root=base)
            except (ValueError, TypeError):
                path_rejections[label] = True
            else:
                path_rejections[label] = False

        base_config = config_semantic_digest(SHARED_PREFIX_CONFIG, root=base)
        base_arm = arm_config_digest("A1-P", root=base)
        parity = layout_ablation_delta(root=skewed)

        facts = {
            "same_content_two_roots_same_registration_digest": (
                preregistration_digest(root=base) == preregistration_digest(root=copy_root)
            ),
            "same_content_two_roots_same_live_digest": (
                preregistration_digest(root=base) == preregistration_digest()
            ),
            "comment_only_keeps_config_digest": (
                config_semantic_digest(SHARED_PREFIX_CONFIG, root=commented) == base_config
            ),
            "comment_only_keeps_arm_digest": (
                arm_config_digest("A1-P", root=commented) == base_arm
            ),
            "value_change_moves_config_digest": (
                config_semantic_digest(SHARED_PREFIX_CONFIG, root=moved) != base_config
            ),
            "value_change_moves_arm_digest": (arm_config_digest("A1-P", root=moved) != base_arm),
            "value_change_moves_registration_digest": (
                preregistration_digest(root=moved) != preregistration_digest(root=base)
            ),
            "other_key_change_breaks_layout_parity": parity["only_layout_differs"] is False,
            "other_key_change_is_named": "tools.max_results" in parity["differing_keys"],
            "initial_fixture_is_unaffected": layout_ablation_delta(root=base)["only_layout_differs"]
            is True,
        }
        rejected_all = all(path_rejections.values())
        return {
            "passed": all(facts.values()) and rejected_all,
            "facts": facts,
            "path_rejections": path_rejections,
            "skewed_differing_keys": parity["differing_keys"],
        }
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


def layout_executable() -> dict[str, Any]:
    legacy = load_config(resolve_repo_path(LEGACY_CONTEXT_CONFIG))
    shared = load_config(resolve_repo_path(SHARED_PREFIX_CONFIG))
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
    ablation = layout_ablation_delta()
    return {
        "legacy_config": LEGACY_CONTEXT_CONFIG,
        "legacy_resolved_layout": legacy.context.prompt_layout,
        "legacy_ok": legacy.context.prompt_layout == "legacy",
        "shared_prefix_config": SHARED_PREFIX_CONFIG,
        "shared_prefix_resolved_layout": shared.context.prompt_layout,
        "shared_prefix_ok": shared.context.prompt_layout == "shared_prefix",
        "policy_task_scoped": shared.context.policy == "task_scoped",
        "renderer_stable": shared.context.renderer == "stable_deterministic",
        "transform_identity": shared.context.transform.name == "identity",
        "unknown_layout_rejected": unknown_rejected,
        "ablation_differing_keys": ablation["differing_keys"],
        "ablation_single_variable": ablation["only_layout_differs"],
    }


def budget_protocol() -> dict[str, Any]:
    """Parameters only. Approval state lives in the record and never enters the frozen digest."""

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
    if "approval" in BUDGET:
        problems.append("approval state must not live inside the frozen budget parameters")
    return {
        "single": single,
        "multi": multi,
        "shared": BUDGET["shared"],
        "semantics": BUDGET_SEMANTICS,
        "problems": problems,
        "consistent": not problems,
    }


def _absolute_paths_in_payload(payload: dict[str, Any]) -> list[str]:
    """Any host-absolute path inside the frozen payload would break cross-machine reproducibility."""

    found: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{trail}.{key}" if trail else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")
        elif isinstance(node, str) and (node.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", node)):
            found.append(f"{trail}={node}")

    walk(payload, "")
    # The container workspace root is a path *inside* the sandbox, not a host path.
    return [item for item in found if "/workspace/project" not in item]


# ---------------------------------------------------------------------------------- entry point


def run_preflight() -> dict[str, Any]:
    per_task: dict[str, Any] = {}
    for task in TASKS:
        selftest = task_selftest(task)
        dag = dag_audit(task)
        audit = router_audit(task)
        per_task[task.task_id] = {
            "family": task.family,
            "selftest": selftest,
            "workspace_isolation": workspace_isolation(task),
            "frozen_identity": frozen_identity(task),
            "answer_leak_guard": answer_leak_guard(task),
            "dag": dag,
            "router": audit,
            "router_gate": router_gate(task, audit, dag),
        }

    parity = digest_parity()
    layout = layout_executable()
    binding = config_binding_probe()
    budget = budget_protocol()
    payload = preregistration_payload()
    gate = approval_gate()
    router_policy_digest = routing_policy(ROUTING_POLICY_VERSION).digest()
    absolute_paths = _absolute_paths_in_payload(payload)
    current_digest = preregistration_digest()

    checks: dict[str, bool] = {}
    for task_id, record in per_task.items():
        selftest = record["selftest"]
        checks[f"{task_id}:seed_public_fails"] = selftest["seed_public_fails"]
        checks[f"{task_id}:seed_hidden_fails"] = selftest["seed_hidden_fails"]
        checks[f"{task_id}:reference_public_passes"] = selftest["reference_public_passes"]
        checks[f"{task_id}:reference_hidden_passes"] = selftest["reference_hidden_passes"]
        checks[f"{task_id}:hidden_oracle_executes"] = selftest["hidden_oracle_really_executes"]
        checks[f"{task_id}:oracle_sessions_independent"] = all(selftest["independence"].values())
        checks[f"{task_id}:no_harness_failure"] = not (
            {
                selftest["seed_public"]["kind"],
                selftest["seed_hidden"]["kind"],
                selftest["reference_public"]["kind"],
                selftest["reference_hidden"]["kind"],
            }
            & _HARNESS_FAILURES
        )
        checks[f"{task_id}:frozen_task_identity"] = record["frozen_identity"]["matches"]
        checks[f"{task_id}:answer_leak_guard"] = record["answer_leak_guard"]["passed"]
        checks[f"{task_id}:dag_legal"] = record["dag"]["dependency_complete"]
        checks[f"{task_id}:dag_integration_dependencies"] = record["dag"][
            "integration_dependencies_are_declared_steps"
        ]
        checks[f"{task_id}:declared_write_paths_match_ownership"] = record["dag"][
            "declared_write_paths_match_ownership"
        ]
        checks[f"{task_id}:router_decision_expected"] = record["router_gate"]["passed"]
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
    checks["layout_ablation_is_single_variable"] = layout["ablation_single_variable"]
    checks["config_digest_bound_to_content"] = binding["passed"]
    checks["budget_protocol_consistent"] = budget["consistent"]

    # The frozen identity, compared against a constant pinned in source — not against itself.
    checks["preregistration_digest_matches_frozen_r1"] = (
        current_digest == C2A_R1_PREREGISTRATION_DIGEST
    )
    checks["preregistration_digest_differs_from_c2a"] = current_digest != C2A_PREREGISTRATION_DIGEST
    checks["preregistration_supersedes_c2a"] = payload["supersedes"] == {
        "protocol_revision": "closure-2a",
        "preregistration_digest": C2A_PREREGISTRATION_DIGEST,
        "task_commit": "7f5c1eae12e466e6a37ce2a7d77608ba65a009a5",
        "head_commit": "ac7af16b4506afafe6e932e2cc351b04354b9f57",
    }
    checks["no_absolute_path_in_frozen_payload"] = not absolute_paths
    checks["native_arms_carry_no_config"] = all(
        payload["arms"][arm]["config_semantic_digest"] is None
        for arm, entry in ARMS.items()
        if entry["config"] is None
    )

    # Approval independence, exercised rather than asserted: sign a fake record, ask the gate, and
    # confirm the frozen digest did not move.
    simulated = approval_gate(
        record={
            "status": "APPROVED",
            "approved_digest": current_digest,
            "approved_by": "preflight-simulation",
            "approval_commit": "preflight-simulation",
            "approved_at": "preflight-simulation",
        }
    )
    checks["approval_outside_frozen_identity"] = (
        "approval" not in BUDGET
        and "approval" not in payload
        and preregistration_digest() == current_digest
        and simulated["execution_ready"] is True
    )
    wrong_digest = approval_gate(
        record={
            "status": "APPROVED",
            "approved_digest": "0" * 64,
            "approved_by": "preflight-simulation",
            "approval_commit": "preflight-simulation",
            "approved_at": "preflight-simulation",
        }
    )
    checks["wrong_digest_approval_fails_closed"] = wrong_digest["execution_ready"] is False
    # A record is either a pending record with no claimed digest, or a usable approval. Anything else
    # (an approval naming a stale digest, an unknown status) is a broken record and must fail.
    if gate["execution_ready"]:
        checks["approval_record_consistent"] = True
    else:
        checks["approval_record_consistent"] = (
            APPROVAL_RECORD["status"] == "PENDING_OWNER_APPROVAL"
            and APPROVAL_RECORD["approved_digest"] is None
        )
    # The enforcement point a live run must pass through: it may only be traversable when ready.
    enforcement_ok = gate["execution_ready"]
    if not enforcement_ok:
        try:
            require_execution_approval()
        except PermissionError:
            enforcement_ok = True
        else:
            enforcement_ok = False
    checks["execution_blocked_while_not_approved"] = enforcement_ok

    checks["router_policy_digest_frozen"] = router_policy_digest == ROUTING_POLICY_DIGEST
    checks["arms_are_distinct"] = len(set(arm_config_digest(arm) for arm in ARMS)) == len(ARMS)
    checks["preregistration_complete"] = all(
        key in payload
        for key in (
            "tasks",
            "arms",
            "budget",
            "comparisons",
            "execution_order",
            "metrics",
            "exclusions",
            "protocol_revision",
            "supersedes",
        )
    )

    oracle_independence = {
        task_id: {
            "seed_public": record["selftest"]["seed_public"]["kind"],
            "seed_hidden": record["selftest"]["seed_hidden"]["kind"],
            "reference_public": record["selftest"]["reference_public"]["kind"],
            "reference_hidden": record["selftest"]["reference_hidden"]["kind"],
            "hidden_oracle_executed_tests": record["selftest"]["hidden_oracle_executed_tests"],
            "seed_hidden_failed_without_executing_assertions": (
                record["selftest"]["seed_hidden_fails"]
                and not record["selftest"]["seed_hidden_executed_assertions"]
            ),
            "seed_hidden_cause": record["selftest"]["hidden_collection_diagnostics"],
        }
        for task_id, record in per_task.items()
    }

    return {
        "stage": "closure-2a-r1",
        "protocol_revision": PROTOCOL_REVISION,
        "kind": "deterministic_benchmark_preflight",
        "supersedes": payload["supersedes"],
        "generated_at": datetime.now(UTC).isoformat(),
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset_version": DATASET_VERSION,
        "model_calls": 0,
        "sandbox_runs": 0,
        "tasks": per_task,
        "oracle_independence": oracle_independence,
        "parity": parity,
        "layout": layout,
        "config_binding": binding,
        "budget": budget,
        "approval": gate,
        "execution_ready": gate["execution_ready"],
        "router_policy": {
            "version": ROUTING_POLICY_VERSION,
            "digest": router_policy_digest,
            "frozen_digest": ROUTING_POLICY_DIGEST,
        },
        "preregistration_digest": current_digest,
        "frozen_preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
        "c2a_preregistration_digest": C2A_PREREGISTRATION_DIGEST,
        "execution_order": EXECUTION_ORDER,
        "metrics": METRICS,
        "unsupported": UNSUPPORTED,
        "checks": checks,
        "status": "PASSED" if all(checks.values()) else "FAILED",
        "absolute_paths_in_payload": absolute_paths,
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="closure-2a-r1 deterministic benchmark preflight")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    payload = run_preflight()
    _write(Path(args.out), payload)
    failed = sorted(name for name, passed in payload["checks"].items() if not passed)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "execution_ready": payload["execution_ready"],
                "approval": payload["approval"]["status"],
                "checks": len(payload["checks"]),
                "failed": failed,
                "preregistration_digest": payload["preregistration_digest"],
                "c2a_preregistration_digest": payload["c2a_preregistration_digest"],
            },
            indent=2,
        )
    )
    return 0 if payload["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
