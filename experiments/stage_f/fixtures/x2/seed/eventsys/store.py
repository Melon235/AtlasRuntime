"""Processed set (v1): in memory only, and a message is recorded as processed before it is handled."""

from __future__ import annotations

from eventsys.errors import StoreError


class EventStore:
    def __init__(self, path) -> None:
        self.path = path
        self._processed: list[str] = []
        self._dead: list[str] = []

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str) -> bool:
        if event_id in self._processed:
            return False
        self._processed.append(event_id)
        return True

    def dead_letter(self, event_id: str) -> None:
        if event_id not in self._dead:
            self._dead.append(event_id)

    def is_dead(self, event_id: str) -> bool:
        return event_id in self._dead

    def processed_ids(self) -> list[str]:
        return sorted(self._processed)

    def dead_ids(self) -> list[str]:
        return sorted(self._dead)
