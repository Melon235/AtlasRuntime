"""Orders and line items (frozen)."""

from __future__ import annotations

import datetime as _datetime
from dataclasses import dataclass

from foundation.errors import DomainError
from foundation.money import Money


def _check_date(value: str) -> str:
    if not isinstance(value, str):
        raise DomainError("placed_on must be an ISO date string")
    try:
        _datetime.date.fromisoformat(value)
    except ValueError as error:
        raise DomainError(f"placed_on is not an ISO date: {value!r}") from error
    return value


@dataclass(frozen=True)
class LineItem:
    """One ordered product."""

    sku: str
    quantity: int
    unit_price: Money

    def __post_init__(self) -> None:
        if not isinstance(self.sku, str) or not self.sku:
            raise DomainError("sku must be a non-empty string")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise DomainError("quantity must be an integer")
        if self.quantity < 1:
            raise DomainError("quantity must be positive")

    @property
    def total(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class Order:
    """One order with at least one line."""

    order_id: str
    placed_on: str
    lines: tuple[LineItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id:
            raise DomainError("order_id must be a non-empty string")
        object.__setattr__(self, "placed_on", _check_date(self.placed_on))
        if not self.lines:
            raise DomainError("an order needs at least one line")
        currencies = {line.unit_price.currency for line in self.lines}
        if len(currencies) != 1:
            raise DomainError(f"an order has one currency, got {sorted(currencies)}")

    @property
    def currency(self) -> str:
        return self.lines[0].unit_price.currency

    @property
    def total(self) -> Money:
        total = Money(0, self.currency)
        for line in self.lines:
            total = total + line.total
        return total
