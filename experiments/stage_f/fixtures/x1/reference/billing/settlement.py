"""Settlement batch (v2): one ledger entry per order, in input order."""

from __future__ import annotations

from collections.abc import Iterable

from billing import ledger
from foundation.audit import Receipt
from foundation.models import Order
from foundation.money import to_minor


def settle(orders: Iterable[Order], *, actor: str) -> list[Receipt]:
    receipts: list[Receipt] = []
    for order in orders:
        receipts.append(
            ledger.post(
                "settlement",
                to_minor(order.total),
                actor=actor,
                order_id=order.order_id,
            )
        )
    return receipts
