"""Daily report (v1 call site)."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import record
from foundation.models import Order


def daily(orders: Iterable[Order], *, actor: str) -> dict:
    items = list(orders)
    total = sum(order.total.minor for order in items) / 100
    date = min((order.placed_on for order in items), default="")
    return record("daily", {"date": date, "total": total})
