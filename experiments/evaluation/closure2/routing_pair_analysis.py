"""Offline analysis for the Closure-2C-1 routing pair.

Three questions, three separate answers, never one number:

* **Deliverable correctness** — the oracle checks that describe the artifact, unchanged.
* **Worker contract compliance** — the multi-worker checks, which are *not applicable* rather than
  *passed* when the router executed a single worker.
* **Efficiency** — requests, tokens, cache split, cost and latency.

Nothing here executes a cell, builds a provider, or writes evidence: the module reads a
``result.json`` the runner already wrote. It also never rewrites ``oracle.passed``; that value is
carried verbatim as ``oracle_passed_observed`` and the derived verdicts are separate fields.

Two traps this module is written to avoid:

* A single-worker execution has no worker fan-out to check. Reporting the missing multi-worker
  checks as a failure would blame the direct arm for the topology it was routed into.
* Equal pre-route task digests do **not** mean byte-identical prompts. The shared prefix fixes
  only the stable part; the per-worker assignment still differs between "two workers, one region
  each" and "one worker, both regions", so prompts are compared only where they are comparable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.catalog import task_digests
from experiments.evaluation.closure2.pilot_runner import (
    ROUTING_PAIR_PLAN,
    frozen_budget,
    route_expectation,
    routing_pair_cells,
    settings_for,
)
from experiments.evaluation.closure2.preflight import ROUTING_POLICY_DIGEST
from experiments.evaluation.closure2.protocol import (
    C2A_R1_PREREGISTRATION_DIGEST,
    preregistration_digest,
)

#: The synthetic run root used when building settings for a report: a fixed relative path, so a
#: report never carries a host path and the pairing digest is machine-independent.
REPORT_RUNS_ROOT = Path("experiments/runs/closure2-routing-pair")

UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

#: The checks the oracle applies to every arm, whatever the topology.
DELIVERABLE_CHECKS: tuple[str, ...] = (
    "hidden_tests_passed",
    "required_artifacts_present",
    "forbidden_paths_unchanged",
    "only_allowed_paths_changed",
    "host_fixture_unchanged",
)

#: The checks only a fan-out topology can satisfy. Absent on a single-worker run, which is not a
#: failure: the question they ask does not apply.
MULTI_WORKER_CHECKS: tuple[str, ...] = (
    "all_workers_produced_output",
    "distinct_workers_executed",
)

_EFFICIENCY_TOKEN_FIELDS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "estimated_cost_usd",
    "tool_calls",
)


# --------------------------------------------------------------------------------- frozen pairing


def pairing_report(*, runs_root: Path | None = None) -> dict[str, Any]:
    """What the two arms share, what they differ in, and what that does *not* imply.

    Computed from the catalog and the frozen protocol — no result file is needed, and none is read.
    """

    cells = routing_pair_cells()
    if len(cells) != 2:
        raise ValueError(f"the routing pair must be two cells, found {len(cells)}")

    first, second = cells
    root = REPORT_RUNS_ROOT if runs_root is None else runs_root
    settings_first = settings_for(first, runs_root=root)
    settings_second = settings_for(second, runs_root=root)
    expectation_first = route_expectation(first)
    expectation_second = route_expectation(second)
    digests = task_digests(first.source_task())
    budget = frozen_budget(first.spec.topology)

    shared = {
        "benchmark_id": ROUTING_PAIR_PLAN.benchmark_id,
        "task_id": first.task_id,
        "seed_digest": digests["seed_digest"],
        "oracle_digest": digests["oracle_digest"],
        "public_requirement_digest": digests["public_requirement_digest"],
        "candidate_dag_digest": digests["candidate_dag_digest"],
        "allowed_paths": list(first.source_task().allowed_paths),
        "forbidden_paths": list(first.source_task().forbidden_paths),
        "model": settings_first.model_override,
        "prompt_layout": settings_first.prompt_layout,
        "token_budget": settings_first.token_budget,
        "max_tokens_per_request": settings_first.max_tokens,
        "temperature": settings_first.temperature,
        "max_tool_calls": settings_first.max_tool_calls,
        "timeout_seconds": settings_first.timeout_seconds,
        "sandbox_image": "atlas-uat-python:3.12",
    }

    differences = {
        "requested_route": {
            first.arm_id: first.spec.requested_route,
            second.arm_id: second.spec.requested_route,
        },
        "config": {
            first.arm_id: first.config_relative(),
            second.arm_id: second.config_relative(),
        },
        "pre_route_public_task_digest": {
            first.arm_id: digests["effective_public_task_digest_multi"],
            second.arm_id: digests["effective_public_task_digest_multi"],
        },
        "expected_route": {
            first.arm_id: expectation_first["route"],
            second.arm_id: expectation_second["route"],
        },
        "expected_route_reason_codes": {
            first.arm_id: list(expectation_first["reason_codes"]),
            second.arm_id: list(expectation_second["reason_codes"]),
        },
        "expected_executed_workers": {
            first.arm_id: expectation_first["executed_worker_count"],
            second.arm_id: expectation_second["executed_worker_count"],
        },
        "candidate_workers": {
            first.arm_id: [worker.task_id for worker in first.benchmark_task().workers],
            second.arm_id: [worker.task_id for worker in second.benchmark_task().workers],
        },
    }

    # The turn cap is per worker, so an arm that executes one worker can never spend the fan-out
    # budget. Recording the product makes the asymmetry explicit instead of hiding it in prose.
    turns = {
        "max_turns_per_worker": settings_first.max_turns,
        "candidate_worker_count": len(first.benchmark_task().workers),
        "budget_worker_ceiling": budget["workers"],
        "max_total_worker_turns_if_all_candidates_run": (
            settings_first.max_turns * len(first.benchmark_task().workers)
        ),
    }

    second_digests = task_digests(second.source_task())
    parity = {
        "same_task": first.task_id == second.task_id,
        "same_seed_digest": digests["seed_digest"] == second_digests["seed_digest"],
        "same_oracle_digest": digests["oracle_digest"] == second_digests["oracle_digest"],
        "same_candidate_dag": (
            digests["candidate_dag_digest"] == second_digests["candidate_dag_digest"]
        ),
        "same_pre_route_public_task_digest": (
            differences["pre_route_public_task_digest"][first.arm_id]
            == differences["pre_route_public_task_digest"][second.arm_id]
        ),
        "same_model": settings_first.model_override == settings_second.model_override,
        "same_layout": settings_first.prompt_layout == settings_second.prompt_layout,
        "same_token_budget": settings_first.token_budget == settings_second.token_budget,
        "different_route": first.spec.requested_route != second.spec.requested_route,
    }

    return {
        "plan_id": ROUTING_PAIR_PLAN.plan_id,
        "plan_digest": ROUTING_PAIR_PLAN.digest(),
        "cells": [first.cell_id, second.cell_id],
        "preregistration_digest": preregistration_digest(),
        "preregistration_digest_matches_frozen": (
            preregistration_digest() == C2A_R1_PREREGISTRATION_DIGEST
        ),
        "router": {
            "version": expectation_second.get("policy_version"),
            "computed_digest": expectation_second.get("policy_digest"),
            "frozen_digest": ROUTING_POLICY_DIGEST,
            "matches_frozen": expectation_second.get("policy_digest") == ROUTING_POLICY_DIGEST,
        },
        "shared": shared,
        "differences": differences,
        "worker_turns": turns,
        "parity": parity,
        "prompt_equality_note": (
            "equal pre-route digests do not imply equal rendered prompts: the direct worker gets "
            "the union assignment for both candidate regions, while each fan-out worker gets only "
            "its own region"
        ),
    }


# ------------------------------------------------------------------------------- evidence readers


def _oracle(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("oracle")
    return value if isinstance(value, Mapping) else {}


def _metrics(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("metrics")
    return value if isinstance(value, Mapping) else {}


def _check_value(checks: Mapping[str, Any], key: str) -> bool | None:
    value = checks.get(key)
    return value if isinstance(value, bool) else None


def deliverable_correctness(result: Mapping[str, Any]) -> dict[str, Any]:
    """Artifact checks only. The multi-worker checks are never folded in here.

    ``oracle_passed_observed`` is the runner's own value, copied unchanged; the ``verdict`` beside
    it is derived from the deliverable checks alone and may legitimately differ from it.
    """

    checks = _oracle(result).get("checks")
    checks = checks if isinstance(checks, Mapping) else {}
    values: dict[str, Any] = {}
    for key in DELIVERABLE_CHECKS:
        observed = _check_value(checks, key)
        values[key] = UNKNOWN if observed is None else observed

    environment_failure = _oracle(result).get("environment_failure")
    if not isinstance(environment_failure, bool):
        environment_failure = None

    states = list(values.values())
    if any(state is False for state in states):
        verdict = "FAILED"
    elif any(state is UNKNOWN for state in states):
        verdict = UNKNOWN
    else:
        verdict = "PASSED"

    return {
        "checks": values,
        "oracle_environment_failure": environment_failure,
        "verdict": verdict,
        "oracle_passed_observed": _oracle(result).get("passed"),
        "oracle_failure_kind_observed": _oracle(result).get("failure_kind"),
        "source": "oracle.checks",
    }


def worker_contract_compliance(
    result: Mapping[str, Any], *, actual_worker_count: int | None = None
) -> dict[str, Any]:
    """Fan-out checks, with ``NOT_APPLICABLE`` for a topology that has no fan-out.

    ``actual_worker_count`` is how many workers really ran. When it is unknown, the count of
    ``worker_results`` is used; when neither exists, the checks are reported as ``UNKNOWN``.
    """

    checks = _oracle(result).get("checks")
    checks = checks if isinstance(checks, Mapping) else {}
    workers = result.get("worker_results")
    workers = workers if isinstance(workers, Mapping) else {}
    selected = _metrics(result).get("worker_selection")
    selected = selected if isinstance(selected, list) else None

    count = actual_worker_count
    if count is None and workers:
        count = len(workers)
    multi = bool(count and count > 1)

    values: dict[str, Any] = {}
    for key in MULTI_WORKER_CHECKS:
        observed = _check_value(checks, key)
        if observed is not None:
            values[key] = observed
        elif multi:
            values[key] = UNKNOWN
        elif count == 1:
            values[key] = NOT_APPLICABLE
        else:
            values[key] = UNKNOWN

    stop_reasons: dict[str, Any] = {}
    output_chars: dict[str, int] = {}
    for name, worker in workers.items():
        reason = worker.get("stop_reason")
        if isinstance(reason, Mapping):
            stop_reasons[str(name)] = reason.get("code")
        elif reason is None:
            stop_reasons[str(name)] = None
        else:
            stop_reasons[str(name)] = str(reason)
        output = worker.get("output")
        output_chars[str(name)] = len(output) if isinstance(output, str) else 0

    contract = _metrics(result).get("worker_contract")
    contract = contract if isinstance(contract, Mapping) else {}

    return {
        "checks": values,
        "actual_worker_count": count,
        "selected_worker_count": len(selected) if selected is not None else None,
        "stop_reasons": stop_reasons,
        "output_chars": output_chars,
        "worker_contract_mode": contract.get("mode"),
        "shared_context_chars": [
            worker.get("shared_context_chars")
            for worker in (contract.get("workers") or [])
            if isinstance(worker, Mapping)
        ],
        "source": "oracle.checks + worker_results + metrics.worker_contract",
    }


def efficiency(result: Mapping[str, Any]) -> dict[str, Any]:
    """Requests, tokens, cache split, cost and latency. No output-token penalty, no ratio score."""

    metrics = _metrics(result)
    provider_calls = metrics.get("provider_calls")
    model_turns = metrics.get("model_turns")
    if isinstance(provider_calls, int):
        requests = provider_calls
        requests_source = "metrics.provider_calls"
    elif isinstance(model_turns, int):
        requests = model_turns
        requests_source = "metrics.model_turns"
    else:
        requests = None
        requests_source = UNKNOWN

    routing = metrics.get("routing")
    decision = routing.get("decision") if isinstance(routing, Mapping) else None
    route = decision.get("route") if isinstance(decision, Mapping) else None
    executed = routing.get("executed_worker_count") if isinstance(routing, Mapping) else None

    values: dict[str, Any] = {}
    for key in _EFFICIENCY_TOKEN_FIELDS:
        value = metrics.get(key)
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        values[key] = value if numeric else None

    # The two latencies do not live under the same key, and the same key does not mean the same
    # measurement: ``metrics.latency_ms`` is end-to-end (it contains provisioning), while the agent
    # phase is only recorded in ``environment.agent_latency_ms``. This mirrors ``normalize_result``.
    environment = result.get("environment")
    environment = environment if isinstance(environment, Mapping) else {}
    agent_latency = environment.get("agent_latency_ms")
    end_to_end = metrics.get("latency_ms")
    values["agent_latency_ms"] = (
        agent_latency
        if isinstance(agent_latency, (int, float)) and not isinstance(agent_latency, bool)
        else None
    )
    values["end_to_end_latency_ms"] = (
        end_to_end
        if isinstance(end_to_end, (int, float)) and not isinstance(end_to_end, bool)
        else None
    )

    return {
        "model_requests": requests,
        "model_requests_source": requests_source,
        "actual_route": route,
        "route_applicable": isinstance(routing, Mapping),
        "executed_worker_count_reported": executed,
        **values,
        "cache_split_status": metrics.get("cache_split_status"),
        "cache_hit_ratio": metrics.get("cache_hit_ratio"),
        "latency_source": {
            "agent_latency_ms": "environment.agent_latency_ms",
            "end_to_end_latency_ms": "metrics.latency_ms",
        },
        "source": "metrics + environment",
    }


def summarize_cell(result_path: Path, *, cell_id: str) -> dict[str, Any]:
    """Read one ``result.json`` and return the three metric groups, side by side but unmerged."""

    payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    return {
        "cell_id": cell_id,
        "run_id": (payload.get("identity") or {}).get("run_id"),
        "sample_id": payload.get("sample_id"),
        "mode": payload.get("mode"),
        "deliverable": deliverable_correctness(payload),
        "worker_contract": worker_contract_compliance(payload),
        "efficiency": efficiency(payload),
        "raw_result_path": str(result_path),
    }


def compare_pair(*, multi: Mapping[str, Any], direct: Mapping[str, Any]) -> dict[str, Any]:
    """Pair two ``summarize_cell`` outputs.

    Deltas are ``direct - multi`` and are only emitted for fields both sides actually reported.
    There is no composite score, no weighting and no significance claim: one observation per arm
    cannot support any of those.
    """

    multi_eff = multi.get("efficiency") or {}
    direct_eff = direct.get("efficiency") or {}
    deltas: dict[str, Any] = {}
    fields = ("model_requests", *_EFFICIENCY_TOKEN_FIELDS)
    for key in fields:
        left = multi_eff.get(key)
        right = direct_eff.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            deltas[key] = right - left
        else:
            deltas[key] = None

    multi_verdict = (multi.get("deliverable") or {}).get("verdict")
    direct_verdict = (direct.get("deliverable") or {}).get("verdict")
    comparable = multi_verdict in {"PASSED", "FAILED"} and direct_verdict in {
        "PASSED",
        "FAILED",
    }

    return {
        "correctness": {"multi": multi_verdict, "direct": direct_verdict},
        "correctness_comparable": comparable,
        "efficiency_delta_direct_minus_multi": deltas,
        "efficiency_comparable": comparable,
        "notes": [
            "one observation per arm: no significance test, no confidence interval, no score",
            "the direct arm executes one worker, so multi-worker checks are NOT_APPLICABLE there",
            "output tokens are part of the cost and are not themselves evidence of lower quality",
        ]
        if comparable
        else ["correctness is not comparable while either side is UNKNOWN: deltas are withheld"],
    }


__all__ = [
    "DELIVERABLE_CHECKS",
    "MULTI_WORKER_CHECKS",
    "NOT_APPLICABLE",
    "REPORT_RUNS_ROOT",
    "UNKNOWN",
    "compare_pair",
    "deliverable_correctness",
    "efficiency",
    "pairing_report",
    "summarize_cell",
    "worker_contract_compliance",
]
