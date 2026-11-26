"""Monthly report (v1 call site)."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import record
from foundation.models import Order


def monthly(orders: Iterable[Order], *, actor: str) -> dict:
    items = list(orders)
    total = sum(order.total.minor for order in items) / 100
    month = min((order.placed_on for order in items), default="")[:7]
    currencies = sorted({order.currency for order in items})
    return record("monthly", {"month": month, "total": total, "currencies": currencies})
