"""CSV connector (v2).

The contract is ``docs/INTERFACE.md``: header projection, quoted cells, per-row validation, strict
conversion, a data-row cursor and a real skip count.
"""

from __future__ import annotations

from ingest.columns import header_index, split_cells
from ingest.errors import SchemaError
from ingest.model import ConnectorResult, Record, Schema
from ingest.values import convert

NAME = "csv"


def _find_header(lines: list[str]) -> tuple[int, int]:
    """Return ``(header_index, skipped_before_header)``."""

    skipped = 0
    for index, line in enumerate(lines):
        if line.strip():
            return index, skipped
        skipped += 1
    raise SchemaError("csv input has no header")


def _row_values(
    cells: list[str],
    columns: dict[str, int],
    schema: Schema,
    line_number: int,
) -> dict[str, object]:
    if len(cells) != len(columns):
        raise SchemaError(f"line {line_number}: expected {len(columns)} cells, got {len(cells)}")
    values: dict[str, object] = {}
    for spec in schema.fields:
        cell = cells[columns[spec.name]].strip()
        if not cell:
            if spec.required:
                raise SchemaError(f"line {line_number}: missing required field {spec.name!r}")
            values[spec.name] = spec.default
            continue
        values[spec.name] = convert(spec.kind, cell, where=f"line {line_number} field {spec.name}")
    return values


def read(
    raw: str,
    schema: Schema,
    *,
    start: int = 0,
    mapping: dict[str, str] | None = None,
) -> ConnectorResult:
    """Read ``raw`` as CSV rows for ``schema``."""

    if start < 0:
        raise ValueError("start must not be negative")
    lines = raw.splitlines()
    header_at, skipped = _find_header(lines)
    header_cells = split_cells(lines[header_at])
    columns = header_index(header_cells, schema)

    records: list[Record] = []
    data_index = 0
    for offset in range(header_at + 1, len(lines)):
        line = lines[offset]
        if not line.strip():
            skipped += 1
            continue
        if data_index < start:
            data_index += 1
            continue
        cells = split_cells(line)
        values = _row_values(cells, columns, schema, offset + 1)
        records.append(Record(source=NAME, line=offset + 1, values=values))
        data_index += 1
    return ConnectorResult(records=tuple(records), cursor=start + len(records), skipped=skipped)
