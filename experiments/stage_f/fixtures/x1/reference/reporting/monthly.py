"""Monthly report (v2 call site): month prefix, count, integer total and currencies."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import Receipt, record
from foundation.models import Order
from foundation.money import to_minor


def monthly(orders: Iterable[Order], *, actor: str) -> Receipt:
    items = list(orders)
    total = sum(to_minor(order.total) for order in items)
    month = min((order.placed_on for order in items), default="")[:7]
    currencies = sorted({order.currency for order in items})
    return record(
        kind="monthly",
        data={
            "month": month,
            "orders": len(items),
            "total": total,
            "currencies": currencies,
        },
        actor=actor,
    )
