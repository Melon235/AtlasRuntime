"""Charge path (v2 call site): minor units, integer tax, keyword audit call."""

from __future__ import annotations

from billing.tax import tax_for
from foundation.audit import Receipt, record
from foundation.models import Order
from foundation.money import to_minor


def charge(order: Order, *, actor: str) -> Receipt:
    amount = to_minor(order.total)
    return record(
        kind="charge",
        data={
            "order_id": order.order_id,
            "amount": amount,
            "currency": order.currency,
            "tax": tax_for(amount),
        },
        actor=actor,
    )
