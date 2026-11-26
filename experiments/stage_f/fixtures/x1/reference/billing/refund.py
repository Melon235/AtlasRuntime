"""Refund path (v2 call site): the tax mirrors the charge sign."""

from __future__ import annotations

from billing.tax import tax_for
from foundation.audit import Receipt, record
from foundation.models import Order
from foundation.money import to_minor


def refund(order: Order, *, actor: str) -> Receipt:
    amount = to_minor(order.total)
    return record(
        kind="refund",
        data={
            "order_id": order.order_id,
            "amount": -amount,
            "currency": order.currency,
            "tax": -tax_for(amount),
        },
        actor=actor,
    )
