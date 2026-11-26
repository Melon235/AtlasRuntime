"""Pipeline wiring (v1)."""

from __future__ import annotations

from eventsys.broker import Broker
from eventsys.clock import Clock
from eventsys.consumer import Consumer
from eventsys.journal import Journal
from eventsys.model import Event
from eventsys.producer import Producer
from eventsys.recovery import recover
from eventsys.retry import RetryPolicy
from eventsys.store import EventStore


class Pipeline:
    def __init__(
        self,
        handler,
        *,
        capacity: int,
        store_path,
        journal_path,
        clock: Clock | None = None,
        policy: RetryPolicy | None = None,
        audit_path=None,
    ) -> None:
        self.clock = clock or Clock()
        self.policy = policy or RetryPolicy()
        self.audit_path = audit_path
        self.store = EventStore(store_path)
        self.journal = Journal(journal_path)
        self.broker = Broker(capacity, journal=self.journal)
        self.producer = Producer(self.broker, self.clock)
        self.consumer = Consumer(handler, self.store, self.policy, self.clock, self.broker)

    def submit(self, kind: str, payload: dict) -> Event:
        return self.producer.emit(kind, payload)

    def run_once(self, limit: int = 1000) -> dict[str, int]:
        return self.consumer.drain(limit)

    def recover(self) -> int:
        return recover(self.broker, self.journal, self.store)

    def metrics(self) -> dict:
        return {"published": 0}

    def stats(self) -> dict[str, int]:
        stats = self.broker.stats()
        stats["processed"] = len(self.store.processed_ids())
        stats["dead_letters"] = len(self.store.dead_ids())
        return stats
