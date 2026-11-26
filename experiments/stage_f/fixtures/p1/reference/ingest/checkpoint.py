"""Checkpoint storage (v2).

One JSON document per store, monotonic per source, written with a temporary file and ``os.replace``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import CheckpointError

VERSION = 1


class CheckpointStore:
    """A file-backed cursor table."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    # -- document handling -------------------------------------------------------------------

    def _read(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise CheckpointError(f"cannot read checkpoint {self.path}: {error}") from error
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise CheckpointError(f"checkpoint is not valid json: {error}") from error
        if not isinstance(document, dict):
            raise CheckpointError("checkpoint root is not an object")
        if document.get("version") != VERSION:
            raise CheckpointError(f"unsupported checkpoint version {document.get('version')!r}")
        cursors = document.get("cursors")
        if not isinstance(cursors, dict):
            raise CheckpointError("checkpoint cursors is not an object")
        parsed: dict[str, int] = {}
        for source, cursor in cursors.items():
            if not isinstance(source, str) or not source:
                raise CheckpointError("checkpoint source is not a non-empty string")
            if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
                raise CheckpointError(f"checkpoint cursor for {source!r} is not an integer")
            parsed[source] = cursor
        return parsed

    def _write(self, cursors: dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": VERSION, "cursors": dict(sorted(cursors.items()))}
        text = json.dumps(document, sort_keys=True) + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            raise CheckpointError(f"cannot write checkpoint {self.path}: {error}") from error
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    # -- public surface ----------------------------------------------------------------------

    def load(self, source: str) -> int:
        return self._read().get(source, 0)

    def save(self, source: str, cursor: int) -> None:
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            raise CheckpointError("cursor must be an integer")
        if cursor < 0:
            raise CheckpointError("cursor must not be negative")
        cursors = self._read()
        previous = cursors.get(source)
        if previous is not None and cursor < previous:
            raise CheckpointError(
                f"cursor for {source!r} would move backwards from {previous} to {cursor}"
            )
        cursors[source] = cursor
        self._write(cursors)

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self._read().items()))
