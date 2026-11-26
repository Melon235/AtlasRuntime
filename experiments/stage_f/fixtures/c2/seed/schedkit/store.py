"""Job store (v1): in memory only; the path is accepted and ignored."""

from __future__ import annotations

from schedkit.errors import DuplicateJob, UnknownJob
from schedkit.model import JobRecord


class JobStore:
    def __init__(self, path=None) -> None:
        self.path = path
        self._records: dict[str, JobRecord] = {}

    def add(self, record: JobRecord) -> None:
        if record.job.id in self._records:
            raise DuplicateJob(f"job {record.job.id!r} already exists")
        self._records[record.job.id] = record

    def get(self, job_id: str) -> JobRecord:
        if job_id not in self._records:
            raise UnknownJob(job_id)
        return self._records[job_id]

    def update(self, record: JobRecord) -> None:
        if record.job.id not in self._records:
            raise UnknownJob(record.job.id)
        self._records[record.job.id] = record

    def records(self) -> list[JobRecord]:
        return [self._records[job_id] for job_id in sorted(self._records)]

    def snapshot(self) -> dict[str, JobRecord]:
        return dict(self._records)
