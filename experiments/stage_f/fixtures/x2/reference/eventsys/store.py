"""Processed set (v2): persisted, monotonic, atomically written."""

from __future__ import annotations

import json
import os
from pathlib import Path

from eventsys.errors import StoreError

VERSION = 1


class EventStore:
    def __init__(self, path) -> None:
        self.path = Path(path)

    def _read(self) -> tuple[list[str], list[str]]:
        if not self.path.exists():
            return [], []
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"cannot read the processed set: {error}") from error
        if not isinstance(document, dict) or document.get("version") != VERSION:
            raise StoreError("unsupported processed-set document")
        processed = document.get("processed")
        dead = document.get("dead")
        if not isinstance(processed, list) or not isinstance(dead, list):
            raise StoreError("processed/dead must be lists")
        for values in (processed, dead):
            if any(not isinstance(value, str) or not value for value in values):
                raise StoreError("processed/dead ids must be non-empty strings")
        return list(processed), list(dead)

    def _write(self, processed: list[str], dead: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": VERSION,
            "processed": sorted(set(processed)),
            "dead": sorted(set(dead)),
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            raise StoreError(f"cannot write the processed set: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)

    def is_processed(self, event_id: str) -> bool:
        processed, _ = self._read()
        return event_id in processed

    def mark_processed(self, event_id: str) -> bool:
        processed, dead = self._read()
        if event_id in processed:
            return False
        self._write([*processed, event_id], dead)
        return True

    def dead_letter(self, event_id: str) -> None:
        processed, dead = self._read()
        if event_id not in dead:
            self._write(processed, [*dead, event_id])

    def is_dead(self, event_id: str) -> bool:
        _, dead = self._read()
        return event_id in dead

    def processed_ids(self) -> list[str]:
        return sorted(set(self._read()[0]))

    def dead_ids(self) -> list[str]:
        return sorted(set(self._read()[1]))
