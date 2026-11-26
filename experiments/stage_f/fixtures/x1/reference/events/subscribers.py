"""Audit subscriber (v2 call site inside a callback)."""

from __future__ import annotations

from foundation.audit import Receipt, record


def audit_subscriber(event: dict) -> Receipt:
    payload = event.get("payload", [])
    return record(
        kind="bus",
        data={"channel": event["channel"], "count": len(payload)},
        actor=event.get("actor"),
    )


def register(bus) -> object:
    bus.subscribe(audit_subscriber)
    return bus
