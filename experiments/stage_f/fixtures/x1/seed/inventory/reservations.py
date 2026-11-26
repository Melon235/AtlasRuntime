"""Reservations (v1 call site)."""

from __future__ import annotations

from foundation.audit import record
from foundation.models import Order
from inventory.stock import StockLedger


def reserve(order: Order, *, actor: str, stock: StockLedger) -> dict:
    units = len(order.lines)
    skus = sorted({line.sku for line in order.lines})
    for line in order.lines:
        stock.reserve(line.sku, line.quantity)
    return record("reservation", {"order_id": order.order_id, "units": units, "skus": skus})
