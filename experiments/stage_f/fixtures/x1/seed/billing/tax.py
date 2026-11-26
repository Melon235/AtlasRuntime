"""Sales tax (frozen): a flat 20 % of a non-negative minor amount."""

from __future__ import annotations

TAX_PERCENT = 20


def tax_for(minor: int) -> int:
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise ValueError("minor must be an integer")
    if minor < 0:
        raise ValueError("tax needs a non-negative amount")
    return minor * TAX_PERCENT // 100
