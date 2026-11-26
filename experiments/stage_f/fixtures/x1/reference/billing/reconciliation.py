"""Reconciliation report (v2): receipts, not dictionaries."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import Receipt, record


def reconcile(charges: Iterable[Receipt], refunds: Iterable[Receipt], *, actor: str) -> Receipt:
    charged = sum(int(receipt.data["amount"]) for receipt in charges)
    refunded = sum(abs(int(receipt.data["amount"])) for receipt in refunds)
    return record(
        kind="reconciliation",
        data={"charged": charged, "refunded": refunded, "net": charged - refunded},
        actor=actor,
    )
