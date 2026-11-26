"""Bounded broker (v2): reject when full, track in-flight, journal every transition."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from eventsys.errors import DuplicateEvent, QueueFull
from eventsys.model import Envelope, Event


class Broker:
    def __init__(self, capacity: int, journal=None) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self.journal = journal
        self._items: deque[Envelope] = deque()
        self._in_flight: dict[str, Envelope] = {}
        self._acked: list[str] = []
        self._dead: list[str] = []

    def _queued_ids(self) -> Iterable[str]:
        return (envelope.event.id for envelope in self._items)

    def _known(self, event_id: str) -> bool:
        return (
            event_id in self._in_flight
            or event_id in self._acked
            or event_id in self._dead
            or event_id in set(self._queued_ids())
        )

    def publish(self, event: Event) -> None:
        if not isinstance(event, Event):
            raise ValueError("publish needs an Event")
        if self._known(event.id):
            raise DuplicateEvent(f"event {event.id!r} is already known")
        if len(self._items) >= self.capacity:
            raise QueueFull(f"broker is full ({self.capacity})")
        self._items.append(Envelope(event))

    def take(self) -> Envelope | None:
        if not self._items:
            return None
        envelope = self._items.popleft()
        self._in_flight[envelope.event.id] = envelope
        if self.journal is not None:
            self.journal.record_take(envelope)
        return envelope

    def ack(self, envelope: Envelope) -> None:
        event_id = envelope.event.id
        self._in_flight.pop(event_id, None)
        if event_id not in self._acked:
            self._acked.append(event_id)
            if self.journal is not None:
                self.journal.record_ack(event_id)

    def nack(self, envelope: Envelope) -> None:
        event_id = envelope.event.id
        self._in_flight.pop(event_id, None)
        retried = envelope.next_attempt()
        self._items.appendleft(retried)
        if self.journal is not None:
            self.journal.record_take(retried)

    def dead_letter(self, envelope: Envelope) -> None:
        event_id = envelope.event.id
        self._in_flight.pop(event_id, None)
        if event_id not in self._dead:
            self._dead.append(event_id)
            if self.journal is not None:
                self.journal.record_dead(event_id)

    def requeue(self, envelope: Envelope) -> None:
        self._items.appendleft(envelope)

    def pending(self) -> int:
        return len(self._items)

    def in_flight_ids(self) -> list[str]:
        return sorted(self._in_flight)

    def acked_ids(self) -> list[str]:
        return sorted(self._acked)

    def dead_letter_ids(self) -> list[str]:
        return sorted(self._dead)

    def stats(self) -> dict[str, int]:
        return {
            "pending": len(self._items),
            "in_flight": len(self._in_flight),
            "acked": len(self._acked),
            "dead": len(self._dead),
        }
