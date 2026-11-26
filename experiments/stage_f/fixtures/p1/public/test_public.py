"""Public checks for the v2 connector contract."""

import pytest

from ingest.batching import build_batches, record_size
from ingest.checkpoint import CheckpointStore
from ingest.csv_connector import read as read_csv
from ingest.errors import CheckpointError, ConversionError, SchemaError
from ingest.jsonl_connector import read as read_jsonl
from ingest.pipeline import run
from ingest.registry import available, build, register
from ingest.report import render
from ingest.schemas import EVENT_SCHEMA, ORDER_SCHEMA

CSV_TEXT = """ Order_ID , Amount , Quantity , Placed_On , Note
A-1,12.5,2,2026-01-02,
B-2,3,1,, "ship, now"
"""


def test_csv_projects_the_header_and_converts_values():
    result = read_csv(CSV_TEXT, ORDER_SCHEMA)
    assert [record.values for record in result.records] == [
        {
            "order_id": "A-1",
            "amount": 12.5,
            "quantity": 2,
            "placed_on": "2026-01-02",
            "note": None,
        },
        {
            "order_id": "B-2",
            "amount": 3.0,
            "quantity": 1,
            "placed_on": None,
            "note": "ship, now",
        },
    ]
    assert [record.line for record in result.records] == [2, 3]


def test_csv_rejects_a_row_that_cannot_be_converted():
    raw = "order_id,amount,quantity,placed_on,note\nA-1,not-a-number,1,,\n"
    with pytest.raises(ConversionError):
        read_csv(raw, ORDER_SCHEMA)


def test_csv_resume_uses_the_data_row_cursor():
    result = read_csv(CSV_TEXT, ORDER_SCHEMA, start=1)
    assert [record.values["order_id"] for record in result.records] == ["B-2"]
    assert result.cursor == 2


def test_jsonl_resolves_a_dotted_mapping():
    raw = (
        '{"id": "E1", "meta": {"kind": "click"}, "count": "3", "ratio": 0.5, '
        '"flag": true, "when": "2026-01-02"}\n'
    )
    mapping = {
        "event_id": "id",
        "kind": "meta.kind",
        "flagged": "flag",
        "occurred_on": "when",
    }
    result = read_jsonl(raw, EVENT_SCHEMA, mapping=mapping)
    assert result.records[0].values == {
        "event_id": "E1",
        "kind": "click",
        "count": 3,
        "ratio": 0.5,
        "flagged": True,
        "occurred_on": "2026-01-02",
    }


def test_jsonl_skips_blanks_and_resumes():
    raw = '{"event_id": "E1", "kind": "a"}\n\n{"event_id": "E2", "kind": "b"}\n'
    result = read_jsonl(raw, EVENT_SCHEMA, start=1)
    assert [record.values["event_id"] for record in result.records] == ["E2"]
    assert result.cursor == 2
    assert result.skipped == 1


def test_batches_respect_the_byte_ceiling():
    result = read_csv(CSV_TEXT, ORDER_SCHEMA)
    first, second = result.records
    max_bytes = record_size(first) + record_size(second) - 1
    batches = build_batches(result.records, max_records=10, max_bytes=max_bytes)
    assert [len(batch) for batch in batches] == [1, 1]
    assert [batch.index for batch in batches] == [0, 1]


def test_checkpoint_is_persisted_and_monotonic(tmp_path):
    path = tmp_path / "state" / "cursors.json"
    store = CheckpointStore(path)
    store.save("csv", 3)
    assert CheckpointStore(path).load("csv") == 3
    with pytest.raises(CheckpointError):
        store.save("csv", 2)
    assert store.load("csv") == 3


def test_registry_knows_both_sources_and_rejects_duplicates():
    assert available() == ["csv", "jsonl"]
    assert callable(build("csv"))
    with pytest.raises(KeyError):
        build("xml")
    with pytest.raises(ValueError):
        register("csv", object())


def test_pipeline_resumes_from_the_checkpoint_and_totals(tmp_path):
    store = CheckpointStore(tmp_path / "cursors.json")
    store.save("csv", 1)
    result = run("csv", CSV_TEXT, ORDER_SCHEMA, store=store)
    assert [record.values["order_id"] for record in result.records] == ["B-2"]
    assert result.totals == {"amount": 3.0, "quantity": 1}
    assert result.cursor == 2
    assert store.load("csv") == 2


def test_report_is_ordered_without_a_trailing_newline(tmp_path):
    store = CheckpointStore(tmp_path / "cursors.json")
    result = run("csv", CSV_TEXT, ORDER_SCHEMA, store=store, max_records=1)
    text = render(result)
    assert text.splitlines() == [
        "source=csv",
        "records=2",
        "batches=2",
        "cursor=2",
        "skipped=0",
        "total.amount=15.5",
        "total.quantity=3",
    ]
    assert not text.endswith("\n")


def test_csv_requires_a_required_field():
    raw = "order_id,amount,quantity,placed_on,note\nA-1,,2,,\n"
    with pytest.raises(SchemaError):
        read_csv(raw, ORDER_SCHEMA)
