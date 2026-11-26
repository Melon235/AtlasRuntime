"""Consumer (v1).

v1 marks an id processed before the handler runs, never retries and dead-letters on the first
failure, so a transient failure is indistinguishable from a permanent one.
"""

from __future__ import annotations

from eventsys.broker import Broker
from eventsys.clock import Clock
from eventsys.model import Envelope
from eventsys.retry import RetryPolicy
from eventsys.store import EventStore


class Consumer:
    def __init__(
        self,
        handler,
        store: EventStore,
        policy: RetryPolicy,
        clock: Clock,
        broker: Broker,
    ) -> None:
        self.handler = handler
        self.store = store
        self.policy = policy
        self.clock = clock
        self.broker = broker

    def deliver(self, envelope: Envelope) -> str:
        event = envelope.event
        if self.store.is_processed(event.id):
            self.broker.ack(envelope)
            return "duplicate"
        self.store.mark_processed(event.id)
        try:
            self.handler(event)
        except Exception:
            self.store.dead_letter(event.id)
            self.broker.dead_letter(envelope)
            return "dead"
        self.broker.ack(envelope)
        return "processed"

    def drain(self, limit: int = 1000) -> dict[str, int]:
        counts = {"processed": 0, "duplicate": 0, "retried": 0, "dead": 0}
        for _ in range(limit):
            envelope = self.broker.take()
            if envelope is None:
                break
            counts[self.deliver(envelope)] += 1
        return counts
