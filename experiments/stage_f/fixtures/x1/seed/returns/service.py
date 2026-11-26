"""Return requests (v1 call site)."""

from __future__ import annotations

from foundation.audit import record
from foundation.errors import DomainError
from foundation.models import Order
from returns.policy import within_window


def request_return(
    order: Order,
    *,
    actor: str,
    delivered_on: str,
    today: str,
    reason: str = "changed_mind",
) -> dict:
    if not within_window(delivered_on, today):
        raise DomainError("the return window has closed")
    return record(
        "return",
        {
            "order_id": order.order_id,
            "reason": reason,
            "fee": 0,
            "refundable": order.total.minor / 100,
        },
    )
