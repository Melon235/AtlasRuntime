"""Restart recovery (v1): does nothing."""

from __future__ import annotations


def recover(broker, journal, store) -> int:
    return 0
