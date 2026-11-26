"""Job payload specs (v1): the table exists but nothing validates against it."""

from __future__ import annotations

JOB_SPECS: dict[str, tuple[str, ...]] = {
    "task": ("n",),
    "email": ("to", "subject"),
    "report": ("report_id",),
    "maintenance": (),
}


def kinds() -> list[str]:
    return sorted(JOB_SPECS)


def validate(job) -> None:
    return None
