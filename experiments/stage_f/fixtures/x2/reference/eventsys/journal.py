"""Delivery journal (v2): append-only JSONL with restart reconstruction."""

from __future__ import annotations

import json
from pathlib import Path

from eventsys.errors import JournalError
from eventsys.model import Envelope, Event


class Journal:
    def __init__(self, path) -> None:
        self.path = Path(path)

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
        except OSError as error:
            raise JournalError(f"cannot append to the journal: {error}") from error

    def record_take(self, envelope: Envelope) -> None:
        self._append(
            {
                "op": "take",
                "event": envelope.event.as_dict(),
                "attempts": envelope.attempts,
            }
        )

    def record_ack(self, event_id: str) -> None:
        self._append({"op": "ack", "id": event_id})

    def record_dead(self, event_id: str) -> None:
        self._append({"op": "dead", "id": event_id})

    def _entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise JournalError(f"cannot read the journal: {error}") from error
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise JournalError(f"journal line {number} is not json: {error}") from error
            if not isinstance(entry, dict) or entry.get("op") not in ("take", "ack", "dead"):
                raise JournalError(f"journal line {number} has an unknown operation")
            entries.append(entry)
        return entries

    def pending_deliveries(self) -> list[Envelope]:
        taken: dict[str, Envelope] = {}
        closed: set[str] = set()
        for entry in self._entries():
            operation = entry["op"]
            if operation == "take":
                try:
                    event = Event.from_dict(entry["event"])
                    attempts = entry["attempts"]
                except (KeyError, TypeError, ValueError) as error:
                    raise JournalError(f"malformed take record: {error}") from error
                if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                    raise JournalError("take record has an invalid attempt count")
                taken[event.id] = Envelope(event=event, attempts=attempts)
            else:
                event_id = entry.get("id")
                if not isinstance(event_id, str) or not event_id:
                    raise JournalError("close record has no id")
                closed.add(event_id)
        return [envelope for event_id, envelope in taken.items() if event_id not in closed]
