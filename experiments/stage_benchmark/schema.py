"""The result record: what one cell must contain, and how each declared metric is read from it.

The schema exists to make the metric catalogue executable. Every metric the protocol declares has
a resolver here; if one is missing, the protocol is not implementable and the offline check says so
instead of leaving a column empty after the money is spent.

A resolver returns ``None`` for UNKNOWN. It never returns 0 in place of a missing measurement: the
distinction between "no cache numbers were reported" and "the cache hit zero tokens" is the one
Stage Closure-1C spent a stage establishing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

SCHEMA_VERSION = "stage-b-result/1.0"

_REQUIRED: dict[str, tuple[str, ...]] = {
    "analyzer": ("version",),
    "run": ("run_id", "task_id", "arm_id", "repetition", "status", "acceptance", "stop_reason"),
    "prompt": ("version", "digest"),
    "counts": ("provider_requests", "assistant_turns", "tool_calls"),
    "consistency": ("gaps",),
    "delegation": ("proposed", "children", "first_delegation_turn", "first_implementation_turn"),
    "context": ("main_peak_context_tokens", "system_total_tokens"),
    "verification": ("public_test_calls", "changed_files", "oracle", "goal_passed"),
}
_REQUIRED_TOP = ("failure_class",)

_ALLOWED = {
    "status": {"succeeded", "failed", "blocked", "cancelled"},
    "acceptance": {"PASS", "FAIL", "UNKNOWN", None},
    "failure_class": {"none", "budget", "turn_limit", "goal", "runtime", "sandbox", "blocked"},
}


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


def _invalid_capability(record: Mapping[str, Any]) -> Any:
    capability = record.get("capability")
    if not isinstance(capability, Mapping) or "invalid" not in capability:
        return None  # UNKNOWN: nobody judged the objectives
    return int(bool(capability["invalid"]))


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
    children = _get(record, "delegation.children")
    succeeded = _get(record, "delegation.children_succeeded")
    if children is None or succeeded is None or children == 0:
        return None
    return (int(succeeded), int(children))


def _effective(record: Mapping[str, Any]) -> Any:
    first_delegate = _get(record, "delegation.first_delegation_turn")
    first_write = _get(record, "delegation.first_implementation_turn")
    succeeded = _get(record, "delegation.children_succeeded")
    if first_delegate is None or succeeded is None:
        return 0
    return int(bool(succeeded) and first_write is not None and first_write > first_delegate)


RESOLVERS: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "oracle_pass_rate": _bool_rate("verification.oracle", expected="PASS"),
    "goal_success_rate": _bool_rate("verification.goal_passed"),
    "budget_exhaustion_rate": _bool_rate("run.stop_reason", expected="budget_exceeded"),
    "runtime_failure_rate": lambda record: int(
        record.get("failure_class") in {"runtime", "sandbox"}
    ),
    "delegation_proposal_rate": _bool_rate("delegation.proposed"),
    "router_admission_rate": lambda record: (
        None if not _delegation("proposed")(record) else int(bool(_delegation("routes")(record)))
    ),
    "child_completion_rate": _child_pair,
    "effective_delegation_rate": _effective,
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
    "duplicate_investigation_files": lambda record: _get(record, "handoff.duplicate_reads"),
    "public_test_calls": _verification("public_test_calls"),
    "public_test_pass": _verification("public_tests_passed"),
    "repair_attempts": _verification("repair_attempts"),
    "changed_files": _verification("changed_files"),
    "ownership_denials": _verification("ownership_denials"),
}


def metric_value(record: Mapping[str, Any], metric_id: str) -> Any:
    """The per-run contribution of one declared metric, or ``None`` when it is UNKNOWN."""

    resolver = RESOLVERS.get(metric_id)
    if resolver is None:
        raise KeyError(f"no resolver for metric {metric_id!r}")
    return resolver(record)


def unresolvable(metric_ids: list[str] | tuple[str, ...]) -> list[str]:
    return sorted(set(metric_ids) - set(RESOLVERS))
