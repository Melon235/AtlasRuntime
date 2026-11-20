"""The result record: what one cell must contain, and how each declared metric is read from it.

The schema exists to make the metric catalogue executable. Every metric the protocol declares has
a resolver here; if one is missing, the protocol is not implementable and the offline check says so
instead of leaving a column empty after the money is spent.

A resolver returns ``None`` for UNKNOWN. It never returns 0 in place of a missing measurement: the
distinction between "no cache numbers were reported" and "the cache hit zero tokens" is the one
Stage Closure-1C spent a stage establishing.

Stage B-E1 added two things a resolver alone cannot give:

* ``metric_status`` — whether a record carries the evidence a metric needs (MEASURED), whether the
  evidence exists but does not decide (UNKNOWN), or whether the field is absent from this schema
  version (UNSUPPORTED). "Has a resolver" was never the same as "can be measured".
* **pair metrics** — rates whose denominator is a sub-population (proposals, admitted children)
  return ``(numerator, denominator)`` instead of a ratio, so a Direct run contributes nothing to
  ``router_admission_rate`` rather than contributing a zero.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

SCHEMA_VERSION = "stage-b-result/1.1"

_REQUIRED: dict[str, tuple[str, ...]] = {
    "analyzer": ("version",),
    "run": ("run_id", "task_id", "arm_id", "repetition", "status", "acceptance", "stop_reason"),
    "prompt": ("version", "digest"),
    "counts": ("provider_requests", "assistant_turns", "tool_calls"),
    "consistency": ("gaps",),
    "delegation": (
        "proposed",
        "proposals",
        "children",
        "children_admitted",
        "admission",
        "levels",
        "first_delegation_turn",
        "first_implementation_turn",
    ),
    "handoff": ("delivered", "usable", "overlap_files"),
    "capability": ("invalid", "basis"),
    "failure": ("primary", "contributing", "confidence"),
    "usage": ("main_prompt_hit_tokens", "main_prompt_miss_tokens", "child_prompt_tokens"),
    "context": ("main_peak_context_tokens", "system_total_tokens"),
    "verification": ("public_test_calls", "changed_files", "oracle", "goal_passed"),
}
_REQUIRED_TOP = ("failure_class",)

_ALLOWED = {
    "status": {"succeeded", "failed", "blocked", "cancelled"},
    "acceptance": {"PASS", "FAIL", "UNKNOWN", None},
    # ``unknown`` is a first-class outcome: a classifier that has to guess is worse than one that
    # says so. ``tool`` covers an execution outcome the runtime could not establish.
    "failure_class": {
        "none",
        "goal",
        "budget",
        "turn_limit",
        "runtime",
        "sandbox",
        "tool",
        "blocked",
        "unknown",
    },
}

#: Metrics whose per-run contribution is ``(numerator, denominator)``, not a ratio.
PAIR_METRICS = frozenset(
    {
        "router_admission_rate",
        "child_completion_rate",
    }
)


def validate_record(record: Mapping[str, Any]) -> list[str]:
    """Every structural problem with one result record."""

    problems: list[str] = []
    for key in _REQUIRED_TOP:
        if key not in record:
            problems.append(f"missing {key!r}")
    for section, fields in _REQUIRED.items():
        block = record.get(section)
        if not isinstance(block, Mapping):
            problems.append(f"missing section {section!r}")
            continue
        for field in fields:
            if field not in block:
                problems.append(f"missing {section}.{field}")
    run = record.get("run") or {}
    if run.get("status") not in _ALLOWED["status"]:
        problems.append(f"unknown status {run.get('status')!r}")
    if run.get("acceptance") not in _ALLOWED["acceptance"]:
        problems.append(f"unknown acceptance {run.get('acceptance')!r}")
    if record.get("failure_class") not in _ALLOWED["failure_class"]:
        problems.append(f"unknown failure_class {record.get('failure_class')!r}")
    if not isinstance(run.get("repetition"), int):
        problems.append("repetition is not an int")
    counts = record.get("counts") or {}
    if counts.get("assistant_turns") is None:
        problems.append("assistant_turns is missing: it must be reconstructed, never omitted")
    return problems


def _get(record: Mapping[str, Any], path: str) -> Any:
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _bool_rate(path: str, *, expected: Any = True) -> Callable[[Mapping[str, Any]], Any]:
    def resolver(record: Mapping[str, Any]) -> Any:
        value = _get(record, path)
        if value is None:
            return None
        return int(value == expected)

    return resolver


def _context(path: str) -> Callable[[Mapping[str, Any]], Any]:
    def resolver(record: Mapping[str, Any]) -> Any:
        return _get(record, f"context.{path}")

    return resolver


def _verification(path: str) -> Callable[[Mapping[str, Any]], Any]:
    def resolver(record: Mapping[str, Any]) -> Any:
        return _get(record, f"verification.{path}")

    return resolver


def _delegation(path: str) -> Callable[[Mapping[str, Any]], Any]:
    def resolver(record: Mapping[str, Any]) -> Any:
        return _get(record, f"delegation.{path}")

    return resolver


def _admission_pair(record: Mapping[str, Any]) -> Any:
    """Admitted proposals out of proposed ones. A Direct run has no denominator and no value."""

    admission = _get(record, "delegation.admission")
    if not isinstance(admission, Mapping):
        return None
    proposals = admission.get("proposals")
    if not proposals:
        return None
    return (int(admission.get("admitted") or 0), int(proposals))


def _invalid_capability(record: Mapping[str, Any]) -> Any:
    capability = record.get("capability")
    if not isinstance(capability, Mapping):
        return None
    verdict = capability.get("invalid")
    if verdict is None:
        return None  # UNKNOWN: the evidence did not decide
    return int(bool(verdict))


#: Pre-registered, not discovered after looking at the data. Stage S1 delegated on turn 17 after
#: sixteen turns of investigation and was called late; six is the point past which a run is late by
#: construction in this protocol.
LATE_DELEGATION_TURN_THRESHOLD = 6


def _late_delegation(record: Mapping[str, Any]) -> Any:
    first_delegate = _get(record, "delegation.first_delegation_turn")
    if first_delegate is None:
        return None
    first_write = _get(record, "delegation.first_implementation_turn")
    if first_write is not None and first_write < first_delegate:
        return 1
    return int(int(first_delegate) > LATE_DELEGATION_TURN_THRESHOLD)


def _child_pair(record: Mapping[str, Any]) -> Any:
    admitted = _get(record, "delegation.children_admitted")
    succeeded = _get(record, "delegation.children_succeeded")
    if admitted is None or succeeded is None or not admitted:
        return None
    return (int(succeeded), int(admitted))


def _levels(record: Mapping[str, Any]) -> Mapping[str, Any]:
    levels = _get(record, "delegation.levels") or {}
    return levels.get("met") or {} if isinstance(levels, Mapping) else {}


def _level_chain(record: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    met = _levels(record)
    if not met or not met.get("L1_proposal"):
        return None  # no proposal: the mechanism metrics have no denominator here
    return int(all(bool(met.get(name)) for name in names))


def _mechanism_completion(record: Mapping[str, Any]) -> Any:
    return _level_chain(
        record,
        (
            "L2_admission",
            "L3_worker_completion",
            "L4_handoff_delivery",
            "L5_main_continuation",
        ),
    )


def _effective(record: Mapping[str, Any]) -> Any:
    return _level_chain(
        record,
        (
            "L2_admission",
            "L3_worker_completion",
            "L4_handoff_delivery",
            "L5_main_continuation",
            "L6_goal_correctness",
        ),
    )


def _level_number(record: Mapping[str, Any]) -> Any:
    return _get(record, "delegation.levels.level_reached")


def _failure_is(*classes: str) -> Callable[[Mapping[str, Any]], Any]:
    def resolver(record: Mapping[str, Any]) -> Any:
        primary = _get(record, "failure.primary")
        if primary is None:
            return None
        return int(primary in classes)

    return resolver


def _duplicate_reads(record: Mapping[str, Any]) -> Any:
    overlap = _get(record, "handoff.overlap_files")
    if overlap is None:
        return None  # no child read anything, so overlap is undefined rather than zero
    return len(overlap)


RESOLVERS: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "oracle_pass_rate": _bool_rate("verification.oracle", expected="PASS"),
    "goal_success_rate": _bool_rate("verification.goal_passed"),
    "budget_exhaustion_rate": _bool_rate("run.stop_reason", expected="budget_exceeded"),
    "runtime_failure_rate": _failure_is("runtime", "sandbox"),
    "unknown_failure_rate": _failure_is("unknown"),
    "delegation_proposal_rate": _bool_rate("delegation.proposed"),
    "router_admission_rate": _admission_pair,
    "child_completion_rate": _child_pair,
    "delegation_mechanism_completion_rate": _mechanism_completion,
    "effective_delegation_rate": _effective,
    "delegation_level_reached": _level_number,
    "direct_execution_rate": lambda record: int(not bool(_delegation("proposed")(record))),
    "invalid_capability_delegation_rate": _invalid_capability,
    "late_delegation_rate": _late_delegation,
    "first_delegation_turn": _delegation("first_delegation_turn"),
    "first_implementation_turn": _delegation("first_implementation_turn"),
    "main_peak_context_tokens": _context("main_peak_context_tokens"),
    "main_cumulative_input_tokens": _context("main_cumulative_input_tokens"),
    "main_cache_hit_tokens": _context("main_cache_hit_tokens"),
    "main_cache_miss_tokens": _context("main_cache_miss_tokens"),
    "child_total_prompt_tokens": _context("child_total_prompt_tokens"),
    "system_total_tokens": _context("system_total_tokens"),
    "completion_tokens": _context("completion_tokens"),
    "wall_clock_seconds": _context("wall_clock_seconds"),
    "handoff_payload_chars": _context("handoff_payload_chars"),
    "duplicate_investigation_files": _duplicate_reads,
    "public_test_calls": _verification("public_test_calls"),
    "public_test_pass": _verification("public_tests_passed"),
    "post_failure_edits": _verification("post_failure_edits"),
    "changed_files": _verification("changed_files"),
    "ownership_denials": _verification("ownership_denials"),
}

#: What a record has to carry for a metric to be measurable at all. Used by the coverage matrix.
EVIDENCE_PATHS: dict[str, str] = {
    "oracle_pass_rate": "verification.oracle",
    "goal_success_rate": "verification.goal_passed",
    "budget_exhaustion_rate": "run.stop_reason",
    "runtime_failure_rate": "failure.primary",
    "unknown_failure_rate": "failure.primary",
    "delegation_proposal_rate": "delegation.proposed",
    "router_admission_rate": "delegation.admission",
    "child_completion_rate": "delegation.children_admitted",
    "delegation_mechanism_completion_rate": "delegation.levels",
    "effective_delegation_rate": "delegation.levels",
    "delegation_level_reached": "delegation.levels",
    "direct_execution_rate": "delegation.proposed",
    "invalid_capability_delegation_rate": "capability.invalid",
    "late_delegation_rate": "delegation.first_delegation_turn",
    "first_delegation_turn": "delegation.first_delegation_turn",
    "first_implementation_turn": "delegation.first_implementation_turn",
    "main_peak_context_tokens": "context.main_peak_context_tokens",
    "main_cumulative_input_tokens": "context.main_cumulative_input_tokens",
    "main_cache_hit_tokens": "context.main_cache_hit_tokens",
    "main_cache_miss_tokens": "context.main_cache_miss_tokens",
    "child_total_prompt_tokens": "context.child_total_prompt_tokens",
    "system_total_tokens": "context.system_total_tokens",
    "completion_tokens": "context.completion_tokens",
    "wall_clock_seconds": "context.wall_clock_seconds",
    "handoff_payload_chars": "handoff.payload_chars",
    "duplicate_investigation_files": "handoff.overlap_files",
    "public_test_calls": "verification.public_test_calls",
    "public_test_pass": "verification.public_tests_passed",
    "post_failure_edits": "verification.post_failure_edits",
    "changed_files": "verification.changed_files",
    "ownership_denials": "verification.ownership_denials",
}


def metric_value(record: Mapping[str, Any], metric_id: str) -> Any:
    """The per-run contribution of one declared metric, or ``None`` when it is UNKNOWN."""

    resolver = RESOLVERS.get(metric_id)
    if resolver is None:
        raise KeyError(f"no resolver for metric {metric_id!r}")
    return resolver(record)


def metric_status(record: Mapping[str, Any], metric_id: str) -> str:
    """MEASURED, UNKNOWN or UNSUPPORTED for one metric on one record.

    ``UNSUPPORTED`` means the record does not carry the evidence path at all, which is a schema
    problem; ``UNKNOWN`` means the evidence is there and does not decide, which is a fact about the
    run.
    """

    path = EVIDENCE_PATHS.get(metric_id)
    if path is None:
        return "UNSUPPORTED"
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return "UNSUPPORTED"
        node = node[part]
    return "UNKNOWN" if metric_value(record, metric_id) is None else "MEASURED"


def unresolvable(metric_ids: list[str] | tuple[str, ...]) -> list[str]:
    return sorted(set(metric_ids) - set(RESOLVERS))
