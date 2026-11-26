"""CSV cell and header helpers (v2).

The tokenizer is the one place that knows how quoting works; the connector owns the record rules.
"""

from __future__ import annotations

from collections.abc import Sequence

from ingest.errors import SchemaError
from ingest.model import Schema


def split_cells(line: str) -> list[str]:
    """Split one CSV line at commas outside double quotes.

    A doubled quote inside a quoted cell is a literal quote. The returned cells are not trimmed;
    the caller decides what a blank cell means.
    """

    cells: list[str] = []
    buffer: list[str] = []
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == '"':
            if quoted and index + 1 < len(line) and line[index + 1] == '"':
                buffer.append('"')
                index += 2
                continue
            quoted = not quoted
            index += 1
            continue
        if char == "," and not quoted:
            cells.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    if quoted:
        raise SchemaError(f"unbalanced quote in {line!r}")
    cells.append("".join(buffer))
    return cells


def header_index(cells: Sequence[str], schema: Schema) -> dict[str, int]:
    """Map every schema field to its column index, rejecting an unusable header."""

    names: list[str] = []
    for cell in cells:
        name = cell.strip().lower()
        if not name:
            raise SchemaError("empty header cell")
        names.append(name)
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise SchemaError(f"duplicate header cells: {duplicates}")
    known = set(schema.names)
    unknown = sorted(set(names) - known)
    if unknown:
        raise SchemaError(f"unknown header cells: {unknown}")
    missing = [name for name in schema.names if name not in names]
    if missing:
        raise SchemaError(f"missing header cells: {missing}")
    return {name: names.index(name) for name in schema.names}
