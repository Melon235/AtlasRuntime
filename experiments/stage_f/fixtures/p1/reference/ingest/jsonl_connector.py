"""JSONL connector (v2).

The contract is ``docs/INTERFACE.md``: one object per line, a dotted field mapping, strict conversion
and a line cursor.
"""

from __future__ import annotations

import json

from ingest.errors import SchemaError
from ingest.model import ConnectorResult, Record, Schema
from ingest.paths import resolve
from ingest.values import convert

NAME = "jsonl"


def _payload(line: str, line_number: int) -> dict:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise SchemaError(f"line {line_number}: invalid json: {error}") from error
    if not isinstance(payload, dict):
        raise SchemaError(f"line {line_number}: json root is not an object")
    return payload


def _row_values(
    payload: dict,
    schema: Schema,
    mapping: dict[str, str],
    line_number: int,
) -> dict[str, object]:
    values: dict[str, object] = {}
    for spec in schema.fields:
        path = mapping.get(spec.name, spec.name)
        found, raw_value = resolve(payload, path)
        if not found or raw_value is None:
            if spec.required:
                raise SchemaError(f"line {line_number}: missing required field {spec.name!r}")
            values[spec.name] = spec.default
            continue
        values[spec.name] = convert(
            spec.kind, raw_value, where=f"line {line_number} field {spec.name}"
        )
    return values


def read(
    raw: str,
    schema: Schema,
    *,
    start: int = 0,
    mapping: dict[str, str] | None = None,
) -> ConnectorResult:
    """Read ``raw`` as newline-delimited JSON for ``schema``."""

    if start < 0:
        raise ValueError("start must not be negative")
    paths = dict(mapping or {})
    unknown = sorted(set(paths) - set(schema.names))
    if unknown:
        raise SchemaError(f"mapping names unknown fields: {unknown}")

    records: list[Record] = []
    skipped = 0
    data_index = 0
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            skipped += 1
            continue
        if data_index < start:
            data_index += 1
            continue
        payload = _payload(line, number)
        values = _row_values(payload, schema, paths, number)
        records.append(Record(source=NAME, line=number, values=values))
        data_index += 1
    return ConnectorResult(records=tuple(records), cursor=start + len(records), skipped=skipped)
