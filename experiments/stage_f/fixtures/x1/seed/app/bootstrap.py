"""Service bootstrap (unchanged by the migration)."""

from __future__ import annotations

from app.container import build_notifier
from events.bus import EventBus
from events.subscribers import register


def bootstrap(bus: EventBus | None = None) -> dict:
    active = bus or EventBus()
    register(active)
    return {"bus": active, "notifier": build_notifier()}
