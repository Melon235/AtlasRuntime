"""The frozen Stage B protocol document.

Three prompt arms, six tasks, two repetitions, one configuration. This module *builds* the protocol
from the repository's own frozen artefacts and validates it; it never runs a model. Every number in
the document is either read from a file (and hashed) or is a declared constant here.

Keeping the document built rather than hand-written is what makes the freeze check meaningful: if a
prompt file, a fixture or a limit changes, the rebuilt document differs and the freeze refuses.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import matrix
from .tasks import SPECS, STRUCTURES, TASK_IDS
from .tasks import base as task_base

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_DIR = REPO_ROOT / "experiments" / "stage_benchmark"
PROTOCOL_PATH = STAGE_DIR / "protocol.json"
PLAN_PATH = STAGE_DIR / "plan.json"

PROTOCOL_VERSION = "1.0.0"
ANALYSIS_SCHEMA_VERSION = "stage-b-result/1.0"

ARM_IDS: tuple[str, ...] = ("P1", "P2", "P3")

#: Where each arm's bytes come from. ``None`` means the shipped constant in the runtime module.
ARM_SOURCES: dict[str, dict[str, Any]] = {
    "P1": {
        "label": "main-v1.0",
        "digest": "sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a",
        "path": None,
        "provenance": "src/atlasos/app/main_driven.py::MAIN_SYSTEM_PROMPT (unchanged since P0.1)",
    },
    "P2": {
        "label": "main-v2.0-rc1",
        "digest": "sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323",
        "path": "experiments/stage_s1_prompt_v2/prompts/main_v2.md",
        "provenance": "Stage S1 frozen candidate",
    },
    "P3": {
        "label": "main-v2.1-rc1",
        "digest": "sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489",
        "path": "experiments/stage_s2_prompt_v21/prompts/main_v21.md",
        "provenance": "Stage S2 frozen candidate",
    },
}

RUN_CONFIG: dict[str, Any] = {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "main_max_turns": 20,
    "child_max_turns": 6,
    "max_concurrent_children": 2,
    "max_delegation_rounds": 3,
    "token_budget": 500_000,
    "tool_error_mode": "corrected",
    "public_test_max_calls": 4,
    "network": "none",
    "cpu": 1,
    "memory_gib": 1.0,
    "sandbox_provider": "opensandbox",
    "image": "atlas-uat-python:3.12",
    "worker_profiles": ("research_worker",),
    "router_policy": "m.1.0",
    "delegation": "enabled",
    "repetitions": 2,
    "seed": matrix.DEFAULT_SEED,
    #: ``deepseek-chat`` accepts no sampling seed. Repetitions are the only control over sampling.
    "sampling_seed_supported": False,
}

#: The five questions the benchmark exists to answer. A metric answering none of them is not used.
QUESTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "RQ1",
        "question": "Does a later prompt produce more appropriate delegation on tasks that have "
        "independent work?",
        "primary_metrics": ["delegation_proposal_rate", "effective_delegation_rate"],
    },
    {
        "id": "RQ2",
        "question": "Does a later prompt reduce inappropriate delegation and capability mismatch?",
        "primary_metrics": ["invalid_capability_delegation_rate", "late_delegation_rate"],
    },
    {
        "id": "RQ3",
        "question": "Does delegation reduce Main peak context?",
        "primary_metrics": ["main_peak_context_tokens"],
    },
    {
        "id": "RQ4",
        "question": "Do child tokens, total cost and latency justify the delegation?",
        "primary_metrics": [
            "system_total_tokens",
            "wall_clock_seconds",
            "child_total_prompt_tokens",
        ],
    },
    {
        "id": "RQ5",
        "question": "What do the prompts do to correctness, test completion and budget exhaustion?",
        "primary_metrics": ["oracle_pass_rate", "goal_success_rate", "budget_exhaustion_rate"],
    },
)

#: Every metric the result schema carries, with its denominator and its missing-value policy.
METRICS: tuple[dict[str, Any], ...] = (
    # Primary
    {
        "id": "oracle_pass_rate",
        "group": "primary",
        "unit": "rate",
        "numerator": "runs with oracle PASS",
        "denominator": "all attempts",
        "missing": "counted as FAIL, never dropped",
    },
    {
        "id": "goal_success_rate",
        "group": "primary",
        "unit": "rate",
        "numerator": "runs whose public suite and oracle both pass",
        "denominator": "all attempts",
        "missing": "counted as FAIL",
    },
    {
        "id": "budget_exhaustion_rate",
        "group": "primary",
        "unit": "rate",
        "numerator": "runs that stopped with budget_exceeded",
        "denominator": "all attempts",
        "missing": "UNKNOWN if the stop reason is absent",
    },
    {
        "id": "runtime_failure_rate",
        "group": "primary",
        "unit": "rate",
        "numerator": "runs that failed for a runtime, sandbox or tool fault",
        "denominator": "all attempts",
        "missing": "counted as FAIL, classified in failure_class",
    },
    # Delegation behaviour
    {
        "id": "delegation_proposal_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "runs with at least one delegate_tasks call",
        "denominator": "all attempts",
        "missing": "0 when the tool was on the surface",
    },
    {
        "id": "router_admission_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "proposals admitted by the router",
        "denominator": "proposals",
        "missing": "UNKNOWN when no proposal exists",
    },
    {
        "id": "child_completion_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "children that ended successfully",
        "denominator": "children started",
        "missing": "UNKNOWN when no child started",
    },
    {
        "id": "effective_delegation_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": (
            "runs where a proposal was admitted, a child succeeded and the Main changed a file "
            "after the handoff"
        ),
        "denominator": "all attempts",
        "missing": "counted as not effective",
    },
    {
        "id": "direct_execution_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "runs with no delegate_tasks call",
        "denominator": "all attempts",
        "missing": "0",
    },
    {
        "id": "invalid_capability_delegation_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "runs where a delegated objective required a tool the profile does not have",
        "denominator": "runs with a proposal",
        "missing": "UNKNOWN when no proposal exists",
    },
    {
        "id": "late_delegation_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "runs whose first delegate_tasks call came after turn "
        "LATE_DELEGATION_TURN_THRESHOLD, or after the first file edit",
        "denominator": "runs with a proposal",
        "missing": (
            "UNKNOWN when no proposal exists; the threshold is pre-registered, not discovered"
        ),
    },
    {
        "id": "first_delegation_turn",
        "group": "delegation",
        "unit": "turn",
        "numerator": "turn index of the first delegate_tasks call",
        "denominator": "runs with a proposal",
        "missing": "UNKNOWN when no proposal exists",
    },
    {
        "id": "first_implementation_turn",
        "group": "delegation",
        "unit": "turn",
        "numerator": "turn index of the first file_edit or file_write",
        "denominator": "all attempts",
        "missing": "UNKNOWN when nothing was written",
    },
    # Context and efficiency
    {
        "id": "main_peak_context_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "max prompt tokens over Main requests",
        "denominator": "runs",
        "missing": "UNKNOWN without provider usage",
    },
    {
        "id": "main_cumulative_input_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "sum of Main prompt tokens",
        "denominator": "runs",
        "missing": "UNKNOWN",
    },
    {
        "id": "main_cache_hit_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "prompt-cache hit tokens reported by the provider",
        "denominator": "runs",
        "missing": "UNKNOWN, never 0",
    },
    {
        "id": "main_cache_miss_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "prompt-cache miss tokens reported by the provider",
        "denominator": "runs",
        "missing": "UNKNOWN, never 0",
    },
    {
        "id": "child_total_prompt_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "sum of child prompt tokens",
        "denominator": "runs",
        "missing": "0 when no child ran",
    },
    {
        "id": "system_total_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "all prompt and completion tokens of the run",
        "denominator": "runs",
        "missing": "UNKNOWN",
    },
    {
        "id": "completion_tokens",
        "group": "context",
        "unit": "tokens",
        "numerator": "Main plus child completion tokens",
        "denominator": "runs",
        "missing": "UNKNOWN",
    },
    {
        "id": "wall_clock_seconds",
        "group": "context",
        "unit": "seconds",
        "numerator": "run duration measured by the runner",
        "denominator": "runs",
        "missing": "UNKNOWN",
    },
    {
        "id": "handoff_payload_chars",
        "group": "context",
        "unit": "chars",
        "numerator": "characters the handoff delivered to the Main",
        "denominator": "runs",
        "missing": "0 when no handoff was produced",
    },
    {
        "id": "duplicate_investigation_files",
        "group": "context",
        "unit": "files",
        "numerator": (
            "files the Main re-read after a child had read them, excluding integration reads "
            "recorded with a reason"
        ),
        "denominator": "runs with a handoff",
        "missing": "UNKNOWN when no handoff exists",
    },
    # Verification
    {
        "id": "public_test_calls",
        "group": "verification",
        "unit": "calls",
        "numerator": "run_public_tests invocations",
        "denominator": "runs",
        "missing": "0",
    },
    {
        "id": "public_test_pass",
        "group": "verification",
        "unit": "boolean",
        "numerator": "the last public suite run exited 0",
        "denominator": "runs",
        "missing": "FAIL",
    },
    {
        "id": "repair_attempts",
        "group": "verification",
        "unit": "count",
        "numerator": "file edits made after a failing public suite run",
        "denominator": "runs",
        "missing": "0",
    },
    {
        "id": "changed_files",
        "group": "verification",
        "unit": "files",
        "numerator": "files in the exported workspace patch",
        "denominator": "runs",
        "missing": "0",
    },
    {
        "id": "ownership_denials",
        "group": "verification",
        "unit": "calls",
        "numerator": "actions refused by the ownership policy",
        "denominator": "runs",
        "missing": "0",
    },
)

STOPPING_RULES: tuple[str, ...] = (
    "Every attempt enters the denominator; a failed or aborted run is never dropped or retried.",
    "A run that exhausts the budget is a result, not an incident: it is recorded, not repeated.",
    "The matrix is executed in the frozen order; an operator may stop the batch between cells for "
    "cost or infrastructure reasons, and a stopped batch is reported as incomplete together with "
    "the cells that ran.",
    "No cell is re-run because its outcome looks unhelpful. A re-run is a new, separately "
    "registered experiment.",
    "If the sandbox provider is unavailable, the affected cells are recorded as "
    "INFRASTRUCTURE_BLOCKED and the batch pauses; they are not silently skipped.",
)

LEAK_RULES: tuple[str, ...] = (
    "The oracle program and the reference implementation live on the Host and are never "
    "provisioned into an agent-readable workspace.",
    "The oracle runs after the agent stops, inside the sandbox, from a command the agent never "
    "sees.",
    "The goal text carries no statement about the experiment, no worker count and no delegation "
    "instruction.",
    "The private tool payload corpus is not published with the results.",
    "Raw evidence is collected before any summarising, so a reporting defect cannot lose a run.",
)


def _digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def arm_text(arm_id: str) -> str:
    """The exact bytes one arm injects."""

    source = ARM_SOURCES[arm_id]
    if source["path"] is None:
        from atlasos.app.main_driven import MAIN_SYSTEM_PROMPT

        return MAIN_SYSTEM_PROMPT
    return (REPO_ROOT / source["path"]).read_text(encoding="utf-8")


def prompt_spec(arm_id: str):
    """The ``MainPromptSpec`` a future runner would pass to ``GoalRunOptions``."""

    from atlasos.app.main_driven import MainPromptSpec

    return MainPromptSpec(version=ARM_SOURCES[arm_id]["label"], text=arm_text(arm_id))


def _reference_change(spec) -> dict[str, int]:
    """Lines the reference actually adds and removes, not the size of the files it writes."""

    added = removed = 0
    for path, text in spec.reference.items():
        before = (spec.files.get(path) or "").splitlines()
        after = text.splitlines()
        for line in difflib.unified_diff(before, after, lineterm="", n=0):
            if line.startswith(("+++", "---", "@@")):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return {"added": added, "removed": removed}


def task_record(spec) -> dict[str, Any]:
    reference_lines = sum(len(text.splitlines()) for text in spec.reference.values())
    return {
        "task_id": spec.task_id,
        "title": spec.title,
        "structure": spec.structure,
        "rationale": spec.rationale,
        "goal": spec.goal,
        "goal_digest": task_base.goal_digest(spec),
        "write_scope": list(spec.write_scope),
        "fixture_digest": task_base.fixture_digest(spec),
        "fixture_files": task_base.manifest(spec),
        "reference_digest": task_base.reference_digest(spec),
        "reference_files": len(spec.reference),
        "reference_lines": reference_lines,
        "reference_change_lines": _reference_change(spec),
        "wrong_variants": [
            {"name": item.name, "covers": item.covers, "file": item.file} for item in spec.wrong
        ],
        "wrong_variants_digest": task_base.wrong_variants_digest(spec),
        "oracle_digest": task_base.oracle_digest(spec),
        "public_suite": {
            "compatibility": list(spec.compatibility_command),
            "target": list(spec.target_command),
        },
        "expectation": dict(spec.expectation),
        "labels": dict(spec.labels),
        "notes": spec.notes,
    }


def build_protocol() -> dict[str, Any]:
    """The whole document, derived from files and declared constants."""

    arms = []
    for arm_id in ARM_IDS:
        source = ARM_SOURCES[arm_id]
        text = arm_text(arm_id)
        digest = _digest_text(text)
        arms.append(
            {
                "arm_id": arm_id,
                "label": source["label"],
                "declared_digest": source["digest"],
                "observed_digest": digest,
                "matches_declaration": digest == source["digest"],
                "chars": len(text),
                "path": source["path"],
                "provenance": source["provenance"],
            }
        )

    cells = matrix.build_plan(
        TASK_IDS, ARM_IDS, repetitions=RUN_CONFIG["repetitions"], seed=RUN_CONFIG["seed"]
    )
    plan = {
        "seed": RUN_CONFIG["seed"],
        "repetitions": RUN_CONFIG["repetitions"],
        "cells": cells,
        "digest": matrix.plan_digest(cells),
        "balance": matrix.balance_report(cells, ARM_IDS),
    }

    return {
        "stage": "B",
        "protocol_version": PROTOCOL_VERSION,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "questions": [dict(item) for item in QUESTIONS],
        "arms": arms,
        "tasks": [task_record(spec) for spec in SPECS],
        "structures": list(STRUCTURES),
        "run_config": {key: value for key, value in RUN_CONFIG.items()},
        "metrics": [dict(item) for item in METRICS],
        "plan": plan,
        "stopping_rules": list(STOPPING_RULES),
        "leak_rules": list(LEAK_RULES),
        "analysis": {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "paired_unit": ["task_id", "repetition"],
            "counted_separately": ["provider_requests", "assistant_turns", "tool_calls"],
            "reconstruction_source": [
                "session.json",
                "tool_conversations.json",
                "transcript.jsonl",
            ],
            "never_inferred_from": ["session.main.turns"],
            "exploratory_note": (
                "Three arms times six tasks times two repetitions is an exploratory design. A "
                "difference is a description of these runs, not an estimate of a general effect."
            ),
        },
    }


def validate(document: Mapping[str, Any]) -> list[str]:
    """Every invariant the protocol promises, checked on the document."""

    problems: list[str] = []
    if document.get("stage") != "B":
        problems.append("stage is not B")
    if document.get("protocol_version") != PROTOCOL_VERSION:
        problems.append("protocol_version differs from the code")

    arms = document.get("arms") or []
    if [item.get("arm_id") for item in arms] != list(ARM_IDS):
        problems.append("arms are not P1..P3")
    for item in arms:
        if not item.get("matches_declaration"):
            problems.append(f"arm {item.get('arm_id')} does not match its declared digest")

    tasks = document.get("tasks") or []
    if [item.get("task_id") for item in tasks] != list(TASK_IDS):
        problems.append("tasks are not the six registered ones")
    for item in tasks:
        if item.get("structure") not in STRUCTURES:
            problems.append(f"{item.get('task_id')} has an unknown structure label")
        if len(item.get("wrong_variants") or []) < 3:
            problems.append(f"{item.get('task_id')} has fewer than three wrong variants")
        goal = (item.get("goal") or "").lower()
        for banned in ("delegate", "subworker", "worker", "benchmark", "prompt"):
            if banned in goal:
                problems.append(f"{item.get('task_id')} goal mentions {banned!r}")

    plan = document.get("plan") or {}
    cells = plan.get("cells") or []
    expected = len(ARM_IDS) * len(TASK_IDS) * RUN_CONFIG["repetitions"]
    if len(cells) != expected:
        problems.append(f"plan has {len(cells)} cells, expected {expected}")
    balance = plan.get("balance") or {}
    for key in ("unique_pairs", "complete_pairs", "each_arm_in_each_round"):
        if not balance.get(key):
            problems.append(f"plan is not balanced: {key}")
    if not balance.get("first_arm_differs_across_repetitions"):
        problems.append("the same arm opens both repetitions")
    if plan.get("digest") != matrix.plan_digest(cells):
        problems.append("plan digest does not match its cells")

    config = document.get("run_config") or {}
    if config.get("token_budget") != 500_000:
        problems.append("token budget is not the frozen 500000")
    if config.get("tool_error_mode") != "corrected":
        problems.append("tool error mode is not corrected")
    if config.get("network") != "none":
        problems.append("network is not none")

    metric_ids = {item.get("id") for item in document.get("metrics") or []}
    for question in document.get("questions") or []:
        for metric in question.get("primary_metrics") or []:
            if metric not in metric_ids:
                problems.append(f"{question.get('id')} cites an undefined metric {metric!r}")
    for item in document.get("metrics") or []:
        if not item.get("denominator") or not item.get("missing"):
            problems.append(f"metric {item.get('id')} has no denominator or no missing policy")
    return problems


def write_protocol(document: Mapping[str, Any], path: Path = PROTOCOL_PATH) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def write_plan(document: Mapping[str, Any], path: Path = PLAN_PATH) -> None:
    payload = {
        "stage": "B",
        "protocol_version": PROTOCOL_VERSION,
        "plan": document["plan"],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_protocol(path: Path = PROTOCOL_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
