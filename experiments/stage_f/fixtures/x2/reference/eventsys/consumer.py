"""Consumer (v2): process, persist, ack; retry with capped backoff, then dead-letter."""

from __future__ import annotations

from eventsys.broker import Broker
from eventsys.clock import Clock
from eventsys.model import Envelope
from eventsys.retry import RetryPolicy, delay, should_retry
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
        if not callable(handler):
            raise ValueError("handler must be callable")
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
        try:
            self.handler(event)
        except Exception:
            failures = envelope.attempts + 1
            if should_retry(failures, self.policy):
                self.clock.advance(delay(failures, self.policy))
                self.broker.nack(envelope)
                return "retried"
            self.store.dead_letter(event.id)
            self.broker.dead_letter(envelope)
            return "dead"
        self.store.mark_processed(event.id)
        self.broker.ack(envelope)
        return "processed"

    def drain(self, limit: int = 1000) -> dict[str, int]:
        if limit < 1:
            raise ValueError("limit must be positive")
        counts = {"processed": 0, "duplicate": 0, "retried": 0, "dead": 0}
        for _ in range(limit):
            envelope = self.broker.take()
            if envelope is None:
                break
            counts[self.deliver(envelope)] += 1
        return counts
