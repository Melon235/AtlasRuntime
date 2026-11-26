"""Lifecycle event log (v2): append-only JSONL, one record per transition."""

from __future__ import annotations

import json
from pathlib import Path

from schedkit.errors import EventLogError

FIELDS = ("job_id", "event", "state", "attempts", "at")


class EventLog:
    def __init__(self, path=None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: list[dict] = []

    def record(self, record, event: str, at: float) -> None:
        entry = {
            "job_id": record.job.id,
            "event": event,
            "state": record.state,
            "attempts": record.attempts,
            "at": float(at),
        }
        if self.path is None:
            self._entries.append(entry)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
        except OSError as error:
            raise EventLogError(f"cannot append to the event log: {error}") from error

    def entries(self) -> list[dict]:
        if self.path is None:
            return [dict(entry) for entry in self._entries]
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise EventLogError(f"cannot read the event log: {error}") from error
        entries: list[dict] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise EventLogError(f"event line {number} is not json: {error}") from error
            if not isinstance(entry, dict) or any(field not in entry for field in FIELDS):
                raise EventLogError(f"event line {number} is malformed")
            if not isinstance(entry["job_id"], str) or not isinstance(entry["event"], str):
                raise EventLogError(f"event line {number} has a non-string id/event")
            if isinstance(entry["attempts"], bool) or not isinstance(entry["attempts"], int):
                raise EventLogError(f"event line {number} has a non-integer attempt count")
            entries.append(entry)
        return entries

    def for_job(self, job_id: str) -> list[dict]:
        return [entry for entry in self.entries() if entry["job_id"] == job_id]


__all__ = ["FIELDS", "EventLog"]
