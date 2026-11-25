"""Stage F — the Unified Cell Evidence schema and its three adapters.

The four arms do not produce the same records, and this module refuses to pretend they do:

* ``A1``/``A3`` run the goal runtime, which writes a main-driven ``session.json``: there is a Main
  there are children, so ``usage_attribution`` is ``main_child`` and every Main metric is real.
* ``A0`` runs one native agent per task node with no Main: ``single_agent``. The primary agent's
  context is recorded; "Main" fields are ``NOT_APPLICABLE``.
* ``A2`` runs a static DAG with no Main at all: ``dag_all_children``. Its integration node is a
  worker, not a Main, and is recorded separately.

``NOT_APPLICABLE`` and ``UNKNOWN`` are distinct sentinels. A metric that does not exist for an
architecture is ``NOT_APPLICABLE``; a metric that exists but was not captured is ``UNKNOWN``.
is ever written as ``0``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "stage-f-cell-evidence/1.0"

NOT_APPLICABLE = "NOT_APPLICABLE"
UNKNOWN = "UNKNOWN"
NOT_RUN = "NOT_RUN"

PRIMARY_ATTRIBUTIONS = frozenset({"single_agent", "main_child"})


def _digest(payload: Any) -> str:
    from experiments.stage_b_plus import authority

    return authority.digest_of(payload)


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _usage_block(
    *, prompt: Any, completion: Any, cache_hit: Any, cache_miss: Any
) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
    }


def blank_evidence(cell: Mapping[str, Any]) -> dict[str, Any]:
    """The identity half of a cell record, before any run has happened."""

    return {
        "schema_version": SCHEMA_VERSION,
        "cell_id": cell["cell_id"],
        "plan_id": cell["plan_id"],
        "batch_id": cell.get("batch_id"),
        "task_id": cell["task_id"],
        "family": cell.get("family"),
        "arm_id": cell["arm_id"],
        "execution_mode": cell["execution_mode"],
        "usage_attribution": cell["usage_attribution"],
        "repetition": cell.get("repetition"),
        "sequence": cell.get("sequence"),
        "is_benchmark_sample": bool(cell.get("is_benchmark_sample")),
        "adapter_version": cell.get("adapter_version"),
        "goal_digest": cell.get("goal_digest"),
        "budget_digest": cell.get("budget_digest"),
    }


def _correctness(
    *,
    agent_success: Any,
    primary_completion: Any,
    static_acceptance: Any,
    public_test: Any,
    oracle_status: Any,
    oracle_passed: Any,
    execution_integrity: Any,
    failure_kind: Any,
) -> dict[str, Any]:
    oracle_ok = oracle_passed if isinstance(oracle_passed, bool) else None
    delivered = bool(
        oracle_status == "PASS"
        and public_test == "PASS"
        and execution_integrity == "PASS"
        and agent_success is True
    )
    return {
        "agent_success": agent_success,
        "primary_agent_completion": primary_completion,
        "static_acceptance": static_acceptance,
        "public_test": public_test,
        "independent_oracle": {
            "status": oracle_status,
            "passed": oracle_ok,
        },
        "execution_integrity": execution_integrity,
        "delivered_success": delivered,
        "failure_kind": failure_kind,
    }


# -- A1 / A3: the goal runtime session ---------------------------------------------------------


def from_main_child_session(cell: Mapping[str, Any], session: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a main-driven ``session.json``. Every field is read, none is invented."""

    metrics = session.get("metrics") or {}
    delegation = session.get("delegation") or {}
    totals = delegation.get("totals") or {}
    batches = delegation.get("batches") or []
    public = session.get("public_tests") or {}
    verification = session.get("verification") or {}
    budget = session.get("budget") or {}

    proposals = [
        {
            "turn": message.get("turn"),
            "input": (block.get("input") or {}),
        }
        for message in (session.get("main_messages") or [])
        for block in (message.get("content") or [])
        if isinstance(block, Mapping) and block.get("name") == "delegate_tasks"
    ]

    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=(session.get("main") or {}).get("success"),
                primary_completion=(session.get("main") or {}).get("stop_reason"),
                static_acceptance=verification.get("static_acceptance", UNKNOWN),
                public_test=public.get("status", NOT_RUN),
                oracle_status=(session.get("oracle") or {}).get("status", UNKNOWN),
                oracle_passed=(session.get("oracle") or {}).get("passed"),
                execution_integrity=_integrity(session),
                failure_kind=(session.get("failure") or {}).get("kind"),
            ),
            "usage": {
                "main": _usage_block(
                    prompt=metrics.get("main_cumulative_input_tokens"),
                    completion=metrics.get("main_completion_tokens"),
                    cache_hit=metrics.get("main_cache_hit_tokens"),
                    cache_miss=metrics.get("main_cache_miss_tokens"),
                ),
                "children": _usage_block(
                    prompt=metrics.get("child_total_prompt_tokens"),
                    completion=metrics.get("child_completion_tokens"),
                    cache_hit=totals.get("cache_hit_tokens"),
                    cache_miss=totals.get("cache_miss_tokens"),
                ),
                "system_total_tokens": metrics.get("system_total_tokens"),
                "budget": {
                    "limit_tokens": budget.get("limit_tokens"),
                    "used_tokens": budget.get("used_tokens"),
                    "stop_reason": budget.get("stop_reason"),
                    "admission_rejections": budget.get("admission_rejections"),
                },
            },
            "context": {
                "primary_agent_peak_context": metrics.get("main_peak_context_tokens"),
                "primary_agent_cumulative_input": metrics.get("main_cumulative_input_tokens"),
                "main_peak_context": metrics.get("main_peak_context_tokens"),
                "main_cumulative_input": metrics.get("main_cumulative_input_tokens"),
                "integration_node_context": NOT_APPLICABLE,
                "system_max_agent_context": metrics.get("main_peak_context_tokens"),
                "per_request_input_tokens": metrics.get("main_context_tokens_by_request"),
            },
            "delegation": {
                "natural_proposal": bool(proposals),
                "proposal_count": len(proposals),
                "proposals": proposals,
                "rounds": metrics.get("delegation_rounds"),
                "batches": [
                    {
                        "round": batch.get("round"),
                        "route": batch.get("route"),
                        "reason_codes": (batch.get("decision") or {}).get("reason_codes"),
                        "blocked_gates": (batch.get("decision") or {}).get("blocked_gates"),
                        "children": [
                            {
                                key: child.get(key)
                                for key in (
                                    "key",
                                    "status",
                                    "success",
                                    "stop_reason",
                                    "turns",
                                    "tool_calls",
                                )
                            }
                            for child in batch.get("children") or []
                        ],
                        "max_observed_concurrency": batch.get("max_observed_concurrency"),
                    }
                    for batch in batches
                ],
            },
            "handoff": {
                "preview_delivered": [
                    {
                        "round": batch.get("round"),
                        "result_chars": [
                            child.get("output_chars") for child in batch.get("children") or []
                        ],
                    }
                    for batch in batches
                ],
                "full_reference_read": len(session.get("handoff_reads") or [])
                if "handoff_reads" in session
                else UNKNOWN,
                "shared_file_consumption": UNKNOWN,
            },
            "resources": {
                "main_requests": metrics.get("main_requests"),
                "delegation_rounds": metrics.get("delegation_rounds"),
                "max_observed_child_concurrency": metrics.get("max_observed_child_concurrency"),
                "ownership": session.get("ownership"),
                "duration_s": None,
            },
            "provenance": {
                "source": "main-driven session.json",
                "source_digest": _digest(dict(session)),
            },
        }
    )
    return evidence


def _integrity(session: Mapping[str, Any]) -> str:
    value = session.get("execution_integrity")
    if isinstance(value, Mapping):
        return str(value.get("mode") or "record")
    return str(value or UNKNOWN)


# -- A0: native single agent -------------------------------------------------------------------


def from_native_result(
    cell: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalise a Closure-1B ``EvalRunResult`` shaped mapping.

    There is no Main. The primary agent's context is the run's own peak; every Main/metric field
    that presupposes a Main is ``NOT_APPLICABLE``, and delegation is ``NOT_APPLICABLE`` because the
    arm has no delegation plane to observe.
    """

    metrics = result.get("metrics") or {}
    budget = result.get("budget") or {}
    environment = result.get("environment") or {}
    oracle = oracle or {}
    export = export or {}

    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=result.get("agent_success"),
                primary_completion=result.get("failure_class") or "completed",
                static_acceptance=export.get("static_acceptance", UNKNOWN),
                public_test=environment.get("public_test_status", NOT_RUN),
                oracle_status=oracle.get("status", UNKNOWN),
                oracle_passed=oracle.get("passed"),
                execution_integrity=export.get("execution_integrity", UNKNOWN),
                failure_kind=result.get("failure_class"),
            ),
            "usage": {
                "main": NOT_APPLICABLE,
                "children": NOT_APPLICABLE,
                "primary_agent": _usage_block(
                    prompt=metrics.get("prompt_tokens"),
                    completion=metrics.get("completion_tokens"),
                    cache_hit=metrics.get("cache_hit_tokens"),
                    cache_miss=metrics.get("cache_miss_tokens"),
                ),
                "system_total_tokens": metrics.get("prompt_tokens", 0)
                + metrics.get("completion_tokens", 0)
                if metrics
                else UNKNOWN,
                "budget": {
                    "limit_tokens": budget.get("limit_tokens", UNKNOWN),
                    "used_tokens": budget.get("used_tokens", UNKNOWN),
                    "stop_reason": budget.get("stop_reason"),
                    "enforcement": environment.get("budget_enforcement", UNKNOWN),
                },
            },
            "context": {
                "primary_agent_peak_context": metrics.get("peak_context_tokens"),
                "primary_agent_cumulative_input": metrics.get("prompt_tokens"),
                "main_peak_context": NOT_APPLICABLE,
                "main_cumulative_input": NOT_APPLICABLE,
                "integration_node_context": NOT_APPLICABLE,
                "system_max_agent_context": metrics.get("peak_context_tokens"),
                "per_request_input_tokens": metrics.get("per_request_prompt_tokens"),
            },
            "delegation": NOT_APPLICABLE,
            "handoff": NOT_APPLICABLE,
            "resources": {
                "agent_latency_ms": environment.get("agent_latency_ms"),
                "export_latency_ms": environment.get("export_latency_ms"),
                "ownership": NOT_APPLICABLE,
            },
            "provenance": {
                "source": "closure1b native result.json",
                "source_digest": _digest(dict(result)),
            },
        }
    )
    return evidence


# -- A2: static DAG ----------------------------------------------------------------------------


def from_dag_result(
    cell: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalise a static-DAG run.

    There is no Main. ``child_total_prompt_tokens`` is every worker of the DAG — the schema says so
    through ``usage_attribution: dag_all_children`` — and the integration node's context is recorded
    separately instead of being called "Main peak context".
    """

    workers = result.get("workers") or []
    metrics = result.get("metrics") or {}
    budget = result.get("budget") or {}
    oracle = oracle or {}
    export = export or {}

    integration = [worker for worker in workers if not worker.get("downstream")]
    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=result.get("agent_success"),
                primary_completion=NOT_APPLICABLE,
                static_acceptance=export.get("static_acceptance", UNKNOWN),
                public_test=result.get("public_test_status", NOT_RUN),
                oracle_status=oracle.get("status", UNKNOWN),
                oracle_passed=oracle.get("passed"),
                execution_integrity=export.get("execution_integrity", UNKNOWN),
                failure_kind=result.get("failure_kind"),
            ),
            "usage": {
                "main": NOT_APPLICABLE,
                "children": _usage_block(
                    prompt=metrics.get("child_total_prompt_tokens"),
                    completion=metrics.get("child_completion_tokens"),
                    cache_hit=metrics.get("child_cache_hit_tokens"),
                    cache_miss=metrics.get("child_cache_miss_tokens"),
                ),
                "system_total_tokens": metrics.get("system_total_tokens", UNKNOWN),
                "budget": {
                    "limit_tokens": budget.get("limit_tokens", UNKNOWN),
                    "used_tokens": budget.get("used_tokens", UNKNOWN),
                    "stop_reason": budget.get("stop_reason"),
                },
            },
            "context": {
                "primary_agent_peak_context": NOT_APPLICABLE,
                "primary_agent_cumulative_input": NOT_APPLICABLE,
                "main_peak_context": NOT_APPLICABLE,
                "main_cumulative_input": NOT_APPLICABLE,
                "integration_node_context": integration[0].get("peak_context_tokens")
                if integration
                else NOT_APPLICABLE,
                "system_max_agent_context": metrics.get("max_worker_peak_context"),
                "per_request_input_tokens": UNKNOWN,
            },
            "delegation": NOT_APPLICABLE,
            "handoff": NOT_APPLICABLE,
            "workers": workers,
            "resources": {
                "workers": len(workers),
                "max_observed_concurrency": metrics.get("max_observed_concurrency"),
                "ownership": "plan_level_only",
            },
            "provenance": {
                "source": "evaluation runner result.json",
                "source_digest": _digest(dict(result)),
            },
        }
    )
    return evidence


# -- guards used by the analysis layer ---------------------------------------------------------


def main_metric(evidence: Mapping[str, Any], name: str) -> Any:
    """Read a Main-scoped metric, refusing to fabricate one for an arm that has no Main."""

    attribution = evidence.get("usage_attribution")
    if attribution not in PRIMARY_ATTRIBUTIONS:
        return NOT_APPLICABLE
    if attribution == "single_agent":
        return NOT_APPLICABLE if name.startswith("main_") else evidence.get("context", {}).get(name)
    return evidence.get("context", {}).get(name, UNKNOWN)


def validate_evidence(evidence: Mapping[str, Any]) -> list[str]:
    """Schema invariants: no N/A written as zero, attribution present, correctness complete."""

    problems: list[str] = []
    if evidence.get("schema_version") != SCHEMA_VERSION:
        problems.append("wrong schema version")
    attribution = evidence.get("usage_attribution")
    if attribution not in {"single_agent", "main_child", "dag_all_children"}:
        problems.append(f"unknown attribution {attribution!r}")
    context = evidence.get("context") or {}
    for key in (
        "primary_agent_peak_context",
        "primary_agent_cumulative_input",
        "main_peak_context",
        "main_cumulative_input",
        "integration_node_context",
        "system_max_agent_context",
    ):
        if key not in context:
            problems.append(f"context.{key} is missing")
        elif context[key] == 0 and key.startswith(("main_", "primary_agent_")):
            problems.append(f"context.{key} is 0; a missing metric must be NOT_APPLICABLE")
    if attribution != "main_child":
        for key in ("main_peak_context", "main_cumulative_input"):
            if context.get(key) not in (NOT_APPLICABLE, UNKNOWN):
                problems.append(f"{key} is present for a {attribution} arm")
    if attribution == "dag_all_children" and evidence.get("delegation") != NOT_APPLICABLE:
        problems.append("a DAG arm must report delegation as NOT_APPLICABLE")
    correctness = evidence.get("correctness") or {}
    for key in (
        "agent_success",
        "static_acceptance",
        "public_test",
        "independent_oracle",
        "execution_integrity",
        "delivered_success",
        "failure_kind",
    ):
        if key not in correctness:
            problems.append(f"correctness.{key} is missing")
    return problems


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    return _digest(dict(evidence))


__all__ = [
    "NOT_APPLICABLE",
    "NOT_RUN",
    "SCHEMA_VERSION",
    "UNKNOWN",
    "blank_evidence",
    "evidence_digest",
    "from_dag_result",
    "from_main_child_session",
    "from_native_result",
    "main_metric",
    "sha256_bytes",
    "validate_evidence",
]

_ = Sequence
