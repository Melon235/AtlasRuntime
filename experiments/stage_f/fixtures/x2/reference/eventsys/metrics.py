"""Pipeline metrics (v2): derived from the broker and the processed set."""

from __future__ import annotations


def snapshot(broker, store) -> dict:
    acked = len(broker.acked_ids())
    dead = len(broker.dead_letter_ids())
    pending = broker.pending()
    in_flight = len(broker.in_flight_ids())
    published = acked + dead + pending + in_flight
    processed = len(store.processed_ids())
    return {
        "published": published,
        "acked": acked,
        "dead": dead,
        "pending": pending,
        "in_flight": in_flight,
        "processed": processed,
        "dead_letters": len(store.dead_ids()),
        "delivery_ratio": (acked / published) if published else 0.0,
    }


__all__ = ["snapshot"]
