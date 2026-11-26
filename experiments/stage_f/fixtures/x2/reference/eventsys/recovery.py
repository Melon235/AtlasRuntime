"""Restart recovery (v2): re-queue journal deliveries that were never closed."""

from __future__ import annotations


def recover(broker, journal, store) -> int:
    ready = []
    for envelope in journal.pending_deliveries():
        event_id = envelope.event.id
        if store.is_processed(event_id) or store.is_dead(event_id):
            continue
        if (
            event_id in broker.acked_ids()
            or event_id in broker.dead_letter_ids()
            or event_id in broker.in_flight_ids()
        ):
            continue
        ready.append(envelope)
    for envelope in reversed(ready):
        broker.requeue(envelope)
    return len(ready)
