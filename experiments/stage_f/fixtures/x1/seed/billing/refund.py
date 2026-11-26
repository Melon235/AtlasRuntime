"""Refund path (v1 call site)."""

from __future__ import annotations

from foundation.audit import record
from foundation.models import Order


def refund(order: Order, *, actor: str) -> dict:
    amount = -order.total.minor / 100
    return record(
        "refund",
        {
            "order_id": order.order_id,
            "amount": amount,
            "currency": order.currency,
            "tax": amount * 0.2,
        },
    )
