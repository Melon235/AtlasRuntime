# ruff: noqa: E501
"""Closure-2A: the frozen arm matrix, the resource contract and the preregistration payload.

This module is data plus pure functions. It starts no run, loads no model and creates no sandbox.
`docs/closure-2-preregistration.md` is the human-readable copy of what is frozen here; when the two
disagree, the *document* is wrong and must be corrected, because the JSON written by
``preflight.py`` is generated from this module.
"""

from __future__ import annotations

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
from experiments.evaluation.models import canonical_digest

REPO_ROOT = Path(__file__).resolve().parents[3]
LEGACY_CONTEXT_CONFIG = REPO_ROOT / "experiments" / "configs" / "final4-context.yaml"
SHARED_PREFIX_CONFIG = REPO_ROOT / "experiments" / "configs" / "closure2-shared-prefix.yaml"

#: The frozen router policy. ``r0.1.0`` is never redefined in place.
ROUTING_POLICY_VERSION = "r0.1.0"

#: One arm per row. ``topology`` selects the BenchmarkTask the arm runs; ``runner`` selects which
#: entry point does it. Nothing else is allowed to differ inside a comparison pair.
ARMS: dict[str, dict[str, Any]] = {
    "N0": {
        "runner": "native",
        "topology": "single",
        "engine": "anycode-0.9.0-native",
        "context": "native",
        "prompt_layout": "native",
        "route": "off",
        "config": None,
    },
    "N1": {
        "runner": "native",
        "topology": "multi",
        "engine": "anycode-0.9.0-native",
        "context": "native prerequisite context",
        "prompt_layout": "native",
        "route": "off",
        "config": None,
    },
    "A0": {
        "runner": "atlas",
        "topology": "single",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "legacy",
        "route": "off",
        "config": str(LEGACY_CONTEXT_CONFIG),
    },
    "A1-L": {
        "runner": "atlas",
        "topology": "multi",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "legacy",
        "route": "off",
        "config": str(LEGACY_CONTEXT_CONFIG),
    },
    "A1-P": {
        "runner": "atlas",
        "topology": "multi",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "shared_prefix",
        "route": "off",
        "config": str(SHARED_PREFIX_CONFIG),
    },
    "A2-P": {
        "runner": "atlas",
        "topology": "multi",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "shared_prefix",
        "route": "selective",
        "config": str(SHARED_PREFIX_CONFIG),
    },
}

#: Every comparison the preregistration commits to, and nothing else.
COMPARISONS: tuple[dict[str, str], ...] = (
    {
        "id": "native-vs-atlas-single",
        "left": "N0",
        "right": "A0",
        "question": "does the Atlas runtime beat a plain AnyCode agent on the same single-worker task",
        "attribution": "whole system: execution control, tool organisation, context management",
    },
    {
        "id": "native-vs-atlas-multi",
        "left": "N1",
        "right": "A1-L",
        "question": "does the Atlas runtime beat a plain AnyCode DAG on the same multi-worker task",
        "attribution": "whole system, not the Context Plane alone",
    },
    {
        "id": "layout-ablation",
        "left": "A1-L",
        "right": "A1-P",
        "question": "does the shared-prefix layout change tokens or quality with everything else fixed",
        "attribution": "prompt layout only: same config file apart from context.prompt_layout",
    },
    {
        "id": "topology-ablation-atlas",
        "left": "A0",
        "right": "A1-P",
        "question": "does decomposing the same task help or hurt when the context plane is on",
        "attribution": "topology plus the context-plane projection it entails",
    },
    {
        "id": "topology-ablation-native",
        "left": "N0",
        "right": "N1",
        "question": "does AnyCode's own DAG help or hurt on the same task",
        "attribution": "topology only, inside one engine",
    },
    {
        "id": "router-admission",
        "left": "A1-P",
        "right": "A2-P",
        "question": "does the frozen router admit or refuse the candidate DAG, and why",
        "attribution": "router decision, audited offline; not a quality comparison",
    },
)

#: Pairs that share one candidate DAG and therefore one BenchmarkTask.
SAME_DAG_GROUP: tuple[tuple[str, ...], ...] = (("N1", "A1-L", "A1-P"), ("A2-P",))
#: Pairs that must produce the same ``effective_public_task_digest``.
SAME_DIGEST_GROUP: tuple[tuple[str, ...], ...] = (("N0", "A0"), ("N1", "A1-L", "A1-P"))

#: Resource contract. Values are a **proposal** pending Owner/Reviewer approval; Closure-2B may not
#: start until this table is approved unchanged or replaced by a new preregistration version.
BUDGET: dict[str, Any] = {
    "single": {
        "max_turns": 36,
        "token_budget": 260_000,
        "max_tool_calls": 90,
        "timeout_seconds": 1800,
    },
    "multi": {
        "max_turns_per_worker": 12,
        "workers": 3,
        "theoretical_worker_turns": 36,
        "token_budget": 260_000,
        "max_tool_calls": 90,
        "timeout_seconds": 1800,
    },
    "shared": {
        "temperature": 0.0,
        "max_tokens_per_request": 8000,
        "sandbox_image": "atlas-uat-python:3.12",
        "cpu": "1",
        "memory": "1Gi",
        "network": "none",
        "workspace_root": "/workspace/project",
        "oracle": "isolated sandbox, same image, hidden tests",
        "initial_fixture": "the task seed, staged into a fresh git repository per run",
    },
    "rationale": (
        "260k prompt tokens sits between the historical multi-worker mean (~139k/run on E1 A1) and "
        "the 300k ceiling: roughly 1.9x the observed multi-worker load for a larger codebase, "
        "identical in both topologies so neither is handicapped. 36 turns and 90 tool calls keep "
        "the same 1:2.5 turn-to-call ratio the Final-4 units used (14 turns / 40 calls). 1800 s is "
        "twice the Final-4 timeout because each run now provisions, migrates and grades three "
        "modules. No value was tuned against a model run; nothing was executed to choose them."
    ),
    "approval": "PENDING_OWNER_APPROVAL",
}

#: Two different enforcement mechanisms. Recorded separately, never presented as equivalent.
BUDGET_SEMANTICS: dict[str, str] = {
    "atlas": (
        "shared run budget guarded before send: a request that would breach the token budget is "
        "refused, so the arm can end with a budget rejection and an incomplete deliverable"
    ),
    "native": (
        "no in-run guard: the same public thresholds are applied after the run, and the wall clock "
        "is the only enforced limit, so an over-budget native run is reported, not stopped"
    ),
    "consequence": (
        "a TASK_SUCCESS comparison alone cannot separate budget effects; results must report oracle "
        "pass, budget overrun, rejected requests and 'deliverable passed but budget failed' "
        "separately"
    ),
}

#: Execution order rule for Closure-2B: interleave, never block-by-arm.
EXECUTION_ORDER: dict[str, Any] = {
    "rule": (
        "tasks x repetitions are the outer loop and arms the inner loop, with the arm order rotated "
        "per repetition; no arm is ever run to completion before the next one starts"
    ),
    "repetitions": 3,
    "pairing": "each repetition of a task is a paired block across all arms of that task",
    "rationale": (
        "provider latency, cache warmth and time-of-day drift are shared inside a block instead of "
        "being aliased onto one arm"
    ),
}

#: Primary and secondary metrics, in the order they must be reported.
METRICS: dict[str, Any] = {
    "primary": "independent oracle correctness (OracleOutcome.passed)",
    "secondary": [
        "TASK_SUCCESS",
        "provider prompt tokens",
        "provider cache hit / miss (Unknown when the provider did not report a split)",
        "estimated cost (None when the split is incomplete)",
        "agent latency",
        "end-to-end latency",
        "model calls",
        "tool calls",
        "budget violations",
        "parallel execution overlap",
        "context delivery correctness",
        "safety and environment failures",
    ],
    "reporting_rules": [
        "a success rate never hides budget overruns",
        "a cost is labelled provider-usage or estimate, never both",
        "cache hit/miss stays Unknown without a provider-reported split",
        "cache hit rate never substitutes for total cost",
    ],
}

#: Data that may be excluded, and nothing else.
EXCLUSIONS: dict[str, Any] = {
    "allowed": [
        "a run whose sandbox never became healthy (ENVIRONMENT_FAILURE before any model call)",
        "a run killed by the operator (recorded with its reason)",
        "a run whose oracle environment failed (ORACLE_TIMEOUT / SANDBOX_* on the oracle side)",
    ],
    "never_allowed": [
        "a run whose deliverable failed the oracle",
        "a run that exceeded the budget",
        "a run from one arm only, chosen after seeing the numbers",
    ],
    "rule": (
        "exclusions are decided by mechanism class, applied identically to every arm, and every "
        "excluded run stays in the ledger with its reason"
    ),
}

#: Comparison cells the design does **not** support.
UNSUPPORTED: list[str] = [
    "Router-assisted vs static multi for a task whose DAG the router refuses: A2-P collapses to the "
    "single worker, so it is not a multi-worker cell and must not be reported as one",
    "single vs multi across different tasks: topologies are compared only inside one task",
    "a Native vs Atlas difference attributed to the Context Plane alone: the two engines differ in "
    "execution control, tool organisation and budget enforcement as well",
    "any cost claim from a run whose provider cache split is Unknown",
    "any statistical claim from a single repetition",
]


def arm_config_digest(arm: str) -> str:
    """Digest of everything an arm is allowed to change."""

    entry = ARMS[arm]
    return canonical_digest(
        {
            "benchmark": BENCHMARK_ID,
            "arm": arm,
            "runner": entry["runner"],
            "topology": entry["topology"],
            "context": entry["context"],
            "prompt_layout": entry["prompt_layout"],
            "route": entry["route"],
            "config_file": Path(entry["config"]).name if entry["config"] else None,
            "budget": BUDGET["single"] if entry["topology"] == "single" else BUDGET["multi"],
            "shared": BUDGET["shared"],
        }
    )


def task_record(task: Task) -> dict[str, Any]:
    digests = task_digests(task)
    return {
        "task_id": task.task_id,
        "family": task.family,
        "title": task.title,
        "notes": task.notes,
        "research_question": task.research_question,
        "workers": [
            {
                "task_id": str(worker["task_id"]),
                "objective": str(worker["objective"]),
                "depends_on": list(worker["depends_on"]),
                "owned_paths": list(worker["owned_paths"]),
            }
            for worker in task.workers
        ],
        "deliverables": list(task.deliverables),
        "allowed_paths": list(task.allowed_paths),
        "forbidden_paths": list(task.forbidden_paths),
        "seed_files": sorted(task.seed),
        "oracle_files": sorted(task.oracle_tests),
        "digests": digests,
    }


def preregistration_payload() -> dict[str, Any]:
    """The frozen definition Closure-2B must verify before it runs anything."""

    return {
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset_version": DATASET_VERSION,
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "tasks": [task_record(task) for task in TASKS],
        "arms": {
            arm: {**entry, "config_digest": arm_config_digest(arm)} for arm, entry in ARMS.items()
        },
        "comparisons": list(COMPARISONS),
        "digest_groups": [list(group) for group in SAME_DIGEST_GROUP],
        "same_dag_groups": [list(group) for group in SAME_DAG_GROUP],
        "budget": BUDGET,
        "budget_semantics": BUDGET_SEMANTICS,
        "execution_order": EXECUTION_ORDER,
        "metrics": METRICS,
        "exclusions": EXCLUSIONS,
        "unsupported": list(UNSUPPORTED),
        "release_boundary": {
            "agent_workspace_contains": "seed files only (source, docs, public tests)",
            "repository_contains": (
                "hidden oracle tests and reference implementations — these tasks are therefore NOT "
                "secret held-out data; only the agent workspace is held out"
            ),
            "rule": (
                "every published result must carry this sentence; the tasks must not be described "
                "as fully unpublished"
            ),
        },
        "model_calls_to_author_this": 0,
        "sandbox_runs_to_author_this": 0,
    }


def preregistration_digest() -> str:
    return canonical_digest(preregistration_payload())


def task_taskspec_parity() -> dict[str, dict[str, Any]]:
    """Per task, the facts that must be identical across single and multi.

    The public requirement, the shared block, the visible fixture, the hidden oracle and the
    deliverables are task data; only the worker decomposition may differ.
    """

    out: dict[str, dict[str, Any]] = {}
    for task in TASKS:
        single = build_task(task, topology="single")
        multi = build_task(task, topology="multi")
        out[task.task_id] = {
            "same_visible_files": single.visible_files == multi.visible_files,
            "same_hidden_files": single.hidden_files == multi.hidden_files,
            "same_required_artifacts": single.required_artifacts == multi.required_artifacts,
            "same_allowed_paths": single.allowed_paths == multi.allowed_paths,
            "same_forbidden_paths": single.forbidden_paths == multi.forbidden_paths,
            "single_prompt_contains_shared_context": task.shared_context.splitlines()[0]
            in single.prompt,
            "multi_steps_carry_shared_context": all(
                step.shared_context == task.shared_context for step in multi.workers
            ),
            "single_digest": single.effective_public_task_digest,
            "multi_digest": multi.effective_public_task_digest,
        }
    return out
