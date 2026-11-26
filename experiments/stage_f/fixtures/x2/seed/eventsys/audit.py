"""Run audit (v1): in memory only."""

from __future__ import annotations


class AuditLog:
    def __init__(self, path=None) -> None:
        self.path = path
        self._entries: list[dict] = []

    def append(self, entry: dict) -> None:
        self._entries.append(dict(entry))

    def entries(self) -> list[dict]:
        return [dict(entry) for entry in self._entries]

    def last(self) -> dict | None:
        return dict(self._entries[-1]) if self._entries else None
