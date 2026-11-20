"""Stage B protocol CLI.

There is no ``--run`` here on purpose. This stage designs the benchmark, proves the fixtures and
freezes the plan; executing it needs a separate authorization and a runner, neither of which exists
yet. A missing command is a cheaper guarantee than a guarded one.

    --offline   run the six-task quality suite (no model, no sandbox, no network)
    --plan      rebuild protocol.json and plan.json from the code
    --freeze    write freeze.json; refuses unless --offline passed
    --estimate  write estimate.json from the frozen price block and the two observed pilots
    --coverage  write coverage.json, the metric coverage matrix
    --check     re-derive everything and report READY or BLOCKED
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_DIR = REPO_ROOT / "experiments" / "stage_benchmark"
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_benchmark import estimate as estimate_module  # noqa: E402
from experiments.stage_benchmark import protocol as protocol_module  # noqa: E402
from experiments.stage_benchmark import reconstruct, schema  # noqa: E402
from experiments.stage_benchmark.tasks import SPECS, STRUCTURES, TASK_IDS  # noqa: E402
from experiments.stage_benchmark.tasks import base as task_base  # noqa: E402

OFFLINE_PATH = STAGE_DIR / "offline.json"
FREEZE_PATH = STAGE_DIR / "freeze.json"
ESTIMATE_PATH = STAGE_DIR / "estimate.json"
COVERAGE_PATH = STAGE_DIR / "coverage.json"
ARCHIVE_DIR = STAGE_DIR / "archive" / "v1.0"

#: The plan published by protocol 1.0.0. Metric semantics may move; the order may not.
HISTORICAL_PLAN_DIGEST = "sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648"

RUNTIME_SOURCE_ROOT = REPO_ROOT / "src" / "atlasos"

HARNESS_FILES = (
    "protocol.py",
    "matrix.py",
    "reconstruct.py",
    "schema.py",
    "estimate.py",
    "run_stage_b.py",
    "tasks/__init__.py",
    "tasks/base.py",
    "tasks/slugkit.py",
    "tasks/pager.py",
    "tasks/zonelint.py",
    "tasks/ledgerclose.py",
    "tasks/retrygate.py",
    "tasks/cascade.py",
)

#: Earlier stages whose frozen artefacts must not move. Digests only; the files are not read for
#: content and are never rewritten.
HISTORICAL_ARTEFACTS = (
    "experiments/stage_p1b_live/fixture_manifest.json",
    "experiments/stage_b0_controlled/freeze.json",
    "experiments/stage_s1_prompt_v2/freeze.json",
    "experiments/stage_s1_prompt_v2/prompts/main_v2.md",
    "experiments/stage_s2_prompt_v21/freeze.json",
    "experiments/stage_s2_prompt_v21/prompts/main_v21.md",
    "experiments/fixtures/stage_s1_releaseguard/README.md",
    "experiments/fixtures/stage_s2_workgrid/README.md",
)

#: The source tree of the last frozen stage. Used to prove the benchmark runs the same runtime.
S2_SOURCE_TREE_DIGEST = "sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae"

#: A task outside the matrix, used to prove the runner collects a complete bundle before the matrix
#: starts. It is not scored and its result never enters an arm.
CALIBRATION = {
    "task": "workgrid",
    "source": "experiments/fixtures/stage_s2_workgrid",
    "why": "it is already solved, already has an oracle, and is not one of the six benchmark tasks",
    "purpose": "prove the evidence collector produces a complete bundle on a live run",
    "status": "PREPARED_NOT_RUN",
    "reason_not_run": "this stage may not call the model",
    "required_evidence": [
        "session.json",
        "attempt-1.json",
        "transcript.jsonl",
        "tool_conversations.json",
        "workspace.patch",
        "trajectory/",
        "analysis record with counts, consistency and gaps",
    ],
}


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60
    )
    return completed.stdout.strip()


def git_identity() -> dict[str, Any]:
    status = git("status", "--porcelain")
    return {
        "git_head": git("rev-parse", "HEAD"),
        "git_branch": git("branch", "--show-current"),
        "git_status": status,
        "dirty_paths": [line for line in status.splitlines() if line.strip()],
    }


def _tree_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
            continue
        files[relative.as_posix()] = digest_bytes(path.read_bytes())
    return files


def source_identity() -> dict[str, Any]:
    files = _tree_files(RUNTIME_SOURCE_ROOT)
    return {
        "root": "src/atlasos",
        "file_count": len(files),
        "tree_digest": digest_text(json.dumps(files, sort_keys=True)),
        "files": files,
    }


def harness_identity() -> dict[str, str]:
    return {name: digest_bytes((STAGE_DIR / name).read_bytes()) for name in HARNESS_FILES}


# --------------------------------------------------------------------------- offline


def _fresh_scratch() -> Path:
    """A scratch root that does not exist yet. Nothing is deleted to make room for a run."""

    base = Path(tempfile.gettempdir()) / "atlas-stage-b-offline"
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = base.with_name(f"{base.name}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free scratch path next to {base}")


def run_offline() -> dict[str, Any]:
    """The six-task quality suite plus the protocol-level structural checks."""

    scratch = _fresh_scratch()
    tasks: dict[str, Any] = {}
    for spec in SPECS:
        tasks[spec.task_id] = task_base.verify_solvability(spec, scratch / spec.task_id)

    structures = [spec.structure for spec in SPECS]
    structural = {
        "two_tasks_per_structure": all(structures.count(name) == 2 for name in STRUCTURES),
        "no_historical_task_reused": not (
            set(TASK_IDS) & {"calculator", "eventpipe", "releaseguard", "workgrid"}
        ),
        "every_task_has_three_wrong_variants": all(len(spec.wrong) >= 3 for spec in SPECS),
        "every_write_scope_is_inside_the_package": all(
            all(not path.startswith("/") and ".." not in path for path in spec.write_scope)
            for spec in SPECS
        ),
        "goals_carry_no_experiment_language": all(
            not any(
                word in spec.goal.lower()
                for word in ("delegate", "subworker", "benchmark", "oracle", "prompt")
            )
            for spec in SPECS
        ),
        "oracle_marker_absent_from_every_fixture": all(
            task_base.ORACLE_OK not in text for spec in SPECS for text in spec.files.values()
        ),
        "reference_paths_are_inside_the_write_scope": all(
            all(path in spec.write_scope for path in spec.reference) for spec in SPECS
        ),
    }

    report = {
        "stage": "B",
        "generated_at": datetime.now(UTC).isoformat(),
        "tasks": tasks,
        "structural_checks": structural,
        "task_count": len(tasks),
        "wrong_variants": sum(len(spec.wrong) for spec in SPECS),
        "rejected_variants": sum(
            1
            for result in tasks.values()
            for value in (result.get("wrong_variants") or {}).values()
            if value.get("rejected_by_oracle")
        ),
        "passed": all(result.get("ok") for result in tasks.values()) and all(structural.values()),
    }
    OFFLINE_PATH.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return report


# --------------------------------------------------------------------------- plan / freeze


def build_and_write() -> dict[str, Any]:
    document = protocol_module.build_protocol()
    problems = protocol_module.validate(document)
    if problems:
        raise SystemExit("protocol does not validate:\n  " + "\n  ".join(problems))
    protocol_module.write_protocol(document)
    protocol_module.write_plan(document)
    return document


def build_estimate(document: Mapping[str, Any]) -> dict[str, Any]:
    cells = len(document["plan"]["cells"])
    payload = estimate_module.from_repository(
        cells=cells, budget_tokens=document["run_config"]["token_budget"]
    )
    payload["stage"] = "B"
    payload["protocol_version"] = protocol_module.PROTOCOL_VERSION
    ESTIMATE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def write_freeze(
    document: Mapping[str, Any], estimate_payload: Mapping[str, Any]
) -> dict[str, Any]:
    if not OFFLINE_PATH.is_file():
        raise SystemExit("refusing to freeze: --offline has not been run")
    offline = json.loads(OFFLINE_PATH.read_text(encoding="utf-8"))
    if not offline.get("passed"):
        raise SystemExit("refusing to freeze: the offline suite did not pass")
    if not COVERAGE_PATH.is_file():
        raise SystemExit("refusing to freeze: --coverage has not been run")
    coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))

    protocol_bytes = (STAGE_DIR / "protocol.json").read_bytes()
    historical = {}
    for relative in HISTORICAL_ARTEFACTS:
        path = REPO_ROOT / relative
        historical[relative] = digest_bytes(path.read_bytes()) if path.is_file() else None

    payload = {
        "stage": "B",
        "protocol_version": protocol_module.PROTOCOL_VERSION,
        "frozen_at": datetime.now(UTC).isoformat(),
        "git": git_identity(),
        "protocol": {
            "digest": digest_bytes(protocol_bytes),
            "path": "experiments/stage_benchmark/protocol.json",
        },
        "plan": {
            "digest": document["plan"]["digest"],
            "cells": len(document["plan"]["cells"]),
            "seed": document["plan"]["seed"],
            "repetitions": document["plan"]["repetitions"],
        },
        "runtime": {
            "root": "src/atlasos",
            "file_count": source_identity()["file_count"],
            "tree_digest": source_identity()["tree_digest"],
        },
        "prompts": {
            arm["arm_id"]: {
                "label": arm["label"],
                "digest": arm["observed_digest"],
                "chars": arm["chars"],
                "path": arm["path"],
            }
            for arm in document["arms"]
        },
        "tasks": {
            task["task_id"]: {
                "structure": task["structure"],
                "fixture_digest": task["fixture_digest"],
                "goal_digest": task["goal_digest"],
                "oracle_digest": task["oracle_digest"],
                "reference_digest": task["reference_digest"],
                "wrong_variants_digest": task["wrong_variants_digest"],
                "public_suite": task["public_suite"],
                "write_scope": task["write_scope"],
            }
            for task in document["tasks"]
        },
        "config": dict(document["run_config"]),
        "harness": harness_identity(),
        "analysis": {
            "schema_version": schema.SCHEMA_VERSION,
            "reconstruct_version": reconstruct.ANALYZER_VERSION,
        },
        "offline": {
            "path": "experiments/stage_benchmark/offline.json",
            "digest": digest_bytes(OFFLINE_PATH.read_bytes()),
            "tasks": offline.get("task_count"),
            "wrong_variants": offline.get("wrong_variants"),
        },
        "estimate": {
            "path": "experiments/stage_benchmark/estimate.json",
            "digest": digest_bytes(ESTIMATE_PATH.read_bytes())
            if ESTIMATE_PATH.is_file()
            else digest_text(json.dumps(estimate_payload, sort_keys=True)),
            "cells": estimate_payload["cells"],
        },
        "coverage": {
            "path": "experiments/stage_benchmark/coverage.json",
            "digest": digest_bytes(COVERAGE_PATH.read_bytes()),
            "metrics": len(coverage.get("rows") or []),
        },
        "archive": {
            "path": "experiments/stage_benchmark/archive/v1.0/manifest.json",
            "digest": digest_bytes((ARCHIVE_DIR / "manifest.json").read_bytes()),
        },
        "historical": historical,
        "calibration": dict(CALIBRATION),
    }
    FREEZE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


# --------------------------------------------------------------------------- check


def _check(ok: bool, label: str, detail: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "label": label, "detail": detail}


def build_coverage(document: Mapping[str, Any]) -> dict[str, Any]:
    """The metric coverage matrix: evidence path, resolver, status on a complete record.

    A resolver proves the metric can be *computed*; the status proves the evidence is *there*. A
    metric that reads UNKNOWN on a complete record is a semantic fact; one that reads UNSUPPORTED is
    a schema defect, and the check in ``check_all`` refuses it.
    """

    record = _synthetic_records()[0]
    rows = []
    counts: dict[str, int] = {}
    for metric in document["metrics"]:
        status = schema.metric_status(record, metric["id"])
        counts[status] = counts.get(status, 0) + 1
        rows.append(
            {
                "metric": metric["id"],
                "group": metric["group"],
                "definition": metric.get("definition"),
                "numerator": metric.get("numerator"),
                "denominator": metric["denominator"],
                "evidence_source": schema.EVIDENCE_PATHS.get(metric["id"]),
                "resolver": metric["id"] in schema.RESOLVERS,
                "returns": metric.get("returns", "value"),
                "missing_policy": metric["missing"],
                "counterexample_test": metric.get("counterexample"),
                "status_on_complete_record": status,
            }
        )
    payload = {
        "protocol_version": document["protocol_version"],
        "schema_version": schema.SCHEMA_VERSION,
        "analyzer_version": reconstruct.ANALYZER_VERSION,
        "metric_count": len(rows),
        "status_counts": counts,
        "rows": rows,
    }
    COVERAGE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _archive_check() -> dict[str, Any]:
    """The 1.0 artifacts are still on disk and still hash to what 1.0 published."""

    manifest_path = ARCHIVE_DIR / "manifest.json"
    if not manifest_path.is_file():
        return _check(False, "archive_matches_published_v1_0", "no archive manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for name, entry in (manifest.get("files") or {}).items():
        path = ARCHIVE_DIR / name
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        actual = digest_bytes(path.read_bytes())
        if actual != entry.get("digest"):
            problems.append(f"{name} digest moved")
    return _check(
        not problems,
        "archive_matches_published_v1_0",
        "; ".join(problems) or f"{len(manifest.get('files') or {})} files intact",
    )


def _archive_invariants(document: Mapping[str, Any]) -> dict[str, Any]:
    """1.0 froze the inputs; 1.1 may only have moved the statistics.

    Every model-facing input — the run configuration, the three prompt bytes, the six task digests
    and the 36-cell plan — is compared against the archived 1.0 freeze. A difference here means the
    revision changed the experiment instead of its measurement.
    """

    path = ARCHIVE_DIR / "freeze.json"
    if not path.is_file():
        return _check(False, "inputs_unchanged_since_1_0", "no archived 1.0 freeze")
    archived = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    if dict(archived.get("config") or {}) != dict(document["run_config"]):
        problems.append("run_config moved")

    old_prompts = archived.get("prompts") or {}
    new_prompts = {arm["arm_id"]: arm["observed_digest"] for arm in document["arms"]}
    for arm_id, entry in old_prompts.items():
        if new_prompts.get(arm_id) != entry.get("digest"):
            problems.append(f"arm {arm_id} bytes moved")

    if (archived.get("plan") or {}).get("digest") != document["plan"]["digest"]:
        problems.append("plan digest moved")

    old_tasks = archived.get("tasks") or {}
    for task in document["tasks"]:
        entry = old_tasks.get(task["task_id"]) or {}
        for field in (
            "fixture_digest",
            "goal_digest",
            "oracle_digest",
            "reference_digest",
            "wrong_variants_digest",
        ):
            if entry.get(field) != task[field]:
                problems.append(f"{task['task_id']}.{field} moved")

    if (archived.get("runtime") or {}).get("tree_digest") != source_identity()["tree_digest"]:
        problems.append("runtime source moved")

    return _check(
        not problems,
        "inputs_unchanged_since_1_0",
        "; ".join(problems) or "config, prompts, tasks and plan identical to 1.0",
    )


def _counterexample_check(document: Mapping[str, Any]) -> dict[str, Any]:
    """Every row of the coverage matrix names a test that exists."""

    test_file = REPO_ROOT / "tests" / "integration" / "test_stage_b_metric_closure.py"
    if not test_file.is_file():
        return _check(False, "every_metric_names_a_real_counterexample", "the test file is missing")
    text = test_file.read_text(encoding="utf-8")
    missing = sorted(
        {
            str(metric.get("counterexample"))
            for metric in document["metrics"]
            if f"def {metric.get('counterexample')}(" not in text
        }
    )
    return _check(
        not missing,
        "every_metric_names_a_real_counterexample",
        f"tests not found: {missing}" if missing else f"{len(document['metrics'])} rows",
    )


def check_all() -> dict[str, Any]:
    """Re-derive everything and report. Read-only: writes nothing."""

    checks: list[dict[str, Any]] = []
    if not protocol_module.PROTOCOL_PATH.is_file():
        return {"status": "BLOCKED", "checks": [_check(False, "protocol_present", "run --plan")]}

    document = protocol_module.read_protocol()
    problems = protocol_module.validate(document)
    checks.append(_check(not problems, "protocol_valid", "; ".join(problems[:4])))

    rebuilt = protocol_module.build_protocol()
    checks.append(
        _check(
            rebuilt["plan"]["digest"] == document["plan"]["digest"],
            "plan_reproducible",
            f"{len(rebuilt['plan']['cells'])} cells",
        )
    )
    arms_match = all(
        left["observed_digest"] == right["observed_digest"]
        for left, right in zip(rebuilt["arms"], document["arms"], strict=False)
    )
    checks.append(
        _check(arms_match, "prompt_digests_stable", "arms re-derived from the prompt files")
    )

    declarable = [
        arm["arm_id"]
        for arm in document["arms"]
        if arm["observed_digest"] != arm["declared_digest"]
    ]
    checks.append(
        _check(not declarable, "prompts_match_declared_digests", f"mismatched arms: {declarable}")
    )

    source = source_identity()
    checks.append(
        _check(
            source["tree_digest"] == S2_SOURCE_TREE_DIGEST,
            "runtime_matches_last_frozen_stage",
            f"{source['tree_digest'][:23]} over {source['file_count']} files",
        )
    )

    metric_ids = [item["id"] for item in document["metrics"]]
    missing = schema.unresolvable(metric_ids)
    checks.append(_check(not missing, "every_metric_has_a_resolver", f"missing: {missing}"))

    # A resolver is not coverage: each metric must name an evidence path the record actually has.
    without_evidence = sorted(
        metric["id"] for metric in document["metrics"] if metric["id"] not in schema.EVIDENCE_PATHS
    )
    checks.append(
        _check(
            not without_evidence,
            "every_metric_names_its_evidence",
            f"without an evidence path: {without_evidence}",
        )
    )
    statuses = {
        metric_id: schema.metric_status(_synthetic_records()[0], metric_id)
        for metric_id in metric_ids
    }
    unsupported = sorted(key for key, value in statuses.items() if value == "UNSUPPORTED")
    checks.append(
        _check(
            not unsupported,
            "a_complete_record_supports_every_metric",
            f"UNSUPPORTED on a complete record: {unsupported}",
        )
    )

    # The plan, the arms and the run configuration are frozen; only metric semantics moved.
    checks.append(
        _check(
            document["plan"]["digest"] == HISTORICAL_PLAN_DIGEST,
            "plan_digest_unchanged",
            f"{document['plan']['digest'][:23]} vs {HISTORICAL_PLAN_DIGEST[:23]}",
        )
    )
    archive = _archive_check()
    checks.append(archive)
    checks.append(_archive_invariants(document))
    checks.append(_counterexample_check(document))

    offline = json.loads(OFFLINE_PATH.read_text(encoding="utf-8")) if OFFLINE_PATH.is_file() else {}
    checks.append(
        _check(
            bool(offline.get("passed")),
            "offline_quality_passed",
            f"{offline.get('task_count', 0)} tasks, {offline.get('rejected_variants', 0)} variants",
        )
    )

    checks.append(
        _check(
            all(cell["task_id"] != CALIBRATION["task"] for cell in document["plan"]["cells"]),
            "calibration_task_not_in_matrix",
            CALIBRATION["task"],
        )
    )

    examples = _synthetic_records()
    record_problems = [schema.validate_record(record) for record in examples]
    checks.append(
        _check(
            not any(record_problems),
            "result_schema_accepts_a_complete_record",
            f"problems: {record_problems[0] if record_problems[0] else 'none'}",
        )
    )

    evidence = _real_evidence_checks()
    checks.extend(evidence)

    status = "READY" if all(item["ok"] for item in checks) else "BLOCKED"
    return {
        "status": status,
        "checks": checks,
        "protocol_version": document["protocol_version"],
        "cells": len(document["plan"]["cells"]),
        "arms": [arm["arm_id"] for arm in document["arms"]],
        "tasks": [task["task_id"] for task in document["tasks"]],
    }


def _synthetic_records() -> list[dict[str, Any]]:
    base = {
        "analyzer": {"version": reconstruct.ANALYZER_VERSION},
        "run": {
            "run_id": "synthetic",
            "task_id": TASK_IDS[0],
            "arm_id": "P1",
            "repetition": 1,
            "status": "succeeded",
            "acceptance": "PASS",
            "stop_reason": "success",
        },
        "prompt": {"version": "main-v1.0", "digest": "sha256:0"},
        "counts": {"provider_requests": 10, "assistant_turns": 10, "tool_calls": 12},
        "consistency": {"gaps": []},
        "delegation": {
            "proposed": False,
            "proposals": 0,
            "children": 0,
            "children_admitted": 0,
            "children_succeeded": 0,
            "first_delegation_turn": None,
            "first_implementation_turn": 5,
            "routes": [],
            "admission": {
                "proposals": 0,
                "admitted": 0,
                "rejected": 0,
                "unknown": 0,
                "state": "NO_PROPOSAL",
            },
            "levels": {
                "met": {
                    "L1_proposal": False,
                    "L2_admission": False,
                    "L3_worker_completion": False,
                    "L4_handoff_delivery": False,
                    "L5_main_continuation": False,
                    "L6_goal_correctness": True,
                },
                "level_reached": 0,
            },
        },
        "handoff": {
            "delivered": False,
            "usable": False,
            "payload_chars": 0,
            "overlap_files": None,
        },
        "capability": {"invalid": None, "basis": "no child was admitted"},
        "failure": {"primary": "none", "contributing": [], "confidence": "HIGH"},
        "usage": {
            "main_prompt_hit_tokens": 1,
            "main_prompt_miss_tokens": 1,
            "child_prompt_tokens": 0,
            "main_completion_tokens": 1,
            "child_completion_tokens": 0,
            "main_input_tokens": 2,
            "child_input_tokens": 0,
            "cache_split_state": "COMPLETE",
            "child_cache_visibility": "UNKNOWN",
            "sum_matches_system_total": True,
        },
        "context": {
            "main_peak_context_tokens": 1,
            "main_cumulative_input_tokens": 2,
            "main_cache_hit_tokens": 1,
            "main_cache_miss_tokens": 1,
            "child_total_prompt_tokens": 0,
            "completion_tokens": 1,
            "system_total_tokens": 3,
            "wall_clock_seconds": 1.0,
            "handoff_payload_chars": 0,
        },
        "verification": {
            "public_test_calls": 1,
            "public_tests_passed": True,
            "post_failure_edits": 0,
            "changed_files": 1,
            "ownership_denials": 0,
            "oracle": "PASS",
            "goal_passed": True,
        },
        "failure_class": "none",
    }
    return [base]


def _real_evidence_checks() -> list[dict[str, Any]]:
    """The reconstruction must reproduce the two runs whose truth we already know."""

    checks: list[dict[str, Any]] = []
    s1 = REPO_ROOT / "experiments/stage_s1_prompt_v2/evidence/run-1"
    s2 = REPO_ROOT / "experiments/stage_s2_prompt_v21/evidence/run-1"
    if s1.is_dir():
        record = reconstruct.reconstruct(s1)
        checks.append(
            _check(
                record["counts"]["provider_requests"] == 18
                and record["counts"]["tool_calls"] == 36
                and record["delegation"]["first_delegation_turn"] == 17,
                "s1_evidence_reconstructs",
                f"{record['counts']['provider_requests']} requests, "
                f"{record['counts']['tool_calls']} calls, "
                f"{record['counts']['turns_delta']} turn delta explained",
            )
        )
        # The Main's own counts must be fully accounted for. A gap that concerns the *child*
        # conversation store is recorded and tolerated: S1's transcript proposes 12 child actions
        # while the child's stored conversation holds 10, which is a fact about that run, not an
        # analyser defect, and hiding it would be the defect.
        main_gaps = [gap for gap in record["consistency"]["gaps"] if "session reports" in gap]
        checks.append(
            _check(
                record["consistency"]["turns_explained"] and not main_gaps,
                "s1_main_counts_explained",
                f"gaps recorded: {record['consistency']['gaps']}",
            )
        )
        # The accounting identity, on a real run: input = hit + miss, and the child prompt is in
        # the total exactly once.
        usage = record["usage"]
        split_identity = (
            usage["main_input_tokens"]
            == usage["main_prompt_hit_tokens"] + usage["main_prompt_miss_tokens"]
        )
        checks.append(
            _check(
                split_identity
                and record["consistency"]["cache_split_agrees"] is True
                and usage["sum_matches_system_total"] is True,
                "s1_usage_identity_holds",
                f"input={usage['main_input_tokens']} hit={usage['main_prompt_hit_tokens']} "
                f"miss={usage['main_prompt_miss_tokens']} child={usage['child_prompt_tokens']}",
            )
        )
        # The semantics this stage rebuilt, checked against the one run where they are known. S1
        # delegated once, the child was refused tools it did not have and died, and the run ended
        # on the budget with no file changed: L2 without L3, a budget failure, and an invalid
        # capability assignment.
        checks.append(
            _check(
                record["delegation"]["admission"]["state"] == "ADMITTED"
                and record["delegation"]["admission"]["admitted"] == 1
                and record["delegation"]["levels"]["level_reached"] == 2
                and record["capability"]["invalid"] is True
                and record["failure"]["primary"] == "budget"
                and record["handoff"]["delivered"] is True
                and record["handoff"]["usable"] is False,
                "s1_semantics_reconstruct",
                "admitted={} level={} invalid={} failure={} usable_handoff={}".format(
                    record["delegation"]["admission"]["admitted"],
                    record["delegation"]["levels"]["level_reached"],
                    record["capability"]["invalid"],
                    record["failure"]["primary"],
                    record["handoff"]["usable"],
                ),
            )
        )
    if s2.is_dir():
        record = reconstruct.reconstruct(s2)
        checks.append(
            _check(
                record["counts"]["provider_requests"] == 14
                and record["counts"]["assistant_turns"] == 13
                and record["counts"]["turns_delta"] == 1
                and record["counts"]["tool_calls"] == 24,
                "s2_evidence_reconstructs",
                f"{record['counts']['provider_requests']} requests, "
                f"{record['counts']['assistant_turns']} stored turns, "
                f"{record['counts']['tool_calls']} calls",
            )
        )
        checks.append(
            _check(
                not record["delegation"]["proposed"]
                and record["delegation"]["admission"]["state"] == "NO_PROPOSAL"
                and record["verification"]["goal_passed"] is True
                and record["failure"]["primary"] == "none",
                "s2_direct_pass_reconstructs",
                f"goal_passed={record['verification']['goal_passed']}, "
                f"admission={record['delegation']['admission']['state']}",
            )
        )
    return checks


# --------------------------------------------------------------------------- main


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B benchmark protocol")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--offline", action="store_true")
    group.add_argument("--plan", action="store_true")
    group.add_argument("--freeze", action="store_true")
    group.add_argument("--estimate", action="store_true")
    group.add_argument("--coverage", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.offline:
        report = run_offline()
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "tasks": {task: value["ok"] for task, value in report["tasks"].items()},
                    "wrong_variants": report["wrong_variants"],
                    "rejected_variants": report["rejected_variants"],
                    "path": str(OFFLINE_PATH),
                },
                indent=2,
            )
        )
        return 0 if report["passed"] else 2

    if args.plan:
        document = build_and_write()
        print(
            json.dumps(
                {
                    "cells": len(document["plan"]["cells"]),
                    "plan_digest": document["plan"]["digest"],
                    "balance": document["plan"]["balance"],
                    "first_five": document["plan"]["cells"][:5],
                    "paths": [
                        str(protocol_module.PROTOCOL_PATH),
                        str(protocol_module.PLAN_PATH),
                    ],
                },
                indent=2,
            )
        )
        return 0

    if args.freeze:
        if not protocol_module.PROTOCOL_PATH.is_file():
            raise SystemExit("refusing to freeze: run --plan first")
        document = protocol_module.read_protocol()
        estimate_payload = (
            json.loads(ESTIMATE_PATH.read_text(encoding="utf-8"))
            if ESTIMATE_PATH.is_file()
            else build_estimate(document)
        )
        payload = write_freeze(document, estimate_payload)
        print(
            json.dumps(
                {
                    "runtime": payload["runtime"]["tree_digest"],
                    "protocol": payload["protocol"]["digest"],
                    "plan": payload["plan"]["digest"],
                    "prompts": {key: value["digest"] for key, value in payload["prompts"].items()},
                    "path": str(FREEZE_PATH),
                },
                indent=2,
            )
        )
        return 0

    if args.estimate:
        if not protocol_module.PROTOCOL_PATH.is_file():
            raise SystemExit("refusing to estimate: run --plan first")
        document = protocol_module.read_protocol()
        payload = build_estimate(document)
        print(json.dumps(payload, indent=2)[:4000])
        print(f"\nwritten: {ESTIMATE_PATH}")
        return 0

    if args.coverage:
        document = protocol_module.read_protocol()
        payload = build_coverage(document)
        print(
            json.dumps(
                {
                    "metrics": payload["metric_count"],
                    "measured": payload["status_counts"].get("MEASURED", 0),
                    "unknown": payload["status_counts"].get("UNKNOWN", 0),
                    "unsupported": payload["status_counts"].get("UNSUPPORTED", 0),
                    "path": str(COVERAGE_PATH),
                },
                indent=2,
            )
        )
        return 0

    report = check_all()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
