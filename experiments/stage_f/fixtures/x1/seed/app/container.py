"""Container wiring (unchanged by the migration: it already injects the frozen API)."""

from __future__ import annotations

from notifications.service import Notifier
from notifications.templates import Templates
from foundation.audit import record


def build_notifier() -> Notifier:
    return Notifier(sink=record, templates=Templates())
