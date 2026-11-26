"""Reservations (v2): units per SKU, all-or-nothing, keyword audit call."""

from __future__ import annotations

from foundation.audit import Receipt, record
from foundation.models import Order
from inventory.stock import InsufficientStock, StockLedger


def _requirements(order: Order) -> dict[str, int]:
    required: dict[str, int] = {}
    for line in order.lines:
        required[line.sku] = required.get(line.sku, 0) + line.quantity
    return required


def reserve(order: Order, *, actor: str, stock: StockLedger) -> Receipt:
    required = _requirements(order)
    for sku in sorted(required):
        if stock.available(sku) < required[sku]:
            raise InsufficientStock(
                f"{sku}: need {required[sku]}, have {stock.available(sku)}"
            )
    for sku in sorted(required):
        stock.reserve(sku, required[sku])
    return record(
        kind="reservation",
        data={
            "order_id": order.order_id,
            "units": sum(required.values()),
            "skus": sorted(required),
        },
        actor=actor,
    )
