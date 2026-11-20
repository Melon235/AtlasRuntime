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

from . import matrix, schema
from .reconstruct import ANALYZER_VERSION, PROPOSAL_STATES
from .reconstruct import LEVELS as DELEGATION_LEVELS
from .tasks import SPECS, STRUCTURES, TASK_IDS
from .tasks import base as task_base

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_DIR = REPO_ROOT / "experiments" / "stage_benchmark"
PROTOCOL_PATH = STAGE_DIR / "protocol.json"
PLAN_PATH = STAGE_DIR / "plan.json"

PROTOCOL_VERSION = "1.1.0"
SUPERSEDES_VERSION = "1.0.0"
ANALYSIS_SCHEMA_VERSION = "stage-b-result/1.1"

#: What changed from 1.0.0 and why. Metric semantics only: the plan, the arms, the tasks and the
#: run configuration are untouched, which is asserted by their digests staying equal.
REVISIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "E1-1",
        "area": "token_and_cost_accounting",
        "change": (
            "the estimate decomposes usage into five fields, each counted once; the child "
            "prompt is priced at the miss rate but is no longer added to the token total twice"
        ),
        "rationale": "EXPECTED per-run was 306124 tokens; the same evidence gives 299022",
    },
    {
        "id": "E1-2",
        "area": "router_admission",
        "change": (
            "admission is decided per proposal from the router's own decision record, with five "
            "states; route=direct is a rejection, not an admission"
        ),
        "rationale": "a non-empty routes list said nothing about whether anyone was admitted",
    },
    {
        "id": "E1-3",
        "area": "delegation_effectiveness",
        "change": (
            "effectiveness becomes an L1-L6 ladder; delegation_mechanism_completion_rate is the "
            "mechanical metric, effective_delegation_rate is a labelled proxy, both over proposals"
        ),
        "rationale": (
            "child success plus a later file edit proved ordering, not that the Main used the "
            "findings; a Direct run was scored zero by construction"
        ),
    },
    {
        "id": "E1-4",
        "area": "failure_classification",
        "change": (
            "failure is classified from status, stop reason, sandbox lifecycle and tool outcome "
            "into nine classes, with a primary cause, contributing diagnostics and a confidence; "
            "unknown is kept when the evidence does not decide"
        ),
        "rationale": "every failure without a public test run used to be labelled goal",
    },
    {
        "id": "E1-5",
        "area": "metric_coverage",
        "change": (
            "each metric declares its evidence path and is reported MEASURED, UNKNOWN or "
            "UNSUPPORTED per record; repair_attempts becomes post_failure_edits"
        ),
        "rationale": "a resolver that always returns None is not a measurement",
    },
    {
        "id": "E1-6",
        "area": "proposal_matching",
        "change": (
            "the flat tool-call list now shares the turn's call objects, so a call carries its own "
            "result; tool errors and denials were silently zero before"
        ),
        "rationale": "results were attached to one object and read from a copy of it",
    },
)

ARM_IDS: tuple[str, ...] = ("P1", "P2", "P3")

#: The nine classes the failure classifier may return. ``unknown`` is an answer, not a hole.
FAILURE_CLASSES: tuple[str, ...] = (
    "none",
    "goal",
    "budget",
    "turn_limit",
    "runtime",
    "sandbox",
    "tool",
    "blocked",
    "unknown",
)
LEVEL_NAMES: tuple[str, ...] = tuple(name for name, _ in DELEGATION_LEVELS)

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
        "numerator": "runs whose primary failure class is runtime or sandbox",
        "denominator": "all attempts",
        "missing": "counted as FAIL; a tool or execution fault keeps its own class",
    },
    {
        "id": "unknown_failure_rate",
        "group": "primary",
        "unit": "rate",
        "numerator": "runs whose primary failure class could not be determined from evidence",
        "denominator": "all attempts",
        "missing": "UNKNOWN is a value, never a guess at a cause",
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
        "numerator": "proposals whose router result admitted at least one child",
        "denominator": "proposals",
        "missing": "UNKNOWN when no proposal exists; route=direct admits nobody and is a decision",
        "returns": "pair",
        "states": [
            "NO_PROPOSAL",
            "PROPOSAL_REJECTED",
            "DELEGATE_ONE_ADMITTED",
            "DELEGATE_PARALLEL_ADMITTED",
            "ROUTER_RESULT_UNKNOWN",
        ],
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
        "id": "delegation_mechanism_completion_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": "runs that reached L2, L3, L4 and L5 of the delegation ladder",
        "denominator": "runs with a proposal",
        "missing": "UNKNOWN when no proposal exists; a Direct run has no mechanism to complete",
        "returns": "pair",
    },
    {
        "id": "effective_delegation_rate",
        "group": "delegation",
        "unit": "rate",
        "numerator": (
            "runs that reached L2 to L6 of the delegation ladder: admitted, worker completed, "
            "handoff delivered, Main continued, and the run passed its public suite and oracle"
        ),
        "denominator": "runs with a proposal",
        "missing": "UNKNOWN when no proposal exists; it is a proxy, not an attribution",
        "returns": "pair",
        "interpretation": (
            "observable proxy: L6 is the correctness of the run and is not caused by the "
            "delegation; the ladder orders evidence, it does not establish benefit"
        ),
    },
    {
        "id": "delegation_level_reached",
        "group": "delegation",
        "unit": "level",
        "numerator": "highest contiguous level of the L1-L6 ladder",
        "denominator": "runs with a proposal",
        "missing": "UNKNOWN when no proposal exists",
        "levels": {
            "L1_proposal": "the Main called delegate_tasks",
            "L2_admission": "the router admitted at least one worker",
            "L3_worker_completion": "every admitted worker completed successfully",
            "L4_handoff_delivery": "a usable handoff reached the Main",
            "L5_main_continuation": "the Main edited or tested after the handoff",
            "L6_goal_correctness": "public suite and oracle passed",
        },
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
            "raw path overlap between a child's successful reads and the Main's post-handoff reads"
        ),
        "denominator": "runs with a handoff",
        "missing": "UNKNOWN when no handoff or no child read exists",
        "interpretation": (
            "a shared path is not redundancy: a necessary integration read looks identical. The "
            "count is reported raw and meaningful duplication requires review"
        ),
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
        "id": "post_failure_edits",
        "group": "verification",
        "unit": "count",
        "numerator": "file edits made after the first failing public suite run",
        "denominator": "runs",
        "missing": "0 when no public suite run failed",
        "interpretation": (
            "a count of edits after a failure, not a claim that each one was a repair: causality "
            "between a failure and an edit is not observable"
        ),
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

#: Each metric's statistical definition and the synthetic case that would catch a regression. Kept
#: apart from ``METRICS`` so the catalogue stays readable; :func:`build_protocol` merges them, and
#: :func:`validate` refuses a metric that has neither. A metric with no counterexample is a number
#: nobody has ever seen go wrong.
METRIC_EVIDENCE: dict[str, dict[str, str]] = {
    "oracle_pass_rate": {
        "definition": "share of attempts whose independent host-side oracle returned PASS",
        "counterexample": "test_1_direct_pass_is_not_scored_as_failed_delegation",
    },
    "goal_success_rate": {
        "definition": "share of attempts whose public suite and oracle both passed",
        "counterexample": "test_2b_a_direct_run_that_fails_its_goal_is_a_goal_failure",
    },
    "budget_exhaustion_rate": {
        "definition": "share of attempts the token guardrail stopped with budget_exceeded",
        "counterexample": "test_9_budget_exhaustion_with_missing_session_turns",
    },
    "runtime_failure_rate": {
        "definition": "share of attempts whose primary failure class is runtime or sandbox",
        "counterexample": "test_12_runtime_internal_failure_is_not_a_goal_failure",
    },
    "unknown_failure_rate": {
        "definition": "share of attempts whose primary failure class the evidence cannot decide",
        "counterexample": "test_12b_an_unexplained_stop_is_unknown_not_goal",
    },
    "delegation_proposal_rate": {
        "definition": "share of attempts in which the Main called delegate_tasks at least once",
        "counterexample": "test_5_child_succeeded_but_main_never_continued",
    },
    "router_admission_rate": {
        "definition": "admitted proposals over proposed ones; route=direct admits nobody",
        "counterexample": "test_15_multiple_proposals_are_counted_one_by_one",
    },
    "child_completion_rate": {
        "definition": "children that ended successfully over children the router admitted",
        "counterexample": "test_3_admitted_but_failed_worker_stops_the_ladder_at_l2",
    },
    "delegation_mechanism_completion_rate": {
        "definition": "runs with a proposal that reached L2 through L5 of the ladder",
        "counterexample": "test_5_child_succeeded_but_main_never_continued",
    },
    "effective_delegation_rate": {
        "definition": "runs with a proposal that reached L2 through L6; an observable proxy",
        "counterexample": "test_6_delegation_completed_but_the_goal_failed",
    },
    "delegation_level_reached": {
        "definition": "highest contiguous level of the L1-L6 ladder",
        "counterexample": "test_3_admitted_but_failed_worker_stops_the_ladder_at_l2",
    },
    "direct_execution_rate": {
        "definition": "share of attempts with no delegate_tasks call at all",
        "counterexample": "test_1_direct_pass_is_not_scored_as_failed_delegation",
    },
    "invalid_capability_delegation_rate": {
        "definition": "runs with a proposal whose objective required a tool the profile lacks",
        "counterexample": "test_8_a_read_only_worker_asked_to_execute_is_an_invalid_assignment",
    },
    "late_delegation_rate": {
        "definition": "runs whose first proposal came after the pre-registered turn threshold or "
        "after the first implementation edit",
        "counterexample": "test_16_a_proposal_after_the_first_edit_is_late",
    },
    "first_delegation_turn": {
        "definition": "assistant turn index of the first delegate_tasks call",
        "counterexample": "test_2_a_rejected_proposal_is_not_an_admission",
    },
    "first_implementation_turn": {
        "definition": "assistant turn index of the first file_edit or file_write",
        "counterexample": "test_1_direct_pass_is_not_scored_as_failed_delegation",
    },
    "main_peak_context_tokens": {
        "definition": "maximum prompt tokens over the Main's provider requests",
        "counterexample": "test_12_missing_usage_fields_stay_missing",
    },
    "main_cumulative_input_tokens": {
        "definition": "sum of the Main's prompt tokens across its requests",
        "counterexample": "test_12_missing_usage_fields_stay_missing",
    },
    "main_cache_hit_tokens": {
        "definition": "prompt-cache hit tokens the provider reported for the Main",
        "counterexample": "test_12_missing_usage_fields_stay_missing",
    },
    "main_cache_miss_tokens": {
        "definition": "prompt-cache miss tokens the provider reported for the Main",
        "counterexample": "test_12_missing_usage_fields_stay_missing",
    },
    "child_total_prompt_tokens": {
        "definition": "sum of child prompt tokens; counted once, never added to a cache split",
        "counterexample": "test_4_two_children_complete_in_parallel",
    },
    "system_total_tokens": {
        "definition": "main input + child input + main completion + child completion",
        "counterexample": "test_12_missing_usage_fields_stay_missing",
    },
    "completion_tokens": {
        "definition": "main plus child completion tokens",
        "counterexample": "test_12_missing_usage_fields_stay_missing",
    },
    "wall_clock_seconds": {
        "definition": "run duration measured by the runner around the whole cell",
        "counterexample": "test_every_declared_metric_reports_a_status_on_a_complete_record",
    },
    "handoff_payload_chars": {
        "definition": "characters the handoff actually delivered to the Main",
        "counterexample": "test_17_a_missing_handoff_record_is_not_fabricated",
    },
    "duplicate_investigation_files": {
        "definition": "raw path overlap between a child's successful reads and the Main's "
        "post-handoff reads",
        "counterexample": "test_14_a_shared_read_is_counted_raw_not_called_redundant",
    },
    "public_test_calls": {
        "definition": "number of run_public_tests invocations",
        "counterexample": "test_17_edits_after_a_failing_suite_are_post_failure_edits",
    },
    "public_test_pass": {
        "definition": "whether the last public suite run exited zero",
        "counterexample": "test_6_delegation_completed_but_the_goal_failed",
    },
    "post_failure_edits": {
        "definition": "file edits made after the first failing public suite run",
        "counterexample": "test_17_edits_after_a_failing_suite_are_post_failure_edits",
    },
    "changed_files": {
        "definition": "files present in the exported workspace patch",
        "counterexample": "test_17_edits_after_a_failing_suite_are_post_failure_edits",
    },
    "ownership_denials": {
        "definition": "actions the ownership policy refused",
        "counterexample": "test_11b_a_single_ownership_denial_is_not_a_run_failure",
    },
}

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
        "supersedes": SUPERSEDES_VERSION,
        "revisions": [dict(item) for item in REVISIONS],
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "questions": [dict(item) for item in QUESTIONS],
        "arms": arms,
        "tasks": [task_record(spec) for spec in SPECS],
        "structures": list(STRUCTURES),
        "run_config": {key: value for key, value in RUN_CONFIG.items()},
        "metrics": [{**dict(item), **METRIC_EVIDENCE.get(item["id"], {})} for item in METRICS],
        "plan": plan,
        "stopping_rules": list(STOPPING_RULES),
        "leak_rules": list(LEAK_RULES),
        "analysis": {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzer_version": ANALYZER_VERSION,
            "paired_unit": ["task_id", "repetition"],
            "counted_separately": ["provider_requests", "assistant_turns", "tool_calls"],
            "reconstruction_source": [
                "session.json",
                "tool_conversations.json",
                "transcript.jsonl",
            ],
            "never_inferred_from": ["session.main.turns"],
            "failure_classes": list(FAILURE_CLASSES),
            "proposal_states": list(PROPOSAL_STATES),
            "delegation_levels": list(LEVEL_NAMES),
            "metric_status_values": ["MEASURED", "UNKNOWN", "UNSUPPORTED"],
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
        # A definition and a counterexample are what make the row auditable. Coverage without a
        # test is a claim; coverage with a test is evidence.
        for field in ("definition", "counterexample"):
            if not item.get(field):
                problems.append(f"metric {item.get('id')} has no {field}")
    undescribed = sorted(set(METRIC_EVIDENCE) - metric_ids)
    if undescribed:
        problems.append(f"metric evidence declared for unknown metrics: {undescribed}")
    missing = schema.unresolvable(sorted(metric_ids))
    if missing:
        problems.append(f"metrics without a resolver: {missing}")
    if document.get("supersedes") != SUPERSEDES_VERSION:
        problems.append("the document does not declare what it supersedes")
    if not document.get("revisions"):
        problems.append("a revision must record why each semantic changed")
    for revision in document.get("revisions") or []:
        for field in ("id", "area", "change", "rationale"):
            if not revision.get(field):
                problems.append(f"revision {revision.get('id')} has no {field}")
    analysis = document.get("analysis") or {}
    if set(analysis.get("failure_classes") or []) != set(FAILURE_CLASSES):
        problems.append("declared failure classes differ from the classifier's")
    if analysis.get("analyzer_version") != ANALYZER_VERSION:
        problems.append("analysis.analyzer_version differs from the code")
    if analysis.get("schema_version") != schema.SCHEMA_VERSION:
        problems.append("analysis.schema_version differs from the schema module")
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
