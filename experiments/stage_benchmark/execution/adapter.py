"""Versioned execution-analysis adapter layered over the frozen Protocol v1.1 analyser.

The E1 analyser stays untouched. This adapter adds one ``execution_view`` field to the *derived*
analysis record. It separates execution integrity, goal correctness and delegation diagnostics so
that a router refusal or a failed worker can never be promoted into a goal failure, and a passing
oracle can never hide a broken collector or sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ANALYSIS_VERSION = "stage-b-execution-analysis/1.0"

AUTHORITATIVE_PREFIXES = ("host:", "fake:host")


def host_status(oracle: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return (PASS/FAIL/UNKNOWN, source). Never substitute another source for the oracle."""

    if not oracle:
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


def sandbox_status(record: Mapping[str, Any]) -> str:
    verification = record.get("verification") or {}
    # The E1 analyser flattens session.verification.dynamic into `oracle`; use that field rather
    # than expecting a nested `dynamic` dict that the frozen analyser does not preserve.
    sandbox_oracle = verification.get("oracle")
    statuses = [str(item).upper() for item in (verification.get("public_test_statuses") or [])]
    if sandbox_oracle == "PASS" and verification.get("goal_passed") is not False:
        return "PASS"
    if sandbox_oracle == "FAIL" or verification.get("goal_passed") is False:
        return "FAIL"
    if statuses and statuses[-1] == "FAIL":
        return "FAIL"
    return "UNKNOWN"


def public_status(record: Mapping[str, Any]) -> str:
    verification = record.get("verification") or {}
    statuses = [str(item).upper() for item in (verification.get("public_test_statuses") or [])]
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
    delegation = record.get("delegation") or {}
    admission = delegation.get("admission") or {}
    proposals = int(admission.get("proposals") or delegation.get("proposals") or 0)
    if not proposals:
        return "NOT_APPLICABLE"
    admitted = int(admission.get("admitted") or 0)
    rejected = int(admission.get("rejected") or 0)
    children = int(delegation.get("children_admitted") or 0)
    succeeded = int(delegation.get("children_succeeded") or 0)
    delivered = bool((record.get("handoff") or {}).get("delivered"))
    parts = [f"proposals={proposals}", f"admitted={admitted}", f"rejected={rejected}"]
    if children and succeeded < children:
        parts.append("child_failure")
    if admitted and not delivered:
        parts.append("handoff_missing")
    if rejected and not admitted:
        parts.insert(0, "ROUTER_REJECTED")
    return "+".join(parts)


def build_view(
    record: Mapping[str, Any], *, oracle: Mapping[str, Any] | None, gaps: list[str]
) -> dict[str, Any]:
    """Create the three independent verdicts. ``record`` itself is not modified here."""

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

    verification = record.get("verification") or {}
    integrity = {
        "sandbox_created": bool((record.get("sandbox") or {}).get("created")),
        "sandbox_destroyed": bool((record.get("sandbox") or {}).get("destroyed")),
        "provider_status": (record.get("counts") or {}).get("provider_requests"),
        "public_test_calls": verification.get("public_test_calls"),
        "failure_class": ((record.get("failure") or {}).get("primary")) or "none",
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
