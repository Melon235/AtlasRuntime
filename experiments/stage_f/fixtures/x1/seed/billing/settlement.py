"""Settlement batch (v1 caller of the ledger wrapper)."""

from __future__ import annotations

from collections.abc import Iterable

from billing import ledger
from foundation.models import Order


def settle(orders: Iterable[Order], *, actor: str) -> list:
    receipts = []
    for order in orders:
        receipts.append(ledger.post("settlement", order.total.minor, order.order_id))
    return receipts
