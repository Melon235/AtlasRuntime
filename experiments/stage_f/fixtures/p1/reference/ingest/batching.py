"""Batch building (v2): count and byte ceilings, contiguous, no split record."""

from __future__ import annotations

import json
from collections.abc import Iterable

from ingest.model import Batch, Record


def record_size(record: Record) -> int:
    """The canonical size of one record, in UTF-8 bytes."""

    payload = {
        "source": record.source,
        "line": record.line,
        "values": record.values,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return len(text.encode("utf-8"))


def build_batches(
    records: Iterable[Record],
    max_records: int = 100,
    max_bytes: int = 4096,
) -> list[Batch]:
    """Group ``records`` into contiguous batches under both ceilings."""

    if max_records < 1:
        raise ValueError("max_records must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    batches: list[Batch] = []
    current: list[Record] = []
    current_bytes = 0
    for record in records:
        size = record_size(record)
        over_count = len(current) >= max_records
        over_bytes = bool(current) and current_bytes + size > max_bytes
        if over_count or over_bytes:
            batches.append(
                Batch(index=len(batches), records=tuple(current), size_bytes=current_bytes)
            )
            current = []
            current_bytes = 0
        current.append(record)
        current_bytes += size
        if size > max_bytes:
            batches.append(
                Batch(index=len(batches), records=tuple(current), size_bytes=current_bytes)
            )
            current = []
            current_bytes = 0
    if current:
        batches.append(Batch(index=len(batches), records=tuple(current), size_bytes=current_bytes))
    return batches
