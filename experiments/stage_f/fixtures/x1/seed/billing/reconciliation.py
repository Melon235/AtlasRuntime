"""Reconciliation report (v1 caller).

The report treats receipts as dictionaries, which was true before the v2 migration.
"""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import record


def reconcile(charges: Iterable, refunds: Iterable, *, actor: str) -> dict:
    charged = sum(int(receipt["data"]["amount"]) for receipt in charges)
    refunded = sum(abs(int(receipt["data"]["amount"])) for receipt in refunds)
    return record(
        "reconciliation",
        {"charged": charged, "refunded": refunded, "net": charged - refunded},
    )
