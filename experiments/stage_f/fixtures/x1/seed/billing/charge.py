"""Charge path (v1 call site).

Written against the positional audit API; the values are still floats in major units.
"""

from __future__ import annotations

from foundation.audit import record
from foundation.models import Order


def charge(order: Order, *, actor: str) -> dict:
    amount = order.total.minor / 100
    return record(
        "charge",
        {
            "order_id": order.order_id,
            "amount": amount,
            "currency": order.currency,
            "tax": amount * 0.2,
        },
    )
