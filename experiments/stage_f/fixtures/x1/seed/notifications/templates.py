"""Notification templates (frozen)."""

from __future__ import annotations

from foundation.money import format_minor
from foundation.models import Order

CHANNELS = ("email", "sms")


class Templates:
    """Render one short message per channel."""

    def render(self, order: Order, channel: str) -> str:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}")
        total = format_minor(order.total.minor, order.currency)
        if channel == "sms":
            return f"SMS {order.order_id}: {total}"
        return f"Order {order.order_id} totals {total}"
