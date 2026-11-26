"""Return requests (v2): policy fee in minor units, keyword audit call."""

from __future__ import annotations

from foundation.audit import Receipt, record
from foundation.errors import DomainError
from foundation.models import Order
from returns.policy import refundable, restocking_fee, within_window


def request_return(
    order: Order,
    *,
    actor: str,
    delivered_on: str,
    today: str,
    reason: str = "changed_mind",
) -> Receipt:
    if not within_window(delivered_on, today):
        raise DomainError("the return window has closed")
    total = order.total.minor
    fee = restocking_fee(total, reason)
    return record(
        kind="return",
        data={
            "order_id": order.order_id,
            "reason": reason,
            "fee": fee,
            "refundable": refundable(total, reason),
        },
        actor=actor,
    )
