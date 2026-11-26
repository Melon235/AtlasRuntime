"""Ingest pipeline (v2): resume, both batch ceilings, typed totals, checkpoint on success."""

from __future__ import annotations

from typing import Any

from ingest.batching import build_batches
from ingest.model import Record, RunResult, Schema
from ingest.registry import build


def _totals(schema: Schema, records: tuple[Record, ...]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for name in schema.numeric_names:
        total: float = 0
        for record in records:
            value = record.values.get(name)
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                total += value
        totals[name] = total
    return totals


def _resolve_start(source: str, start: int | None, store: Any) -> int:
    if start is not None:
        if start < 0:
            raise ValueError("start must not be negative")
        return start
    if store is not None:
        return store.load(source)
    return 0


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
    """Run one ingest pass over ``raw``."""

    connector = build(source)
    first = _resolve_start(source, start, store)
    read = connector(raw, schema, start=first, mapping=mapping)
    batches = build_batches(read.records, max_records=max_records, max_bytes=max_bytes)
    result = RunResult(
        source=source,
        records=read.records,
        batches=tuple(batches),
        cursor=read.cursor,
        skipped=read.skipped,
        totals=_totals(schema, read.records),
    )
    if store is not None:
        store.save(source, result.cursor)
    return result
