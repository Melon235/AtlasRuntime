"""Daily report (v2 call site): integer minor total and an order count."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import Receipt, record
from foundation.models import Order
from foundation.money import to_minor


def daily(orders: Iterable[Order], *, actor: str) -> Receipt:
    items = list(orders)
    total = sum(to_minor(order.total) for order in items)
    date = min((order.placed_on for order in items), default="")
    return record(
        kind="daily",
        data={"date": date, "orders": len(items), "total": total},
        actor=actor,
    )
