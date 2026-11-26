"""Stock ledger (frozen)."""

from __future__ import annotations

from foundation.errors import DomainError


class InsufficientStock(DomainError):
    """A reservation asked for more units than are available."""


class StockLedger:
    """On-hand units per SKU."""

    def __init__(self, on_hand: dict[str, int] | None = None) -> None:
        self._on_hand = dict(on_hand or {})

    def available(self, sku: str) -> int:
        return self._on_hand.get(sku, 0)

    def reserve(self, sku: str, units: int) -> None:
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise DomainError("units must be a positive integer")
        if self.available(sku) < units:
            raise InsufficientStock(f"{sku}: need {units}, have {self.available(sku)}")
        self._on_hand[sku] = self.available(sku) - units

    def release(self, sku: str, units: int) -> None:
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise DomainError("units must be a positive integer")
        self._on_hand[sku] = self.available(sku) + units

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self._on_hand.items()))
