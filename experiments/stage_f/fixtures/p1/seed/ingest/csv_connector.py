"""CSV connector (v1).

v1 splits every row on commas, trusts the header to be a plain list and stores the raw text. It has
no quoting, no validation and no type conversion yet.
"""

from __future__ import annotations

from ingest.model import ConnectorResult, Record, Schema

NAME = "csv"


def _header(raw: str) -> list[str]:
    for line in raw.splitlines():
        if line.strip():
            return [cell.strip().lower() for cell in line.split(",")]
    return []


def read(
    raw: str,
    schema: Schema,
    *,
    start: int = 0,
    mapping: dict[str, str] | None = None,
) -> ConnectorResult:
    """Read ``raw`` as CSV rows."""

    header = _header(raw)
    records: list[Record] = []
    seen_rows = 0
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        if number == 1:
            continue
        if seen_rows < start:
            seen_rows += 1
            continue
        cells = line.split(",")
        values = {}
        for index, name in enumerate(header):
            if index < len(cells):
                values[name] = cells[index].strip()
        records.append(Record(source=NAME, line=number, values=values))
        seen_rows += 1
    return ConnectorResult(records=tuple(records), cursor=seen_rows, skipped=0)
