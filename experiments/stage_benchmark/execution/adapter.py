"""Versioned execution-analysis adapter layered over the frozen Protocol v1.1 analyser.

The E1 analyser stays untouched. This adapter adds one ``execution_view`` field to the *derived*
analysis. It separates execution integrity, goal correctness and delegation diagnostics so that a
router refusal or a failed worker can never be promoted into a goal failure, and a passing oracle
can never hide a broken collector or sandbox.

1.1: the input may be a reconstructed record *or* a raw session. ``session.failure`` can be a
string, so every field access is shape-tolerant; a statistics-layer exception must never replace
the original sandbox/provider failure.

1.2: lifecycle facts (``status``, ``sandbox.created``, ``sandbox.destroyed``) are read from the raw
session when the caller supplies one. A reconstructed record keeps ``run.status`` and
``failure.evidence`` but drops the sandbox block, so reading them off the record reported a created
and destroyed sandbox as ``false``/``false``/``null``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ANALYSIS_VERSION = "stage-b-execution-analysis/1.2"

AUTHORITATIVE_PREFIXES = ("host:", "fake:host")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def host_status(oracle: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return (PASS/FAIL/UNKNOWN, source). Never substitute another source for the oracle."""

    if not isinstance(oracle, Mapping):
        return "UNKNOWN", "UNKNOWN"
    source = str(oracle.get("source") or "UNKNOWN")
    if not source.startswith(AUTHORITATIVE_PREFIXES):
        return "UNKNOWN", source
    value = oracle.get("ok")
    if value is True:
        return "PASS", source
    if value is False:
        return "FAIL", source
    return "UNKNOWN", source


def _verification(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(record.get("verification"))


def sandbox_status(record: Mapping[str, Any]) -> str:
    verification = _verification(record)
    # The E1 analyser flattens session.verification.dynamic into `oracle`; use that field rather
    # than expecting a nested `dynamic` dict that the frozen analyser does not preserve.
    sandbox_oracle = verification.get("oracle")
    statuses = [str(item).upper() for item in _sequence(verification.get("public_test_statuses"))]
    if sandbox_oracle == "PASS" and verification.get("goal_passed") is not False:
        return "PASS"
    if sandbox_oracle == "FAIL" or verification.get("goal_passed") is False:
        return "FAIL"
    if statuses and statuses[-1] == "FAIL":
        return "FAIL"
    return "UNKNOWN"


def public_status(record: Mapping[str, Any]) -> str:
    verification = _verification(record)
    statuses = [str(item).upper() for item in _sequence(verification.get("public_test_statuses"))]
    if statuses and statuses[-1] == "PASS":
        return "PASS"
    if statuses and statuses[-1] == "FAIL":
        return "FAIL"
    return "UNKNOWN"


def agreement(host: str, sandbox: str) -> str:
    if host == "UNKNOWN" or sandbox == "UNKNOWN":
        return "UNKNOWN"
    return "AGREE" if host == sandbox else "ORACLE_DISAGREEMENT"


def delegation_diagnostics(record: Mapping[str, Any]) -> str:
    delegation = _mapping(record.get("delegation"))
    admission = _mapping(delegation.get("admission"))
    proposals = int(admission.get("proposals") or delegation.get("proposals") or 0)
    if not proposals:
        return "NOT_APPLICABLE"
    admitted = int(admission.get("admitted") or 0)
    rejected = int(admission.get("rejected") or 0)
    children = int(delegation.get("children_admitted") or 0)
    succeeded = int(delegation.get("children_succeeded") or 0)
    delivered = bool(_mapping(record.get("handoff")).get("delivered"))
    parts = [f"proposals={proposals}", f"admitted={admitted}", f"rejected={rejected}"]
    if children and succeeded < children:
        parts.append("child_failure")
    if admitted and not delivered:
        parts.append("handoff_missing")
    if rejected and not admitted:
        parts.insert(0, "ROUTER_REJECTED")
    return "+".join(parts)


def failure_class(record: Mapping[str, Any]) -> str:
    """Read a reconstructed record's ``failure.primary`` or a raw session's string failure."""

    failure = record.get("failure")
    if isinstance(failure, str):
        return failure or "none"
    if isinstance(failure, Mapping):
        return str(failure.get("primary") or "none")
    return "none"


def provider_requests(record: Mapping[str, Any]) -> int | None:
    counts = _mapping(record.get("counts"))
    if counts.get("provider_requests") is not None:
        return int(counts["provider_requests"])
    metrics = _mapping(record.get("metrics"))
    value = metrics.get("main_requests")
    return int(value) if isinstance(value, int) else None


def execution_stage(record: Mapping[str, Any]) -> str:
    """``PRE_AGENT`` when the run died before the model loop; ``AGENT`` otherwise."""

    sandbox = _mapping(record.get("sandbox"))
    requests = provider_requests(record)
    if requests == 0 and not sandbox.get("created"):
        return "PRE_AGENT"
    if requests is None and not sandbox.get("created"):
        return "PRE_AGENT"
    return "AGENT"


def build_view(
    record: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any] | None,
    gaps: list[str],
    session: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the three independent verdicts. ``record`` itself is not modified here.

    ``session`` is the raw session when the caller has one. Lifecycle facts are read from it because
    a reconstructed record does not carry the sandbox block; without it the record remains the only
    source, which is what the degraded (pre-agent) path relies on.
    """

    lifecycle = session if isinstance(session, Mapping) else record
    host, source = host_status(oracle)
    sandbox = sandbox_status(record)
    public = public_status(record)
    match = agreement(host, sandbox)
    if host == "FAIL" or public == "FAIL":
        goal = "FAIL"
    elif host == "PASS" and public == "PASS":
        goal = "PASS"
    else:
        goal = "UNKNOWN"
    if host == "UNKNOWN":
        gaps.append("host oracle verdict is UNKNOWN; no other source may substitute for it")
    if match == "ORACLE_DISAGREEMENT":
        gaps.append(f"ORACLE_DISAGREEMENT: host={host} sandbox={sandbox}")

    verification = _verification(record)
    sandbox_info = _mapping(lifecycle.get("sandbox"))
    integrity = {
        "stage": execution_stage(lifecycle),
        "session_status": lifecycle.get("status"),
        "sandbox_created": bool(sandbox_info.get("created")),
        "sandbox_destroyed": bool(sandbox_info.get("destroyed")),
        "provider_requests": provider_requests(record),
        "public_test_calls": verification.get("public_test_calls"),
        "primary_failure": failure_class(record),
    }
    return {
        "analysis_version": ANALYSIS_VERSION,
        "execution_integrity": integrity,
        "goal_correctness": {
            "status": goal,
            "host_oracle": host,
            "sandbox_verification": sandbox,
            "public_tests": public,
            "authoritative_source": source if host != "UNKNOWN" else "UNKNOWN",
            "oracle_agreement": match,
        },
        "delegation_diagnostics": {
            "status": delegation_diagnostics(record),
            "authoritative": False,
        },
        "sources": {
            "host_oracle": "host:oracle.json (executed after the Main stopped)",
            "sandbox_verification": "session.verification (Sandbox-side, advisory)",
            "delegation": "session.delegation + handoff evidence (diagnostic only)",
        },
    }
