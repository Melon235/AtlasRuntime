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
Neither is ever written as ``0``.

Every adapter reads a field the producer actually writes. Where the producer writes it somewhere
other than where the schema first looked, the read moved to the producer's location — the schema did
not learn to invent a value.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "stage-f-cell-evidence/1.1"

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


def _delivered(
    *,
    oracle_status: Any,
    public_test: Any,
    execution_integrity: Any,
    agent_success: Any,
) -> bool:
    """Delivered success: a correct artifact was produced by a run whose own checks did not fail.

    It is deliberately a conjunction of four *independently recorded* verdicts and never a copy of
    one of them: an oracle PASS alone is not delivery (the agent may have failed), and an agent
    completion alone is not delivery (the artifact may be wrong). A verdict that does not exist for
    the arm (``NOT_APPLICABLE``) does not block delivery; a verdict that failed does.
    """

    if agent_success is not True:
        return False
    if oracle_status != "PASS":
        return False
    return all(verdict != "FAIL" for verdict in (public_test, execution_integrity))


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
        "delivered_success": _delivered(
            oracle_status=oracle_status,
            public_test=public_test,
            execution_integrity=execution_integrity,
            agent_success=agent_success,
        ),
        "failure_kind": failure_kind,
    }


def _integrity_verdict(block: Any) -> str:
    """The goal runtime's integrity evidence as a verdict, without upgrading a gap to a pass.

    ``record`` mode writes the exact input of every proposed call; ``off`` is a configuration that
    ran no mechanism at all. Zero records with zero write failures is *unproven*, not clean.
    """

    if not isinstance(block, Mapping):
        return UNKNOWN
    mode = str(block.get("mode") or "")
    if mode in {"", "off"}:
        return NOT_APPLICABLE
    if int(block.get("write_failures") or 0) > 0:
        return "FAIL"
    return "PASS" if int(block.get("records") or 0) > 0 else UNKNOWN


def _request_rows(requests: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(row) for row in (requests or [])]


def _peaks(requests: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    from experiments.stage_f import scripted

    return scripted.per_agent_peaks(_request_rows(requests))


def _node_value(requests: Sequence[Mapping[str, Any]] | None, task_id: str, field: str) -> Any:
    """One per-agent value, or ``UNKNOWN`` when no request carried that node's identity.

    A node may be named ``<run_id>-<task_id>`` by the worker factory; the suffix match is accepted
    only when exactly one node matches, so an ambiguous match is never resolved by guessing.
    """

    values = _peaks(requests)[field]
    if task_id in values:
        return values[task_id]
    matches = [value for key, value in values.items() if key.endswith(f"-{task_id}")]
    return matches[0] if len(matches) == 1 else UNKNOWN


def _node_peak(requests: Sequence[Mapping[str, Any]] | None, task_id: str) -> Any:
    return _node_value(requests, task_id, "per_agent_peak_context_tokens")


def _node_requests(requests: Sequence[Mapping[str, Any]] | None, task_id: str) -> Any:
    return _node_value(requests, task_id, "per_agent_requests")


# -- A1 / A3: the goal runtime session ---------------------------------------------------------


def from_main_child_session(
    cell: Mapping[str, Any],
    session: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
    proposals: Sequence[Mapping[str, Any]] | None = None,
    requests: Sequence[Mapping[str, Any]] | None = None,
    public_test: Any = None,
) -> dict[str, Any]:
    """Normalise a main-driven ``session.json``. Every field is read, none is invented.

    The proposal list is the Main's *own* recorded tool call, read from the run's conversation
    snapshot by the runner. It is passed in rather than looked up here, because the session record
    never held the model's tool inputs.
    """

    from experiments.stage_b_plus import executor

    metrics = session.get("metrics") or {}
    delegation = session.get("delegation") or {}
    totals = delegation.get("totals") or {}
    batches = delegation.get("batches") or []
    budget = session.get("budget") or {}
    main = session.get("main") or {}
    oracle = oracle or {}
    export = export or {}
    peaks = _peaks(requests)
    main_peak = metrics.get("main_peak_context_tokens")

    public_verdict = public_test
    if public_verdict is None:
        public_verdict = executor.public_test_verdict(session.get("public_tests")).get("state")

    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=main.get("success"),
                primary_completion=main.get("stop_reason"),
                static_acceptance=(session.get("verification") or {}).get(
                    "verdict", NOT_APPLICABLE
                ),
                public_test=public_verdict or NOT_RUN,
                oracle_status=oracle.get("status", UNKNOWN),
                oracle_passed=oracle.get("passed"),
                execution_integrity=_integrity_verdict(session.get("execution_integrity")),
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
                    "enforcement": "pre_admission_guard",
                },
            },
            "context": {
                "primary_agent_peak_context": main_peak,
                "primary_agent_cumulative_input": metrics.get("main_cumulative_input_tokens"),
                "main_peak_context": main_peak,
                "main_cumulative_input": metrics.get("main_cumulative_input_tokens"),
                "integration_node_context": NOT_APPLICABLE,
                "system_max_agent_context": peaks["system_max_agent_context"],
                "per_request_input_tokens": [
                    row.get("prompt_tokens") for row in _request_rows(requests)
                ],
                "per_agent_peak_context_tokens": peaks["per_agent_peak_context_tokens"],
            },
            "delegation": {
                "natural_proposal": bool(proposals),
                "proposal_count": len(list(proposals or ())),
                "proposals": [dict(item) for item in (proposals or ())],
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
                                    "prompt_tokens",
                                    "completion_tokens",
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
                "read_metrics": session.get("handoff_reads"),
            },
            "resources": {
                "main_requests": metrics.get("main_requests"),
                "delegation_rounds": metrics.get("delegation_rounds"),
                "max_observed_child_concurrency": metrics.get("max_observed_child_concurrency"),
                "public_test_runs": len(((session.get("public_tests") or {}).get("runs")) or []),
                "tool_surface": session.get("tool_surface"),
                "ownership": session.get("ownership"),
                "sandbox": session.get("sandbox"),
                "artifact_export": export.get("workspace_path"),
            },
            "provenance": {
                "source": "main-driven session.json",
                "source_digest": _digest(dict(session)),
                "provider": session.get("provider"),
                "model": session.get("model"),
            },
        }
    )
    return evidence


# -- A0: native single agent -------------------------------------------------------------------


def from_native_result(
    cell: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
    requests: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalise a Closure-1B ``EvalRunResult`` shaped mapping.

    There is no Main. The primary agent's context is the run's own peak; every Main/metric field
    that presupposes a Main is ``NOT_APPLICABLE``, and delegation is ``NOT_APPLICABLE`` because the
    arm has no delegation plane to observe.
    """

    metrics = result.get("metrics") or {}
    budget = metrics.get("budget") or {}
    environment = result.get("environment") or {}
    safety = result.get("safety") or {}
    oracle = oracle or {}
    export = export or {}
    peaks = _peaks(requests)

    prompt = metrics.get("prompt_tokens")
    completion = metrics.get("completion_tokens")
    total = (
        prompt + completion if isinstance(prompt, int) and isinstance(completion, int) else UNKNOWN
    )

    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=result.get("agent_success"),
                primary_completion=result.get("failure_class") or "completed",
                static_acceptance=NOT_APPLICABLE,
                # The native arm has no public-test contract: no tool, no suite, nothing to run.
                public_test=NOT_APPLICABLE,
                oracle_status=oracle.get("status", UNKNOWN),
                oracle_passed=oracle.get("passed"),
                # The native arm runs no Atlas integrity mechanism; its own isolation facts are
                # recorded separately under ``safety``.
                execution_integrity=NOT_APPLICABLE,
                failure_kind=result.get("failure_class"),
            ),
            "usage": {
                "main": NOT_APPLICABLE,
                "children": NOT_APPLICABLE,
                "primary_agent": _usage_block(
                    prompt=prompt,
                    completion=completion,
                    cache_hit=metrics.get("cache_hit_tokens"),
                    cache_miss=metrics.get("cache_miss_tokens"),
                ),
                "system_total_tokens": total,
                "budget": {
                    "limit_tokens": budget.get("limit_tokens", UNKNOWN),
                    "used_tokens": budget.get("used_tokens", UNKNOWN),
                    "stop_reason": budget.get("stop_reason"),
                    "admission_rejections": budget.get("admission_rejections"),
                    "enforcement": environment.get("budget_enforcement", UNKNOWN),
                },
            },
            "context": {
                "primary_agent_peak_context": peaks["system_max_agent_context"],
                "primary_agent_cumulative_input": prompt,
                "main_peak_context": NOT_APPLICABLE,
                "main_cumulative_input": NOT_APPLICABLE,
                "integration_node_context": NOT_APPLICABLE,
                "system_max_agent_context": peaks["system_max_agent_context"],
                "per_request_input_tokens": [
                    row.get("prompt_tokens") for row in _request_rows(requests)
                ],
                "per_agent_peak_context_tokens": peaks["per_agent_peak_context_tokens"],
            },
            "delegation": NOT_APPLICABLE,
            "handoff": NOT_APPLICABLE,
            "resources": {
                "agent_latency_ms": environment.get("agent_latency_ms"),
                "export_latency_ms": environment.get("export_latency_ms"),
                "provision_latency_ms": metrics.get("provision_latency_ms"),
                "tool_calls": metrics.get("tool_calls"),
                "model_turns": metrics.get("model_turns"),
                "sandbox_created": metrics.get("sandbox_created"),
                "sandbox_destroyed": metrics.get("sandbox_destroyed"),
                "sandbox": environment.get("sandbox"),
                "ownership": NOT_APPLICABLE,
                "artifact_export": export.get("workspace_path"),
            },
            "safety": safety,
            "provenance": {
                "source": "closure1b native result.json",
                "source_digest": _digest(dict(result)),
                "provider": (result.get("identity") or {}).get("provider"),
                "model": (result.get("identity") or {}).get("model"),
            },
        }
    )
    return evidence


# -- A2: static DAG ----------------------------------------------------------------------------


def _dag_events(result: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Concurrency and dependency facts from the scheduler's own events.

    Two DAG nodes are not concurrency: a pair counts as concurrent only when each started before the
    other finished. A dependency is satisfied only when the sink started after *both* of its
    prerequisites completed.
    """

    environment = result.get("environment") or {}
    events = [
        dict(item) for item in (environment.get("task_events") or []) if isinstance(item, Mapping)
    ]
    starts: dict[str, str] = {}
    ends: dict[str, str] = {}
    for event in events:
        agent = str(event.get("agent") or "")
        if not agent:
            continue
        if event.get("type") == "agent_start":
            starts[agent] = str(event.get("ts"))
        elif event.get("type") == "agent_complete":
            ends[agent] = str(event.get("ts"))
    overlapping: list[list[str]] = []
    agents = sorted(set(starts) & set(ends))
    for index, left in enumerate(agents):
        for right in agents[index + 1 :]:
            if starts[left] < ends[right] and starts[right] < ends[left]:
                overlapping.append([left, right])
    return events, {
        "starts": starts,
        "ends": ends,
        "agents_started": len(starts),
        "agents_completed": len(ends),
        "overlapping_pairs": overlapping,
        "max_observed_concurrency": (2 if overlapping else 1 if starts else 0),
        "basis": "the scheduler's agent_start/agent_complete events, not the DAG shape",
    }


def from_dag_result(
    cell: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
    requests: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalise a static-DAG run.

    There is no Main. ``child_total_prompt_tokens`` is every worker of the DAG — the schema says so
    through ``usage_attribution: dag_all_children`` — and the integration node's context is recorded
    separately instead of being called "Main peak context".
    """

    from experiments.stage_b_plus import executor

    worker_results = result.get("worker_results") or {}
    metrics = result.get("metrics") or {}
    budget = metrics.get("budget") or {}
    oracle = oracle or {}
    export = export or {}
    _, concurrency = _dag_events(result)
    peaks = _peaks(requests)

    nodes = [dict(item) for item in (cell.get("static_dag") or [])]
    # The integration node is the one nothing depends on. "No dependencies of its own" describes a
    # source node, which is the opposite end of the same DAG.
    depended_on = {str(dep) for node in nodes for dep in node.get("depends_on") or ()}
    sinks = [node for node in nodes if str(node["task_id"]) not in depended_on]
    sink = sinks[0] if len(sinks) == 1 else None
    integration_peak = _node_peak(requests, str(sink["task_id"])) if sink else UNKNOWN

    workers: list[dict[str, Any]] = []
    for node in nodes:
        task_id = str(node["task_id"])
        agent = next(
            (
                name
                for name in worker_results
                if str(name).endswith(f"-{task_id}") or name == task_id
            ),
            None,
        )
        record = worker_results.get(agent) if agent else None
        workers.append(
            {
                "task_id": task_id,
                "depends_on": list(node.get("depends_on") or ()),
                "agent": agent,
                "success": (record or {}).get("success"),
                "stop_reason": (record or {}).get("stop_reason"),
                "tool_calls": (record or {}).get("tool_calls"),
                "token_usage": (record or {}).get("token_usage"),
                "peak_context_tokens": _node_peak(requests, task_id),
                "requests": _node_requests(requests, task_id),
                "is_integration_node": bool(sink and task_id == str(sink["task_id"])),
            }
        )

    prompt = metrics.get("prompt_tokens")
    completion = metrics.get("completion_tokens")
    total = (
        prompt + completion if isinstance(prompt, int) and isinstance(completion, int) else UNKNOWN
    )

    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=result.get("agent_success"),
                primary_completion=NOT_APPLICABLE,
                static_acceptance=NOT_APPLICABLE,
                public_test=executor.public_test_verdict(
                    (result.get("environment") or {}).get("public_tests")
                ).get("state", NOT_RUN),
                oracle_status=oracle.get("status", UNKNOWN),
                oracle_passed=oracle.get("passed"),
                execution_integrity=_integrity_verdict(
                    (result.get("environment") or {}).get("execution_integrity")
                )
                if isinstance((result.get("environment") or {}).get("execution_integrity"), Mapping)
                else UNKNOWN,
                failure_kind=result.get("failure_class"),
            ),
            "usage": {
                "main": NOT_APPLICABLE,
                "children": _usage_block(
                    prompt=prompt,
                    completion=completion,
                    cache_hit=metrics.get("cache_hit_tokens"),
                    cache_miss=metrics.get("cache_miss_tokens"),
                ),
                "system_total_tokens": total,
                "budget": {
                    "limit_tokens": budget.get("limit_tokens", UNKNOWN),
                    "used_tokens": budget.get("used_tokens", UNKNOWN),
                    "stop_reason": budget.get("stop_reason"),
                    "admission_rejections": budget.get("admission_rejections"),
                    "enforcement": "pre_admission_guard" if budget else UNKNOWN,
                },
            },
            "context": {
                "primary_agent_peak_context": NOT_APPLICABLE,
                "primary_agent_cumulative_input": NOT_APPLICABLE,
                "main_peak_context": NOT_APPLICABLE,
                "main_cumulative_input": NOT_APPLICABLE,
                "integration_node_context": integration_peak,
                "system_max_agent_context": peaks["system_max_agent_context"],
                "per_request_input_tokens": [
                    row.get("prompt_tokens") for row in _request_rows(requests)
                ],
                "per_agent_peak_context_tokens": peaks["per_agent_peak_context_tokens"],
            },
            "delegation": NOT_APPLICABLE,
            "handoff": NOT_APPLICABLE,
            "workers": workers,
            "resources": {
                "workers": len(workers),
                "max_observed_concurrency": concurrency["max_observed_concurrency"],
                "concurrency": concurrency,
                "ownership": "off",
                "sandbox": (result.get("environment") or {}).get("sandbox"),
                "artifact_export": export.get("workspace_path"),
            },
            "provenance": {
                "source": "evaluation runner result.json",
                "source_digest": _digest(dict(result)),
                "provider": (result.get("identity") or {}).get("provider"),
                "model": (result.get("identity") or {}).get("model"),
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
