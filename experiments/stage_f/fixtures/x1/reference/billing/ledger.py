"""Ledger wrapper (v2): keyword-only identity, zero entries rejected."""

from __future__ import annotations

from foundation.audit import Receipt, record


def post(kind: str, amount: int, *, actor: str, order_id: str) -> Receipt:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError("amount must be an integer")
    if amount == 0:
        raise ValueError("a ledger entry needs a non-zero amount")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("order_id must be a non-empty string")
    return record(kind=kind, data={"order_id": order_id, "amount": amount}, actor=actor)
