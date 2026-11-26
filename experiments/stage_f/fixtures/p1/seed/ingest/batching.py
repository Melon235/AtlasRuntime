"""Batch building (v1).

v1 counts records only. The byte ceiling is accepted for signature compatibility and then ignored.
"""

from __future__ import annotations

from collections.abc import Iterable

from ingest.model import Batch, Record


def record_size(record: Record) -> int:
    """v1 reports the size of the raw values only."""

    return len(repr(record.values).encode("utf-8"))


def build_batches(
    records: Iterable[Record],
    max_records: int = 100,
    max_bytes: int = 4096,
) -> list[Batch]:
    """Group ``records`` into contiguous batches."""

    ordered = list(records)
    if max_records < 1:
        raise ValueError("max_records must be positive")
    batches: list[Batch] = []
    for begin in range(0, len(ordered), max_records):
        chunk = tuple(ordered[begin : begin + max_records])
        batches.append(Batch(index=len(batches), records=chunk))
    return batches
