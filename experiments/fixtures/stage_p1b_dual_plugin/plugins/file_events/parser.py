"""Parsing for the file channel: a JSON Lines document, one record per non-blank line.

The parser returns the channel's raw values. It applies the structural rules from ``schema`` and
nothing else: no default level, no timestamp conversion, no tag splitting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .errors import FileEventError
from .schema import EVENT_ID_PATTERN, KIND_PATTERN, FileEventRecord, validate_fields


def parse_line(line: str, *, line_number: int | None = None) -> FileEventRecord:
    """Parse one line. A blank line is not an error; the caller decides to skip it."""

    text = line.strip()
    if not text:
        raise FileEventError("bad_json", "blank line", line_number=line_number)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise FileEventError("bad_json", str(error), line_number=line_number) from error
    if not isinstance(raw, Mapping):
        raise FileEventError("wrong_type", "a line must be a JSON object", line_number=line_number)

    validate_fields(raw, line_number=line_number)
    if not EVENT_ID_PATTERN.match(raw["event_id"]):
        raise FileEventError(
            "bad_event_id",
            "event_id must look like ev-<8 hex digits>",
            line_number=line_number,
        )
    if not KIND_PATTERN.match(raw["kind"]):
        raise FileEventError(
            "bad_kind",
            "kind must be a lowercase dotted identifier",
            line_number=line_number,
        )

    attrs: dict[str, str] = dict(raw.get("attrs") or {})
    return FileEventRecord(
        event_id=raw["event_id"],
        ts=raw["ts"],
        kind=raw["kind"],
        level=raw.get("level"),
        tags=raw.get("tags"),
        attrs=attrs,
    )


def parse_document(text: str) -> tuple[FileEventRecord, ...]:
    """Parse a whole document. The first invalid line stops the parse and names its position."""

    records: list[FileEventRecord] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        records.append(parse_line(line, line_number=index))
    return tuple(records)


def parse_path(path: Path) -> tuple[FileEventRecord, ...]:
    """Read and parse a ``.jsonl`` file."""

    return parse_document(Path(path).read_text(encoding="utf-8"))


__all__ = ["parse_document", "parse_line", "parse_path"]
