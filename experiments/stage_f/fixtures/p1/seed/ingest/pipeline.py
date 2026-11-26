"""Ingest pipeline (v1).

v1 reads, batches and sums. It does not resume from a checkpoint, it ignores the byte ceiling and it
computes totals with ``int()`` so a float field loses precision.
"""

from __future__ import annotations

from typing import Any

from ingest.batching import build_batches
from ingest.model import Record, RunResult, Schema
from ingest.registry import build


def _totals(schema: Schema, records: tuple[Record, ...]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for name in schema.numeric_names:
        total = 0
        for record in records:
            value = record.values.get(name)
            if value is None:
                continue
            total += int(value)
        totals[name] = total
    return totals


def run(
    source: str,
    raw: str,
    schema: Schema,
    *,
    start: int | None = None,
    store: Any = None,
    max_records: int = 100,
    max_bytes: int = 4096,
    mapping: dict[str, str] | None = None,
) -> RunResult:
    """Run one ingest pass."""

    connector = build(source)
    first = 0 if start is None else start
    read = connector(raw, schema, start=first, mapping=mapping)
    batches = build_batches(read.records, max_records=max_records, max_bytes=max_bytes)
    totals = _totals(schema, read.records)
    if store is not None:
        store.save(source, read.cursor)
    return RunResult(
        source=source,
        records=read.records,
        batches=tuple(batches),
        cursor=read.cursor,
        skipped=read.skipped,
        totals=totals,
    )
