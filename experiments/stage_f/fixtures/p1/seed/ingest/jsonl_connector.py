"""JSONL connector (v1).

v1 parses each line, copies the payload and ignores the mapping. There is no nested resolution and
no conversion.
"""

from __future__ import annotations

import json

from ingest.model import ConnectorResult, Record, Schema

NAME = "jsonl"


def read(
    raw: str,
    schema: Schema,
    *,
    start: int = 0,
    mapping: dict[str, str] | None = None,
) -> ConnectorResult:
    """Read ``raw`` as newline-delimited JSON."""

    records: list[Record] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        records.append(Record(source=NAME, line=number, values=dict(payload)))
    records = records[start:]
    return ConnectorResult(records=tuple(records), cursor=len(records), skipped=0)
