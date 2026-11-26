"""Return policy (frozen)."""

from __future__ import annotations

import datetime as _datetime

from foundation.errors import DomainError

RETURN_WINDOW_DAYS = 30

RESTOCKING_PERCENT: dict[str, int] = {
    "damaged": 0,
    "wrong_item": 0,
    "changed_mind": 15,
    "late": 25,
}


def _date(value: str) -> _datetime.date:
    try:
        return _datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise DomainError(f"not an ISO date: {value!r}") from error


def days_since(delivered_on: str, today: str) -> int:
    difference = _date(today) - _date(delivered_on)
    if difference.days < 0:
        raise DomainError("today is before the delivery date")
    return difference.days


def within_window(delivered_on: str, today: str) -> bool:
    return days_since(delivered_on, today) <= RETURN_WINDOW_DAYS


def restocking_fee(total_minor: int, reason: str) -> int:
    if reason not in RESTOCKING_PERCENT:
        raise DomainError(f"unknown return reason {reason!r}")
    if isinstance(total_minor, bool) or not isinstance(total_minor, int) or total_minor < 0:
        raise DomainError("total must be a non-negative integer")
    return total_minor * RESTOCKING_PERCENT[reason] // 100


def refundable(total_minor: int, reason: str) -> int:
    return total_minor - restocking_fee(total_minor, reason)
