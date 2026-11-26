"""Bounded broker (v1).

v1 drops the oldest queued message when the queue is full, does not track in-flight deliveries and
records an ack more than once.
"""

from __future__ import annotations

from eventsys.errors import DuplicateEvent, QueueFull
from eventsys.model import Envelope, Event


class Broker:
    def __init__(self, capacity: int, journal=None) -> None:
        self.capacity = capacity
        self.journal = journal
        self._items: list[Envelope] = []
        self._acked: list[str] = []
        self._dead: list[str] = []

    def publish(self, event: Event) -> None:
        if len(self._items) >= self.capacity:
            self._items.pop(0)
        self._items.append(Envelope(event))

    def take(self) -> Envelope | None:
        if not self._items:
            return None
        return self._items.pop(0)

    def ack(self, envelope: Envelope) -> None:
        self._acked.append(envelope.event.id)

    def nack(self, envelope: Envelope) -> None:
        self._items.append(envelope)

    def requeue(self, envelope: Envelope) -> None:
        self._items.insert(0, envelope)

    def dead_letter(self, envelope: Envelope) -> None:
        self._dead.append(envelope.event.id)

    def pending(self) -> int:
        return len(self._items)

    def in_flight_ids(self) -> list[str]:
        return []

    def acked_ids(self) -> list[str]:
        return list(self._acked)

    def dead_letter_ids(self) -> list[str]:
        return list(self._dead)

    def stats(self) -> dict[str, int]:
        return {
            "pending": len(self._items),
            "in_flight": 0,
            "acked": len(self._acked),
            "dead": len(self._dead),
        }
