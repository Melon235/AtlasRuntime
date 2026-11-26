"""Checkpoint storage (v1).

v1 keeps cursors in a dictionary. The constructor accepts a path so callers do not have to change
later; nothing is persisted yet.
"""

from __future__ import annotations

from .errors import CheckpointError


class CheckpointStore:
    """In-memory cursor table."""

    def __init__(self, path) -> None:
        self.path = path
        self._cursors: dict[str, int] = {}

    def load(self, source: str) -> int:
        return self._cursors.get(source, 0)

    def save(self, source: str, cursor: int) -> None:
        if cursor < 0:
            raise CheckpointError("cursor must not be negative")
        self._cursors[source] = cursor

    def snapshot(self) -> dict[str, int]:
        return dict(self._cursors)
