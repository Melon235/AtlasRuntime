"""Job store (v2): one atomically written JSON document."""

from __future__ import annotations

import json
import os
from pathlib import Path

from schedkit.errors import DuplicateJob, StoreError, UnknownJob
from schedkit.model import STATES, JobRecord

VERSION = 1


class JobStore:
    def __init__(self, path=None) -> None:
        self.path = Path(path) if path is not None else None
        self._records: dict[str, JobRecord] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"cannot read the job store: {error}") from error
        if not isinstance(document, dict) or document.get("version") != VERSION:
            raise StoreError("unsupported job store document")
        records = document.get("records")
        if not isinstance(records, list):
            raise StoreError("job store records must be a list")
        loaded: dict[str, JobRecord] = {}
        for item in records:
            try:
                record = JobRecord.from_dict(item)
            except (KeyError, TypeError, ValueError) as error:
                raise StoreError(f"malformed job record: {error}") from error
            if record.state not in STATES:
                raise StoreError(f"unknown state {record.state!r}")
            if record.job.id in loaded:
                raise StoreError(f"duplicate job id {record.job.id!r}")
            loaded[record.job.id] = record
        self._records = loaded

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": VERSION,
            "records": [self._records[job_id].as_dict() for job_id in sorted(self._records)],
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            raise StoreError(f"cannot write the job store: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)

    def add(self, record: JobRecord) -> None:
        if record.job.id in self._records:
            raise DuplicateJob(f"job {record.job.id!r} already exists")
        self._records[record.job.id] = record
        self._save()

    def get(self, job_id: str) -> JobRecord:
        if job_id not in self._records:
            raise UnknownJob(job_id)
        return self._records[job_id]

    def update(self, record: JobRecord) -> None:
        if record.job.id not in self._records:
            raise UnknownJob(record.job.id)
        self._records[record.job.id] = record
        self._save()

    def records(self) -> list[JobRecord]:
        return [self._records[job_id] for job_id in sorted(self._records)]

    def snapshot(self) -> dict[str, JobRecord]:
        return dict(self._records)
