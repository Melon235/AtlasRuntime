"""Customer statement (v1 call site through an alias)."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.audit import record as write_statement
from foundation.models import Order

_emit = write_statement


def statement(orders: Iterable[Order], *, actor: str) -> dict:
    items = list(orders)
    gross = sum(int(order.total.minor) for order in items)
    credits = 0
    return _emit(
        "statement",
        {"orders": len(items), "gross": gross, "credits": credits, "net": gross - credits},
    )
