"""Hidden checks for the v2 connector contract (never staged into the agent workspace)."""

import json

import pytest

from ingest.batching import build_batches, record_size
from ingest.checkpoint import CheckpointStore
from ingest.csv_connector import read as read_csv
from ingest.errors import CheckpointError, ConversionError, SchemaError
from ingest.jsonl_connector import read as read_jsonl
from ingest.model import Record
from ingest.pipeline import run
from ingest.registry import build, register
from ingest.report import render
from ingest.schemas import EVENT_SCHEMA, ORDER_SCHEMA

CSV_HEADER = "order_id,amount,quantity,placed_on,note\n"


def _csv(*rows: str) -> str:
    return CSV_HEADER + "".join(row + "\n" for row in rows)


# -- CSV ---------------------------------------------------------------------------------------


def test_csv_skips_blank_lines_before_the_header():
    result = read_csv("\n\n" + CSV_HEADER + "A-1,1,1,,\n", ORDER_SCHEMA)
    assert result.skipped == 2
    assert [record.line for record in result.records] == [4]


def test_csv_header_problems_are_schema_errors():
    with pytest.raises(SchemaError):
        read_csv("order_id,amount,amount,placed_on,note\n", ORDER_SCHEMA)
    with pytest.raises(SchemaError):
        read_csv("order_id,amount,quantity,placed_on,note,extra\n", ORDER_SCHEMA)
    with pytest.raises(SchemaError):
        read_csv("order_id,amount,quantity\n", ORDER_SCHEMA)
    with pytest.raises(SchemaError):
        read_csv("order_id,amount,,placed_on,note\n", ORDER_SCHEMA)
    with pytest.raises(SchemaError):
        read_csv("", ORDER_SCHEMA)


def test_csv_quoting_handles_commas_and_doubled_quotes():
    result = read_csv(_csv('"A,1",2,3,,"say ""hi"""'), ORDER_SCHEMA)
    assert result.records[0].values["order_id"] == "A,1"
    assert result.records[0].values["note"] == 'say "hi"'


def test_csv_unbalanced_quote_is_a_schema_error():
    with pytest.raises(SchemaError):
        read_csv(_csv('A-1,1,1,,"open'), ORDER_SCHEMA)


def test_csv_cell_count_must_match_the_header():
    with pytest.raises(SchemaError):
        read_csv(_csv("A-1,1,1"), ORDER_SCHEMA)
    with pytest.raises(SchemaError):
        read_csv(_csv("A-1,1,1,,x,y"), ORDER_SCHEMA)


def test_csv_optional_fields_take_their_defaults():
    result = read_csv(_csv("A-1,1.5,,,"), ORDER_SCHEMA)
    values = result.records[0].values
    assert values["quantity"] == 0
    assert values["placed_on"] is None
    assert values["note"] is None


def test_csv_conversion_errors_are_typed_and_positioned():
    with pytest.raises(ConversionError) as error:
        read_csv(_csv("A-1,1,2.5,,"), ORDER_SCHEMA)
    assert "quantity" in str(error.value)
    with pytest.raises(ConversionError):
        read_csv(_csv("A-1,1,1,2026-13-01,"), ORDER_SCHEMA)


def test_csv_start_is_a_data_row_index_and_may_exceed_the_input():
    raw = _csv("A-1,1,1,,", "B-2,2,2,,")
    result = read_csv(raw, ORDER_SCHEMA, start=1)
    assert [record.values["order_id"] for record in result.records] == ["B-2"]
    assert result.cursor == 2
    empty = read_csv(raw, ORDER_SCHEMA, start=9)
    assert empty.records == ()
    assert empty.cursor == 9


def test_csv_negative_start_is_a_value_error():
    with pytest.raises(ValueError):
        read_csv(CSV_HEADER, ORDER_SCHEMA, start=-1)


def test_csv_blank_data_lines_are_skipped_not_counted():
    result = read_csv(CSV_HEADER + "\nA-1,1,1,,\n\nB-2,2,2,,\n", ORDER_SCHEMA)
    assert result.skipped == 2
    assert result.cursor == 2
    assert [record.line for record in result.records] == [3, 5]


# -- JSONL -------------------------------------------------------------------------------------


def test_jsonl_rejects_invalid_documents():
    with pytest.raises(SchemaError):
        read_jsonl("{not json\n", EVENT_SCHEMA)
    with pytest.raises(SchemaError):
        read_jsonl("[1, 2]\n", EVENT_SCHEMA)
    with pytest.raises(SchemaError):
        read_jsonl('{"event_id": "E1"}\n', EVENT_SCHEMA)


def test_jsonl_missing_nested_path_is_a_schema_error():
    with pytest.raises(SchemaError):
        read_jsonl('{"id": "E1"}\n', EVENT_SCHEMA, mapping={"event_id": "id", "kind": "meta.kind"})
    with pytest.raises(SchemaError):
        read_jsonl(
            '{"id": "E1", "meta": 3}\n',
            EVENT_SCHEMA,
            mapping={"event_id": "id", "kind": "meta.kind"},
        )


def test_jsonl_mapping_must_name_known_fields():
    with pytest.raises(SchemaError):
        read_jsonl('{"event_id": "E1", "kind": "a"}\n', EVENT_SCHEMA, mapping={"nope": "id"})


def test_jsonl_converts_native_scalars_and_ignores_extra_keys():
    raw = '{"event_id": 7, "kind": 3, "extra": {"deep": 1}}\n'
    result = read_jsonl(raw, EVENT_SCHEMA)
    assert result.records[0].values["event_id"] == "7"
    assert result.records[0].values["kind"] == "3"
    assert result.records[0].values["count"] == 0
    assert result.records[0].values["flagged"] is False


def test_jsonl_optional_fields_and_start_boundary():
    raw = '{"event_id": "E1", "kind": "a"}\n{"event_id": "E2", "kind": "b"}\n'
    result = read_jsonl(raw, EVENT_SCHEMA, start=2)
    assert result.records == ()
    assert result.cursor == 2
    with pytest.raises(ValueError):
        read_jsonl(raw, EVENT_SCHEMA, start=-1)


# -- batching ----------------------------------------------------------------------------------


def _record(index: int) -> Record:
    return Record(source="csv", line=index + 1, values={"order_id": f"R{index}", "amount": 1.0})


def test_batching_validates_both_limits():
    with pytest.raises(ValueError):
        build_batches([], max_records=0)
    with pytest.raises(ValueError):
        build_batches([], max_bytes=0)


def test_batching_counts_records_and_bytes():
    records = [_record(0), _record(1), _record(2)]
    batches = build_batches(records, max_records=2, max_bytes=100_000)
    assert [len(batch) for batch in batches] == [2, 1]
    assert batches[0].size_bytes == record_size(records[0]) + record_size(records[1])
    assert build_batches([], max_records=2, max_bytes=10) == []


def test_an_oversized_record_forms_its_own_batch():
    records = [_record(0), _record(1)]
    batches = build_batches(records, max_records=10, max_bytes=1)
    assert [len(batch) for batch in batches] == [1, 1]
    assert [batch.index for batch in batches] == [0, 1]


def test_a_batch_is_flushed_before_exceeding_the_byte_ceiling():
    records = [_record(0), _record(1)]
    max_bytes = record_size(records[0]) + record_size(records[1]) - 1
    batches = build_batches(records, max_records=10, max_bytes=max_bytes)
    assert [len(batch) for batch in batches] == [1, 1]


def test_record_size_is_the_canonical_json_length():
    record = Record(source="s", line=3, values={"b": 2, "a": "x"})
    text = json.dumps(
        {"source": "s", "line": 3, "values": {"b": 2, "a": "x"}},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert record_size(record) == len(text.encode("utf-8"))


# -- checkpoint --------------------------------------------------------------------------------


def test_checkpoint_missing_file_and_round_trip(tmp_path):
    store = CheckpointStore(tmp_path / "nested" / "cursors.json")
    assert store.load("csv") == 0
    store.save("csv", 2)
    store.save("jsonl", 5)
    reopened = CheckpointStore(tmp_path / "nested" / "cursors.json")
    assert reopened.snapshot() == {"csv": 2, "jsonl": 5}
    document = json.loads((tmp_path / "nested" / "cursors.json").read_text(encoding="utf-8"))
    assert document["version"] == 1


def test_checkpoint_rejects_corruption(tmp_path):
    path = tmp_path / "cursors.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load("csv")
    path.write_text('{"version": 2, "cursors": {}}', encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load("csv")
    path.write_text('{"version": 1, "cursors": []}', encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load("csv")
    path.write_text('{"version": 1, "cursors": {"csv": true}}', encoding="utf-8")
    with pytest.raises(CheckpointError):
        CheckpointStore(path).load("csv")


def test_checkpoint_monotonicity_and_equal_save(tmp_path):
    store = CheckpointStore(tmp_path / "cursors.json")
    store.save("csv", 4)
    store.save("csv", 4)
    assert store.load("csv") == 4
    with pytest.raises(CheckpointError):
        store.save("csv", 3)
    assert store.load("csv") == 4
    with pytest.raises(CheckpointError):
        store.save("csv", -1)


# -- registry and pipeline ---------------------------------------------------------------------


def test_register_does_not_replace_an_existing_source():
    with pytest.raises(ValueError):
        register("csv", lambda raw, schema, **kwargs: None)
    assert build("csv").__name__ == "read"


def test_pipeline_explicit_start_beats_the_checkpoint(tmp_path):
    store = CheckpointStore(tmp_path / "cursors.json")
    store.save("csv", 0)
    raw = _csv("A-1,1,1,,", "B-2,2,2,,")
    result = run("csv", raw, ORDER_SCHEMA, start=1, store=store)
    assert [record.values["order_id"] for record in result.records] == ["B-2"]
    assert result.cursor == 2
    assert store.load("csv") == 2


def test_pipeline_does_not_advance_the_checkpoint_on_failure(tmp_path):
    store = CheckpointStore(tmp_path / "cursors.json")
    store.save("csv", 0)
    with pytest.raises(ConversionError):
        run("csv", _csv("A-1,bad,1,,"), ORDER_SCHEMA, store=store)
    assert store.load("csv") == 0


def test_pipeline_totals_are_numeric_and_sorted(tmp_path):
    raw = _csv("A-1,1.5,2,,", "B-2,2.5,3,,")
    result = run("csv", raw, ORDER_SCHEMA, store=CheckpointStore(tmp_path / "cursors.json"))
    assert result.totals == {"amount": 4.0, "quantity": 5}
    assert list(result.totals) == ["amount", "quantity"]


def test_pipeline_resumes_from_the_store_without_an_explicit_start(tmp_path):
    store = CheckpointStore(tmp_path / "cursors.json")
    raw = _csv("A-1,1,1,,", "B-2,2,2,,")
    first = run("csv", raw, ORDER_SCHEMA, store=store, max_records=1)
    assert first.cursor == 2 and first.batch_count == 2
    second = run("csv", raw, ORDER_SCHEMA, store=store)
    assert second.records == ()
    assert second.cursor == 2
    assert store.load("csv") == 2


def test_report_renders_an_empty_run(tmp_path):
    result = run(
        "csv",
        CSV_HEADER,
        ORDER_SCHEMA,
        store=CheckpointStore(tmp_path / "cursors.json"),
    )
    assert render(result) == "\n".join(
        [
            "source=csv",
            "records=0",
            "batches=0",
            "cursor=0",
            "skipped=0",
            "total.amount=0",
            "total.quantity=0",
        ]
    )
