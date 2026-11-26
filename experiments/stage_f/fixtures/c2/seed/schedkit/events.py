"""Lifecycle event log (v1): in-memory only."""

from __future__ import annotations


class EventLog:
    def __init__(self, path=None) -> None:
        self.path = path
        self._entries: list[dict] = []

    def record(self, record, event: str, at: float) -> None:
        self._entries.append(
            {
                "job_id": record.job.id,
                "event": event,
                "state": record.state,
                "attempts": record.attempts,
                "at": at,
            }
        )

    def entries(self) -> list[dict]:
        return list(self._entries)

    def for_job(self, job_id: str) -> list[dict]:
        return [entry for entry in self._entries if entry["job_id"] == job_id]
