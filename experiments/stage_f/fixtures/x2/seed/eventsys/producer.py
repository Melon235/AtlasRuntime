"""Producer (unchanged by the repair)."""

from __future__ import annotations

from eventsys.broker import Broker
from eventsys.clock import Clock
from eventsys.model import Event


class Producer:
    def __init__(self, broker: Broker, clock: Clock) -> None:
        self._broker = broker
        self._clock = clock
        self._sequence = 0

    def emit(self, kind: str, payload: dict) -> Event:
        self._sequence += 1
        event = Event(
            id=f"e-{self._sequence}",
            kind=kind,
            payload=dict(payload),
            created_at=self._clock.now(),
        )
        self._broker.publish(event)
        return event
