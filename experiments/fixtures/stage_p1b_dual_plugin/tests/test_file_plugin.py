"""Native behaviour of the file channel.

These assertions are a regression contract: the package is vendored and its callers depend on every
value checked here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from plugins.file_events import (
    FileEventError,
    merge_attributes,
    parse_document,
    parse_line,
    parse_timestamp,
    severity_of,
    split_tags,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture(scope="module")
def document() -> str:
    return (SAMPLES / "events.jsonl").read_text(encoding="utf-8")


def test_the_document_decodes_in_document_order(document: str) -> None:
    records = parse_document(document)
    assert [record.event_id for record in records] == [
        "ev-1a2b3c4d",
        "ev-5e6f7a8b",
        "ev-9c0d1e2f",
        "ev-3a4b5c6d",
    ]


def test_native_records_keep_the_raw_values(document: str) -> None:
    records = parse_document(document)
    retry = records[1]
    assert retry.level == "warn"
    assert retry.tags == "CI, build,ci"
    assert retry.attrs == {"retries": "2"}
    assert retry.ts == "2026-11-19T02:05:09Z"


def test_optional_fields_are_absent_not_defaulted(document: str) -> None:
    last = parse_document(document)[3]
    assert last.level is None
    assert last.tags is None
    assert last.attrs == {}


def test_unknown_fields_are_ignored(document: str) -> None:
    assert parse_document(document)[2].kind == "build.failed"


def test_levels_resolve_to_the_channel_vocabulary() -> None:
    assert severity_of(None) == "info"
    assert severity_of("warn") == "warning"
    assert severity_of("ERR") == "error"
    assert severity_of("fatal") == "error"
    assert severity_of("trace") == "debug"


def test_an_unknown_level_is_refused() -> None:
    with pytest.raises(FileEventError) as raised:
        severity_of("critical")
    assert raised.value.code == "unknown_level"


def test_timestamps_are_converted_to_utc() -> None:
    assert parse_timestamp("2026-11-19T04:05:06+02:00") == datetime(
        2026, 11, 19, 2, 5, 6, tzinfo=UTC
    )
    assert parse_timestamp("2026-11-18T23:59:59-05:00") == datetime(
        2026, 11, 19, 4, 59, 59, tzinfo=UTC
    )


def test_a_timestamp_without_an_offset_is_refused() -> None:
    with pytest.raises(FileEventError) as raised:
        parse_timestamp("2026-11-19T02:05:06")
    assert raised.value.code == "naive_timestamp"


def test_tags_are_split_lowercased_and_deduplicated() -> None:
    assert split_tags(" b, A ,b,, ") == ("a", "b")
    assert split_tags(None) == ()


def test_attributes_are_copied_sorted() -> None:
    merged = merge_attributes({"b": "2", "a": "1"})
    assert merged == {"a": "1", "b": "2"}
    assert list(merged) == ["a", "b"]


def test_a_broken_line_names_its_position() -> None:
    with pytest.raises(FileEventError) as raised:
        parse_document('{"event_id": "ev-1a2b3c4d"}\nnot json\n')
    assert raised.value.code == "missing_field"
    assert raised.value.line_number == 1

    with pytest.raises(FileEventError) as broken:
        parse_document('{"event_id": "ev-1a2b3c4d", "ts": "x", "kind": "a"}\n{oops\n')
    assert broken.value.code == "bad_json"
    assert broken.value.line_number == 2


def test_a_malformed_identifier_is_refused() -> None:
    with pytest.raises(FileEventError) as raised:
        parse_line(json.dumps({"event_id": "ev-XYZ", "ts": "2026-11-19T02:05:06Z", "kind": "a"}))
    assert raised.value.code == "bad_event_id"


def test_attribute_values_must_already_be_strings() -> None:
    with pytest.raises(FileEventError) as raised:
        parse_line(
            json.dumps(
                {
                    "event_id": "ev-1a2b3c4d",
                    "ts": "2026-11-19T02:05:06Z",
                    "kind": "a",
                    "attrs": {"count": 3},
                }
            )
        )
    assert raised.value.code == "wrong_type"
