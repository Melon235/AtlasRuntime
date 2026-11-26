"""Notifier (v1 call site through an injected dependency).

The audit API is not imported here at all: the sink is passed in by the container, so the broken
call only shows up when the notifier actually sends.
"""

from __future__ import annotations

from notifications.templates import CHANNELS, Templates
from foundation.models import Order


class Notifier:
    def __init__(self, sink, templates: Templates | None = None) -> None:
        if not callable(sink):
            raise ValueError("sink must be callable")
        self._sink = sink
        self._templates = templates or Templates()

    def send(self, order: Order, *, actor: str, channel: str = "email") -> dict:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}")
        text = self._templates.render(order, channel)
        return self._sink(
            "notification",
            {"order_id": order.order_id, "channel": channel, "length": len(text)},
            actor,
        )
