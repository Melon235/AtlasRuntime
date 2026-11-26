"""Integer money arithmetic (frozen)."""

from __future__ import annotations

from dataclasses import dataclass

from foundation.errors import CurrencyError, DomainError


def _check_currency(currency: str) -> str:
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise DomainError(f"currency must be three letters, got {currency!r}")
    return currency.upper()


@dataclass(frozen=True)
class Money:
    """An amount in integer minor units plus its ISO currency."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise DomainError("minor must be an integer")
        object.__setattr__(self, "currency", _check_currency(self.currency))

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        if other.currency != self.currency:
            raise CurrencyError(f"cannot add {self.currency} and {other.currency}")
        return Money(self.minor + other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __mul__(self, quantity: int) -> Money:
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise DomainError("quantity must be an integer")
        return Money(self.minor * quantity, self.currency)


def to_minor(money: Money) -> int:
    return money.minor


def parse_minor(text: str) -> int:
    """Parse ``12.34`` / ``-1.5`` / ``7`` into minor units."""

    if not isinstance(text, str) or not text.strip():
        raise DomainError(f"not a decimal amount: {text!r}")
    body = text.strip()
    sign = 1
    if body.startswith("-"):
        sign = -1
        body = body[1:]
    elif body.startswith("+"):
        body = body[1:]
    if body.count(".") > 1:
        raise DomainError(f"not a decimal amount: {text!r}")
    whole, _, fraction = body.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise DomainError(f"not a decimal amount: {text!r}")
    if len(fraction) > 2:
        raise DomainError(f"more than two fraction digits: {text!r}")
    minor = int(whole) * 100 + int(fraction.ljust(2, "0") or "0")
    return sign * minor


def format_minor(minor: int, currency: str) -> str:
    """Render minor units with exactly two fraction digits."""

    sign = "-" if minor < 0 else ""
    value = abs(int(minor))
    return f"{sign}{value // 100}.{value % 100:02d} {_check_currency(currency)}"
