"""Warehouse transfers (v2 call site)."""

from __future__ import annotations

from foundation.audit import Receipt, record
from foundation.errors import DomainError
from inventory.stock import InsufficientStock, StockLedger


def transfer(
    stock: StockLedger,
    *,
    actor: str,
    from_sku: str,
    to_sku: str,
    units: int,
) -> Receipt:
    if from_sku == to_sku:
        raise DomainError("a transfer needs two different skus")
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise DomainError("units must be a positive integer")
    if stock.available(from_sku) < units:
        raise InsufficientStock(f"{from_sku}: need {units}, have {stock.available(from_sku)}")
    stock.reserve(from_sku, units)
    stock.release(to_sku, units)
    return record(
        kind="transfer",
        data={"from": from_sku, "to": to_sku, "units": units},
        actor=actor,
    )
