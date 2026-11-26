"""Job payload specs (v2): each kind declares the keys its handler needs."""

from __future__ import annotations

from schedkit.errors import InvalidJobPayload, UnknownJobKind
from schedkit.model import Job

JOB_SPECS: dict[str, tuple[str, ...]] = {
    "task": ("n",),
    "email": ("to", "subject"),
    "report": ("report_id",),
    "maintenance": (),
}


def kinds() -> list[str]:
    return sorted(JOB_SPECS)


def validate(job: Job) -> None:
    if job.kind not in JOB_SPECS:
        raise UnknownJobKind(f"unknown job kind {job.kind!r}")
    missing = [key for key in JOB_SPECS[job.kind] if key not in job.payload]
    if missing:
        raise InvalidJobPayload(f"{job.kind}: missing payload keys {missing}")
