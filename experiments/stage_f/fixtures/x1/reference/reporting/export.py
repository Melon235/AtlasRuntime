"""CSV export request (v2 call site behind an alias and a partial)."""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial

from foundation.audit import Receipt
from foundation.audit import record as write_audit
from foundation.models import Order
from foundation.money import to_minor

_emit = partial(write_audit)


def export(orders: Iterable[Order], *, actor: str) -> Receipt:
    items = list(orders)
    total = sum(to_minor(order.total) for order in items)
    return _emit(
        kind="export",
        data={"rows": len(items), "format": "csv", "total": total},
        actor=actor,
    )
