"""Price book (frozen)."""

from __future__ import annotations

from collections.abc import Iterable

from foundation.errors import PriceError
from foundation.money import Money
from foundation.models import LineItem


class PriceBook:
    """A small in-memory price list."""

    def __init__(self, prices: dict[str, Money] | None = None) -> None:
        self._prices = dict(prices or DEFAULT_PRICES)

    def price(self, sku: str) -> Money:
        if sku not in self._prices:
            raise PriceError(f"unknown sku {sku!r}")
        return self._prices[sku]

    def quote(self, requests: Iterable[tuple[str, int]], currency: str) -> list[LineItem]:
        items = []
        for sku, quantity in requests:
            price = self.price(sku)
            if price.currency != currency:
                raise PriceError(f"{sku} is priced in {price.currency}, not {currency}")
            items.append(LineItem(sku=sku, quantity=quantity, unit_price=price))
        return items


DEFAULT_PRICES: dict[str, Money] = {
    "notebook": Money(1250, "EUR"),
    "pen": Money(199, "EUR"),
    "lamp": Money(4300, "EUR"),
    "mug": Money(899, "EUR"),
}
