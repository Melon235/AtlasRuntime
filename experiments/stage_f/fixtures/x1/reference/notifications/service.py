"""Notifier (v2 call site through an injected dependency).

The sink is the frozen ``foundation.audit.record``, so the injected call has to use the v2 keyword
protocol. This module never imports the audit API itself.
"""

from __future__ import annotations

from notifications.templates import CHANNELS, Templates
from foundation.audit import Receipt
from foundation.models import Order


class Notifier:
    def __init__(self, sink, templates: Templates | None = None) -> None:
        if not callable(sink):
            raise ValueError("sink must be callable")
        self._sink = sink
        self._templates = templates or Templates()

    def send(self, order: Order, *, actor: str, channel: str = "email") -> Receipt:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}")
        text = self._templates.render(order, channel)
        return self._sink(
            kind="notification",
            data={"order_id": order.order_id, "channel": channel, "length": len(text)},
            actor=actor,
        )
