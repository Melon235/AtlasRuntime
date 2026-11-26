"""Run audit (v2): append-only JSONL, one record per drain."""

from __future__ import annotations

import json
from pathlib import Path

from eventsys.errors import AuditError


class AuditLog:
    def __init__(self, path=None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: list[dict] = []

    def append(self, entry: dict) -> None:
        if not isinstance(entry, dict):
            raise AuditError("an audit entry must be a dict")
        try:
            line = json.dumps(entry, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise AuditError(f"the audit entry is not serialisable: {error}") from error
        if self.path is None:
            self._entries.append(json.loads(line))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError as error:
            raise AuditError(f"cannot append to the audit log: {error}") from error

    def entries(self) -> list[dict]:
        if self.path is None:
            return [dict(entry) for entry in self._entries]
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise AuditError(f"cannot read the audit log: {error}") from error
        entries: list[dict] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise AuditError(f"audit line {number} is not json: {error}") from error
            if not isinstance(entry, dict):
                raise AuditError(f"audit line {number} is not an object")
            entries.append(entry)
        return entries

    def last(self) -> dict | None:
        entries = self.entries()
        return entries[-1] if entries else None


__all__ = ["AuditLog"]
