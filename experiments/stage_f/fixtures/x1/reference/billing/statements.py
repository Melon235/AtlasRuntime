"""Customer statement (v2): credits from the standard discount, keyword audit call."""

from __future__ import annotations

from collections.abc import Iterable

from catalog.discounts import discount_minor
from foundation.audit import Receipt
from foundation.audit import record as write_statement
from foundation.models import Order

_emit = write_statement

DISCOUNT_CODE = "WELCOME10"


def statement(orders: Iterable[Order], *, actor: str) -> Receipt:
    items = list(orders)
    gross = sum(order.total.minor for order in items)
    credits = sum(discount_minor(order, DISCOUNT_CODE) for order in items)
    return _emit(
        kind="statement",
        data={
            "orders": len(items),
            "gross": gross,
            "credits": credits,
            "net": gross - credits,
        },
        actor=actor,
    )
