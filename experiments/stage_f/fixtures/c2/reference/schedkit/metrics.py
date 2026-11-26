"""Scheduler metrics (v2): per-state counts, attempts and success rate."""

from __future__ import annotations

from collections.abc import Iterable

from schedkit.model import STATES, TERMINAL, JobRecord


def summarize(records: Iterable[JobRecord]) -> dict:
    items = list(records)
    counts = {state: 0 for state in STATES}
    for record in items:
        counts[record.state] += 1
    attempts = [record.attempts for record in items]
    jobs = len(items)
    return {
        "jobs": jobs,
        **counts,
        "attempts": sum(attempts),
        "max_attempts": max(attempts, default=0),
        "retrying": counts["failed"],
        "terminal": sum(counts[state] for state in TERMINAL),
        "success_rate": (counts["done"] / jobs) if jobs else 0.0,
    }


def attempt_histogram(records: Iterable[JobRecord]) -> dict[int, int]:
    histogram: dict[int, int] = {}
    for record in records:
        histogram[record.attempts] = histogram.get(record.attempts, 0) + 1
    return dict(sorted(histogram.items()))


__all__ = ["attempt_histogram", "summarize"]
