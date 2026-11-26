"""Pipeline wiring (v2): one audit record per drain and a derived metrics snapshot."""

from __future__ import annotations

from eventsys import metrics
from eventsys.audit import AuditLog
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
        self.store = EventStore(store_path)
        self.journal = Journal(journal_path)
        self.audit = AuditLog(audit_path)
        self.broker = Broker(capacity, journal=self.journal)
        self.producer = Producer(self.broker, self.clock)
        self.consumer = Consumer(handler, self.store, self.policy, self.clock, self.broker)

    def submit(self, kind: str, payload: dict) -> Event:
        return self.producer.emit(kind, payload)

    def run_once(self, limit: int = 1000) -> dict[str, int]:
        counts = self.consumer.drain(limit)
        self.audit.append(
            {
                "counts": counts,
                "broker": self.broker.stats(),
                "at": self.clock.now(),
            }
        )
        return counts

    def recover(self) -> int:
        return recover(self.broker, self.journal, self.store)

    def metrics(self) -> dict:
        return metrics.snapshot(self.broker, self.store)

    def stats(self) -> dict[str, int]:
        stats = self.broker.stats()
        stats["processed"] = len(self.store.processed_ids())
        stats["dead_letters"] = len(self.store.dead_ids())
        return stats
