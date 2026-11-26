"""Audit subscriber (v1 call site inside a callback)."""

from __future__ import annotations

from foundation.audit import record


def audit_subscriber(event: dict) -> dict:
    return record("bus", {"channel": event["channel"]})


def register(bus) -> object:
    bus.subscribe(audit_subscriber)
    return bus
