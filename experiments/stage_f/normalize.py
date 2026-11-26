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

SCHEMA_VERSION = "stage-f-cell-evidence/1.2"

NOT_APPLICABLE = "NOT_APPLICABLE"
UNKNOWN = "UNKNOWN"
NOT_RUN = "NOT_RUN"
PASS = "PASS"
FAIL = "FAIL"

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
    execution_integrity: Any,
    agent_success: Any,
) -> bool:
    """Delivered success: a correct artifact produced by a run whose own checks all passed.

    The pre-registered definition is a conjunction of three *independently recorded* verdicts: an
    oracle PASS alone is not delivery (the agent may have failed), and an agent completion alone is
    not delivery (the artifact may be wrong). ``execution_integrity`` must be **PASS**, not merely
    "not FAIL": a common integrity check that could not be evaluated is ``UNKNOWN`` and does not
    establish delivery. Only an architecture-specific mechanism that does not exist for this arm is
    exempt, and that exemption happens before this function sees the value.

    The public suite is deliberately *not* part of this definition. It is a separate metric with its
    own verdict — a run can deliver a correct artifact and still leave the public suite failing in
    one arm, or not run it at all in another (A0 has no such tool, A2 configures none). Folding it
    into delivery would make the primary success metric depend on the arm's tool surface.
    """

    if agent_success is not True:
        return False
    if oracle_status != "PASS":
        return False
    return execution_integrity == PASS


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


def apply_integrity(evidence: dict[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp the common integrity verdict onto an evidence record and re-derive delivery.

    The verdict is the runner's, not the adapter's: only the runner holds the fixture digest, the
    exported directory and the oracle's sandbox identity at the same time. ``delivered_success`` is
    recomputed from the stamped verdict so the two can never disagree in a written record.
    """

    correctness = evidence.setdefault("correctness", {})
    verdict = str(report.get("verdict") or UNKNOWN)
    correctness["execution_integrity"] = verdict
    correctness["delivered_success"] = _delivered(
        oracle_status=(correctness.get("independent_oracle") or {}).get("status"),
        execution_integrity=verdict,
        agent_success=correctness.get("agent_success"),
    )
    evidence["integrity"] = dict(report)
    return evidence


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


# -- production measurement --------------------------------------------------------------------
#
# The proof's request rows come from the deterministic provider; a production run has no such
# injector, and its usage lives in the record each arm actually writes. These readers map that
# record onto the same row shape, so one peak/uniqueness rule scores both. Nothing here estimates a
# token count: a counter the record does not carry stays ``None`` and becomes ``UNKNOWN``.


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _main_request_rows(session: Mapping[str, Any], *, agent: str) -> list[dict[str, Any]]:
    """The Main's own per-request context series, from the session's provider-observed metrics."""

    metrics = session.get("metrics") or {}
    series = metrics.get("main_context_tokens_by_request")
    if not isinstance(series, list):
        main = session.get("main") or {}
        series = main.get("context_tokens_by_request")
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(series if isinstance(series, list) else []):
        tokens = _int_or_none(value)
        if tokens is not None:
            rows.append(
                {
                    "index": index,
                    "trusted_task_id": agent,
                    "prompt_tokens": tokens,
                    "source": "session.main.context_tokens_by_request",
                }
            )
    return rows


def _native_request_rows(result: Mapping[str, Any], *, agent: str) -> list[dict[str, Any]]:
    """The native run's ``per_request_usage`` rows, in the order the requests were sent."""

    metrics = result.get("metrics") or {}
    rows: list[dict[str, Any]] = []
    for record in metrics.get("per_request_usage") or []:
        if not isinstance(record, Mapping):
            continue
        tokens = _int_or_none(record.get("prompt_tokens"))
        if tokens is not None:
            rows.append(
                {
                    "index": record.get("index"),
                    "trusted_task_id": agent,
                    "prompt_tokens": tokens,
                    "completion_tokens": _int_or_none(record.get("completion_tokens")),
                    "source": "result.metrics.per_request_usage",
                }
            )
    return rows


def _dag_request_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The evaluation runner's request-evidence records, one per observed provider request."""

    metrics = result.get("metrics") or {}
    evidence = metrics.get("request_evidence")
    records = evidence.get("requests") if isinstance(evidence, Mapping) else None
    rows: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else {}
        tokens = _int_or_none(usage.get("prompt_tokens"))
        if tokens is None:
            continue
        rows.append(
            {
                "index": record.get("call_index"),
                "trusted_task_id": record.get("trusted_task_id"),
                "prompt_tokens": tokens,
                "cache_hit_tokens": usage.get("cache_hit_tokens"),
                "cache_miss_tokens": usage.get("cache_miss_tokens"),
                "source": "request_evidence.records",
            }
        )
    return rows


def _census(
    *,
    rows: Sequence[Mapping[str, Any]],
    expected: Sequence[str],
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    """Peak context for every agent that actually ran, and whether the census is complete.

    ``system_max_agent_context`` is the maximum over **all** agents. A Main peak is never promoted
    to a system peak: if one expected agent has no observed peak, the system value is ``UNKNOWN``
    and the missing agent is named.
    """

    observed = _peaks(rows)["per_agent_peak_context_tokens"] if rows else {}
    # A single-agent arm whose provider did not carry a trusted task id still measured exactly one
    # agent: all of its requests belong to the only agent that ran. That is an attribution of one
    # observation to one expected agent, not a guess between several.
    sole_observed = (
        next(iter(observed.values())) if len(observed) == 1 and len(expected) == 1 else None
    )
    peaks: dict[str, Any] = {}
    missing: list[str] = []
    for agent in expected:
        value = observed.get(agent)
        if value is None:
            matches = [item for key, item in observed.items() if key.endswith(f"-{agent}")]
            value = matches[0] if len(matches) == 1 else None
        if value is None:
            value = sole_observed
        if value is None:
            fallback_value = fallback.get(agent)
            value = fallback_value if _int_or_none(fallback_value) is not None else None
        if value is None:
            peaks[agent] = UNKNOWN
            missing.append(agent)
        else:
            peaks[agent] = int(value)
    complete = not missing and bool(peaks)
    return {
        "peaks": peaks,
        "complete": complete,
        "missing_agents": sorted(missing),
        "system_max_agent_context": max(peaks.values()) if complete else UNKNOWN,
        "basis": (
            "per-request provider-observed prompt size for every agent that ran"
            if complete
            else "at least one agent's peak context was not observed"
        ),
    }


def _request_count(*, main_requests: Any, child_requests: Any, total: Any) -> dict[str, Any]:
    """Request accounting, with the child side ``NOT_APPLICABLE`` when the arm has no children.

    A role the arm does not have is ``NOT_APPLICABLE``; a role that exists but whose count was not
    recorded is ``UNKNOWN`` and makes the total unknown, instead of silently equal to the Main's.
    """

    main = _int_or_none(main_requests)
    if child_requests in (NOT_APPLICABLE, UNKNOWN):
        children: Any = child_requests
    else:
        children = _int_or_none(child_requests)
    combined = _int_or_none(total)
    if combined is None and main is not None:
        if children == NOT_APPLICABLE:
            combined = main
        elif isinstance(children, int):
            combined = main + children
    return {"main": main, "children": children, "total": combined}


def _latency(block: Mapping[str, Any]) -> dict[str, Any]:
    """Wall-clock split. ``UNKNOWN`` for a stage the arm does not time, never ``0``."""

    stages = (
        "agent_execution_ms",
        "sandbox_provision_ms",
        "export_ms",
        "oracle_ms",
        "cleanup_ms",
        "end_to_end_ms",
    )
    known: dict[str, Any] = {}
    unknown: list[str] = []
    for stage in stages:
        value = block.get(stage)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            known[stage] = round(float(value), 3)
        else:
            known[stage] = UNKNOWN
            unknown.append(stage)
    return {**known, "unknown_stages": unknown}


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
    latency: Mapping[str, Any] | None = None,
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
    main_peak = metrics.get("main_peak_context_tokens")
    main_agent = str(main.get("task_id") or "main")
    # A child's provider identity is its ``worker_id`` (the trusted id the provider context
    # carries); the plan ``key`` is only the delegation name. Using the key would report an observed
    # child as missing and turn a complete census into UNKNOWN.
    children_seen = [
        str(child.get("worker_id") or child.get("key") or "child")
        for batch in batches
        for child in (batch.get("children") or [])
    ]
    rows = _request_rows(requests) or _main_request_rows(session, agent=main_agent)
    census = _census(
        rows=rows,
        expected=[main_agent, *children_seen],
        fallback={main_agent: main_peak},
    )

    public_verdict = public_test
    if public_verdict is None:
        public_verdict = executor.public_test_verdict(session.get("public_tests")).get("state")

    # Completion means the run *effectively* succeeded, not that AnyCode handed back an empty
    # result whose ``success`` is True. A run that never completed a turn is a failure even when
    # the raw AgentResult says otherwise; the raw value is kept beside it so the two are never
    # conflated.
    effective_success = session.get("execution_succeeded")
    agent_success = (
        effective_success if isinstance(effective_success, bool) else main.get("success")
    )

    evidence = blank_evidence(cell)
    evidence.update(
        {
            "correctness": _correctness(
                agent_success=agent_success,
                primary_completion=main.get("stop_reason"),
                static_acceptance=(session.get("verification") or {}).get(
                    "verdict", NOT_APPLICABLE
                ),
                public_test=public_verdict or NOT_RUN,
                oracle_status=oracle.get("status", UNKNOWN),
                oracle_passed=oracle.get("passed"),
                execution_integrity=UNKNOWN,
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
                "requests": _request_count(
                    main_requests=metrics.get("main_requests"),
                    # A child's request count is not part of the delegation evidence, so an arm
                    # with children reports the total as unknown instead of assuming turns = calls.
                    child_requests=UNKNOWN if children_seen else NOT_APPLICABLE,
                    total=None,
                ),
                "requested_model": str((cell.get("budget") or {}).get("model") or UNKNOWN),
                # The goal path records the model it *asked for*; it does not record the model the
                # provider resolved it to, so the returned identity is unknown rather than assumed.
                "returned_model": UNKNOWN,
                "accounting": "provider-reported per request (Atlas UsageAttributionHook)",
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
                "system_max_agent_context": census["system_max_agent_context"],
                "per_request_input_tokens": [row.get("prompt_tokens") for row in rows],
                "per_agent_peak_context_tokens": census["peaks"],
                "agent_context_census": census,
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
                                    "worker_id",
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
                "latency": _latency(latency or {}),
            },
            "provenance": {
                "source": "main-driven session.json",
                "source_digest": _digest(dict(session)),
                "provider": session.get("provider"),
                "model": session.get("model"),
            },
        }
    )
    # Architecture-specific mechanism, recorded beside the common verdict rather than instead of
    # it: the payload corpus is Atlas's own record, not the shared integrity gate.
    evidence["correctness"]["atlas_execution_records"] = _integrity_verdict(
        session.get("execution_integrity")
    )
    evidence["correctness"]["raw_agent_success"] = main.get("success")
    return evidence


# -- A0: native single agent -------------------------------------------------------------------


def from_native_result(
    cell: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
    requests: Sequence[Mapping[str, Any]] | None = None,
    latency: Mapping[str, Any] | None = None,
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
    agent = "primary_agent"
    rows = _request_rows(requests) or _native_request_rows(result, agent=agent)
    census = _census(
        rows=rows,
        expected=[agent],
        fallback={agent: metrics.get("peak_context_tokens")},
    )

    prompt = metrics.get("prompt_tokens")
    completion = metrics.get("completion_tokens")
    total = (
        prompt + completion if isinstance(prompt, int) and isinstance(completion, int) else UNKNOWN
    )
    resolved = environment.get("resolved_models")
    returned_model = (
        resolved[0]
        if isinstance(resolved, list) and len(resolved) == 1 and isinstance(resolved[0], str)
        else UNKNOWN
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
                # Replaced by the common integrity verdict the runner computes. Until then the
                # adapter says "not established" rather than "not applicable": the common checks
                # are not an Atlas-only mechanism.
                execution_integrity=UNKNOWN,
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
                "requests": _request_count(
                    main_requests=len(rows) or metrics.get("model_turns"),
                    child_requests=NOT_APPLICABLE,
                    total=None,
                ),
                "requested_model": str((cell.get("budget") or {}).get("model") or UNKNOWN),
                "returned_model": returned_model,
                "accounting": "provider-reported (InferenceObservation.usage)",
                "budget": {
                    "limit_tokens": budget.get("limit_tokens", UNKNOWN),
                    "used_tokens": budget.get("used_tokens", UNKNOWN),
                    "stop_reason": budget.get("stop_reason"),
                    "admission_rejections": budget.get("admission_rejections"),
                    "enforcement": environment.get("budget_enforcement", UNKNOWN),
                },
            },
            "context": {
                "primary_agent_peak_context": census["peaks"].get(agent),
                "primary_agent_cumulative_input": prompt,
                "main_peak_context": NOT_APPLICABLE,
                "main_cumulative_input": NOT_APPLICABLE,
                "integration_node_context": NOT_APPLICABLE,
                "system_max_agent_context": census["system_max_agent_context"],
                "per_request_input_tokens": [row.get("prompt_tokens") for row in rows],
                "per_agent_peak_context_tokens": census["peaks"],
                "agent_context_census": census,
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
                "latency": _latency(
                    latency
                    or {
                        "agent_execution_ms": environment.get("agent_latency_ms"),
                        "sandbox_provision_ms": metrics.get("provision_latency_ms"),
                        "export_ms": metrics.get("export_latency_ms"),
                        "oracle_ms": metrics.get("oracle_latency_ms"),
                        "end_to_end_ms": metrics.get("latency_ms"),
                    }
                ),
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
    latency: Mapping[str, Any] | None = None,
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
    rows = _request_rows(requests) or _dag_request_rows(result)

    nodes = [dict(item) for item in (cell.get("static_dag") or [])]
    node_ids = [str(node["task_id"]) for node in nodes]
    census = _census(rows=rows, expected=node_ids, fallback={})
    # The integration node is the one nothing depends on. "No dependencies of its own" describes a
    # source node, which is the opposite end of the same DAG.
    depended_on = {str(dep) for node in nodes for dep in node.get("depends_on") or ()}
    sinks = [node for node in nodes if str(node["task_id"]) not in depended_on]
    sink = sinks[0] if len(sinks) == 1 else None
    integration_peak = census["peaks"].get(str(sink["task_id"])) if sink else UNKNOWN

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
                "peak_context_tokens": census["peaks"].get(task_id, UNKNOWN),
                "requests": _node_requests(rows, task_id),
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
                execution_integrity=UNKNOWN,
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
                "requests": _request_count(
                    main_requests=NOT_APPLICABLE,
                    child_requests=len(rows) or metrics.get("model_turns"),
                    total=None,
                ),
                "requested_model": str(
                    (result.get("environment") or {}).get("requested_model")
                    or (cell.get("budget") or {}).get("model")
                    or UNKNOWN
                ),
                "returned_model": (
                    (result.get("environment") or {}).get("resolved_models")[0]
                    if isinstance((result.get("environment") or {}).get("resolved_models"), list)
                    and len((result.get("environment") or {}).get("resolved_models")) == 1
                    else UNKNOWN
                ),
                "accounting": "provider-reported per request (request_evidence.records)",
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
                "system_max_agent_context": census["system_max_agent_context"],
                "per_request_input_tokens": [row.get("prompt_tokens") for row in rows],
                "per_agent_peak_context_tokens": census["peaks"],
                "agent_context_census": census,
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
                "latency": _latency(
                    latency
                    or {
                        "agent_execution_ms": (result.get("environment") or {}).get(
                            "agent_latency_ms"
                        ),
                        "sandbox_provision_ms": metrics.get("provision_latency_ms"),
                        "export_ms": metrics.get("export_latency_ms"),
                        "oracle_ms": metrics.get("oracle_latency_ms"),
                        "end_to_end_ms": metrics.get("latency_ms"),
                    }
                ),
            },
            "provenance": {
                "source": "evaluation runner result.json",
                "source_digest": _digest(dict(result)),
                "provider": (result.get("identity") or {}).get("provider"),
                "model": (result.get("identity") or {}).get("model"),
            },
        }
    )
    environment = result.get("environment") or {}
    evidence["correctness"]["atlas_execution_records"] = (
        _integrity_verdict(environment.get("execution_integrity"))
        if isinstance(environment, Mapping)
        else UNKNOWN
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
    census = context.get("agent_context_census")
    if isinstance(census, Mapping):
        if census.get("system_max_agent_context") != context.get("system_max_agent_context"):
            problems.append("the system max agent context disagrees with its own census")
        if census.get("complete") is False and context.get("system_max_agent_context") not in (
            UNKNOWN,
        ):
            problems.append("an incomplete agent census reported a system maximum")
    if "integrity" in evidence:
        report = evidence.get("integrity")
        if not isinstance(report, Mapping):
            problems.append("integrity is not a report")
        elif report.get("verdict") not in {PASS, FAIL, UNKNOWN}:
            problems.append(f"integrity verdict is {report.get('verdict')!r}")
        elif not report.get("checks"):
            problems.append("integrity carries no checks")
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
    "FAIL",
    "NOT_APPLICABLE",
    "NOT_RUN",
    "PASS",
    "SCHEMA_VERSION",
    "UNKNOWN",
    "apply_integrity",
    "blank_evidence",
    "evidence_digest",
    "from_dag_result",
    "from_main_child_session",
    "from_native_result",
    "main_metric",
    "sha256_bytes",
    "validate_evidence",
]
