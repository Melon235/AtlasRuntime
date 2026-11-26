"""Delivery journal (v1): keeps entries in memory and cannot reconstruct a restart."""

from __future__ import annotations

from eventsys.errors import JournalError
from eventsys.model import Envelope


class Journal:
    def __init__(self, path) -> None:
        self.path = path
        self._entries: list[tuple] = []

    def record_take(self, envelope: Envelope) -> None:
        self._entries.append(("take", envelope.event.id, envelope.attempts))

    def record_ack(self, event_id: str) -> None:
        self._entries.append(("ack", event_id))

    def record_dead(self, event_id: str) -> None:
        self._entries.append(("dead", event_id))

    def pending_deliveries(self) -> list[Envelope]:
        return []
