"""Scheduler metrics (v1): only a job count."""

from __future__ import annotations


def summarize(records) -> dict:
    return {"jobs": len(list(records))}


def attempt_histogram(records) -> dict[int, int]:
    return {}
