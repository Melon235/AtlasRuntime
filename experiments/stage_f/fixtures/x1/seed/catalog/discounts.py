"""Discount rules (frozen)."""

from __future__ import annotations

from dataclasses import dataclass

from foundation.errors import DomainError
from foundation.models import Order


@dataclass(frozen=True)
class DiscountRule:
    code: str
    percent: int
    minimum_minor: int


RULES: tuple[DiscountRule, ...] = (
    DiscountRule("WELCOME10", 10, 0),
    DiscountRule("BULK20", 20, 5000),
)


def _rule(code: str) -> DiscountRule:
    for rule in RULES:
        if rule.code == code:
            return rule
    raise DomainError(f"unknown discount code {code!r}")


def discount_minor(order: Order, code: str) -> int:
    rule = _rule(code)
    total = order.total.minor
    if total < rule.minimum_minor:
        return 0
    return total * rule.percent // 100


def discounted_total(order: Order, code: str) -> int:
    return order.total.minor - discount_minor(order, code)
